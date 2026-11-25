"""Explicit duplicate decisions; filenames are compared without reading OCR data."""
import hashlib
import json
import secrets
import time
import unicodedata

if __package__:
    from .file_paths import native_path
else:
    from file_paths import native_path


def filename_key(name):
    return unicodedata.normalize('NFKC', name).casefold()


class DuplicateUploads:
    def __init__(self, store, error_type):
        self.store, self.error_type = store, error_type
        self.decisions = {}
        self.inflight = set()

    def groups(self, name):
        store, key = self.store, filename_key(name)
        groups = {}
        # The caller holds storage_lock, so uploads cannot be added or removed.
        with store.lock:
            candidates = [job for job in store.jobs.values() if filename_key(job['name']) == key]
        for job in candidates:
            source = native_path(store.root / job['id'] / 'input' / job['name'])
            try:
                if not source.is_file():
                    continue
                stat = source.stat()
            except OSError:
                continue
            groups[job['id']] = dict(job_id=job['id'], job=job, records=[],
                source_identity=[stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
        for record in store.saved_call('list'):
            if filename_key(record['name']) != key or not record.get('exists', True):
                continue
            group = groups.setdefault(record['job_id'], dict(job_id=record['job_id'], job=None,
                                                             records=[], source_identity=None))
            group['records'].append(record)
        return sorted(groups.values(), key=lambda g: g['job_id'])

    def _fingerprint(self, groups):
        data = [[g['job_id'], g['source_identity'], sorted(r['id'] for r in g['records']),
                 (g['job'] or {}).get('format'), (g['job'] or {}).get('saved_result_id'),
                 (g['job'] or {}).get('started_at'), (g['job'] or {}).get('completed_at'),
                 (g['job'] or {}).get('replay_html'), (g['job'] or {}).get('pages'),
                 (g['job'] or {}).get('attempt_id')]
                for g in groups]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _busy(self, groups):
        return any(g['job_id'] in self.store._file_operations or g['job_id'] in self.store.scheduled
                   or (g['job'] or {}).get('status') == 'running'
                   for g in groups)

    def _existing(self, groups):
        # Prefer the running upload, then the newest complete/queued upload.
        def rank(group):
            job = group['job']
            date = job.get('created_at', '') if job else max(r.get('created_at', '') for r in group['records'])
            return bool(job and (job['status'] == 'running' or group['job_id'] in self.store.scheduled)), date
        group = max(groups, key=rank)
        if group['job']:
            value = dict(self.store.public(group['job']), kind='job', job_id=group['job_id'])
        else:
            value = dict(max(group['records'], key=lambda r: r.get('created_at', '')), kind='saved')
        return value

    def check(self, name, groups=None):
        if groups is None:
            groups = [] if filename_key(name) in self.inflight else self.groups(name)
        with self.store.lock:
            now = time.monotonic()
            self.decisions = {k: v for k, v in self.decisions.items() if v['expires'] > now}
            if filename_key(name) in self.inflight:
                return dict(duplicate=True, busy=True, can_replace=False, can_use_existing=False,
                            reason='Файл с этим именем уже загружается. Дождитесь завершения загрузки.')
            if any(g['job_id'] in self.store._file_operations for g in groups):
                return dict(duplicate=True, busy=True, can_replace=False, can_use_existing=False,
                            reason='Дождитесь завершения операции с файлами документа.')
            if not groups:
                return dict(duplicate=False)
            token = secrets.token_urlsafe(24)
            if len(self.decisions) >= 1024:
                self.decisions.pop(next(iter(self.decisions)))
            self.decisions[token] = dict(key=filename_key(name), name=name, fingerprint=self._fingerprint(groups),
                                         expires=now + 600)
            busy = self._busy(groups)
            return dict(duplicate=True, token=token, existing=self._existing(groups), count=len(groups),
                        can_use_existing=True, can_replace=not busy,
                        reason='Файл обрабатывается. Его можно использовать, но пока нельзя заменить.' if busy else '')

    def conflict(self, name, groups, message='Файл с таким именем уже был загружен. Выберите действие.'):
        error = self.error_type(message, 409)
        error.details = {'duplicate': self.check(name, groups)}
        return error

    def _decision(self, name, token, groups):
        decision = self.decisions.get(token) if isinstance(token, str) else None
        if not decision or decision['expires'] <= time.monotonic() or decision['key'] != filename_key(name):
            raise self.conflict(name, groups, 'Выбор устарел. Проверьте загруженный файл ещё раз.')
        return decision

    def begin(self, name, token):
        key = filename_key(name)
        if key in self.inflight:
            raise self.conflict(name, [])
        groups = self.groups(name)
        with self.store.lock:
            if groups:
                decision = self._decision(name, token, groups) if token else None
                if (decision is None or decision['fingerprint'] != self._fingerprint(groups)
                        or self._busy(groups)):
                    raise self.conflict(name, groups)
            elif token:
                raise self.conflict(name, groups, 'Прежний файл уже изменился. Повторите загрузку.')
            self.inflight.add(key)
            return dict(name=name, key=key, groups=groups, fingerprint=self._fingerprint(groups), token=token)

    def validate_commit(self, ticket, groups):
        if self._fingerprint(groups) != ticket['fingerprint'] or self._busy(groups):
            raise self.conflict(ticket['name'], groups, 'Прежний файл изменился или начал обрабатываться. Замена отменена.')

    def finish(self, ticket):
        self.inflight.discard(ticket['key'])

    def reuse(self, token):
        decision = self.decisions.get(token) if isinstance(token, str) else None
        if not decision or decision['expires'] <= time.monotonic():
            raise self.error_type('Выбор устарел. Повторите загрузку файла.', 409)
        if decision['key'] in self.inflight:
            raise self.conflict(decision['name'], [])
        groups = self.groups(decision['name'])
        with self.store.lock:
            if not groups:
                raise self.error_type('Прежний файл больше недоступен. Загрузите его заново.', 409)
            if (self._fingerprint(groups) != decision['fingerprint']
                    or any(g['job_id'] in self.store._file_operations for g in groups)):
                raise self.conflict(decision['name'], groups, 'Прежний файл изменился. Выберите действие ещё раз.')
            existing = self._existing(groups)
            job = None
            if existing['kind'] == 'job':
                job = self.store.jobs[existing['job_id']]
                was_hidden, old_saved = job.get('hidden'), job.get('saved_result_id')
                resurfaced = old_saved in getattr(self.store.saved, 'removed_result_ids', set())
                busy = job['status'] == 'running' or job['id'] in self.store.scheduled
                if not busy and (was_hidden or resurfaced):
                    job['hidden'] = False
                    if resurfaced:
                        job['saved_result_id'] = None
                    try:
                        self.store._save()
                    except Exception:
                        job['hidden'], job['saved_result_id'] = was_hidden, old_saved
                        raise
                job = self.store.public(job)
                existing = dict(job, kind='job', job_id=existing['job_id'])
            self.decisions.pop(token, None)
            return dict(existing=existing, job=job)
