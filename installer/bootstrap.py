"""Pinned online installation into the application directory, using private Python."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile


def safe_path(root, name):
    root = Path(root).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError('Unsafe installation path: ' + name)
    return target


def extract_zip(archive, root):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            target = safe_path(root, entry.filename)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)


class Installer:
    def __init__(self, root, plan, profile):
        self.root, self.plan, self.profile = Path(root).resolve(), plan, profile
        self.cache = self.root / '.setup-downloads'
        self.cache.mkdir(exist_ok=True)
        self.python = self.root / 'runtime/python.exe'
        self.vendor = self.root / 'local_web/desktop_runtime'
        self.log = self.root / 'installation.log'
        self.completed = 0
        self.total = sum(p['bytes'] for p in plan['packages'] + plan['desktop_packages']
                         + plan['torch'][profile] + plan['models'])
        self.total += plan['pip']['bytes'] + plan['tcltk']['bytes'] + plan['native'][profile]['bytes']

    def report(self, message, current=0):
        progress = min(95, 8 + int(87 * (self.completed + current) / max(1, self.total)))
        print(json.dumps({'progress': progress, 'message': message}, ensure_ascii=True), flush=True)

    def download(self, item):
        name = item.get('filename') or Path(item['path']).name
        target = safe_path(self.cache, name)
        for attempt in range(4):
            try:
                digest = hashlib.sha256()
                count = 0
                tick = 0.0
                req = urllib.request.Request(item['url'], headers={'User-Agent': 'REGION-OCR-Setup/1.8.1'})
                with urllib.request.urlopen(req, timeout=90) as response, target.open('wb') as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        digest.update(block)
                        count += len(block)
                        if time.monotonic() - tick > .4:
                            self.report(f'Загрузка {name}: {count // 1048576} / {item["bytes"] // 1048576} МБ', count)
                            tick = time.monotonic()
                if digest.hexdigest() != item['sha256'] or count != item['bytes']:
                    raise ValueError('SHA256/size mismatch: ' + name)
                self.completed += count
                return target
            except Exception:
                target.unlink(missing_ok=True)
                if attempt == 3:
                    raise
                self.report(f'Повторная попытка загрузить {name} ({attempt + 2}/4)')
                time.sleep(2 ** attempt)

    def pip(self, archive, *, target=None):
        args = [str(self.python), '-s', '-m', 'pip', '--isolated', 'install',
                '--no-deps', '--no-index', '--no-cache-dir', '--no-compile',
                '--disable-pip-version-check', '--no-warn-script-location']
        if target is not None:
            args += ['--target', str(target), '--upgrade']
        args.append(str(archive))
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env['PYTHONUTF8'] = '1'
        with self.log.open('a', encoding='utf-8') as log:
            result = subprocess.run(args, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, env=env)
        if result.returncode:
            raise RuntimeError('Не удалось установить ' + archive.name + '. Подробности в installation.log')

    def install_proxy_tools(self, archive):
        # The published sdist contains a single pure-Python module and license.
        # Extract explicitly; do not execute setup.py or install a build toolchain.
        with tarfile.open(archive, 'r:gz') as source:
            found = False
            for entry in source.getmembers():
                parts = Path(entry.name).parts[1:]
                if not entry.isfile() or not parts:
                    continue
                rel = Path(*parts)
                if rel.as_posix() == 'proxy_tools.py' or rel.parts[0] == 'proxy_tools':
                    target = safe_path(self.vendor, rel.as_posix())
                    found = True
                elif rel.parts[0].endswith('.egg-info') or rel.name.lower().startswith(('license', 'copying')):
                    target = safe_path(self.vendor, rel.as_posix())
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(entry) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            if not found:
                raise RuntimeError('Unexpected proxy-tools distribution layout')

    def run(self):
        try:
            site = self.root / 'runtime/Lib/site-packages'
            site.mkdir(parents=True, exist_ok=True)
            pip = self.download(self.plan['pip'])
            extract_zip(pip, site)
            pip.unlink()
            self.vendor.mkdir(parents=True, exist_ok=True)
            for item in self.plan['packages'] + self.plan['torch'][self.profile]:
                archive = self.download(item)
                self.report('Установка ' + item['name'] + ' ' + item['version'])
                self.pip(archive)
                archive.unlink()
            for item in self.plan['desktop_packages']:
                archive = self.download(item)
                self.report('Установка интерфейса: ' + item['name'])
                if item['name'] == 'proxy-tools':
                    self.install_proxy_tools(archive)
                else:
                    self.pip(archive, target=self.vendor)
                archive.unlink()
            tk = self.download(self.plan['tcltk'])
            extract_zip(tk, self.root)
            tk.unlink()
            native = self.download(self.plan['native'][self.profile])
            extract_zip(native, self.root / '.runtime/llamacpp')
            native.unlink()
            if not (self.root / '.runtime/llamacpp/llama-server.exe').is_file():
                raise RuntimeError('В загруженной сборке отсутствует llama-server.exe')
            for item in self.plan['models']:
                source = self.download(item)
                target = safe_path(self.root, item['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
            for item in self.plan['cache_files']:
                target = safe_path(self.root, item['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item['text'], encoding='utf-8')
            shutil.copy2(self.root / 'THIRD-PARTY-NOTICES/llama-MIT-LICENSE.txt',
                         self.root / '.runtime/llamacpp/LICENSE')
            self.report('Проверка Python, интерфейса и моделей')
            check = (
                "import sys; from pathlib import Path; import portable_runtime; portable_runtime.configure_runtime(); "
                "import torch,docling,numpy,PIL,pypdfium2,onnxruntime; "
                "from local_web.desktop import prepare_runtime; prepare_runtime(); import webview; "
                "import tkinter; t=tkinter.Tcl(); assert t.eval('info patchlevel'); "
                "from huggingface_hub import snapshot_download; "
                "assert Path(snapshot_download('docling-project/docling-layout-heron',local_files_only=True)).is_dir(); "
                "assert Path(snapshot_download('docling-project/docling-models',revision='v2.3.0',local_files_only=True)).is_dir(); "
                "print('INSTALLATION CHECK OK',sys.version,torch.__version__)"
            )
            with self.log.open('a', encoding='utf-8') as log:
                result = subprocess.run([str(self.python), '-s', '-c', check], cwd=self.root,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=180)
            if result.returncode:
                raise RuntimeError('Проверка окружения не пройдена. Подробности в installation.log')
            self.report('Компоненты установлены и проверены')
        finally:
            shutil.rmtree(self.cache, ignore_errors=True)


def main():
    root = Path(sys.argv[1]).resolve()
    plan = json.loads((root / 'installer/install-plan.json').read_text('utf-8'))
    Installer(root, plan, sys.argv[2]).run()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=True), flush=True)
        raise
