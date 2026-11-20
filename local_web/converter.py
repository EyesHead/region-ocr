"""Local conversion adapter; the existing converter remains the source of truth.

Heavy conversion runs in a cancellable child process. The four public formats
are derived from the *published*, repaired HTML, never the pre-repair model.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

if __package__:
    from .file_paths import native_path, normal_path
else:
    from file_paths import native_path, normal_path


ENGINE_DIR = Path(__file__).resolve().parent.parent
SUPPORTED_EXTENSIONS = frozenset({
    '.pdf', '.docx', '.pptx', '.xlsx', '.html', '.htm',
    '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp', '.gif',
})
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp', '.gif'})
RESULT_MARKER = 'LOCAL_WEB_RESULT '
ERROR_MARKER = 'LOCAL_WEB_ERROR '

_FAILURES = {
    'quality_match': ('Не удалось однозначно сопоставить распознанный текст таблицы.',
        'Результат не опубликован, чтобы не потерять текст. OCR сохранён; повторная сборка без исправления причины может дать ту же ошибку. Скопируйте журнал для разбора.', False),
    'quality_integrity': ('Проверка целостности таблицы или формулы не пройдена.',
        'Результат не опубликован, чтобы не потерять содержимое. Скопируйте журнал для разбора; повторная сборка без исправления причины может дать ту же ошибку.', False),
    'source_unreadable': ('Не удалось прочитать исходный документ.',
        'Убедитесь, что исходник открывается и доступен для чтения. При необходимости загрузите исправленный файл.', False),
    'source_missing': ('Исходный файл или необходимый ресурс не найден.',
        'Проверьте наличие исходника и файлов программы, затем повторите обработку.', True),
    'access_denied': ('Нет доступа к исходнику или папке результата.',
        'Проверьте права доступа и закройте приложения, блокирующие файлы, затем повторите обработку.', True),
    'storage_full': ('Недостаточно места для сохранения результата.',
        'Освободите место на диске с папкой результата, затем повторите обработку.', True),
    'write_failed': ('Не удалось сохранить результат.',
        'Проверьте доступность папки результата и свободное место, затем повторите обработку.', True),
    'timeout': ('Сервер OCR не ответил за отведённое время.',
        'Повторите обработку после восстановления работы OCR. Если ошибка повторится, скопируйте журнал для разбора.', True),
    'engine_unavailable': ('Локальный OCR недоступен.',
        'Дождитесь готовности OCR или перезапустите программу, затем повторите обработку.', True),
    'memory_exhausted': ('Недостаточно памяти для обработки документа.',
        'Закройте другие программы или уменьшите диапазон страниц, затем повторите обработку.', True),
    'interrupted': ('Обработка была прервана.',
        'Повторите обработку. Доступный снимок OCR будет использован повторно.', True),
    'internal': ('Внутренняя ошибка обработки документа.',
        'Скопируйте журнал для разбора. Повторение без исправления причины может дать ту же ошибку.', False),
}


class ConversionFailure(RuntimeError):
    """Keep the full cause in logs and transport a separate public diagnosis."""
    def __init__(self, message, failure=None):
        super().__init__(message)
        self.failure = failure or {}


def _public_failure(kind, location=None):
    kind = kind if kind in _FAILURES else 'internal'
    message, action, retryable = _FAILURES[kind]
    clean, labels = {}, []
    location = location if isinstance(location, dict) else {}
    for field in ('page', 'row', 'column', 'row_span', 'column_span'):
        value = location.get(field)
        if type(value) is int and value >= (0 if field in {'row', 'column'} else 1):
            clean[field] = value
    ref = location.get('element')
    if isinstance(ref, str) and re.fullmatch(r'#/tables/\d+', ref):
        clean['element'] = ref
    if clean.get('page'):
        labels.append(f'Страница PDF {clean["page"]}')
    if 'row' in clean:
        labels.append(f'строка {clean["row"] + 1}')
    if 'column' in clean:
        first = clean['column'] + 1
        span = clean.get('column_span', 1)
        labels.append(f'столбцы {first}–{first + span - 1}' if span > 1 else f'столбец {first}')
    if labels:
        message = ', '.join(labels) + ': ' + message
    return dict(kind=kind, message=message, action=action, retryable=retryable, location=clean)


def _failure_report(destination, since=None):
    """Read only this attempt's diagnostic report beneath its engine directory."""
    if destination is None:
        return {}
    root = normal_path(Path(destination)).resolve()
    status = native_path(root / 'logs' / 'conversion_status.json')
    try:
        if since is not None and status.stat().st_mtime < since:
            return {}
        records = json.loads(status.read_text(encoding='utf-8')).get('documents', [])
        if len(records) != 1 or records[0].get('status') not in {'DIAG', 'FAIL'}:
            return {}
        fallback = dict(reason=records[0].get('reason', ''))
        if records[0]['status'] == 'FAIL':
            return fallback
        path = normal_path(Path(records[0]['report'])).resolve()
        if not path.is_relative_to(root):
            return fallback
        try:
            report = json.loads(native_path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return fallback
        return report if isinstance(report, dict) else {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def describe_conversion_failure(error, destination=None, *, since=None):
    """Classify failures without exposing source paths or a traceback to the UI."""
    carried = getattr(error, 'failure', None)
    if isinstance(carried, dict) and carried.get('kind') in _FAILURES:
        return _public_failure(carried['kind'], carried.get('location'))
    raw = str(error).casefold()
    kind = None
    if isinstance(error, PermissionError) or 'permissionerror' in raw or 'access is denied' in raw:
        kind = 'access_denied'
    elif isinstance(error, FileNotFoundError) or 'filenotfounderror' in raw or 'исходный файл не найден' in raw:
        kind = 'source_missing'
    elif isinstance(error, MemoryError) or 'out of memory' in raw or 'cuda error: out of memory' in raw:
        kind = 'memory_exhausted'
    elif isinstance(error, OSError) and getattr(error, 'errno', None) == 28:
        kind = 'storage_full'
    elif isinstance(error, TimeoutError) or 'timeouterror' in raw or 'превышено время' in raw:
        kind = 'timeout'
    elif isinstance(error, (BrokenPipeError, ConnectionError)) or any(term in raw for term in
            ('ocr не запустился', 'ocr не готов', 'не удалось подготовить локальный ocr',
             'ocr runtime отсутствует', 'connection refused', 'процесс локального ocr завершился')):
        kind = 'engine_unavailable'
    elif 'приложение было закрыто' in raw or 'обработка прервана' in raw:
        kind = 'interrupted'
    if kind:
        return _public_failure(kind)
    diagnostic = (isinstance(error, SystemExit) or type(error).__name__ in
                  {'DiagnosticExportError', 'TableContentError', 'VectorContentConflict'}) or any(term in raw for term in
        ('diagnosticexporterror', 'tablecontenterror', 'overlapping typed table content'))
    report = _failure_report(destination, since) if diagnostic else {}
    reason = str(report.get('reason', raw)).casefold()
    exception = str(report.get('exception', '')).casefold()
    context = report.get('context') or {}
    if not isinstance(context, dict):
        context = {}
    cell = context.get('cell') if isinstance(context.get('cell'), dict) else {}
    location = dict(cell, page=report.get('page', context.get('page')),
                    element=report.get('element', context.get('element')))
    if 'overlapping typed table content has no unique primary-text match' in reason:
        kind = 'quality_match'
    elif exception in {'tablecontenterror', 'vectorcontentconflict'} or any(term in reason for term in
            ('typed table', 'table image lacks', 'unverified/deferred math', 'source grid')):
        kind = 'quality_integrity'
    elif 'permissionerror' in exception or 'permission denied' in reason or 'access is denied' in reason:
        kind = 'access_denied'
    elif exception == 'memoryerror' or 'out of memory' in reason:
        kind = 'memory_exhausted'
    elif exception == 'timeouterror' or 'timed out' in reason or 'превышено время' in reason:
        kind = 'timeout'
    elif exception == 'filenotfounderror' or 'исходный файл не найден' in reason or 'no such file' in reason:
        kind = 'source_missing'
    elif 'no space left' in reason or '[errno 28]' in reason:
        kind = 'storage_full'
    elif any(term in reason for term in ('password', 'encrypted', 'invalid pdf', 'failed to load document', 'cannot open broken')):
        kind = 'source_unreadable'
    elif isinstance(error, OSError):
        kind = 'write_failed'
    else:
        kind = 'internal'
    return _public_failure(kind, location)


class ConversionCancelled(RuntimeError):
    """The user cancelled this conversion; partial files are not published."""


def _runtime_python() -> str:
    return os.environ.get('DOCUMENT_CONVERTER_PYTHON') or os.environ.get('DOCLING_PYTHON') or sys.executable


def get_engine_status() -> dict:
    """Probe imports without importing torch, starting models, or downloading."""
    python = _runtime_python()
    missing = []
    try:
        probe = ('import importlib.util,json;'
                 'print(json.dumps([n for n in ("docling","bs4","PIL","docling_surya_ocr") '
                 'if importlib.util.find_spec(n) is None]))')
        completed = subprocess.run([python, '-c', probe], capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=8,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip()[-400:] or 'Python probe failed')
        missing = json.loads(completed.stdout.strip().splitlines()[-1])
        if not (ENGINE_DIR / 'convert_docling.py').is_file():
            missing.append('convert_docling.py')
        return dict(available=not missing, python=python, missing=missing, device='auto',
                    message=('Локальный движок доступен. Модели проверяются при запуске.' if not missing
                             else 'Не найдены компоненты: ' + ', '.join(missing)))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return dict(available=False, python=python, missing=missing, device='auto',
                    message='Не удалось проверить Python: ' + str(error))


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ConversionCancelled('Конвертация отменена')


def _process_exit_message(code):
    if code is None:
        return 'Поток вывода локального OCR закрылся; код завершения процесса недоступен.'
    detail = f'код {code}'
    if os.name == 'nt':
        detail += f' (0x{code & 0xFFFFFFFF:08X})'
    return f'Процесс локального OCR завершился без результата: {detail}.'


def _validate_pages(pages: str) -> str:
    pages = (pages or '').strip()
    if not pages:
        return ''
    match = re.fullmatch(r'([1-9]\d*)\s*-\s*([1-9]\d*)', pages)
    if not match or int(match[1]) > int(match[2]):
        raise ValueError('Диапазон страниц: START-END, например 1-3')
    return f'{int(match[1])}-{int(match[2])}'


def _decode_html(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    declared = re.search(br'charset\s*=\s*["\']?\s*([a-zA-Z0-9_-]+)', data[:4096], re.I)
    encodings = ['utf-8-sig']
    if declared:
        encodings.append(declared[1].decode('ascii'))
    encodings.append('cp1251')
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError('Не удалось определить кодировку HTML')


def _safe_html(content: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, 'html.parser')
    removed = 0
    for tag in list(soup.find_all(['script', 'iframe', 'object', 'embed', 'applet', 'base', 'link', 'form'])):
        tag.decompose()
        removed += 1
    for meta in list(soup.find_all('meta')):
        if meta.get('http-equiv', '').lower() == 'refresh':
            meta.decompose()
            removed += 1
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on') or attr.lower() in {'srcdoc', 'action', 'formaction', 'ping', 'srcset', 'poster'}:
                del tag[attr]
                removed += 1
        for attr in ('href', 'xlink:href', 'src'):
            if attr not in tag.attrs:
                continue
            value = str(tag[attr]).strip()
            # Legacy data images are accepted as input and externalized before
            # publication. Local raster paths are resolved only by assets.py.
            if attr == 'src' and tag.name == 'img' and value.lower().startswith('data:image/'):
                continue
            if attr == 'src' and tag.name == 'img' and value and not re.match(r'^(?:[a-z][a-z0-9+.-]*:|//)', value, re.I):
                continue
            if attr in {'href', 'xlink:href'} and value.startswith('#'):
                continue
            if attr == 'href' and tag.name == 'a' and re.match(r'^(?:https?://|mailto:)', value, re.I):
                continue
            del tag[attr]
            removed += 1
        if 'style' in tag.attrs and re.search(r'url\s*\(|expression\s*\(|@import', str(tag['style']), re.I):
            del tag['style']
            removed += 1
        if tag.name == 'style' and re.search(r'url\s*\(|expression\s*\(|@import', tag.get_text(), re.I):
            tag.clear()
            removed += 1
    if soup.html is None:
        shell = BeautifulSoup('<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body></body></html>', 'html.parser')
        for child in list(soup.contents):
            shell.body.append(child.extract())
        soup = shell
    elif soup.body is None:
        body = soup.new_tag('body')
        for child in list(soup.html.contents):
            if getattr(child, 'name', None) != 'head':
                body.append(child.extract())
        soup.html.append(body)
    if soup.head is None:
        soup.html.insert(0, soup.new_tag('head'))
    charset = soup.find('meta', charset=True)
    if charset:
        charset['charset'] = 'utf-8'
    else:
        soup.head.insert(0, soup.new_tag('meta', charset='utf-8'))
    warnings = []
    if removed:
        warnings.append('Из HTML удалены исполняемое содержимое и внешние ресурсы; встроенные таблицы и MathML сохранены.')
    return soup, warnings


def export_formats(content: str, source: Path, destination: Path, *, pages=None,
                   output_format=None, resource_root=None, initial_warnings=()) -> dict:
    """Export requested bundles; None retains the explicit library all-formats API."""
    soup, warnings = _safe_html(content)
    if __package__:
        from .export_bundle import build_exports
    else:
        from export_bundle import build_exports
    result = build_exports(str(soup), source, destination, pages=pages,
                           output_format=output_format, resource_root=resource_root,
                           initial_warnings=list(initial_warnings) + warnings)
    return result


def _stop_process(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        # Kill only this worker's descendant tree, including its own OCR server.
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           timeout=4, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            # taskkill itself can hang on Windows. The known child PID must
            # still be terminated before the queue starts the next document.
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _run_engine(source: Path, destination: Path, pages: str, on_progress, cancel_event, engine_host=None) -> dict:
    if engine_host is not None:
        return engine_host.run(source, destination, pages, on_progress, cancel_event)
    engine_destination = destination / 'engine'
    engine_destination.mkdir(parents=True, exist_ok=True)
    command = [_runtime_python(), '-u', str(Path(__file__).resolve()), '--worker', str(source), str(engine_destination)]
    if pages:
        command += ['--pages', pages]
    environment = os.environ.copy()
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PYTHONFAULTHANDLER'] = '1'
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding='utf-8', errors='replace', env=environment,
                               cwd=ENGINE_DIR, bufsize=1,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                               start_new_session=os.name != 'nt')
    lines = queue.Queue()
    def read_output():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    started, result, read_complete = time.monotonic(), None, False
    last_error, error_detail, failure = '', '', None
    deadline = max(60, int(os.environ.get('DOCUMENT_CONVERTER_TIMEOUT_SECONDS', '7200')))
    try:
        with (destination / 'engine.log').open('w', encoding='utf-8') as log:
            while not read_complete or process.poll() is None:
                _check_cancel(cancel_event)
                if time.monotonic() - started > deadline:
                    raise TimeoutError(f'Превышено время конвертации ({deadline} с); журнал: engine.log')
                try:
                    line = lines.get(timeout=.15)
                except queue.Empty:
                    continue
                if line is None:
                    read_complete = True
                    continue
                log.write(line)
                log.flush()
                if re.search(r'error:', line, re.I):
                    message = line.strip()
                    last_error = message[-800:]
                    if not re.fullmatch(r'ERROR:\s*\d+\s+of\s+\d+\s+document conversions failed\.?', message, re.I):
                        # Keep the cause even when progress/report lines push it
                        # out of the console tail. The full output stays in log.
                        error_detail = last_error
                if line.startswith(RESULT_MARKER):
                    result = json.loads(line[len(RESULT_MARKER):])
                if line.startswith(ERROR_MARKER):
                    failure = json.loads(line[len(ERROR_MARKER):])
                match = re.search(r'сконвертировано страниц:\s*(\d+) из (\d+)', line)
                if match and on_progress:
                    completed, total = int(match[1]), max(1, int(match[2]))
                    on_progress(min(85, 10 + int(75 * completed / total)), f'Распознано страниц: {completed} из {total}')
                elif 'Подготовка HTML:' in line and on_progress:
                    on_progress(88, 'Восстановление таблиц и формул')
            code = process.wait()
            _check_cancel(cancel_event)
            if code or not result:
                detail = error_detail or last_error
                message = _process_exit_message(code) + (' ' + detail if detail else '')
                log.write('\n' + message + '\n')
                log.flush()
                raise ConversionFailure(message + ' Журнал: engine.log', failure)
            return result
    finally:
        _stop_process(process)
        reader.join(timeout=1)
        process.stdout.close()


def convert_file(source: Path, destination: Path, *, pages: str = '',
                 on_progress: Callable[[int, str], None] | None = None, cancel_event=None,
                 output_format: str = 'html', replay_html=None, engine_host=None) -> dict:
    source, destination = native_path(Path(source).resolve()), Path(destination).resolve()
    pages = _validate_pages(pages)
    _check_cancel(cancel_event)
    if not source.is_file():
        raise FileNotFoundError('Исходный файл не найден')
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError('Неподдерживаемый формат: ' + source.suffix)
    if normal_path(source) == destination / f'{source.stem}.html':
        raise ValueError('Папка результата должна отличаться от расположения исходного HTML')
    destination.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(3, 'Чтение документа')
    _check_cancel(cancel_event)
    warnings = []
    technical_status = None
    resource_root = source.parent
    page_count = 1 if source.suffix.lower() in {'.html', '.htm'} else None
    if replay_html:
        if __package__:
            from .export_bundle import build_exports
        else:
            from export_bundle import build_exports
        if on_progress:
            on_progress(94, 'Экспорт из сохранённого канонического HTML')
        result = build_exports(None, source, destination, output_format=output_format,
                               pages=page_count, replay_html=replay_html)
        _check_cancel(cancel_event)
        if on_progress:
            on_progress(100, 'Готово')
        return result
    elif source.suffix.lower() in {'.html', '.htm'}:
        content = _decode_html(source)
    elif source.suffix.lower() == '.docx':
        # DOCX already contains ordered text, tables and source media.
        # Image OCR is an explicit core option, not a prerequisite for export.
        engine_dir = str(ENGINE_DIR)
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)
        from docling_office import export_docx_html
        if on_progress:
            on_progress(25, 'Чтение текста, таблиц и изображений DOCX')
        content, _office_stats = export_docx_html(source)
    elif source.suffix.lower() == '.xlsx':
        if __package__:
            from .xlsx import export_xlsx
        else:
            # server.py is also launched directly: its module search path is
            # local_web itself, so converter is imported as a top-level module.
            from xlsx import export_xlsx
        resource_root = destination / '.xlsx'
        content, xlsx_warnings, page_count = export_xlsx(source, check_cancel=lambda: _check_cancel(cancel_event),
                                                       assets_dir=resource_root / 'src')
        warnings.extend(xlsx_warnings)
    else:
        engine = _run_engine(source, destination, pages, on_progress, cancel_event, engine_host=engine_host)
        content = Path(engine['html']).read_text(encoding='utf-8')
        resource_root = Path(engine['html']).parent
        page_count = engine.get('pages')
        technical_status = engine.get('technical_status')
    _check_cancel(cancel_event)
    if pages and source.suffix.lower() != '.pdf':
        warnings.append('Диапазон страниц применяется только к PDF; этот файл обработан полностью.')
    if on_progress:
        on_progress(94, 'Подготовка папки результата и изображений')
    result = export_formats(content, source, destination, pages=page_count,
                            output_format=output_format, resource_root=resource_root,
                            initial_warnings=warnings)
    if technical_status:
        result['technical_status'] = technical_status
    _check_cancel(cancel_event)
    if on_progress:
        on_progress(100, 'Готово')
    return result


def _worker(source: Path, destination: Path, pages: str):
    """Run core conversion with private input, output and diagnostics paths."""
    sys.path.insert(0, str(ENGINE_DIR))
    import convert_docling as core
    destination = native_path(destination.resolve())
    destination.mkdir(parents=True, exist_ok=True)
    engine_source = source
    image_pages = None
    if source.suffix.lower() in IMAGE_EXTENSIONS:
        from PIL import Image, ImageSequence
        folder = destination / 'input'
        folder.mkdir(parents=True, exist_ok=True)
        engine_source = native_path(folder / (source.stem + '.pdf'))
        frames = []
        with Image.open(source) as image:
            for frame in ImageSequence.Iterator(image):
                rgba = frame.convert('RGBA')
                white = Image.new('RGBA', rgba.size, 'white')
                white.alpha_composite(rgba)
                frames.append(white.convert('RGB'))
                white.close()
                rgba.close()
        if not frames:
            raise ValueError('Изображение не содержит страниц')
        image_pages = len(frames)
        try:
            frames[0].save(engine_source, 'PDF', save_all=True, append_images=frames[1:], resolution=150.0)
        finally:
            for frame in frames:
                frame.close()
    # parse_args constrains explicit input paths to DATA_DIR. This private worker
    # scopes it to its single uploaded source.
    core.DATA_DIR = engine_source.parent
    argv = ['--mode', 'local', '--desktop-output', '--input', str(engine_source),
            '--output-dir', str(destination / 'html'), '--log-dir', str(destination / 'logs'),
            '--diagnostics-dir', str(destination / 'diagnostics')]
    if pages and source.suffix.lower() == '.pdf':
        argv += ['--pages', pages]
    sys.argv = [str(ENGINE_DIR / 'convert_docling.py')] + argv
    # Do not launch the interactive binary installer from a web request.
    core.offer_llama_install = lambda device: (_ for _ in ()).throw(
        RuntimeError('Локальный OCR runtime отсутствует. Установите зависимости из requirements-docling-surya.txt.'))
    failure_started = time.time()
    try:
        core.main()
    except (Exception, SystemExit) as error:
        failure = describe_conversion_failure(error, destination, since=failure_started)
        print(ERROR_MARKER + json.dumps(failure, ensure_ascii=False), flush=True)
        raise ConversionFailure(str(error), failure) from error
    status = json.loads((destination / 'logs' / 'conversion_status.json').read_text(encoding='utf-8'))
    records = status.get('documents', [])
    if len(records) != 1 or records[0].get('status') not in {'OK', 'REVIEW', 'SKIP'}:
        raise RuntimeError('Конвертер не опубликовал проверенный HTML')
    record = records[0]
    output = Path(record['output'])
    if not output.is_file():
        raise RuntimeError('Опубликованный HTML отсутствует')
    page_count = record.get('pages', image_pages)
    print(RESULT_MARKER + json.dumps(dict(html=str(output.resolve()), pages=page_count,
                                         technical_status=record.get('status', 'OK')), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--pages', default='')
    arguments = parser.parse_args()
    if not arguments.worker:
        parser.error('Internal conversion worker; start the application server instead')
    _worker(arguments.source.resolve(), arguments.destination.resolve(), _validate_pages(arguments.pages))
