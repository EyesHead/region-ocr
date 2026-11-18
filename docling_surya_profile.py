"""Configure the pinned Surya 2.5 model and its local llama.cpp backend."""

import hashlib
import os
from functools import lru_cache
from pathlib import Path


SURYA25_VERSION = '0.24.0'
SURYA25_CHECKPOINT = 'datalab-to/surya-ocr-2.5'
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


def surya25_cache_path():
    return Path.home() / '.cache' / 'datalab' / 'surya-2.5'


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
