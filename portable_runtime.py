"""Resolve an optional bundled Python/model installation relative to the app."""
from pathlib import Path
import os
import sys


def configure_runtime():
    root = Path(__file__).resolve().parent
    runtime = root / 'runtime'
    models = root / 'models'
    if not runtime.is_dir():
        return False
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.environ.pop('PYTHONPATH', None)
    os.environ['DOCUMENT_CONVERTER_PYTHON'] = str(runtime / (
        'python.exe' if os.name == 'nt' else 'bin/python3'))
    os.environ['HF_HOME'] = str(models / 'huggingface')
    os.environ['HF_HUB_CACHE'] = str(models / 'huggingface' / 'hub')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    return True


if __name__ == '__main__':
    configure_runtime()
    import runpy
    target = Path(__file__).resolve().parent / 'local_web' / 'server.py'
    sys.path.insert(0, str(target.parent))
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name='__main__')
