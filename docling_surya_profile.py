"""Configure the pinned Surya 2.5 model and its local llama.cpp backend."""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from functools import lru_cache
from pathlib import Path


SURYA_INSTALL = 'python -m pip install -r requirements-docling-surya.txt'
SURYA_BACKEND = "llamacpp"  # Native CUDA / Metal / CPU; no Docker setup required.
SURYA25_VERSION = '0.24.0'
SURYA25_CHECKPOINT = 'datalab-to/surya-ocr-2.5'
_CONTEXT_PER_SLOT = 16384
_TWO_SLOT_MIN_VRAM = 5 * 1024 ** 3
_GGUF_REPOSITORY = 'datalab-to/surya-ocr-2.5-gguf'
_GGUF_REVISION = 'eb78671ae9af9bdddfac2e25507c55337b6098e2'
_ASSETS = (
    ('surya-2.5.gguf', 1180178976,
     '45fa3e587cf5a475f06e949fbe40e67b759dbcee1830e7effbcb172445d4b6bf'),
    ('surya-2.5-mmproj.gguf', 207345920,
     'f245dbf784cf73683a273a37fefae0d0d7983b083a5d2fecf9309476f861f659'),
)


@lru_cache(maxsize=len(_ASSETS))
def _asset_hash(path, size, modified_at):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def configure_surya25_model(root):
    bundled = (root / 'runtime').is_dir()
    directory = (root / 'models' / 'huggingface' / 'hub' /
                 ('models--' + _GGUF_REPOSITORY.replace('/', '--')) / 'snapshots' / _GGUF_REVISION)
    paths = []
    for name, size, checksum in _ASSETS:
        asset = directory / name
        if not bundled:
            from huggingface_hub import hf_hub_download
            asset = Path(hf_hub_download(_GGUF_REPOSITORY, filename=name, revision=_GGUF_REVISION))
        if not asset.is_file():
            raise FileNotFoundError(f'В комплекте REGION OCR отсутствует модель Surya 2.5: {asset}')
        stat = asset.stat()
        if stat.st_size != size:
            raise ValueError(f'Surya 2.5: неверный размер файла {asset}')
        if _asset_hash(asset, stat.st_size, stat.st_mtime_ns) != checksum:
            raise ValueError(f'Surya 2.5: неверный SHA-256 файла {asset}')
        paths.append(asset)

    model, projector = paths
    os.environ.update(
        SURYA_MODEL_CHECKPOINT=SURYA25_CHECKPOINT,
        SURYA_GGUF_LOCAL_MODEL_PATH=str(model),
        SURYA_GGUF_LOCAL_MMPROJ_PATH=str(projector),
        SURYA_INFERENCE_URL='',
        SURYA_INFERENCE_HOST='127.0.0.1',
        LLAMA_CPP_SPECIAL='true',
    )


def configure_surya25_parallel(device):
    """Keep the full-page context budget for each concurrent CUDA request."""
    slots = 1
    if device == 'cuda':
        import torch

        try:
            memory = torch.cuda.get_device_properties(torch.cuda.current_device()).total_memory
        except (AssertionError, RuntimeError):
            memory = 0
        if memory >= _TWO_SLOT_MIN_VRAM:
            slots = 2
    os.environ['SURYA_INFERENCE_PARALLEL'] = str(slots)
    os.environ.setdefault('SURYA_INFERENCE_CTX_PER_SLOT', str(_CONTEXT_PER_SLOT))
    return slots


def surya25_cache_path(config=None):
    if config is None:
        from surya.settings import settings as config

    # Surya only checks the model name when reusing a server. Identify the
    # actual binary, weights and launch options before sharing its registry.
    paths = tuple(os.path.normcase(os.path.abspath(path)) if path else None for path in (
        config.LLAMA_CPP_BINARY, config.SURYA_GGUF_LOCAL_MODEL_PATH,
        config.SURYA_GGUF_LOCAL_MMPROJ_PATH,
    ))
    options = (
        config.SURYA_MODEL_CHECKPOINT, config.SURYA_INFERENCE_HOST,
        config.SURYA_INFERENCE_PORT, config.SURYA_INFERENCE_PARALLEL,
        config.SURYA_INFERENCE_CTX_PER_SLOT, config.SURYA_INFERENCE_CTX_SIZE,
        config.LLAMA_CPP_NGL, config.LLAMA_CPP_NO_MMPROJ_OFFLOAD,
        config.LLAMA_CPP_SPECIAL, config.LLAMA_CPP_EXTRA_ARGS,
    )
    profile = hashlib.sha256(json.dumps((paths, options)).encode('utf-8')).hexdigest()[:16]
    return Path.home() / '.cache' / 'datalab' / 'surya-2.5' / profile


def configure_surya25_backend():
    from surya.inference.backends import llamacpp, spawn

    def cache_dir():
        path = surya25_cache_path()
        path.mkdir(parents=True, exist_ok=True)
        return path

    # Upstream keys both the file lock and its running server only by backend.
    spawn._cache_dir = llamacpp._cache_dir = cache_dir
    if os.name == 'nt':
        spawn._server_is_alive = windows_server_is_alive


def windows_server_is_alive(handle):
    # Windows has no WNOHANG, and os.kill(pid, 0) is unsafe there.
    import psutil

    return bool(handle.pid and psutil.pid_exists(handle.pid))


def resolve_device():
    """Automatically select the local accelerator usable by Docling."""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if (platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64")
            and torch.backends.mps.is_available()):
        return "mps"
    return "cpu"


def configure_local_surya(device, *, root=None, num_threads=None, install_missing=None):
    """Set Surya settings BEFORE plugin discovery imports surya.settings."""
    root = Path(__file__).resolve().parent if root is None else root
    num_threads = min(8, os.cpu_count() or 1) if num_threads is None else num_threads
    for package, expected in (('docling-surya-ocr', '0.2.0'), ('surya-ocr', SURYA25_VERSION)):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = None
        if installed != expected:
            raise ValueError(f"Requires {package}=={expected} (installed: {installed}). {SURYA_INSTALL}")
    configure_surya25_model(root)
    os.environ["TORCH_DEVICE"] = device
    os.environ["SURYA_INFERENCE_BACKEND"] = SURYA_BACKEND
    parallel = configure_surya25_parallel(device)
    os.environ["SURYA_INFERENCE_KEEP_ALIVE"] = "false"
    os.environ["SURYA_INFERENCE_STARTUP_TIMEOUT"] = "600"
    # httpx also reads the Windows system proxy. Loopback must reach the local
    # server directly, even when the computer uses a VPN/proxy for the internet.
    bypass = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    os.environ["NO_PROXY"] = ",".join(filter(None, (bypass, "localhost", "127.0.0.1", "::1")))
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    candidates = [root / ".runtime" / "llamacpp" / name]
    cached = runtime_directory(device)
    candidates.extend(cached.rglob(name) if cached.is_dir() else ())
    on_path = shutil.which(name)
    if on_path:
        candidates.append(Path(on_path))
    # CUDA release archives can also use CUDA 12 DLLs shipped with PyTorch.
    if os.name == "nt":
        import torch
        torch_lib = str(Path(torch.__file__).parent / "lib")
        os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
    def usable(candidate):
        try:
            # On Windows the device probe can exit successfully with both
            # pipes empty. Retry that inconclusive result before discovery.
            for flags in ([], ["--log-verbosity", "5"]):
                probe = subprocess.run([str(candidate), *flags, "--list-devices"], capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=30,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                devices = probe.stdout + probe.stderr
                if os.name != "nt" or probe.returncode or devices.strip() or device == "cpu":
                    break
        except (OSError, subprocess.SubprocessError):
            return False
        return probe.returncode == 0 and (
            device == "cpu"
            or device == "cuda" and bool(re.search(
                r"CUDA\d*:|ggml_cuda_init:\s+found [1-9]\d* CUDA devices?\b", devices, re.I))
            or device == "mps" and "metal" in devices.lower()
        )

    binary = next((str(path) for path in candidates if path.is_file() and usable(path)), None)
    if binary is None:
        if install_missing is None:
            raise RuntimeError('Локальный OCR runtime отсутствует. Установите комплект llama.cpp для конвертера.')
        binary = install_missing(device)
        if not usable(binary):
            raise ValueError("Установленная сборка llama.cpp не запускается на этой системе")
    os.environ["LLAMA_CPP_BINARY"] = str(binary)
    os.environ["LLAMA_CPP_NGL"] = "0" if device == "cpu" else "99"
    os.environ["LLAMA_CPP_NO_MMPROJ_OFFLOAD"] = "true" if device == "cpu" else "false"
    # Keep the vision encoder and KV cache within a laptop's memory budget.
    os.environ["LLAMA_CPP_EXTRA_ARGS"] = f"--threads {num_threads}"
    configure_surya25_backend()
    print(f"Surya 2.5 runtime: {binary}; device: {device}; slots: {parallel}", flush=True)
    if os.name == "nt":
        from surya.inference.backends import spawn

        def stop_windows_server(pid, name):
            # POSIX os.kill(pid, 0) polling is not a safe liveness check on
            # Windows. Surya's atexit owner still decides
            # which server may be stopped; only the stop primitive differs.
            # taskkill can hang while waiting for a process tree to exit. Do
            # not let that escape Surya's atexit callback; terminate the known
            # server PID directly as a fallback, then report any cleanup issue.
            try:
                result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                        capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=15,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode == 0:
                    return
                taskkill_error = f"exit code {result.returncode}: {result.stderr.strip()}"
            except Exception as exc:
                taskkill_error = exc

            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                open_process = kernel32.OpenProcess
                open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                open_process.restype = wintypes.HANDLE
                terminate_process = kernel32.TerminateProcess
                terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
                terminate_process.restype = wintypes.BOOL
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL

                handle = open_process(0x0001, False, int(pid))  # PROCESS_TERMINATE
                if not handle:  # taskkill may have finished just before timing out
                    return
                try:
                    if not terminate_process(handle, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    close_handle(handle)
            except Exception as exc:
                print(f"WARNING: Could not stop {name} (pid {pid}); taskkill failed ({taskkill_error}), "
                      f"and direct termination failed ({exc})", file=sys.stderr)

        spawn._stop_process = stop_windows_server

    from docling_backend_health import guard_managed_process_cleanup
    from surya.inference.backends import spawn
    guard_managed_process_cleanup(spawn)


def runtime_directory(device):
    from platformdirs import user_data_path
    return user_data_path("docling-surya", appauthor=False) / f"llamacpp-{device}"
