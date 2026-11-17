"""PDF conversion identity and clearly separated failure artifacts."""
from datetime import datetime, timezone
from html import escape
import hashlib
import json
from pathlib import Path
from uuid import uuid4


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


class DiagnosticExportError(RuntimeError):
    def __init__(self, message, artifacts):
        super().__init__(message)
        self.artifacts = artifacts


def write_failure_artifacts(source, args, api, error, document=None, content=None):
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
        draft_renderer='completed_custom_export')
    if content is None:
        try:
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
