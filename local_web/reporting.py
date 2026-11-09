"""Durable, self-contained conversion reports; delivery never runs at import.

Only owner configuration selects the Telegram destination. Browser requests
cannot provide attachment paths, bot credentials or a destination.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

if __package__:
    from .conversion_logs import assemble_conversion_log
    from .file_paths import native_path, normal_path
    from .telegram_transport import TelegramDeliveryError, destination_key, send_document, validate_transport_settings
else:
    from conversion_logs import assemble_conversion_log
    from file_paths import native_path, normal_path
    from telegram_transport import TelegramDeliveryError, destination_key, send_document, validate_transport_settings


FIXED_RECIPIENT = 'Telegram'
SUBJECT_PREFIX = '[REGION OCR REPORT]'
REPORT_SCHEMA = 1
DEFAULT_CHUNK_BYTES = 12 * 1024 * 1024
TRIGGERS = {'automatic_error', 'automatic_quality', 'user_complaint'}
_ID = re.compile(r'^[0-9a-f]{32}$')
_SECRET = re.compile(r'''(?im)(\b(?:password|passwd|api[_-]?key|(?:bot|access|refresh)[_-]?token|authorization)\b["']?\s*[:=]\s*["']?)([^\r\n"']+)''')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(path):
    value = hashlib.sha256()
    with native_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _plain_path(value):
    return normal_path(value).absolute()


def _owned(root, value, *, file=True):
    """Reject escapes and every symlink/junction, including an owned redirect."""
    root = _plain_path(root)
    path = _plain_path(value)
    if '..' in path.parts or not path.is_relative_to(root):
        raise ValueError('Путь вложения выходит за пределы задания')
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:
            current = current / part
        try:
            info = native_path(current).lstat()
        except FileNotFoundError:
            raise FileNotFoundError('Файл вложения отсутствует') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('Ссылки в путях вложений не разрешены')
    if not normal_path(path).resolve().is_relative_to(root.resolve()):
        raise ValueError('Путь вложения выходит за пределы задания')
    if file and not native_path(path).is_file():
        raise FileNotFoundError('Файл вложения отсутствует')
    return path


def _atomic_json(path, value):
    path = normal_path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with native_path(temporary).open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    native_path(temporary).replace(native_path(path))


def _safe_text(value, limit=None):
    text = str(value or '').replace('\x00', '')
    return text[:limit] if limit is not None else text


def _clean_metadata(value):
    if isinstance(value, dict):
        return {str(key): _clean_metadata(item) for key, item in value.items()
                if not re.search(r'(?i)password|passwd|api.?key|token|secret|credential|authorization|environment', str(key))}
    if isinstance(value, (list, tuple)):
        return [_clean_metadata(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redact(value):
    text = _SECRET.sub(lambda match: match[1] + '[REDACTED]', str(value))
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\b[1-9][0-9]{4,}:[A-Za-z0-9_-]{20,}\b', '[REDACTED TELEGRAM TOKEN]', text)
    for key in ('REGION_REPORT_TELEGRAM_BOT_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
        secret = os.environ.get(key)
        if secret and len(secret) >= 6:
            text = text.replace(secret, '[REDACTED]')
    return text


def _redacted_metadata(value):
    value = _clean_metadata(value)
    if isinstance(value, dict):
        return {key: _redacted_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_metadata(item) for item in value]
    return _redact(value) if isinstance(value, str) else value


def _current_artifact(job, path):
    if job.get('status') != 'error' or not job.get('started_at'):
        return True
    try:
        started = datetime.fromisoformat(str(job['started_at']).replace('Z', '+00:00')).timestamp()
        return native_path(path).stat().st_mtime >= started - 1
    except (ValueError, TypeError, OSError):
        return False


def _regular_files(root, folder):
    """Walk owned ordinary directories only; never even traverse a junction."""
    try:
        folder = _owned(root, folder, file=False)
    except (OSError, ValueError):
        return
    for current, directories, files in os.walk(native_path(folder), followlinks=False):
        allowed = []
        for name in directories:
            try:
                _owned(root, normal_path(current) / name, file=False)
                allowed.append(name)
            except (OSError, ValueError):
                pass
        directories[:] = allowed
        for name in files:
            try:
                yield _owned(root, normal_path(current) / name)
            except (OSError, ValueError):
                continue


def _public(state):
    result = {key: state.get(key) for key in (
        'id', 'status', 'created_at', 'subject', 'trigger', 'recipient',
        'attempts', 'next_attempt_at', 'last_error_code', 'sent_at',
        'part_count', 'accepted_parts', 'missing_artifacts')}
    previous = state.get('status') == 'sent' and (state.get('legacy_delivery') or {}).get('status') == 'sent'
    return result | {'recipient': FIXED_RECIPIENT, 'transport': 'telegram',
                     'delivery_channel': 'previous' if previous else 'telegram'}


def _selected_items(context, trigger):
    allowed = ('id', 'kind', 'page', 'pages', 'ref', 'final_ref', 'table_ref', 'bbox',
               'source_regions', 'reasons', 'decision', 'comment', 'label')
    result = []
    for entry in (context or {}).get('items', []):
        if not isinstance(entry, dict):
            continue
        if trigger == 'user_complaint' and entry.get('decision') not in ('error',):
            continue
        clean = {key: entry[key] for key in allowed if key in entry}
        # JSON round-trip prevents callers from smuggling executable/path objects.
        result.append(json.loads(json.dumps(_clean_metadata(clean), ensure_ascii=False, default=str)))
    return result


def _confirmed_message_id(value):
    return (type(value) is int and value > 0 or isinstance(value, str)
            and bool(re.fullmatch(r'[1-9][0-9]{0,30}', value)))


class ReportOutbox:
    def __init__(self, root, recipient=FIXED_RECIPIENT, telegram_settings=None, send_func=None):
        if recipient != FIXED_RECIPIENT:
            raise ValueError('Получатель отчётов фиксирован конфигурацией приложения')
        self.root = _plain_path(root)
        native_path(self.root).mkdir(parents=True, exist_ok=True)
        _owned(self.root, self.root, file=False)
        self.recipient = FIXED_RECIPIENT
        self.telegram_settings = dict(telegram_settings) if telegram_settings is not None else None
        self.send_func = send_func
        self.lock = threading.RLock()
        # Legacy archives are immutable: migrate only the delivery state. SMTP
        # acknowledgements cannot count as delivery to a Telegram destination.
        for state in self._states():
            if state.get('transport') != 'telegram':
                state['legacy_delivery'] = dict(transport='smtp', **{key: state.get(key) for key in
                                            ('status', 'accepted_parts', 'sent_at', 'message_ids')})
                if state.get('status') != 'sent':
                    state.update(accepted_parts=[], message_ids={}, next_attempt_at=0 if state.get('parts') else None,
                                 last_error_code=None)
                state.update(transport='telegram', recipient=FIXED_RECIPIENT, delivery_destination=None)
                self._save(state)
            if state.get('status') == 'sending':
                state.update(status='queued', last_error_code='delivery_interrupted')
                self._save(state)
            elif state.get('status') == 'preparing':
                try:
                    folder = self._folder(state['id'])
                    delivery = json.loads(native_path(_owned(self.root, folder / 'delivery.json')).read_text(encoding='utf-8'))
                    archive = _owned(self.root, folder / 'report.zip')
                    if delivery.get('report_id') != state['id'] or _digest(archive) != delivery['archive_sha256']:
                        raise ValueError('Incomplete report archive')
                    parts = delivery['parts']
                    if not parts or any(_digest(_owned(self.root, folder / part['name'])) != part['sha256'] for part in parts):
                        raise ValueError('Incomplete report parts')
                    report = json.loads(native_path(_owned(self.root, folder / 'package' / 'report.json')).read_text(encoding='utf-8'))
                    state.update(status='queued', parts=parts, part_count=len(parts),
                                 archive_sha256=delivery['archive_sha256'], last_error_code=None,
                                 missing_artifacts=report.get('missing_artifacts', []), next_attempt_at=0)
                except (OSError, ValueError, TypeError, KeyError):
                    state.update(status='failed', last_error_code='package_creation_interrupted', next_attempt_at=None)
                self._save(state)

    def _folder(self, ident):
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise ValueError('Некорректный идентификатор отчёта')
        folder = self.root / ident
        if native_path(folder).exists():
            _owned(self.root, folder, file=False)
        return folder

    def _load(self, ident):
        target = self._folder(ident) / 'state.json'
        _owned(self.root, target)
        state = json.loads(native_path(target).read_text(encoding='utf-8'))
        if state.get('id') != ident or state.get('recipient') != self.recipient:
            raise ValueError('Повреждённое состояние отчёта')
        return state

    def _save(self, state):
        _atomic_json(self._folder(state['id']) / 'state.json', state)

    def _states(self):
        result = []
        for folder in native_path(self.root).iterdir():
            if not _ID.fullmatch(folder.name):
                continue
            try:
                result.append(self._load(folder.name))
            except (OSError, ValueError, TypeError, KeyError):
                continue  # Retain corrupt entries for owner inspection.
        return sorted(result, key=lambda state: (state.get('created_at', ''), state['id']))

    def get(self, report_id):
        with self.lock:
            return _public(self._load(report_id))

    def list_reports(self):
        with self.lock:
            return [_public(state) for state in self._states()]

    def list_pending(self):
        with self.lock:
            return [_public(state) for state in self._states() if state.get('status') not in {'sent', 'superseded'}]

    def package_path(self, report_id):
        with self.lock:
            state = self._load(report_id)
            return native_path(_owned(self.root, self._folder(state['id']) / 'report.zip'))

    def enqueue(self, job, job_root, trigger, review_context=None, comment='', idempotency_key=None):
        if trigger not in TRIGGERS:
            raise ValueError('Неизвестный тип отчёта')
        if len(str(comment)) > 3000:
            raise ValueError('Комментарий не должен превышать 3000 символов')
        job_root = _plain_path(job_root)
        _owned(job_root, job_root, file=False)
        items = _selected_items(review_context, trigger)
        identity = dict(job=job.get('id'), attempt=job.get('attempt_id') or job.get('conversion_attempt_id')
                        or job.get('started_at') or job.get('created_at'), trigger=trigger,
                        revision=(review_context or {}).get('revision'), items=items,
                        comment=str(comment), idempotency_key=idempotency_key)
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                default=str).encode('utf-8')).hexdigest()
        with self.lock:
            previous = next((state for state in self._states() if state.get('fingerprint') == fingerprint), None)
            if previous is not None and (previous.get('parts') or previous.get('status') != 'failed'):
                return _public(previous)
            if previous is not None:
                previous.update(fingerprint=None, status='superseded')
                self._save(previous)
            ident = uuid.uuid4().hex
            folder = self._folder(ident)
            native_path(folder).mkdir(exist_ok=False)
            package = folder / 'package'
            native_path(package).mkdir()
            created = _now()
            filename = re.sub(r'[\r\n\x00-\x1f]+', ' ', _safe_text(job.get('name'), 140))
            state = dict(id=ident, fingerprint=fingerprint, status='preparing', created_at=created,
                         recipient=self.recipient, transport='telegram', delivery_destination=None,
                         report_name=filename, trigger=trigger, subject=f'{SUBJECT_PREFIX} {trigger} {ident} · {filename}',
                         attempts=0, next_attempt_at=0, accepted_parts=[], part_count=0,
                         last_error_code=None, sent_at=None, parts=[])
            self._save(state)
            try:
                missing, inventory = self._build_package(package, ident, job, job_root, trigger,
                                                         review_context or {}, items, comment, created)
                archive = folder / 'report.zip'
                with zipfile.ZipFile(native_path(archive), 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                    for origin in sorted(native_path(package).rglob('*')):
                        if origin.is_file():
                            checked = _owned(package, normal_path(origin))
                            output.write(native_path(checked), arcname=checked.relative_to(package).as_posix())
                chunk_bytes = (self.telegram_settings or {}).get('chunk_bytes', DEFAULT_CHUNK_BYTES)
                if not isinstance(chunk_bytes, int) or not 1024 <= chunk_bytes <= 16 * 1024 * 1024:
                    raise ValueError('Некорректный размер части отчёта')
                parts = []
                if native_path(archive).stat().st_size <= chunk_bytes:
                    parts.append(dict(index=1, name='report.zip', bytes=native_path(archive).stat().st_size,
                                      sha256=_digest(archive)))
                else:
                    with native_path(archive).open('rb') as stream:
                        index = 1
                        for block in iter(lambda: stream.read(chunk_bytes), b''):
                            name = f'report.zip.part{index:04d}'
                            native_path(folder / name).write_bytes(block)
                            parts.append(dict(index=index, name=name, bytes=len(block),
                                              sha256=hashlib.sha256(block).hexdigest()))
                            index += 1
                delivery = dict(schema=REPORT_SCHEMA, transport='telegram', report_id=ident, archive='report.zip',
                                archive_sha256=_digest(archive), archive_bytes=native_path(archive).stat().st_size,
                                parts=parts, reconstruction='Concatenate the part files in ascending index order; verify archive_sha256; then extract ZIP.')
                _atomic_json(folder / 'delivery.json', delivery)
                state.update(status='queued', missing_artifacts=missing, inventory_count=len(inventory),
                             parts=parts, part_count=len(parts), archive_sha256=delivery['archive_sha256'])
                self._save(state)
                return _public(state)
            except Exception:
                state.update(status='failed', last_error_code='package_creation_failed', next_attempt_at=None)
                self._save(state)
                raise

    def _build_package(self, target, ident, job, job_root, trigger, context, items, comment, created):
        missing = []
        # Work from a metadata whitelist; no environment/credentials/API keys are
        # passed to the log assembler or report JSON.
        clean_job = _clean_metadata({key: job[key] for key in ('name', 'format', 'status', 'pages', 'created_at',
            'started_at', 'completed_at', 'stage', 'error', 'stats', 'warnings') if key in job})
        content = _redact(assemble_conversion_log(clean_job, job_root))
        native_path(target / 'conversion-log.txt').write_text(content, encoding='utf-8', newline='\n')
        source = self._source_pdf(job_root, job)
        source_sha = None
        if source is not None:
            shutil.copyfile(native_path(source), native_path(target / 'source.pdf'))
            source_sha = _digest(target / 'source.pdf')
        else:
            missing.append('source.pdf')
            if job.get('source'):
                try:
                    original = _owned(job_root, job['source'])
                    extension = original.suffix.lower()
                    if not re.fullmatch(r'\.[a-z0-9]{1,8}', extension):
                        extension = '.bin'
                    shutil.copyfile(native_path(original), native_path(target / ('source-original' + extension)))
                except (OSError, ValueError):
                    pass
        canonical, draft = self._result_html(job_root, job)
        html_mapping = None
        if canonical is not None:
            html_mapping = self._copy_html(canonical, target / ('draft-result.html' if draft else 'result.html'),
                                           job_root, target, ident, items)
            if draft:
                missing.append('result.html')
        else:
            missing.append('result.html')
        for item in context.get('items', []):
            if not isinstance(item, dict) or (trigger == 'user_complaint' and item.get('decision') != 'error'):
                continue
            issue_id = re.sub(r'[^A-Za-z0-9_-]', '_', str(item.get('id', 'fragment')))[:96]
            for index, value in enumerate(item.get('source_images', []), 1):
                try:
                    origin = _owned(job_root, value)
                except (OSError, ValueError):
                    continue
                if origin.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
                    continue
                destination = target / 'comparison' / (issue_id + f'-source-{index}' + origin.suffix.lower())
                native_path(destination.parent).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(native_path(origin), native_path(destination))
        snapshots = self._copy_diagnostics(job_root, target, job)
        pipeline_schemas = self._snapshot_schemas(target, snapshots)
        for supplied in (context.get('pipeline_schemas'), job.get('pipeline_schemas')):
            if isinstance(supplied, dict):
                pipeline_schemas.update(_redacted_metadata(supplied))
        inventory = [dict(path=normal_path(path).relative_to(target).as_posix(),
                          bytes=path.stat().st_size, sha256=_digest(path))
                     for path in sorted(native_path(target).rglob('*')) if path.is_file()]
        metadata = dict(schema=REPORT_SCHEMA, transport='telegram', report_id=ident, recipient=self.recipient,
                        trigger=trigger, created_at=created, job_id=job.get('id'),
                        attempt_id=job.get('attempt_id') or job.get('conversion_attempt_id') or job.get('started_at'),
                        name=_safe_text(job.get('name')), format=job.get('format'), status=job.get('status'),
                        application_version=job.get('application_version') or job.get('version'),
                        export_schema=job.get('export_schema'), source_sha256=source_sha,
                        pipeline_schemas=pipeline_schemas,
                        review_revision=context.get('revision'), user_comment=_safe_text(comment),
                        issues=items, missing_artifacts=missing, result_is_draft=draft,
                        html_issue_mapping=html_mapping, diagnostic_snapshots=snapshots, inventory=inventory)
        _atomic_json(target / 'report.json', metadata)
        return missing, inventory

    def _copy_diagnostics(self, root, target, job):
        snapshots = []
        result_root = root / 'result'
        for origin in _regular_files(root, result_root):
            name = origin.name.lower()
            if name not in {'quality.json', 'input.json', 'report.json'} and not name.endswith('.report.json'):
                continue
            if not _current_artifact(job, origin):
                continue
            relative = origin.relative_to(result_root)
            destination = target / 'diagnostics' / relative
            native_path(destination.parent).mkdir(parents=True, exist_ok=True)
            raw = native_path(origin).read_text(encoding='utf-8-sig', errors='replace')
            try:
                data = _redacted_metadata(json.loads(raw))
            except ValueError:
                destination = destination.with_name(destination.name + '.invalid-json.txt')
                native_path(destination).write_text(_redact(raw), encoding='utf-8', newline='\n')
                parseable = False
            else:
                _atomic_json(destination, data)
                parseable = True
            snapshots.append(dict(path=destination.relative_to(target).as_posix(), parseable_json=parseable,
                                  source_modified_at=datetime.fromtimestamp(native_path(origin).stat().st_mtime,
                                                                            timezone.utc).isoformat()))
        return snapshots

    def _snapshot_schemas(self, target, snapshots):
        """Describe the actual run, without importing the OCR/model stack."""
        schemas = {}
        for snapshot in snapshots:
            if not snapshot.get('parseable_json'):
                continue
            path = _owned(target, target / snapshot['path'])
            data = json.loads(native_path(path).read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                continue
            values = {key: value for key, value in data.items()
                      if key.endswith('_schema') and isinstance(value, (str, int)) and not isinstance(value, bool)}
            # quality.json's root schema is LOCAL_SCHEMA; the other snapshots
            # have their own unrelated envelope schemas.
            if path.name == 'quality.json' and isinstance(data.get('schema'), (str, int)) and not isinstance(data['schema'], bool):
                values['local_schema'] = data['schema']
            for key in ('schemas', 'pipeline_schemas'):
                if isinstance(data.get(key), dict):
                    values.update({name: value for name, value in data[key].items()
                                   if isinstance(value, (str, int)) and not isinstance(value, bool)})
            for key, value in values.items():
                if key not in schemas:
                    schemas[key] = value
                elif schemas[key] != value:
                    # Multiple owned snapshots can represent several documents.
                    # Keep every observed version instead of silently replacing it.
                    existing = schemas[key] if isinstance(schemas[key], list) else [schemas[key]]
                    if value not in existing:
                        schemas[key] = existing + [value]
        return schemas

    def _source_pdf(self, root, job):
        candidates = [job.get('source'), job.get('source_pdf')]
        input_folder = root / 'result' / 'engine' / 'input'
        if native_path(input_folder).is_dir():
            candidates.extend(normal_path(path) for path in native_path(input_folder).glob('*.pdf'))
        for value in candidates:
            if value and normal_path(value).suffix.lower() == '.pdf':
                try:
                    return _owned(root, value)
                except (OSError, ValueError):
                    continue
        return None

    def _result_html(self, root, job):
        candidates = [job.get('replay_html'), (job.get('artifacts') or {}).get('html')]
        for value in candidates:
            if value:
                try:
                    candidate = _owned(root, value)
                    if _current_artifact(job, candidate):
                        return candidate, False
                except (OSError, ValueError):
                    continue
        for candidate in (root / 'result' / 'draft.html', root / 'result' / 'engine' / 'draft.html'):
            try:
                owned = _owned(root, candidate)
                if _current_artifact(job, owned):
                    return owned, True
            except (OSError, ValueError):
                continue
        # Engine diagnostics may keep a draft in a per-document directory. Never
        # trust a path declared inside a diagnostic JSON file.
        folder = root / 'result' / 'engine'
        if native_path(folder).is_dir():
            for candidate in sorted(path for path in _regular_files(root, folder) if path.name == 'draft.html'):
                try:
                    owned = _owned(root, normal_path(candidate))
                    if _current_artifact(job, owned):
                        return owned, True
                except (OSError, ValueError):
                    continue
        return None, False

    def _copy_html(self, source, destination, job_root, target, ident, items):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(native_path(source).read_text(encoding='utf-8-sig', errors='replace'), 'html.parser')
        for node in soup.find_all(['script', 'iframe', 'object', 'embed', 'base', 'form', 'link']):
            node.decompose()
        for node in soup.find_all(True):
            for attribute in list(node.attrs):
                if attribute.lower().startswith('on') or attribute.lower() in {'srcdoc', 'formaction'}:
                    del node[attribute]
            if node.name == 'a' and not str(node.get('href', '')).startswith('#'):
                node.attrs.pop('href', None)
        for image in soup.find_all('img'):
            value = str(image.get('src', ''))
            image.attrs.pop('srcset', None)
            if value.startswith('data:image/'):
                continue
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc or parsed.path.startswith(('/', '\\')):
                image.attrs.pop('src', None)
                continue
            relative = Path(unquote(parsed.path))
            try:
                origin = _owned(job_root, source.parent / relative)
            except (OSError, ValueError):
                image.attrs.pop('src', None)
                continue
            extension = origin.suffix.lower()
            if extension not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff'):
                image.attrs.pop('src', None)
                continue
            copied = target / 'src' / (_digest(origin) + extension)
            native_path(copied.parent).mkdir(exist_ok=True)
            if not native_path(copied).exists():
                shutil.copyfile(native_path(origin), native_path(copied))
            image['src'] = copied.relative_to(target).as_posix()
        refs = {str(item.get('final_ref') or item.get('table_ref') or item.get('ref')) for item in items
                if item.get('final_ref') or item.get('table_ref') or item.get('ref')}
        matched = set()
        for node in soup.find_all('table', attrs={'data-pdf-table-ref': True}):
            if node.get('data-pdf-table-ref') in refs:
                matched.add(node['data-pdf-table-ref'])
                node['data-region-report-id'] = ident
                node['class'] = list(node.get('class') or []) + ['region-report-highlight']
        if soup.html is None:
            wrapper = BeautifulSoup('<!doctype html><html lang="ru"><head></head><body></body></html>', 'html.parser')
            for node in list(soup.contents):
                wrapper.body.append(node.extract())
            soup = wrapper
        if soup.head is None:
            soup.html.insert(0, soup.new_tag('head'))
        for meta in soup.find_all('meta', attrs={'http-equiv': True}):
            meta.decompose()
        csp = soup.new_tag('meta', attrs={'http-equiv': 'Content-Security-Policy', 'content': "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"})
        soup.head.append(csp)
        style = soup.new_tag('style')
        style.string = '.region-report-highlight{outline:3px solid #b66b10;outline-offset:3px} '
        soup.head.append(style)
        native_path(destination).write_text(str(soup), encoding='utf-8', newline='\n')
        return dict(matched_table_refs=sorted(matched), unlocated_table_refs=sorted(refs - matched),
                    canonical_sha256=_digest(source), diagnostic_copy_sha256=_digest(destination))

    def delivery_parts(self, report_id):
        """Owner-side paths for a connector bridge; never expose via user API."""
        with self.lock:
            state = self._load(report_id)
            return [dict(part, total=state['part_count'], sent=part['index'] in state['accepted_parts'],
                         path=str(_owned(self.root, self._folder(report_id) / part['name'])))
                    for part in state['parts']]

    def acknowledge(self, report_id, part_index, message_id='', *, destination=None):
        """Owner bridge acknowledgement must name the pinned destination."""
        with self.lock:
            state = self._load(report_id)
            if type(part_index) is not int or not 1 <= part_index <= state['part_count']:
                raise ValueError('Некорректный номер части отчёта')
            if not destination or destination != state.get('delivery_destination'):
                raise ValueError('Получатель подтверждения не совпадает с получателем отчёта')
            if not _confirmed_message_id(message_id):
                raise ValueError('Доставка сообщения не подтверждена')
            if part_index not in state['accepted_parts']:
                state['accepted_parts'].append(part_index)
                state['accepted_parts'].sort()
            state.setdefault('message_ids', {})[str(part_index)] = str(message_id)
            complete = len(state['accepted_parts']) == state['part_count']
            state.update(status='sent' if complete else 'queued', last_error_code=None,
                         next_attempt_at=0, sent_at=_now() if complete else None)
            self._save(state)
            return _public(state)

    def _settings(self):
        if self.telegram_settings is not None:
            settings = dict(self.telegram_settings)
        else:
            if __package__:
                from .report_telegram_config import load_telegram_settings
            else:
                from report_telegram_config import load_telegram_settings
            settings = load_telegram_settings()
        if not settings:
            return None
        return validate_transport_settings(settings)

    def _caption(self, state, part):
        labels = {'automatic_error': 'Сбой конвертации', 'automatic_quality': 'Техническая проверка',
                  'user_complaint': 'Жалоба пользователя'}
        name = state.get('report_name') or str(state.get('subject', '')).split(' · ', 1)[-1]
        name = re.sub(r'[\r\n\x00-\x1f]+', ' ', _safe_text(name, 140))
        return (SUBJECT_PREFIX + '\n' + labels.get(state['trigger'], 'Отчёт') + ' (' + state['trigger'] + ')\n'
                + 'Документ: ' + name + '\nID: ' + state['id']
                + f"\nЧасть: {part['index']}/{state['part_count']}"
                + '\nSHA256 ZIP: ' + state['archive_sha256'] + '\nSHA256 части: ' + part['sha256']
                + ('\nДля восстановления ZIP соедините части по порядку и проверьте SHA256 ZIP.'
                   if state['part_count'] > 1 else ''))

    def _send(self, state, part, settings):
        origin = _owned(self.root, self._folder(state['id']) / part['name'])
        if _digest(origin) != part['sha256']:
            raise TelegramDeliveryError('telegram_part_hash_mismatch')
        attachment = dict(part, path=str(origin), report_id=state['id'], total=state['part_count'],
                          archive_sha256=state['archive_sha256'])
        message_id = (self.send_func or send_document)(attachment, self._caption(state, part), settings)
        if not _confirmed_message_id(message_id):
            raise TelegramDeliveryError('telegram_message_unconfirmed')
        return str(message_id)

    def send_pending(self, limit=10, force=False):
        if type(limit) is not int or limit < 1:
            raise ValueError('Некорректное число отчётов')
        try:
            settings = self._settings()
        except (ValueError, TypeError, OSError, TelegramDeliveryError):
            return [dict(status='configuration_error', last_error_code='telegram_configuration_invalid')]
        if settings is None:
            return []
        destination = destination_key(settings)
        with self.lock:
            states = self._states()
            # Telegram's retry_after also applies to other queued reports for
            # this destination, including explicit/manual retry attempts.
            if any(state.get('last_error_code') == 'telegram_rate_limited'
                   and state.get('delivery_destination') == destination
                   and state.get('next_attempt_at', 0) > time.time() for state in states):
                return []
            ids = [state['id'] for state in states if state.get('status') in {'queued', 'failed'}
                   and state.get('parts') and (force or (state.get('next_attempt_at') is not None
                                                        and state['next_attempt_at'] <= time.time()))][:limit]
        results = []
        for ident in ids:
            with self.lock:
                state = self._load(ident)
                if state.get('status') not in {'queued', 'failed'}:
                    continue
                pinned = state.get('delivery_destination')
                if pinned and pinned != destination:
                    state.update(status='failed', last_error_code='telegram_destination_changed',
                                 next_attempt_at=time.time() + 60)
                    self._save(state)
                    results.append(_public(state))
                    continue
                state.update(status='sending', attempts=state.get('attempts', 0) + 1,
                             delivery_destination=destination)
                self._save(state)
            try:
                for part in state['parts']:
                    if part['index'] in state['accepted_parts']:
                        continue
                    message_id = self._send(state, part, settings)
                    with self.lock:
                        state = self._load(ident)
                        if state.get('delivery_destination') != destination:
                            raise TelegramDeliveryError('telegram_destination_changed')
                        if part['index'] not in state['accepted_parts']:
                            state['accepted_parts'].append(part['index'])
                        state.setdefault('message_ids', {})[str(part['index'])] = message_id
                        self._save(state)
                with self.lock:
                    state.update(status='sent', sent_at=_now(), next_attempt_at=0, last_error_code=None)
                    self._save(state)
                    results.append(_public(state))
            except Exception as error:
                with self.lock:
                    state = self._load(ident)
                    retry_after = error.retry_after if isinstance(error, TelegramDeliveryError) else None
                    delay = min(3600, 30 * 2 ** min(state.get('attempts', 1) - 1, 7))
                    if type(retry_after) is int and retry_after > 0:
                        delay = max(delay, retry_after)
                    code = error.code if isinstance(error, TelegramDeliveryError) else 'telegram_delivery_failed'
                    state.update(status='failed', last_error_code=code, next_attempt_at=time.time() + delay)
                    self._save(state)
                    results.append(_public(state))
                    if code == 'telegram_rate_limited':
                        break
        return results

    flush = send_pending
