"""Complete per-document conversion logs, including earlier engine attempts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
import uuid

if __package__:
    from .file_paths import native_path, normal_path
else:
    from file_paths import native_path, normal_path

_LOCK = threading.RLock()


def _root(value):
    return normal_path(value).resolve()


def _owned(root, path):
    """Only a real file beneath this job may enter its combined log."""
    normalized = normal_path(path).resolve()
    return normalized.is_relative_to(root) and native_path(normalized).is_file()


def _engine_files(root):
    result = []
    console = root / 'result' / 'engine.log'
    if _owned(root, console):
        result.append(console)
    locations = ((root / 'result' / 'engine' / 'logs', {'.log', '.txt', '.jsonl'}),
                 (root / 'result' / 'engine' / 'diagnostics', {'.log', '.jsonl'}))
    for folder, suffixes in locations:
        resolved = normal_path(folder).resolve()
        if not resolved.is_relative_to(root) or not native_path(resolved).is_dir():
            continue
        for candidate in native_path(resolved).rglob('*'):
            relative = normal_path(candidate)
            if not _owned(root, relative):
                continue
            if relative.suffix.lower() in suffixes or relative.name == 'conversion_status.json':
                result.append(relative)
    return sorted(set(result), key=lambda path: (path.relative_to(root).as_posix().casefold(), str(path)))


def append_job_log(job_root: Path, message: str) -> None:
    """Append an application event in UTF-8; never dump the job or environment."""
    root = _root(job_root)
    timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    with _LOCK:
        native_path(root).mkdir(parents=True, exist_ok=True)
        target = root / 'conversion.log'
        if normal_path(target).resolve().parent != root:
            raise ValueError('Путь журнала выходит за пределы задания')
        with native_path(target).open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(f'[{timestamp}] {str(message).rstrip()}\n')


def archive_engine_logs(job_root: Path) -> str | None:
    """Preserve the completed attempt before a fresh native conversion reuses it.

    Cached exports do not need this function. Only log files are copied; the
    current engine output, canonical HTML, sources and OCR cache stay intact.
    """
    root = _root(job_root)
    with _LOCK:
        files = _engine_files(root)
        if not files:
            return None
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        folder = root / 'log_history' / (stamp + '-' + uuid.uuid4().hex[:8])
        if not normal_path(folder).resolve().is_relative_to(root):
            raise ValueError('Путь истории журнала выходит за пределы задания')
        native_path(folder).mkdir(parents=True, exist_ok=False)
        for source in files:
            target = folder / source.relative_to(root)
            native_path(target.parent).mkdir(parents=True, exist_ok=True)
            shutil.copy2(native_path(source), native_path(target))
        return str(folder)


def _history_files(root):
    folder = normal_path(root / 'log_history').resolve()
    if not folder.is_relative_to(root) or not native_path(folder).is_dir():
        return []
    return sorted((normal_path(path) for path in native_path(folder).rglob('*')
                   if _owned(root, path)),
                  key=lambda path: (path.relative_to(root).as_posix().casefold(), str(path)))


def assemble_conversion_log(job: dict, job_root: Path) -> str:
    """Return the entire current log and archived attempts without a size cap.

    Only explicit display metadata is included. Secret fields in ``job`` and
    process environment values are never serialized. Existing converter log
    files already pass through the converter's secret-redaction mechanism.
    """
    root = _root(job_root)
    labels = (('name', 'Файл'), ('format', 'Формат результата'), ('status', 'Состояние'),
              ('pages', 'Диапазон страниц'), ('created_at', 'Добавлен'),
              ('started_at', 'Обработка начата'), ('completed_at', 'Обработка завершена'),
              ('stage', 'Последняя стадия'), ('error', 'Ошибка'))
    sections = ['ЛОГ КОНВЕРТАЦИИ\n']
    for key, label in labels:
        value = job.get(key)
        if value is not None and value != '':
            sections.append(f'{label}: {value}\n')
    if job.get('stats'):
        sections.append('Статистика: ' + json.dumps(job['stats'], ensure_ascii=False, sort_keys=True) + '\n')
    for warning in job.get('warnings') or []:
        sections.append('Предупреждение: ' + str(warning) + '\n')
    with _LOCK:
        files = []
        application = root / 'conversion.log'
        if _owned(root, application):
            files.append(application)
        files.extend(_history_files(root))
        files.extend(_engine_files(root))
        for path in files:
            label = path.relative_to(root).as_posix()
            sections.append('\n' + '=' * 20 + ' ' + label + ' ' + '=' * 20 + '\n')
            try:
                # read_text has no byte/line limit. This is the user's complete
                # log rather than the short engine-error tail used in the UI.
                value = native_path(path).read_text(encoding='utf-8-sig', errors='replace')
                sections.append(value)
                if not value.endswith('\n'):
                    sections.append('\n')
            except OSError as exc:
                sections.append('Не удалось прочитать журнал: ' + str(exc) + '\n')
    if not files:
        sections.append('\nЖурнал движка для этого задания отсутствует. Метаданные и сообщения задания приведены выше.\n')
    return ''.join(sections)
