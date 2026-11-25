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


def prepare_runtime():
    import convert_docling as core
    core.offer_llama_install = lambda device: (_ for _ in ()).throw(
        RuntimeError('Локальный OCR runtime отсутствует. Настройте его по README_convert_docling.md.'))
    core.configure_surya(SimpleNamespace(device=core.resolve_device()))
    import surya.inference as inference
    owner = inference.SuryaInferenceManager(method='llamacpp', lazy=True)
    inference.SuryaInferenceManager = borrow_manager_class(owner)
    return owner


def backend_identity(manager):
    if manager is None:
        return dict(pid=os.getpid(), backend_pid=None, backend_identity=None)
    from surya.inference.backends import spawn
    from urllib.parse import urlsplit
    handle = manager.backend.handle
    registry = (spawn._read_sentinel('llamacpp') or {}) if handle is not None else {}
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
    manager = None

    def runtime():
        nonlocal manager
        if manager is None:
            manager = prepare_runtime()
        else:
            ensure_backend_running(manager, identity)
        return manager

    if __package__:
        from .converter import _fast_worker, _worker, _validate_pages, describe_conversion_failure
    else:
        from converter import _fast_worker, _worker, _validate_pages, describe_conversion_failure
    identity = backend_identity(manager)
    emit('ready', message='Локальный движок готов', **identity)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            ident = request.get('id')
            if request.get('operation') != 'convert':
                emit('complete', id=ident, ok=False, message='Unknown engine operation')
                continue
            before = time.monotonic()
            emit('job_started', id=ident, **identity)
            original_argv = sys.argv[:]
            original_environment = {name: os.environ.get(name) for name in ('PATH', 'NO_PROXY', 'no_proxy')}
            attempt_started = time.time()
            try:
                source = Path(request['source']).resolve()
                pages = _validate_pages(request.get('pages', ''))
                if request.get('pdf_mode') == 'balanced':
                    _fast_worker(source, pages, pdf_mode='balanced', manager_provider=runtime)
                else:
                    runtime()
                    _worker(source, Path(request['destination']).resolve(), pages)
            except (Exception, SystemExit) as error:
                traceback.print_exc()
                emit('complete', id=ident, ok=False, message=str(error),
                     failure=describe_conversion_failure(error, Path(request['destination']), since=attempt_started))
            else:
                identity = backend_identity(manager)
                emit('complete', id=ident, ok=True, seconds=round(time.monotonic()-before, 3),
                     **identity)
            finally:
                identity = backend_identity(manager)
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
        if manager is not None:
            manager.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
