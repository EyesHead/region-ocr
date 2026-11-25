"""Recover a proven-dead managed llama.cpp endpoint after a request error.

No models, network clients or processes are constructed during import. A
normal OCR result or a formula-reader disagreement must not trigger recovery.
The caller may repeat its original request once after a True return, resolving
``backend._client`` and ``backend.handle`` again rather than reusing old ones.
"""
import json
import math
from dataclasses import replace
from itertools import groupby
import os
from threading import RLock
from urllib.parse import urlsplit

from docling_surya_profile import surya25_cache_path


_RECOVERY_LOCK = RLock()


class ManagedProcessCleanup:
    """Constrain Surya's PID-only atexit callbacks to proven session children."""
    def __init__(self, spawn):
        self.spawn = spawn
        self.births = {}
        self.original_write = spawn._write_sentinel
        self.original_delete = spawn._delete_sentinel
        self.stop_callback = self.stop
        self.refresh_stop()
        spawn._write_sentinel = self.write
        spawn._delete_sentinel = self.delete

    def refresh_stop(self):
        # configure_surya replaces the Windows primitive on every CLI run.
        if self.spawn._stop_process is not self.stop_callback:
            self.primitive = self.spawn._stop_process
        self.spawn._stop_process = self.stop_callback

    def write(self, backend, data):
        import psutil
        pid = data.get('pid')
        if data.get('cleanup_kind') == 'process' and type(pid) is int:
            try:
                process = psutil.Process(pid)
                if any(parent.pid == os.getpid() for parent in process.parents()):
                    self.births[pid] = process.create_time()
            except psutil.Error:
                pass
        self.original_write(backend, data)

    def stop(self, pid, name):
        import psutil
        birth = self.births.get(pid)
        if birth is None:
            return
        try:
            if psutil.Process(pid).create_time() == birth:
                self.primitive(pid, name)
        except psutil.NoSuchProcess:
            pass

    def delete(self, backend):
        import psutil
        registry = self.spawn._read_sentinel(backend) or {}
        pid = registry.get('pid')
        if pid not in self.births:
            return
        try:
            if psutil.Process(pid).create_time() != self.births[pid]:
                return
        except psutil.NoSuchProcess:
            pass
        self.original_delete(backend)


def guard_managed_process_cleanup(spawn):
    cleanup = getattr(spawn, '_region_ocr_managed_cleanup', None)
    if cleanup is None:
        cleanup = spawn._region_ocr_managed_cleanup = ManagedProcessCleanup(spawn)
    else:
        cleanup.refresh_stop()
    return cleanup


def _read_sentinel(name):
    # Unlike Surya's private reader, this read does not mkdir outside the
    # workspace merely to discover that no managed endpoint is registered.
    path = surya25_cache_path() / f'{name}_server.json'
    return json.loads(path.read_text('utf-8'))


def recover_dead_local_backend(backend, diagnostic, *, failed_request=False,
                               config=None, read_sentinel=None, pid_exists=None,
                               probe_health=None):
    """Reset only a matching local registry handle with a confirmed dead PID.

    Injected probes/readers make the lifecycle decision testable without GPU,
    network or operating-system process operations. An unavailable probe is
    unknown evidence, never proof of death. Only the official Backend.start
    owns the Surya registry file lock and actual attach/spawn operation.
    """
    if not failed_request:
        return False
    diagnostic = diagnostic or (lambda *args, **kwargs: None)

    def skip(reason, **details):
        diagnostic('LOCAL_BACKEND_RECOVERY_SKIPPED', reason=reason, **details)
        return False

    with _RECOVERY_LOCK:
        if config is None:
            from surya.settings import settings
            config = settings
        if getattr(config, 'SURYA_INFERENCE_URL', None):
            return skip('pinned_external_endpoint')
        name = getattr(backend, 'name', None)
        # The installed vllm backend owns a Docker handle without a process
        # PID. It cannot satisfy this positive process-death contract.
        if name != 'llamacpp':
            return skip('backend_has_no_managed_process_contract')
        handle = getattr(backend, 'handle', None)
        if handle is None or getattr(backend, '_client', None) is None:
            return skip('missing_backend_handle_or_client')
        model_name = getattr(handle, 'model_name', None)
        if not isinstance(model_name, str) or not model_name:
            return skip('handle_model_identity_missing')
        try:
            endpoint = urlsplit(str(handle.base_url).rstrip('/'))
            configured_host = str(getattr(config, 'SURYA_INFERENCE_HOST', '')).strip('[]').casefold()
            if (endpoint.scheme != 'http' or endpoint.path != '/v1'
                    or endpoint.query or endpoint.fragment or endpoint.username or endpoint.password
                    or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}
                    or endpoint.hostname.casefold() != configured_host or endpoint.port is None):
                return skip('not_the_configured_managed_loopback_endpoint')
            expected_model = getattr(config, 'SURYA_MODEL_CHECKPOINT', model_name)
            registry = (read_sentinel or _read_sentinel)(name)
        except (OSError, ValueError, TypeError, AttributeError) as error:
            return skip('registry_or_endpoint_unavailable', error_type=type(error).__name__)
        if not isinstance(registry, dict):
            return skip('registry_metadata_missing')
        pid, port = registry.get('pid'), registry.get('port')
        if (type(pid) is not int or pid <= 0 or type(port) is not int or port <= 0
                or registry.get('backend') != name or registry.get('cleanup_kind') != 'process'
                or registry.get('cleanup_id') != str(pid)):
            return skip('registry_process_identity_invalid')
        if (port != endpoint.port or registry.get('model') != model_name
                or registry.get('model') != expected_model):
            return skip('registry_endpoint_or_model_mismatch')
        try:
            if pid_exists is None:
                import psutil
                pid_exists = psutil.pid_exists
            alive = pid_exists(pid)
        except Exception as error:
            return skip('process_liveness_unknown', error_type=type(error).__name__)
        if alive is True:
            return skip('registered_process_is_alive', pid=pid)
        if alive is not False:
            return skip('process_liveness_unknown', pid=pid)
        health_base = f'{endpoint.scheme}://{endpoint.netloc}'
        try:
            if probe_health is None:
                from surya.inference.backends.spawn import probe_health
            healthy = probe_health(health_base)
        except Exception as error:
            return skip('endpoint_health_unknown', error_type=type(error).__name__)
        if healthy is not False:
            return skip('endpoint_responding_or_health_unknown', pid=pid)
        diagnostic('LOCAL_BACKEND_PROCESS_DEAD', backend=name, pid=pid,
                   base_url=handle.base_url, model=handle.model_name)
        # The recorded process is gone. Surya 0.24 also tries owned cleanup
        # in stop(); skip that stale callback before taking the spawn lock.
        cleanup = getattr(handle, 'cleanup_fn', None)
        if cleanup is not None:
            from atexit import unregister
            unregister(cleanup)
            handle.cleanup_fn = None
        try:
            backend.stop()
            backend.start()
            if getattr(backend, 'handle', None) is None or getattr(backend, '_client', None) is None:
                raise RuntimeError('Backend restart did not provide a handle and client')
        except Exception as error:
            diagnostic('LOCAL_BACKEND_RECOVERY_FAILED', backend=name, old_pid=pid,
                       error_type=type(error).__name__, error=str(error))
            raise
        diagnostic('LOCAL_BACKEND_RECOVERED', backend=name, old_pid=pid,
                   base_url=backend.handle.base_url, model=backend.handle.model_name,
                   method='confirmed_dead_process_and_unhealthy_endpoint_official_attach_or_spawn')
        return True


def request_with_backend_recovery(backend, request_closure, diagnostic, **recovery_options):
    """Repeat the same transport request once only after confirmed recovery.

    Resolve the client's/model's fields inside ``request_closure`` so that its
    second invocation uses the fresh handle. Formula/content verification
    belongs outside this transport seam. A second exception is not swallowed.
    Extra options inject the pure lifecycle probes in unit tests.
    """
    try:
        outputs = request_closure()
    except Exception:
        if recover_dead_local_backend(backend, diagnostic, failed_request=True, **recovery_options):
            return request_closure()
        raise
    if any(getattr(output, 'error', False) for output in (outputs or ())):
        if recover_dead_local_backend(backend, diagnostic, failed_request=True, **recovery_options):
            return request_closure()
    return outputs


def request_local_ocr(backend, items, timeout, diagnostic, *, max_workers=1, retry_dead_backend=True):
    from surya.inference.backends.openai_client import chat_completions_batch

    workers = max_workers
    if workers > 1:
        workers = max(1, min(workers, len(items), backend.capacity()))
    # With --special, llama.cpp rejects constrained JSON decoding.
    def dispatch():
        return chat_completions_batch(
            items, client=backend._client.with_options(max_retries=0, timeout=timeout),
            model_name=backend.handle.model_name, timeout=timeout, max_workers=workers,
            max_retries=0, request_logprobs_default=False, allow_guided_json=False,
            allow_stop_token_ids=False)

    if retry_dead_backend:
        return request_with_backend_recovery(backend, dispatch, diagnostic)
    return dispatch()


LOCAL_REQUEST_SECONDS = 90.0


def local_request_seconds(items, workers=1):
    """Allow the declared output to finish before Surya can validate/fallback."""
    tokens=max((item.max_tokens or 2048 for item in items),default=2048)
    return max(LOCAL_REQUEST_SECONDS, math.ceil(tokens * workers / 80) + 15)


def generate_local_ocr_requests(batch, request, diagnostic):
    """Keep Surya's full-page -> layout/block recovery instead of repeating loops."""
    from docling_pdf_ocr import recovering_generate
    from surya.inference.prompts import LAYOUT_JSON_SCHEMA
    def route(item):
        if item.metadata.get('purpose') == 'balanced_formula':
            return 'formula'
        if item.prompt_type == 'high_accuracy_bbox':
            return 'positioned'
        if item.metadata.get('purpose') in {'layout_graphic_caption', 'vector_source_counterread'}:
            return 'caller_fallback'
        return 'recovery'

    outputs = []
    for kind, group in groupby(batch, route):
        items = list(group)
        if kind == 'recovery':
            items = [replace(item, guided_json=LAYOUT_JSON_SCHEMA)
                     if item.prompt_type == 'layout' and item.guided_json is None else item for item in items]
            outputs.extend(recovering_generate(items, request, diagnostic))
            continue
        result = request(items)
        if kind == 'formula':
            # Preserve every available answer and the original crop ordering.
            partial = list(result or ())[:len(items)]
            outputs.extend(partial + [None] * (len(items) - len(partial)))
            continue
        if len(result) != len(items):
            raise RuntimeError('Local inference result count mismatch')
        if kind == 'positioned':
            # RecognitionPredictor handles layout fallback for rejected pages.
            for item, output in zip(items, result, strict=True):
                if output.error:
                    diagnostic('OCR_FULL_PAGE_LAYOUT_FALLBACK', reason='positioned_request_failed',
                               metadata=item.metadata, image_size=list(item.image.size))
        outputs.extend(result)
    return outputs


def bound_surya_backend(backend, diagnostic):
    if not getattr(backend, "_convert_docling_bounded", False):
        def generate(batch):
            if backend.handle is None or backend._client is None:
                backend.start()
            def request(items):
                direct_formulas = bool(items) and items[0].metadata.get('purpose') == 'balanced_formula'
                workers = max(1, min(len(items), backend.capacity()))
                timeout = local_request_seconds(items, workers)
                if timeout>LOCAL_REQUEST_SECONDS:
                    diagnostic('OCR_REQUEST_TOKEN_TIME_ALLOWANCE',seconds=timeout,
                        max_tokens=max(item.max_tokens or 2048 for item in items), workers=workers)
                outputs = request_local_ocr(backend, items, timeout, diagnostic, max_workers=workers,
                                            retry_dead_backend=not direct_formulas)
                if direct_formulas:
                    return outputs
                from surya.recognition import _detect_repeat_loop
                for item, output in zip(items, outputs):
                    at_limit = (getattr(output, 'finish_reason', None) == 'length'
                                or item.max_tokens and output.token_count >= item.max_tokens)
                    reason = ('repeating_output' if _detect_repeat_loop(output.raw)
                              else 'output_token_limit' if at_limit else None)
                    if reason:
                        output.error = True
                        diagnostic('OCR_RESPONSE_REQUIRES_RECOVERY', reason=reason,
                            prompt_type=item.prompt_type, tokens=output.token_count,
                            max_tokens=item.max_tokens, metadata=item.metadata)
                    elif item.prompt_type == 'block' and not output.error and output.raw:
                        # Some PDF crop readers consume raw block HTML without a predictor.
                        from surya.inference.parsers import clean_block_html
                        output.raw = clean_block_html(output.raw)
                return outputs
            return generate_local_ocr_requests(batch, request, diagnostic)
        backend.generate = generate
        backend._convert_docling_bounded = True
    return backend
