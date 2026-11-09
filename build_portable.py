"""Assemble the installed Windows runtime and the converter's pinned models.

This copies existing components; it never installs packages or copies reports,
credentials, uploaded documents, conversion caches, or unrelated HF models.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parent
MODEL_REPOSITORIES = (
    'models--datalab-to--surya-ocr-2-gguf',
    'models--docling-project--docling-layout-heron',
    'models--docling-project--docling-models',
)
OMIT_DIRECTORIES = {'__pycache__', '.git', '.locks', 'test', 'tests',
                    'benchmarks', 'docs', 'Doc', 'include', 'libs', 'Scripts',
                    'idlelib', 'turtledemo', 'ensurepip'}


def include(relative):
    if re.search(r'(?i)license|copying|copyright|notice|eula', relative.name):
        return True
    if any(part in OMIT_DIRECTORIES for part in relative.parts):
        return False
    name = relative.name.lower()
    return (not name.endswith(('.pyc', '.pyo', '.lib', '.exp', '.pdb', '.incomplete', '.log'))
            and name not in {'direct_url.json', 'token', 'stored_tokens'})


def copy_file(source, destination):
    if destination.is_file() and source.stat().st_size == destination.stat().st_size:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_directory(source, destination):
    if not source.is_dir():
        raise FileNotFoundError(source)
    for base, directories, files in os.walk(source):
        directories[:] = [name for name in directories if name not in OMIT_DIRECTORIES]
        for name in files:
            path = Path(base) / name
            relative = path.relative_to(source)
            if include(relative):
                copy_file(path, destination / relative)


def dependency_closure():
    from packaging.requirements import Requirement
    pending = []
    for file in ROOT.glob('requirements-*.txt'):
        if file.name == 'requirements-portable.txt':
            continue
        for line in file.read_text('utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate():
                pending.append((requirement.name, set(requirement.extras)))
    found, missing, seen_extras = {}, set(), {}
    while pending:
        name, extras = pending.pop()
        key = re.sub(r'[-_.]+', '-', name).lower()
        extras = set(extras) | {''}
        if (key in found and extras <= seen_extras[key]) or name in missing:
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.add(name)
            continue
        found[key] = distribution
        seen_extras.setdefault(key, set()).update(extras)
        for value in distribution.requires or ():
            requirement = Requirement(value)
            if requirement.marker is None or any(requirement.marker.evaluate({'extra': extra})
                                                 for extra in seen_extras[key]):
                pending.append((requirement.name, set(requirement.extras)))
    return found, sorted(missing)


def unpack_desktop(wheels):
    destination = ROOT / 'local_web' / 'desktop_runtime'
    needed = ['bottle', 'cffi', 'clr_loader', 'pythonnet', 'pywebview']
    for name in needed:
        matches = list(wheels.glob(name + '-*.whl'))
        if len(matches) != 1:
            raise RuntimeError('Expected one audited wheel for ' + name)
        with zipfile.ZipFile(matches[0]) as archive:
            for entry in archive.infolist():
                relative = Path(entry.filename)
                if entry.is_dir() or not include(relative):
                    continue
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Unsafe wheel path')
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
    matches = list(wheels.glob('proxy_tools-*.tar.gz'))
    if len(matches) != 1:
        raise RuntimeError('Expected the audited proxy_tools sdist')
    with tarfile.open(matches[0]) as archive:
        for entry in archive.getmembers():
            if not entry.isfile():
                continue
            parts = Path(entry.name).parts[1:]
            relative = Path(*parts)
            if not parts or relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unsafe sdist path')
            if not (parts[0] == 'proxy_tools' or parts[0].endswith('.egg-info')
                    or parts[0].upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))):
                continue
            if not include(relative):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-home', type=Path, default=Path(sys.executable).parent)
    parser.add_argument('--hf-cache', type=Path, default=Path.home() / '.cache' / 'huggingface' / 'hub')
    parser.add_argument('--llamacpp', type=Path, required=True)
    parser.add_argument('--orientation', type=Path, required=True)
    parser.add_argument('--desktop-wheels', type=Path, required=True)
    args = parser.parse_args()
    runtime = ROOT / 'runtime'
    if args.python_home.resolve() == runtime.resolve():
        raise ValueError('Run the builder from the original installed Python')
    runtime.mkdir(exist_ok=True)
    for name in ['python.exe', 'pythonw.exe', 'python3.dll', 'python312.dll',
                 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt']:
        copy_file(args.python_home / name, runtime / name)
    for name in ['DLLs', 'tcl']:
        copy_directory(args.python_home / name, runtime / name)
    for entry in (args.python_home / 'Lib').iterdir():
        if entry.name == 'site-packages' or entry.name in OMIT_DIRECTORIES:
            continue
        if entry.is_dir():
            copy_directory(entry, runtime / 'Lib' / entry.name)
        elif include(Path(entry.name)):
            copy_file(entry, runtime / 'Lib' / entry.name)
    distributions, missing = dependency_closure()
    site = (args.python_home / 'Lib' / 'site-packages').resolve()
    packages = []
    for distribution in distributions.values():
        packages.append({'name': distribution.metadata['Name'], 'version': distribution.version})
        for entry in distribution.files or ():
            source = Path(distribution.locate_file(entry)).resolve()
            if not source.is_relative_to(site) or not source.is_file():
                continue
            relative = source.relative_to(site)
            if include(relative):
                copy_file(source, runtime / 'Lib' / 'site-packages' / relative)
    print('Python and packages copied', len(packages), flush=True)
    unpack_desktop(args.desktop_wheels)
    for repository in MODEL_REPOSITORIES:
        copy_directory(args.hf_cache / repository, ROOT / 'models' / 'huggingface' / 'hub' / repository)
        print('Model copied', repository, flush=True)
    copy_file(args.orientation, ROOT / 'models' / 'orientation' / args.orientation.name)
    for path in args.llamacpp.iterdir():
        if path.is_file() and (path.name == 'llama-server.exe' or path.suffix.lower() == '.dll'
                               or path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))):
            copy_file(path, ROOT / '.runtime' / 'llamacpp' / path.name)
    manifest = {'python': sys.version.split()[0], 'platform': 'windows-x64',
                'packages': sorted(packages, key=lambda p: p['name'].lower()),
                'separately_bundled': ['pywebview', 'pythonnet', 'clr_loader', 'proxy_tools', 'bottle', 'cffi'],
                'optional_uninstalled_packages': missing, 'files': []}
    for folder in ['runtime', 'models', '.runtime', 'local_web/desktop_runtime']:
        for path in (ROOT / folder).rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                manifest['files'].append({'path': path.relative_to(ROOT).as_posix(),
                                          'bytes': path.stat().st_size, 'sha256': digest})
    (ROOT / 'portable-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'files': len(manifest['files']),
                      'bytes': sum(p['bytes'] for p in manifest['files'])}), flush=True)


if __name__ == '__main__':
    main()
