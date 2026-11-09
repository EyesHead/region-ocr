"""Durable pre-export PDF snapshots and clearly separated failure artifacts."""
from datetime import datetime, timezone
from html import escape
import hashlib
import inspect
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4


CACHE_SCHEMA = 1


class CachedPostprocessModel:
    """Backend-only model interface for regional/table/math OCR after a hit.

    It never constructs a Docling OCR/layout pipeline. All cached documents
    borrow one lazy Surya manager; the owner closes it after the whole batch.
    """
    def __init__(self):
        self.runtime = None

    def _get_runtime(self):
        if self.runtime is None:
            from surya.inference import SuryaInferenceManager
            self.runtime = SimpleNamespace(_manager=SuryaInferenceManager(method='llamacpp', lazy=True))
        return self.runtime

    def close(self):
        if self.runtime is not None:
            self.runtime._manager.stop()
            self.runtime = None


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), 'utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def ocr_configuration(args, api):
    """Export/math edits deliberately do NOT invalidate expensive OCR snapshots."""
    packages = ('docling', 'docling-core', 'docling-surya-ocr', 'surya-ocr',
                'torch', 'transformers', 'pypdfium2')
    names = ('surya_pdf_pipeline', 'build_converter', 'normalized_pdf', 'semantic_ocr_text')
    functions = tuple(getattr(api, name) for name in names)
    frozen = getattr(api, '_ocr_snapshot_hooks', None)
    if frozen is None or frozen[0] != functions:
        frozen = (functions, {name: hashlib.sha256(inspect.getsource(function).encode()).hexdigest()
                              for name, function in zip(names, functions)})
        api._ocr_snapshot_hooks = frozen
    return dict(schema=CACHE_SCHEMA, versions={name: api.version(name) for name in packages},
        page_range=args.page_range, device=args.device,
        image_scale=api.IMAGE_SCALE, retained_image_scale=api.RETAINED_IMAGE_SCALE,
        orientation_schema=api.ORIENTATION_SCHEMA, orientation_model=api.ORIENTATION_MODEL_SHA256,
        hooks=frozen[1], ocr_recovery_sha256=file_hash(Path(api.__file__).with_name('docling_pdf_ocr.py')))


class OcrSnapshot:
    def __init__(self, source, args, api):
        self.source, self.api = Path(source), api
        self.source_hash = file_hash(source)
        self.config = ocr_configuration(args, api)
        self.config_hash = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        root = getattr(args, 'ocr_cache_dir', None) or api.SCRIPT_DIR / '.cache' / 'docling_ocr'
        # Short on-disk indexes avoid Windows MAX_PATH (full SHA256 is still
        # checked in ready.json; an index collision is never accepted as a hit).
        self.directory = Path(root) / self.source_hash[:24] / self.config_hash[:24]
        self.metadata = None

    def load(self):
        if not self.directory.exists():
            self.api.diagnostic_event('OCR_CACHE_MISS', source=self.source, key=self.config_hash)
            return None
        try:
            meta = json.loads((self.directory / 'ready.json').read_text('utf-8'))
            if (meta['schema'] != CACHE_SCHEMA or meta['source_sha256'] != self.source_hash
                    or meta['configuration_sha256'] != self.config_hash):
                raise ValueError('Snapshot identity mismatch')
            expected = {'document.json', 'records.json'} | ({'normalized.pdf'} if meta['normalized'] else set())
            if set(meta['files']) != expected:
                raise ValueError('Snapshot artifact manifest is incomplete')
            for name, digest in meta['files'].items():
                if name not in {'document.json', 'records.json', 'normalized.pdf'}:
                    raise ValueError('Unexpected snapshot artifact')
                if file_hash(self.directory / name) != digest:
                    raise ValueError(f'Snapshot integrity mismatch: {name}')
            from docling_core.types.doc import DoclingDocument
            from docling.datamodel.base_models import ConversionStatus
            document = DoclingDocument.load_from_json(self.directory / 'document.json')
            records = json.loads((self.directory / 'records.json').read_text('utf-8'))
            orientation = {int(k): v for k, v in meta['orientation'].items()}
            if set(document.pages) != set(orientation):
                raise ValueError('Snapshot page coverage mismatch')
            self.metadata = meta
            self.api.diagnostic_event('OCR_CACHE_HIT', source=self.source, key=self.config_hash,
                                      pages=len(orientation), directory=self.directory)
            print(f'OCR cache HIT: {self.source.name}; {len(orientation)} pages; OCR not repeated', flush=True)
            return SimpleNamespace(document=document, records=records, orientation=orientation,
                normalized=self.directory / 'normalized.pdf' if meta['normalized'] else self.source,
                status=ConversionStatus(meta['status']), errors=meta.get('errors', []))
        except Exception as error:
            # Corrupt/incomplete snapshots are never promoted to usable OCR.
            self.api.diagnostic_event('OCR_CACHE_INVALID', directory=self.directory,
                                      error_type=type(error).__name__, error=str(error))
            print(f'OCR cache invalid: {error}; OCR will run again', flush=True)
            return None

    def save(self, result, records, normalized, orientation):
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        staging = self.directory.with_name(self.directory.name + '.pending-' + uuid4().hex[:12])
        staging.mkdir()
        try:
            from docling_core.types.doc import ImageRefMode
            result.document.save_as_json(staging / 'document.json', image_mode=ImageRefMode.EMBEDDED,
                                          ensure_ascii=False)
            save_json(staging / 'records.json', records)
            is_normalized = Path(normalized).resolve() != self.source.resolve()
            names = ['document.json', 'records.json']
            if is_normalized:
                shutil.copy2(normalized, staging / 'normalized.pdf')
                names.append('normalized.pdf')
            meta = dict(schema=CACHE_SCHEMA, source=str(self.source), source_sha256=self.source_hash,
                configuration=self.config, configuration_sha256=self.config_hash,
                status=result.status.value, errors=[str(error) for error in result.errors],
                normalized=is_normalized, orientation=orientation,
                files={name: file_hash(staging / name) for name in names},
                saved_utc=datetime.now(timezone.utc).isoformat())
            save_json(staging / 'ready.json', meta)
            if self.directory.exists():
                # Keep the rejected prior snapshot as evidence, not as a hit.
                self.directory.rename(self.directory.with_name(self.directory.name + '.invalid-' + uuid4().hex[:12]))
            staging.rename(self.directory)
            self.metadata = meta
            self.api.diagnostic_event('OCR_CACHE_SAVED', directory=self.directory,
                                      source=self.source, pages=len(orientation), key=self.config_hash)
            print(f'OCR cache SAVED before HTML export: {self.directory}', flush=True)
        finally:
            if staging.exists():
                if (staging.parent.resolve() != self.directory.parent.resolve()
                        or not staging.name.startswith(self.directory.name + '.pending-')):
                    raise RuntimeError('Unexpected cache staging cleanup target')
                shutil.rmtree(staging)

    def document(self):
        from docling_core.types.doc import DoclingDocument
        return DoclingDocument.load_from_json(self.directory / 'document.json')


class DiagnosticExportError(RuntimeError):
    def __init__(self, message, artifacts):
        super().__init__(message)
        self.artifacts = artifacts


def write_failure_artifacts(source, args, api, error, snapshot=None, document=None, content=None):
    """Never write the final destination or call the failed custom serializer."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid4().hex[:8]
    logs = getattr(args, 'log_dir', None) or Path.cwd() / 'convert_docling_log'
    folder = Path(logs) / 'diagnostics' / (Path(source).stem[:60] + '_' + stamp)
    folder.mkdir(parents=True, exist_ok=False)
    context = dict(getattr(args, 'pdf_export_context', {}) or {})
    if isinstance(getattr(error, 'context', None), dict):
        context.update(error.context)
    quality = getattr(args, 'pdf_quality_current', {}) or {}
    report = dict(status='DIAG', source=str(source), final_output=str(api.output_base(args, source)) + '.html',
        final_output_replaced=False, page=context.get('page'), element=context.get('element'),
        bbox=context.get('bbox'), stage=context.get('stage', 'assembly/export'),
        exception=type(error).__name__, reason=str(error), context=context,
        issues=quality.get('deferred_math', [])+quality.get('table_content_issues', []), budgets=quality.get('image_budgets', {}),
        ocr_cache=str(snapshot.directory) if snapshot else None, draft_renderer='completed_custom_export')
    if content is None:
        try:
            if snapshot is not None:
                document = snapshot.document()
            from docling_core.types.doc import ImageRefMode
            content = document.export_to_html(image_mode=ImageRefMode.EMBEDDED)
            report['draft_renderer'] = 'stock_serializer_of_pre_export_ocr'
        except Exception as draft_error:
            # Even a broken stock serializer must leave a readable diagnosis.
            report['draft_renderer'] = 'escaped_text_fallback'
            report['draft_error'] = f'{type(draft_error).__name__}: {draft_error}'
            texts = getattr(document, 'texts', ()) or ()
            content = '<html><body><pre>' + escape('\n'.join(getattr(t, 'text', '') for t in texts)) + '</pre></body></html>'
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, 'html.parser')
    if soup.body is None:
        body = soup.new_tag('body')
        for node in list(soup.contents):
            body.append(node.extract())
        soup.append(body)
    banner = soup.new_tag('aside', attrs={'role': 'alert', 'data-conversion-status': 'DIAG',
        'style': 'border:4px solid #b00020;background:#fff0f0;color:#850019;padding:1em;font:18px sans-serif'})
    banner.string = ('ДИАГНОСТИЧЕСКИЙ ЧЕРНОВИК — НЕ ИТОГОВЫЙ ДОКУМЕНТ. '
                     'Проверки экспорта не пройдены. ' + str(error))
    soup.body.insert(0, banner)
    draft = folder / 'draft.html'
    api.write_atomic(draft, str(soup))
    save_json(folder / 'report.json', report)
    artifacts = dict(draft=str(draft), report=str(folder / 'report.json'))
    api.diagnostic_event('DIAGNOSTIC_DRAFT_SAVED', **artifacts, page=report['page'], element=report['element'],
                          reason=str(error), final_output_replaced=False)
    return artifacts
