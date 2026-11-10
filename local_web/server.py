#!/usr/bin/env python3
"""Loopback-only web application. No additional server dependencies."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid
import webbrowser
import zipfile
import tempfile

if __package__:
    from .file_paths import native_path, normal_path
    from .result_storage import ResultStorage
    from .conversion_logs import append_job_log, archive_engine_logs, assemble_conversion_log
else:
    from file_paths import native_path, normal_path
    from result_storage import ResultStorage
    from conversion_logs import append_job_log, archive_engine_logs, assemble_conversion_log

APP_ID = "region-document-converter"
VERSION = "1.8.4"
HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
FORMATS = {
    "html": (".html", "text/html; charset=utf-8"),
    "markdown": (".md", "text/markdown; charset=utf-8"),
    "json": (".json", "application/json; charset=utf-8"),
    "plaintext": (".txt", "text/plain; charset=utf-8"),
}
EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm",
              ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
MAX_UPLOAD = 512 * 1024 * 1024
MAX_JOBS = 200
MAX_PREVIEW = 8 * 1024 * 1024
PAGES = re.compile(r"^[1-9]\d*-[1-9]\d*$")
LOG = logging.getLogger(APP_ID)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def source_page_metadata(path):
    """Read only the PDF page tree; no OCR, layout or model is involved."""
    if Path(path).suffix.lower() != '.pdf':
        return {'source_page_count': None, 'page_count_error': ''}
    try:
        import pdfplumber
        from pdfminer.pdfdocument import PDFPasswordIncorrect
        try:
            with pdfplumber.open(native_path(path)) as document:
                count = len(document.pages)
        except PDFPasswordIncorrect:
            return {'source_page_count': None, 'page_count_error': 'PDF защищён паролем. Выбор страниц недоступен.'}
        if count <= 0:
            raise ValueError('PDF has no pages')
        return {'source_page_count': count, 'page_count_error': ''}
    except Exception:
        return {'source_page_count': None, 'page_count_error': 'Не удалось определить страницы PDF. Можно открыть оригинал для проверки.'}


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def safe_filename(value):
    # Both Windows and POSIX paths, control characters and reserved device names.
    name = str(value).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    if not name or len(name) > 180:
        raise APIError("Имя файла пустое или слишком длинное (максимум 180 символов).")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        name = "_" + name
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise APIError("Этот формат не поддерживается. Выберите PDF, DOCX, PPTX, XLSX, HTML или изображение.")
    return name


class JobStore:
    def __init__(self, root, converter=None, output_dir=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / "jobs.json"
        self.saved = ResultStorage(self.root, output_dir or self.root / 'ocr_output', application_root=HERE.parent)
        self.lock = threading.RLock()
        self.jobs = {}
        self.events = {}
        self.scheduled = set()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.converter = converter
        self.session_id = uuid.uuid4().hex
        self.engine = {"available": False, "message": "Проверка окружения…"}
        self.engine_host = None
        self.engine_probe_done = threading.Event()
        self._load()
        self._backfill_saved_html()
        self.worker = threading.Thread(target=self._work, name="conversion-queue", daemon=True)
        self.worker.start()
        if converter is None:
            threading.Thread(target=self._probe_engine, daemon=True).start()
        else:
            self.engine_probe_done.set()

    def _probe_engine(self):
        try:
            adapter = importlib.import_module("converter")
            info = adapter.get_engine_status()
            if info.get('available'):
                if __package__:
                    from .engine_host import PersistentEngine
                else:
                    from engine_host import PersistentEngine
                with self.lock:
                    if self.stopping.is_set():
                        return
                    self.engine_host = PersistentEngine(self.root / 'engine_runtime', python=info['python'])
                    self.engine_host.start()
        except Exception as exc:
            info = {"available": False, "message": f"Не удалось проверить конвертер: {exc}"}
        with self.lock:
            self.engine = info
        self.engine_probe_done.set()

    def _load(self):
        if not self.manifest.exists():
            return
        try:
            saved = json.loads(self.manifest.read_text(encoding="utf-8"))
            for job in saved.get("jobs", []):
                ident = job.get("id", "")
                if not re.fullmatch(r"[0-9a-f]{32}", ident):
                    continue
                # A manifest cannot direct file reads/deletion outside this workspace.
                job["source"] = str(self.root / ident / "input" / safe_filename(job["name"]))
                job.setdefault('pages', '')
                if 'source_page_count' not in job and native_path(job['source']).is_file():
                    job.update(source_page_metadata(job['source']))
                job.setdefault('source_page_count', None)
                job.setdefault('page_count_error', '')
                job['source_available'] = native_path(job['source']).is_file()
                clean = {}
                for fmt, value in job.get("artifacts", {}).items():
                    path = normal_path(value).resolve()
                    if fmt in FORMATS and path.is_relative_to(self.root / ident) and native_path(path).is_file():
                        clean[fmt] = str(path)
                job["artifacts"] = clean
                packages = {}
                for fmt, value in job.get("packages", {}).items():
                    folder = normal_path(value).resolve()
                    if fmt in clean and folder.is_relative_to(self.root / ident) and native_path(folder).is_dir():
                        packages[fmt] = str(folder)
                job["packages"] = packages
                job.setdefault("format", next(iter(clean), "html"))
                job['hidden'] = job.get('hidden') is True
                replay = normal_path(job.get("replay_html") or clean.get("html") or self.root / ident / "missing").resolve()
                job["replay_html"] = str(replay) if replay.is_relative_to(self.root / ident) and replay.is_file() else None
                job["legacy_export"] = bool(clean and not packages)
                if job.get("status") == "running":
                    job.update(status="error", stage="Обработка прервана", error="Приложение было закрыто. Повторите обработку файла.")
                if job.get("status") == "done" and not clean:
                    job.update(status="error", error="Файлы результата не найдены. Повторите обработку.")
                self.jobs[ident] = job
        except (OSError, ValueError, TypeError, KeyError):
            LOG.exception("Cannot load saved queue; the manifest has been retained")

    def _save(self):
        temporary = self.manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": VERSION, "jobs": list(self.jobs.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.manifest)

    def _backfill_saved_html(self):
        # Old installed deliveries can outlive their queue history. Only the
        # still-recorded exact package can establish their HTML provenance.
        for record in self.saved.list():
            job = self.jobs.get(record['job_id'])
            if not job or job.get('name') != record['name'] or job.get('format') != record['format']:
                continue
            try:
                self.saved.backfill_html(record['id'], package=self.package(job['id']),
                                         canonical=self.canonical(job['id']))
            except (APIError, OSError, ValueError, TypeError, KeyError) as error:
                LOG.warning('HTML старого сохранённого результата %s не дополнен: %s', record['id'], error)

    def public(self, job):
        editable = (job.get('status') in {'queued', 'error', 'cancelled'}
                    and job['id'] not in self.scheduled and Path(job['name']).suffix.lower() == '.pdf'
                    and type(job.get('source_page_count')) is int and job['source_page_count'] > 0)
        result = {key: value for key, value in job.items()
                  if key not in {"source", "source_pdf", "artifacts", "packages", "replay_html", "attempt_id", "review_count", "reviewed_count", "review_error_count"}
                  and not key.startswith(('_', 'technical_'))}
        result['warnings'] = [text for text in result.get('warnings', [])
                              if 'Движок пометил результат для визуальной проверки таблиц' not in str(text)]
        if job['id'] in self.scheduled and job.get('status') in {'done', 'error'}:
            # A completed conversion may still be finalizing its durable local
            # artifacts. Do not expose a restartable terminal state before the
            # queue releases it, otherwise an immediate export gets skipped.
            result.update(status='running', progress=99, stage='Завершение обработки')
        return result | {"artifacts": list(job.get("artifacts", {})), 'pages_editable': editable}

    def snapshot(self):
        with self.lock:
            saved_ids = {item['id'] for item in self.saved.list() if item.get('exists', True)}
            jobs = []
            for job in self.jobs.values():
                if job.get('hidden'):
                    continue
                public = self.public(job)
                if public.get('saved_result_id') not in saved_ids:
                    public['saved_result_id'] = None
                jobs.append(public)
            runtime = self.engine_host.snapshot() if self.engine_host is not None else None
            return {"version": VERSION, "session_id": self.session_id,
                    "engine": "convert_docling · local", "engine_ready": runtime.get('phase') == 'ready' if runtime else bool(self.engine.get("available")),
                    "engine_message": runtime['message'] if runtime else self.engine.get("message", ""),
                    "engine_runtime": runtime,
                    "jobs": jobs, "output_dir": str(self.saved.root), "storage_error": self.saved.storage_error}

    def history(self):
        """Saved deliveries and retained uploads share a recoverable history."""
        with self.lock:
            saved = [dict(item, kind='saved') for item in self.saved_call('list')]
            saved_ids = {item['id'] for item in saved if item.get('exists', True)}
            unsaved = []
            for job in self.jobs.values():
                if job.get('saved_result_id') in saved_ids:
                    continue
                if (job.get('saved_result_id') in self.saved.removed_result_ids
                        and job.get('status') != 'running' and job['id'] not in self.scheduled):
                    continue  # An externally removed delivery is not an unsaved upload.
                item = self.public(job)
                item.update(id='job:' + job['id'], job_id=job['id'], kind='job',
                            format=job.get('format', 'html'), exists=native_path(self.root / job['id']).is_dir(),
                            path=str(self.root / job['id']))
                unsaved.append(item)
            return sorted(saved + unsaved, key=lambda item: item.get('created_at', ''), reverse=True)

    def refresh_history(self):
        """Reconcile external folder deletion without touching active work."""
        with self.lock:
            protected = self.scheduled | {job['id'] for job in self.jobs.values() if job.get('status') == 'running'}
            removed = self.saved_call('prune_missing', protected_job_ids=protected)
            return {'items': self.history(), 'output_dir': str(self.saved.root), 'removed': len(removed)}

    def settings(self):
        with self.lock:
            return self.saved.settings()

    def change_output_dir(self, value):
        with self.lock:
            try:
                return self.saved.change_root(value)
            except ValueError as error:
                raise APIError(str(error)) from None
            except OSError as error:
                raise APIError('Не удалось выбрать папку сохранения. Проверьте путь и права доступа: ' + str(error), 409) from None

    def hide(self, ident):
        """Remove only a queue row; files, logs and history remain available."""
        with self.lock:
            job = self._get(ident)
            if job['status'] == 'running':
                raise APIError('Сначала остановите обработку файла.', 409)
            job['hidden'] = True
            self._save()
        return self.snapshot()

    def job_log(self, ident):
        with self.lock:
            job = self._get(ident)
            self._owned_job_folder(ident)
            return assemble_conversion_log(job, self.root / ident)

    def open_job(self, ident, target):
        with self.lock:
            job = self._get(ident)
            folder = self._owned_job_folder(ident)
            if target == 'source':
                path = folder / 'input' / safe_filename(job['name'])
                if not native_path(path).is_file():
                    path = self.saved_call('upload_source', ident)
            elif target == 'converted':
                path = normal_path(self.artifact(ident, job.get('format', 'html'))[0])
            elif target == 'folder':
                path = folder
            else:
                raise APIError('Неизвестный файл для открытия.')
            if not native_path(path).exists():
                raise APIError('Файл не найден.', 404)
            if os.name == 'nt':
                os.startfile(str(path))
            else:
                import subprocess
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _owned_job_folder(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise APIError('Некорректный идентификатор файла.')
        target = self.root / ident
        candidate = native_path(target)
        if (candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction())
                or normal_path(candidate.resolve()).parent != self.root):
            raise APIError('Рабочая папка файла была заменена ссылкой.', 409)
        if candidate.exists():
            try:
                self.saved._check_tree(target)
            except (OSError, ValueError) as error:
                raise APIError('Рабочая папка файла не прошла проверку: ' + str(error), 409) from None
        return target

    def _upload_duplicates(self):
        if not hasattr(self, '_duplicate_uploads'):
            if __package__:
                from .duplicate_uploads import DuplicateUploads
            else:
                from duplicate_uploads import DuplicateUploads
            self._duplicate_uploads = DuplicateUploads(self, APIError)
        return self._duplicate_uploads

    def upload_check(self, name):
        name = safe_filename(name)
        with self.lock:
            return self._upload_duplicates().check(name)

    def upload_existing(self, token):
        with self.lock:
            return self._upload_duplicates().reuse(token)

    def upload(self, stream, name, size, *, replace_token=None):
        name = safe_filename(name)
        if size <= 0 or size > MAX_UPLOAD:
            raise APIError("Файл пустой или превышает 512 МБ.", 413)
        with self.lock:
            guard = self._upload_duplicates()
            ticket = guard.begin(name, replace_token)
            if sum(not job.get('hidden') for job in self.jobs.values()) >= MAX_JOBS and not ticket['groups']:
                guard.finish(ticket)
                raise APIError("В очереди уже 200 файлов. Удалите ненужные записи.", 409)
            ident = uuid.uuid4().hex
            folder = self.root / ident / "input"
            try:
                folder.mkdir(parents=True)
            except Exception:
                guard.finish(ticket)
                raise
        path = folder / name
        remaining = size
        committed = False
        try:
            with native_path(path).open("wb") as target:
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise APIError("Загрузка файла оборвалась.")
                    target.write(block)
                    remaining -= len(block)
            job = {"id": ident, "name": name, "size": size, "source": str(path),
                   "status": "queued", "progress": 0, "stage": "Ожидает запуска", "error": "",
                   "stats": {}, "warnings": [], "artifacts": {}, "created_at": utc_now(),
                   'pages': '', 'source_available': True, **source_page_metadata(path)}
            with self.lock:
                previous = guard.validate_commit(ticket)
                self.jobs[ident] = job
                append_job_log(self.root / ident, f'Загружен файл: {name}; размер: {size} байт.')
                self._save()
                committed = True
                # The new source and manifest are durable before old results
                # are removed. Interrupted uploads can never delete old data.
                for group in previous:
                    try:
                        if group['job']:
                            self.delete(group['job_id'])
                        else:
                            self.delete_saved(group['records'][0]['id'])
                    except (APIError, OSError) as error:
                        message = ('Новый файл загружен, но прежнюю копию не удалось полностью удалить. '
                                   'Закройте открытые файлы и удалите прежнюю запись в Истории: ' + str(error))
                        job['warnings'].append(message)
                        if group['job_id'] in self.jobs:
                            self.jobs[group['job_id']]['hidden'] = True
                        LOG.warning('Old upload cleanup after replacement failed: %s', error)
                if job['warnings']:
                    self._save()
                if replace_token:
                    guard.decisions.pop(replace_token, None)
            return self.public(job)
        except Exception:
            if not committed:
                with self.lock:
                    self.jobs.pop(ident, None)
                    # New uploads exist only in private job storage. No saved
                    # reservation needs cleanup, even if an old output drive
                    # is unavailable and complete saved-result deletion blocks.
                target = self._owned_job_folder(ident)
                if native_path(target).exists():
                    shutil.rmtree(native_path(target))
            raise
        finally:
            guard.finish(ticket)

    def _get(self, ident):
        try:
            return self.jobs[ident]
        except KeyError:
            raise APIError("Файл не найден.", 404) from None

    def _validate_job_pages(self, job, pages, *, legacy=False):
        if not isinstance(pages, str):
            raise APIError('Выберите диапазон страниц PDF.')
        pages = pages.strip()
        if not pages:
            return ''
        if not PAGES.fullmatch(pages):
            raise APIError('Выберите первую и последнюю страницы PDF.')
        first, last = map(int, pages.split('-'))
        if first > last:
            raise APIError('Последняя страница не может быть раньше первой.')
        if Path(job['name']).suffix.lower() != '.pdf':
            if legacy:
                return pages
            raise APIError('Выбор страниц доступен только для PDF.')
        count = job.get('source_page_count')
        if not isinstance(count, int) or count <= 0:
            if legacy:
                return pages
            raise APIError(job.get('page_count_error') or 'Количество страниц PDF ещё не определено.')
        if last > count:
            raise APIError(f'В этом PDF только {count} страниц.')
        return f'{first}-{last}'

    def set_pages(self, ident, pages):
        with self.lock:
            job = self._get(ident)
            if job['status'] not in {'queued', 'error', 'cancelled'} or ident in self.scheduled:
                raise APIError('Страницы можно выбрать до запуска обработки.', 409)
            job['pages'] = self._validate_job_pages(job, pages)
            self._save()
        return self.snapshot()

    def start(self, ids, pages=None, output_format="html", *, export_only=False, reprocess=False):
        if output_format not in FORMATS:
            raise APIError("Неизвестный формат результата.")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise APIError("Выберите файлы для обработки.")
        with self.lock:
            if self.stopping.is_set():
                raise APIError("Приложение завершает работу.", 409)
            selected = [self._get(i) for i in dict.fromkeys(ids)]
            if export_only and any(not job.get("replay_html") for job in selected):
                raise APIError("Снимок документа для нового экспорта не найден. Повторите конвертацию.", 409)
            chosen_pages = {job['id']: self._validate_job_pages(job, job.get('pages', '') if pages is None else pages,
                            legacy=pages is not None or job.get('source_page_count') is None) for job in selected}
            for job in selected:
                ident = job["id"]
                if job["status"] == "running" or (job["status"] == "done" and not export_only and not reprocess) or ident in self.scheduled:
                    continue
                selected_pages = chosen_pages[ident]
                if not export_only:
                    job.update(attempt_id=uuid.uuid4().hex,
                               artifacts={}, packages={}, replay_html=None, stats={}, warnings=[])
                    job.pop('completed_at', None)
                    job.pop('export_schema', None)
                    for key in list(job):
                        if key.startswith('technical_'):
                            job.pop(key, None)
                job.update(status="queued", progress=0, stage="В очереди", error="", pages=selected_pages,
                           format=output_format, _replay=job.get("replay_html") if export_only else None,
                           saved_result_id=None)
                append_job_log(self.root / ident, f'{"Повторный экспорт из HTML" if export_only else "Конвертация"}: формат={output_format}; страницы={selected_pages or "все"}.')
                self.events[ident] = threading.Event()
                self.scheduled.add(ident)
                self.pending.put(ident)
            self._save()
        return self.snapshot()

    def cancel(self, ids):
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise APIError("Некорректный список файлов.")
        with self.lock:
            selected = [self._get(i) for i in ids]
            for job in selected:
                if job["status"] not in {"queued", "running"}:
                    continue
                event = self.events.get(job["id"])
                if event:
                    event.set()
                append_job_log(self.root / job['id'], 'Запрошена отмена обработки.')
                if job["status"] == "queued":
                    job.update(status="cancelled", stage="Отменено")
                    self.scheduled.discard(job["id"])
                else:
                    job["stage"] = "Остановка обработки…"
            self._save()
        return self.snapshot()

    def delete(self, ident):
        with self.lock:
            job = self._get(ident)
            if job["status"] == "running" or ident in self.scheduled:
                raise APIError("Сначала остановите обработку файла.", 409)
            target = self._owned_job_folder(ident)
            # Preflight every saved version before deleting either private or
            # downloaded files. Same-named independent uploads are unrelated.
            self.saved_call('validate_delete_job', ident)
            self.saved_call('delete_job', ident)
            if native_path(target).exists():
                try:
                    shutil.rmtree(native_path(target))
                except OSError as error:
                    raise APIError('Не удалось полностью удалить рабочие файлы. Закройте открытые файлы и повторите удаление: '
                                   + str(error), 409) from None
            del self.jobs[ident]
            self.events.pop(ident, None)
            self._save()

    def artifact(self, ident, fmt):
        with self.lock:
            job = self._get(ident)
            fmt = fmt or job.get("format", "html")
            if fmt not in FORMATS:
                raise APIError("Неизвестный формат результата.")
            if job["status"] != "done" or fmt not in job.get("artifacts", {}):
                raise APIError("Результат ещё не готов.", 409)
            path = normal_path(job["artifacts"][fmt]).resolve()
            if not path.is_relative_to(self.root / ident) or not native_path(path).is_file():
                raise APIError("Результат не найден.", 404)
            return native_path(path), path.name

    def package(self, ident, fmt=None):
        with self.lock:
            job = self._get(ident)
            fmt = fmt or job.get("format", "html")
            if fmt not in FORMATS:
                raise APIError("Неизвестный формат результата.")
            if job.get("legacy_export"):
                raise APIError("Для старого результата соберите папку заново кнопкой «Экспортировать»; OCR не повторяется.", 409)
            self.artifact(ident, fmt)
            folder = normal_path(job.get("packages", {}).get(fmt, self.root / "missing")).resolve()
            if not folder.is_relative_to(self.root / ident) or not native_path(folder).is_dir():
                raise APIError("Папка результата не найдена.", 404)
            return folder

    def canonical(self, ident):
        with self.lock:
            job = self._get(ident)
            if job.get('status') != 'done' or job.get('legacy_export'):
                raise APIError('HTML-предпросмотр станет доступен после подготовки нового экспорта.', 409)
            value = job.get('replay_html')
            if not value:
                raise APIError('Исходный HTML для предпросмотра не найден.', 404)
            path = normal_path(value).resolve()
            if not path.is_relative_to(self.root / ident) or not native_path(path).is_file():
                raise APIError('Исходный HTML для предпросмотра не найден.', 404)
            return native_path(path)

    def save_result(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise APIError('Не выбран документ для сохранения.')
        with self.lock:
            job = self._get(ident)
            package = self.package(ident)
            if __package__:
                from .export_bundle import EXPORT_SCHEMA, build_exports
            else:
                from export_bundle import EXPORT_SCHEMA, build_exports
            if job.get('export_schema') != EXPORT_SCHEMA:
                # Rebuild legacy deliveries from private canonical HTML without
                # OCR so newly saved folders contain only the selected format.
                try:
                    result = build_exports(None, Path(job['source']), self.root / ident / 'result',
                                           output_format=job.get('format', 'html'),
                                           replay_html=self.canonical(ident), pages=job.get('stats', {}).get('pages'))
                    artifacts = {fmt: str(normal_path(value).resolve()) for fmt, value in result['artifacts'].items()}
                    packages = {fmt: str(normal_path(value).resolve()) for fmt, value in result['packages'].items()}
                    replay = normal_path(result['replay_html']).resolve()
                    if (set(artifacts) != {job.get('format', 'html')} or set(packages) != set(artifacts)
                            or any(not Path(path).is_relative_to(self.root / ident) or not native_path(path).is_file() for path in artifacts.values())
                            or any(not Path(path).is_relative_to(self.root / ident) or not native_path(path).is_dir() for path in packages.values())
                            or not replay.is_relative_to(self.root / ident) or not native_path(replay).is_file()):
                        raise ValueError('Новый пакет результата не прошёл проверку')
                except (OSError, ValueError) as error:
                    raise APIError('Не удалось обновить папку результата из HTML: ' + str(error), 409) from None
                job.update(artifacts=artifacts, packages=packages, replay_html=str(replay),
                           export_schema=result.get('export_schema'), legacy_export=False,
                           stats=result.get('stats', {}), warnings=list(dict.fromkeys(job.get('warnings', []) + result.get('warnings', []))),
                           saved_result_id=None)
                append_job_log(self.root / ident, 'Папка результата обновлена из сохранённого HTML без повторного OCR.')
                self._save()
                package = self.package(ident)
            append_job_log(self.root / ident, 'Сохранение результата в ' + str(self.saved.root) + '.')
            content = assemble_conversion_log(job, self.root / ident)
            saved = self.saved_call('save', package, job_id=ident, name=job['name'],
                                    format=job.get('format', 'html'), log=content, canonical=self.canonical(ident))
            job['saved_result_id'] = saved['id']
            self._save()
            return saved

    def delete_saved(self, ident):
        with self.lock:
            record = self.saved_call('get', ident)
            job_id = record['job_id']
            if job_id in self.jobs:
                self.delete(job_id)
            else:
                # A saved delivery may outlive an older queue manifest. Its
                # upload ID also identifies an owned private cache, if one
                # survived. Legacy non-UUID IDs cannot name a private folder.
                target = self._owned_job_folder(job_id) if re.fullmatch(r'[0-9a-f]{32}', job_id) else None
                self.saved_call('validate_delete_job', job_id)
                if target is not None and native_path(target).exists():
                    try:
                        shutil.rmtree(native_path(target))
                    except OSError as error:
                        raise APIError('Не удалось полностью удалить рабочие файлы. Закройте открытые файлы и повторите удаление: '
                                       + str(error), 409) from None
                self.saved_call('delete_job', job_id)

    def saved_document(self, ident, target):
        with self.lock:
            record = self.saved_call('get', ident)
            job_id = record['job_id']
            if target == 'source' and job_id in self.jobs:
                job = self._get(job_id)
                folder = self._owned_job_folder(job_id)
                path = folder / 'input' / safe_filename(job['name'])
                if native_path(path).is_file():
                    return native_path(path)
            return self.saved_call('target_file', ident, target)

    def open_saved(self, ident, target):
        if target == 'source':
            path = self.saved_document(ident, target)
            if os.name == 'nt':
                os.startfile(str(path))
            else:
                import subprocess
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        return self.saved_call('open_target', ident, target)

    def saved_call(self, operation, *args, **kwargs):
        try:
            return getattr(self.saved, operation)(*args, **kwargs)
        except KeyError:
            raise APIError('Сохранённый результат не найден.', 404) from None
        except FileNotFoundError as exc:
            raise APIError(str(exc), 404) from None
        except PermissionError:
            raise APIError('Нет доступа к папке результата. Закройте открытые файлы и проверьте права доступа.', 409) from None
        except ValueError as exc:
            raise APIError(str(exc), 409) from None
        except OSError as exc:
            raise APIError('Операция с сохранённым результатом не выполнена: ' + str(exc), 409) from None

    def save_all(self):
        with self.lock:
            saved_ids = {item['id'] for item in self.saved_call('list') if item.get('exists', True)}
            ids = [job['id'] for job in self.jobs.values() if job['status'] == 'done'
                   and not job.get('hidden') and not job.get('legacy_export')
                   and job.get('saved_result_id') not in saved_ids]
        items, errors = [], []
        for ident in ids:
            try:
                items.append(self.save_result(ident))
            except APIError as exc:
                errors.append({'id': ident, 'error': str(exc)})
        return {'items': items, 'errors': errors}

    def asset(self, ident, name):
        if Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp|gif|bmp|tif|tiff)", name, re.I):
            raise APIError("Изображение не найдено.", 404)
        folder = self.package(ident)
        path = (folder / "src" / name).resolve()
        if not path.is_relative_to(folder / "src") or not native_path(path).is_file():
            raise APIError("Изображение не найдено.", 404)
        kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}[path.suffix.lower()]
        return native_path(path), kind

    def _work(self):
        while not self.stopping.is_set():
            try:
                ident = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            with self.lock:
                job = self.jobs.get(ident)
                if not job or job["status"] != "queued":
                    self.scheduled.discard(ident)
                    self.pending.task_done()
                    continue
                event = self.events[ident]
                job.update(status="running", stage="Подготовка документа", started_at=utc_now())
                append_job_log(self.root / ident, 'Начата обработка документа.')
                self._save()

            def progress(percent, stage):
                with self.lock:
                    job["progress"] = max(job["progress"], min(99, max(0, int(percent))))
                    if not event.is_set():
                        if job.get('stage') != str(stage):
                            append_job_log(self.root / ident, f'{int(percent)}%: {stage}')
                        job["stage"] = str(stage)

            try:
                if self.converter is None and not job.get('_replay') and Path(job['source']).suffix.lower() not in {'.html', '.htm', '.xlsx'}:
                    while not self.engine_probe_done.wait(.15):
                        if event.is_set() or self.stopping.is_set():
                            raise RuntimeError('Обработка отменена до подготовки движка')
                if not job.get('_replay'):
                    archive_engine_logs(self.root / ident)
                convert = self.converter or importlib.import_module("converter").convert_file
                runtime_options = {'engine_host': self.engine_host} if self.converter is None else {}
                result = convert(Path(job["source"]), self.root / ident / "result",
                                 pages=job.get("pages", ""), on_progress=progress, cancel_event=event,
                                 output_format=job.get("format", "html"), replay_html=job.get("_replay"), **runtime_options)
                with self.lock:
                    if event.is_set():
                        job.update(status="cancelled", stage="Отменено")
                        append_job_log(self.root / ident, 'Обработка отменена.')
                    else:
                        artifacts = {fmt: str(normal_path(value).resolve()) for fmt, value in result["artifacts"].items()}
                        if set(artifacts) != {job.get("format", "html")} or any(not Path(p).is_relative_to(self.root / ident) or not native_path(p).is_file() for p in artifacts.values()):
                            raise RuntimeError("Конвертер не создал выбранный формат результата.")
                        packages = {fmt: str(normal_path(value).resolve()) for fmt, value in result.get("packages", {}).items()}
                        if set(packages) != set(artifacts) or any(not Path(p).is_relative_to(self.root / ident) or not native_path(p).is_dir() for p in packages.values()):
                            raise RuntimeError("Конвертер не создал папку результата.")
                        replay = normal_path(result.get('replay_html') or self.root / 'missing').resolve()
                        if not replay.is_relative_to(self.root / ident) or not native_path(replay).is_file():
                            raise RuntimeError('Конвертер не сохранил исходный HTML документа.')
                        if job.get("_replay") and result.get("stats", {}).get("pages") is None:
                            result["stats"]["pages"] = job.get("stats", {}).get("pages")
                        if job.get("_replay"):
                            result['warnings'] = list(dict.fromkeys(job.get('warnings', []) + result.get('warnings', [])))
                        job.update(status="done", progress=100, stage="Готово", artifacts=artifacts, packages=packages,
                                   replay_html=str(replay), export_schema=result.get("export_schema"), legacy_export=False,
                                   stats=result.get("stats", {}), warnings=result.get("warnings", []), completed_at=utc_now())
                        if result.get('technical_status'):
                            job['technical_status'] = result['technical_status']
                        append_job_log(self.root / ident, 'Обработка успешно завершена.')
                        for join in result.get('table_joins', []):
                            append_job_log(self.root / ident, 'Склейка фрагментов HTML-таблицы: ' + json.dumps(join, ensure_ascii=False))
                        for warning in job.get('warnings', []):
                            append_job_log(self.root / ident, 'ПРЕДУПРЕЖДЕНИЕ: ' + str(warning))
            except Exception as exc:
                with self.lock:
                    if event.is_set():
                        LOG.info("Conversion cancelled for %s", ident)
                        job.update(status="cancelled", stage="Отменено")
                        append_job_log(self.root / ident, 'Обработка отменена.')
                    else:
                        LOG.exception("Conversion failed for %s", ident)
                        job.update(status="error", stage="Ошибка обработки", error=str(exc)[:2000])
                        append_job_log(self.root / ident, 'ОШИБКА: ' + traceback.format_exc())
            finally:
                with self.lock:
                    self.scheduled.discard(ident)
                    try:
                        self._save()
                    except OSError:
                        LOG.exception('Cannot persist completed queue state for %s', ident)
                self.pending.task_done()

    def stop(self):
        self.stopping.set()
        with self.lock:
            for event in self.events.values():
                event.set()
            for job in self.jobs.values():
                if job["status"] == "queued" and job["id"] in self.scheduled:
                    job.update(status="cancelled", stage="Отменено при закрытии")
                    append_job_log(self.root / job['id'], 'Задание отменено при завершении приложения.')
            self._save()
        self.worker.join(timeout=15)
        if self.engine_host is not None:
            self.engine_host.close()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalDocumentConverter/1.0"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(90)

    def log_message(self, fmt, *args):
        # Never log query parameters, uploaded content or original document names.
        if self.command == "GET" and urlsplit(self.path).path == "/api/status":
            return
        LOG.info("HTTP %s %s", self.command, urlsplit(self.path).path)

    def _check_host(self, mutation=False):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed:
            raise APIError("Доступ разрешён только с этого компьютера.", 403)
        if mutation:
            if self.headers.get("X-Local-App") != "1":
                raise APIError("Откройте приложение в локальном браузере.", 403)
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                raise APIError("Запрос с другого сайта отклонён.", 403)

    def _headers(self, status, kind, size, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'none'")
        if disposition:
            self.send_header("Content-Disposition", f"attachment; filename=\"document{Path(disposition).suffix}\"; filename*=UTF-8''{quote(disposition)}")
        self.end_headers()

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError("Некорректный размер запроса.") from None
        if not 0 < length < 65536:
            raise APIError("Некорректный размер запроса.")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise APIError("Некорректный JSON.") from None
        if not isinstance(data, dict):
            raise APIError("Ожидается JSON-объект.")
        return data

    def _file(self, path, kind, name=None):
        with path.open("rb") as stream:
            self._headers(200, kind, path.stat().st_size, name)
            shutil.copyfileobj(stream, self.wfile, 1024 * 1024)

    @staticmethod
    def _image_type(path):
        return {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
                '.tif': 'image/tiff', '.tiff': 'image/tiff'}[path.suffix.lower()]

    def _dispatch(self):
        self._check_host(self.command != "GET")
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        store = self.server.store
        fmt = query.get("format", [None])[0]
        if self.command == "GET":
            if path == "/api/health":
                return self._json({"app": APP_ID, "version": VERSION})
            if path == "/api/status":
                return self._json(store.snapshot())
            if path == '/api/settings':
                return self._json(store.settings())
            if path == '/api/installed':
                return self._json({'items': store.history(), 'output_dir': str(store.saved.root)})
            match = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/log', path)
            if match:
                return self._json({'content': store.job_log(match[1])})
            match = re.fullmatch(r'/api/installed/([0-9a-f]{32})/log', path)
            if match:
                return self._json({'content': store.saved_call('read_log', match[1])})
            match = re.fullmatch(r'/api/(?:view/([0-9a-f]{32})|installed/([0-9a-f]{32})/view)/(document\.html|src/[^/]+)', path)
            if match:
                job_id, saved_id, resource = match.groups()
                if resource == 'document.html':
                    file = (store.saved_document(saved_id, 'html') if saved_id
                            else store.canonical(job_id))
                    return self._file(file, 'text/html; charset=utf-8')
                name = unquote(resource[4:])
                if saved_id:
                    file = store.saved_call('asset', saved_id, name)
                    kind = self._image_type(file)
                else:
                    file, kind = store.asset(job_id, name)
                return self._file(file, kind)
            match = re.fullmatch(r"/api/(download|document|preview)/([0-9a-f]{32})", path)
            if match:
                if match[1] == "download":
                    return self._archive(fmt, [match[2]])
                if match[1] == 'preview' and query.get('view', [''])[0] == 'html':
                    file = store.canonical(match[2])
                    selected_format, name = 'html', file.name
                else:
                    file, name = store.artifact(match[2], fmt)
                    selected_format = fmt or store.jobs[match[2]].get("format", "html")
                if match[1] == "document":
                    return self._file(file, FORMATS[selected_format][1], name)
                with file.open("rb") as stream:
                    data = stream.read(MAX_PREVIEW + 1)
                return self._json({"content": data[:MAX_PREVIEW].decode("utf-8", errors="replace"),
                                   "format": selected_format, "assets_base_url": f"/api/assets/{match[2]}/",
                                   "truncated": len(data) > MAX_PREVIEW})
            match = re.fullmatch(r"/api/assets/([0-9a-f]{32})/([^/]+)", path)
            if match:
                file, kind = store.asset(match[1], unquote(match[2]))
                return self._file(file, kind)
            if path == "/api/archive":
                return self._archive(fmt)
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/index.html": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/app.css": ("app.css", "text/css; charset=utf-8"),
                      "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
                      "/brand-logo.png": ("brand-logo.png", "image/png"),
                      "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                      "/app-icon.png": ("app-icon.png", "image/png"),
                      "/history-icon.png": ("history-icon.png", "image/png")}
            if path in static and (WEB / static[path][0]).is_file():
                return self._file(WEB / static[path][0], static[path][1])
        elif self.command == "POST":
            if path == '/api/settings':
                return self._json(store.change_output_dir(self._body().get('output_dir')))
            if path == '/api/upload-check':
                return self._json(store.upload_check(self._body().get('name', '')))
            if path == '/api/upload-existing':
                return self._json(store.upload_existing(self._body().get('token')))
            if path == '/api/installed/refresh':
                self._body()
                return self._json(store.refresh_history())
            match = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/pages', path)
            if match:
                return self._json(store.set_pages(match[1], self._body().get('pages', '')))
            match = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/hide', path)
            if match:
                self._body()
                return self._json(store.hide(match[1]))
            match = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/open', path)
            if match:
                store.open_job(match[1], self._body().get('target', 'folder'))
                return self._json({'ok': True})
            if path == '/api/desktop/focus':
                self._body()
                focus = getattr(self.server, 'desktop_focus', None)
                if focus is None:
                    raise APIError('Окно pywebview не запущено.', 409)
                focus()
                return self._json({'ok': True})
            if path == '/api/save':
                return self._json({'saved': store.save_result(self._body().get('id'))})
            if path == '/api/save-all':
                self._body()
                return self._json(store.save_all())
            match = re.fullmatch(r'/api/installed/([0-9a-f]{32})/open', path)
            if match:
                data = self._body()
                if 'target' in data:
                    store.open_saved(match[1], data['target'])
                else:
                    store.saved_call('open_folder', match[1])
                return self._json({'ok': True})
            if path == "/api/upload":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise APIError("Некорректный размер файла.") from None
                return self._json(store.upload(self.rfile, query.get("name", [""])[0], size,
                                              replace_token=query.get('replace_token', [None])[0]), 201)
            if path == "/api/start":
                data = self._body()
                return self._json(store.start(data.get("ids"), data.get("pages"), data.get("format", "html")))
            if path == "/api/export":
                data = self._body()
                return self._json(store.start(data.get("ids"), data.get("pages"), data.get("format", "html"), export_only=True))
            if path == "/api/reprocess":
                data = self._body()
                return self._json(store.start(data.get("ids"), data.get("pages"), data.get("format", "html"), reprocess=True))
            if path == "/api/cancel":
                return self._json(store.cancel(self._body().get("ids")))
            if path == "/api/shutdown":
                self.close_connection = True
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        elif self.command == "DELETE":
            match = re.fullmatch(r'/api/installed/([0-9a-f]{32})', path)
            if match:
                store.delete_saved(match[1])
                return self._json({'ok': True})
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
            if match:
                store.delete(match[1])
                return self._json({"ok": True})
        raise APIError("Страница не найдена.", 404)

    def _archive(self, fmt, ids=None):
        store = self.server.store
        if fmt is not None and fmt not in FORMATS:
            raise APIError("Неизвестный формат результата.")
        if ids is None:
            with store.lock:
                ids = [j["id"] for j in store.jobs.values() if j["status"] == "done"
                       and not j.get('hidden') and not j.get("legacy_export")]
        if not ids:
            raise APIError("Готовых файлов пока нет.", 409)
        folders = [store.package(ident, fmt) for ident in ids]
        archive_name = folders[0].name + '.zip' if len(folders) == 1 else 'converted-documents.zip'
        # A zip is the browser's portable download representation of a folder.
        with tempfile.TemporaryFile(dir=store.root) as temporary:
            used = set()
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for folder in folders:
                    name, counter = folder.name, 2
                    while name.casefold() in used:
                        name = f"{folder.name} ({counter})"
                        counter += 1
                    used.add(name.casefold())
                    archive.writestr(name + '/', b'')
                    native_folder = native_path(folder)
                    for entry in sorted(native_folder.rglob('*')):
                        if not entry.resolve().is_relative_to(native_folder.resolve()):
                            raise APIError("Некорректный путь внутри папки результата.", 409)
                        relative = name + '/' + entry.relative_to(native_folder).as_posix()
                        if entry.is_dir():
                            archive.writestr(relative + '/', b'')
                        elif native_path(entry).is_file():
                            archive.write(native_path(entry), relative)
            size = temporary.tell()
            temporary.seek(0)
            self._headers(200, "application/zip", size, archive_name)
            shutil.copyfileobj(temporary, self.wfile, 1024 * 1024)

    def _handle(self):
        try:
            self._dispatch()
        except APIError as exc:
            self.close_connection = True
            self._json({"error": str(exc), **getattr(exc, 'details', {})}, exc.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            LOG.exception("Request failed")
            self.close_connection = True
            self._json({"error": "Ошибка локального сервера. Подробности в журнале приложения."}, 500)

    do_GET = do_POST = do_DELETE = _handle


def main(argv=None):
    installed = (HERE.parent / '.region-ocr-install.json').is_file()
    user_home = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'RegionDocumentConverter'
    parser = argparse.ArgumentParser(description="Локальный конвертер документов")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path, default=user_home / 'data' if installed else HERE / "data")
    parser.add_argument('--output-dir', type=Path, default=user_home / 'Output' if installed else HERE.parent / 'ocr_output')
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть от 0 до 65535.")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.desktop:
        if __package__:
            from .desktop import native_runtime_available, prepare_runtime
        else:
            from desktop import native_runtime_available, prepare_runtime
        prepare_runtime()
        if not native_runtime_available():
            args.desktop = False
            args.open_browser = True
    instance = None
    if args.desktop:
        if __package__:
            from .desktop import acquire_or_focus, prepare_runtime
        else:
            from desktop import acquire_or_focus, prepare_runtime
        instance = acquire_or_focus(args.data_dir)
        if instance is None:
            return
        try:
            prepare_runtime()
        except Exception:
            instance.close()
            raise
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(args.data_dir / "application.log", maxBytes=5 * 1024 * 1024,
                                                     backupCount=3, encoding="utf-8"), logging.StreamHandler()])
    store = JobStore(args.data_dir, output_dir=args.output_dir)
    try:
        server = LocalServer(("127.0.0.1", args.port), store)
    except OSError:
        store.stop()
        raise
    url = f"http://127.0.0.1:{server.server_port}"
    LOG.info("Application ready at %s", url)
    print(url, flush=True)
    if args.open_browser:
        webbrowser.open(url)
    try:
        if args.desktop:
            if __package__:
                from .desktop import run
            else:
                from desktop import run
            run(server, url, args.data_dir)
        else:
            server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        store.stop()
        server.server_close()
        if instance:
            instance.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        LOG.exception('Application startup failed')
        if '--desktop' in sys.argv and os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'Ошибка запуска конвертера', 16)
        raise
