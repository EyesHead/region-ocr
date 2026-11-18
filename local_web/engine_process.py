"""Persistent engine child. No HTTP endpoint; requests arrive over private stdin."""
import gc
import json
import os
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if __package__:
    from .engine_host import MARKER
else:
    from engine_host import MARKER


def emit(event, **fields):
    print(MARKER + json.dumps(dict(event=event, **fields), ensure_ascii=True), flush=True)


def borrow_manager_class(owner):
    """Each document borrows the exact warm backend, never owns its lifetime."""
    class SessionManager:
        def __init__(self, method=None, lazy=True):
            if method not in (None, 'llamacpp'):
                raise ValueError('The application session owns only its configured llama.cpp backend')
            self.method = owner.method
            self.backend = owner.backend
            if not lazy:
                owner.start()

        def start(self):
            return owner.start()

        def stop(self):
            pass  # Only process/session shutdown releases the loaded model.

        def capacity(self):
            return owner.capacity()

        def generate(self, batch):
            return owner.generate(batch)
    return SessionManager


def warm_runtime():
    import convert_docling as core
    core.offer_llama_install = lambda device: (_ for _ in ()).throw(
        RuntimeError('Локальный OCR runtime отсутствует. Настройте его по README_convert_docling.md.'))
    core.configure_surya(SimpleNamespace(device=core.resolve_device()))
    import surya.inference as inference
    owner = inference.SuryaInferenceManager(method='llamacpp', lazy=False)
    inference.SuryaInferenceManager = borrow_manager_class(owner)
    return owner


def backend_identity(manager):
    from surya.inference.backends import spawn
    from urllib.parse import urlsplit
    registry = spawn._read_sentinel('llamacpp') or {}
    handle = manager.backend.handle
    pid = registry.get('pid') if (handle is not None and registry.get('port') == urlsplit(handle.base_url).port
                                and registry.get('model') == handle.model_name) else None
    return dict(pid=os.getpid(), backend_pid=pid, backend_identity=id(manager.backend))


def ensure_backend_running(manager, previous_identity):
    """Reattach/restart through Surya only after the known backend PID died."""
    import psutil
    pid = previous_identity.get('backend_pid')
    if isinstance(pid, int) and pid > 0 and not psutil.pid_exists(pid):
        manager.backend.stop()
        manager.start()
        return backend_identity(manager)
    return previous_identity


def main():
    started = time.monotonic()
    try:
        manager = warm_runtime()
    except Exception as error:
        traceback.print_exc()
        emit('startup_error', message=f'Не удалось подготовить локальный OCR: {error}')
        return 1
    identity = backend_identity(manager)
    emit('ready', message='Локальный OCR готов', **identity,
         startup_seconds=round(time.monotonic()-started, 3))
    from converter import _worker, _validate_pages, describe_conversion_failure
    try:
        for line in sys.stdin:
            request = json.loads(line)
            ident = request.get('id')
            if request.get('operation') != 'convert':
                emit('complete', id=ident, ok=False, message='Unknown engine operation')
                continue
            before = time.monotonic()
            identity = ensure_backend_running(manager, identity)
            emit('job_started', id=ident, **backend_identity(manager))
            original_argv = sys.argv[:]
            original_environment = {name: os.environ.get(name) for name in ('PATH', 'NO_PROXY', 'no_proxy')}
            attempt_started = time.time()
            try:
                _worker(Path(request['source']).resolve(), Path(request['destination']).resolve(),
                        _validate_pages(request.get('pages', '')))
            except (Exception, SystemExit) as error:
                traceback.print_exc()
                emit('complete', id=ident, ok=False, message=str(error),
                     failure=describe_conversion_failure(error, Path(request['destination']), since=attempt_started))
            else:
                identity = backend_identity(manager)
                emit('complete', id=ident, ok=True, seconds=round(time.monotonic()-before, 3),
                     **identity)
            finally:
                sys.argv = original_argv
                # configure_surya was designed for a short-lived CLI process;
                # its PATH/proxy additions must not accumulate across jobs.
                for name, value in original_environment.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                gc.collect()
    finally:
        manager.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
