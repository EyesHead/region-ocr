"""One private, persistent OCR process per application; stdio is the only IPC."""
from __future__ import annotations

from hashlib import sha256
import json
from logging.handlers import RotatingFileHandler
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

MARKER = 'LOCAL_ENGINE_HOST '


def _source_revision(directory=None):
    """Fingerprint executable conversion sources, never jobs, models or caches."""
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parents[1]
    files = [directory / 'convert_docling.py', *directory.glob('docling_*.py'),
             *(directory / 'local_web').glob('*.py')]
    digest = sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(directory)).replace('\\', '/').encode('utf-8'))
        digest.update(b'\0')
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class PersistentEngine:
    def __init__(self, root, *, python=None, command=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.python = python
        self.command = command
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.process = None
        self.process_birth = None
        self.process_source_revision = None
        self.reader = None
        self.watcher = None
        self.owned_children = {}
        self.active = None
        self.closed = False
        self.ready = threading.Event()
        self.state = dict(phase='stopped', message='Движок не запущен')
        self.logger = logging.getLogger('engine-host.' + uuid.uuid4().hex)
        self.logger.propagate = False
        handler = RotatingFileHandler(self.root / 'runtime.log', maxBytes=2*1024*1024,
                                     backupCount=2, encoding='utf-8')
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self):
        with self.lock:
            dead_previous = self.process is not None and self.process.poll() is not None
        if dead_previous:
            self._terminate()
        with self.lock:
            if self.closed:
                raise RuntimeError('Локальный движок уже завершён')
            if self.process is not None and self.process.poll() is None:
                return
            self.ready.clear()
            self.state = dict(phase='warming', message='Подготовка локального OCR…')
            source_revision = _source_revision()
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONFAULTHANDLER'] = '1'
            command = self.command or [self.python, '-u', str(Path(__file__).with_name('engine_process.py'))]
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', bufsize=1,
                    env=env, cwd=Path(__file__).resolve().parents[1],
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    start_new_session=os.name != 'nt')
            except OSError as error:
                self.state.update(phase='error', message=f'Не удалось запустить локальный OCR: {error}')
                self.ready.set()
                raise
            self.process = process
            self.process_source_revision = source_revision
            self.state['source_revision'] = source_revision
            import psutil
            try:
                self.process_birth = psutil.Process(process.pid).create_time()
            except psutil.NoSuchProcess:
                self.process_birth = None
            self.state['pid'] = process.pid
            self.reader = threading.Thread(target=self._read, args=(process,), daemon=True,
                                           name='persistent-ocr-output')
            self.reader.start()
            self.watcher = threading.Thread(target=self._watch_children, args=(process, self.process_birth), daemon=True,
                                            name='persistent-ocr-ownership')
            self.watcher.start()

    def _watch_children(self, process, birth):
        import psutil
        while True:
            with self.lock:
                if self.process is not process:
                    return
            try:
                # Remember PID *and* birth time before a crashed host can
                # orphan a detached llama process. Registry PIDs are not proof.
                parent = psutil.Process(process.pid)
                if birth is None or parent.create_time() != birth:
                    return
                children = parent.children(recursive=True)
                identities = {}
                for child in children:
                    try:
                        identities[child.pid] = child.create_time()
                    except psutil.Error:
                        pass
                with self.lock:
                    if self.process is process:
                        self.owned_children.update(identities)
            except psutil.Error:
                pass
            if process.poll() is not None:
                return
            time.sleep(.1)

    def _read(self, process):
        try:
            for line in process.stdout:
                event = None
                if line.startswith(MARKER):
                    try:
                        event = json.loads(line[len(MARKER):])
                    except ValueError:
                        pass
                with self.lock:
                    if self.process is not process:
                        break
                    if event and event.get('event') in {'ready', 'startup_error'}:
                        self.state.update(event)
                        self.state['phase'] = 'ready' if event['event'] == 'ready' else 'error'
                        self.ready.set()
                    elif event and event.get('event') in {'job_started', 'complete'}:
                        self.state.update({key: event[key] for key in ('backend_pid', 'backend_identity') if key in event})
                    active = self.active
                if active is not None:
                    active.put((line, event))
                else:
                    self.logger.info(line.rstrip())
        finally:
            if __package__:
                from .converter import _process_exit_message
            else:
                from converter import _process_exit_message
            try:
                code = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                code = process.poll()
            with self.lock:
                if self.process is process:
                    if not self.closed:
                        message = _process_exit_message(code)
                        if self.state.get('phase') == 'error':
                            message = self.state.get('message', '') + ' ' + message
                        self.state.update(phase='error', message=message, exit_code=code)
                        self.logger.error(message)
                    self.ready.set()
                    if self.active is not None:
                        self.active.put((None, None))

    def run(self, source, destination, pages, on_progress=None, cancel_event=None):
        # Import lazily: converter imports this lifecycle class only for a real app.
        if __package__:
            from .converter import ConversionCancelled, RESULT_MARKER, _check_cancel
        else:
            from converter import ConversionCancelled, RESULT_MARKER, _check_cancel
        import re
        with self.run_lock:
            _check_cancel(cancel_event)
            # warm_runtime imports core once. Refresh only between jobs: the
            # run lock protects an active document from a source-code update.
            source_revision = _source_revision()
            with self.lock:
                changed = (self.process is not None and self.process.poll() is None
                           and self.process_source_revision != source_revision)
            if changed:
                self.logger.info('Conversion sources changed; restarting the owned idle OCR process')
                self._terminate()
            self.start()
            started = time.monotonic()
            timeout = max(60, int(os.environ.get('DOCUMENT_CONVERTER_TIMEOUT_SECONDS', '7200')))
            log_path = Path(destination) / 'engine.log'
            output = queue.Queue()
            ident = uuid.uuid4().hex
            result = None
            error_detail = last_error = ''
            try:
                if on_progress:
                    on_progress(5, 'Ожидание готовности локального OCR')
                while not self.ready.wait(.15):
                    _check_cancel(cancel_event)
                    if time.monotonic() - started > timeout:
                        raise TimeoutError('Превышено время подготовки локального OCR')
                _check_cancel(cancel_event)
                with self.lock:
                    if self.state.get('phase') != 'ready':
                        raise RuntimeError(self.state.get('message', 'Локальный OCR не запустился') + ' Журнал: engine_runtime/runtime.log')
                    process = self.process
                    self.active = output
                    command = dict(operation='convert', id=ident, source=str(source),
                                   destination=str(Path(destination) / 'engine'), pages=pages)
                    process.stdin.write(json.dumps(command, ensure_ascii=True) + '\n')
                    process.stdin.flush()
                with log_path.open('w', encoding='utf-8') as log:
                    log.write('Persistent OCR: ' + json.dumps(self.snapshot(), ensure_ascii=False) + '\n')
                    while True:
                        _check_cancel(cancel_event)
                        if time.monotonic() - started > timeout:
                            raise TimeoutError(f'Превышено время конвертации ({timeout} с); журнал: engine.log')
                        try:
                            line, event = output.get(timeout=.15)
                        except queue.Empty:
                            continue
                        if line is None:
                            message = self.snapshot()['message']
                            log.write('\n' + message + '\n')
                            log.flush()
                            self._terminate()
                            raise RuntimeError(message + ' Журнал: engine.log')
                        log.write(line)
                        log.flush()
                        if event and event.get('event') == 'complete' and event.get('id') == ident:
                            if not event.get('ok') or result is None:
                                detail = error_detail or last_error or event.get('message', '')
                                raise RuntimeError('Локальный конвертер завершился с ошибкой. ' + detail + ' Журнал: engine.log')
                            return result
                        if re.search(r'error:', line, re.I):
                            last_error = line.strip()[-800:]
                            if not re.fullmatch(r'ERROR:\s*\d+\s+of\s+\d+\s+document conversions failed\.?', last_error, re.I):
                                error_detail = last_error
                        if line.startswith(RESULT_MARKER):
                            result = json.loads(line[len(RESULT_MARKER):])
                        match = re.search(r'(?:сконвертировано страниц:\s*|OCR cache;\s*)(\d+) из (\d+)', line)
                        if match and on_progress:
                            n, total = int(match[1]), max(1, int(match[2]))
                            on_progress(min(85, 10 + int(75*n/total)), f'Распознано страниц: {n} из {total}')
                        elif 'Подготовка HTML:' in line and on_progress:
                            on_progress(88, 'Восстановление таблиц и формул')
            except (ConversionCancelled, TimeoutError, BrokenPipeError):
                self._terminate()
                raise
            finally:
                with self.lock:
                    self.active = None

    def _terminate(self):
        with self.lock:
            process = self.process
            birth = self.process_birth
            self.process = None
            self.process_birth = None
            self.process_source_revision = None
            owned = self.owned_children
            self.owned_children = {}
            if self.closed or self.state.get('phase') != 'error':
                self.state.update(phase='stopped', message='Движок остановлен')
            self.ready.set()
        if process is None:
            return
        # llama-server starts its own POSIX session. Walk only our descendants,
        # rather than killing arbitrary shared endpoints from Surya's registry.
        children = []
        try:
            import psutil
            try:
                parent = psutil.Process(process.pid)
                current = parent.children(recursive=True) if birth is not None and parent.create_time() == birth else []
            except psutil.NoSuchProcess:
                current = []
            for child in current:
                try:
                    owned[child.pid] = child.create_time()
                except psutil.Error:
                    pass
            for pid, created in owned.items():
                try:
                    child = psutil.Process(pid)
                    if child.create_time() == created:
                        children.append(child)
                except psutil.Error:
                    pass
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(children, timeout=2)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
        except (ImportError, OSError):
            pass
        except Exception:
            self.logger.exception('Could not enumerate owned OCR children')
        if __package__:
            from .converter import _stop_process
        else:
            from converter import _stop_process
        _stop_process(process)
        if self.reader is not None:
            self.reader.join(timeout=2)
        if self.watcher is not None:
            self.watcher.join(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def close(self):
        with self.lock:
            self.closed = True
            process = self.process
        # EOF is the normal shutdown signal; the host's atexit cleanup owns llama.
        if process is not None and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._terminate()
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
