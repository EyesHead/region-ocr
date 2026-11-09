"""Recover a proven-dead managed llama.cpp endpoint after a request error.

No models, network clients or processes are constructed during import. A
normal OCR result or a formula-reader disagreement must not trigger recovery.
The caller may repeat its original request once after a True return, resolving
``backend._client`` and ``backend.handle`` again rather than reusing old ones.
"""
import json
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit


_RECOVERY_LOCK = RLock()


def _read_sentinel(name):
    # Unlike Surya's private reader, this read does not mkdir outside the
    # workspace merely to discover that no managed endpoint is registered.
    path = Path.home() / '.cache' / 'datalab' / 'surya' / f'{name}_server.json'
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
        expected_model = getattr(config, 'SURYA_MODEL_CHECKPOINT', model_name)
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
        # Official stop resets the cached client/handle; it does not kill the
        # process. Official start re-probes and takes Surya's own file lock.
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
