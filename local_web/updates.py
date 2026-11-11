"""Check official releases and hand a verified installer the running installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = 'EyesHead/region-ocr'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_INSTALLER = 64 * 1024 * 1024


def version_number(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(value))
    if not match:
        raise ValueError('Некорректная версия обновления.')
    return tuple(map(int, match.groups()))


def asset_url(value, version, name):
    expected = f'https://github.com/{REPOSITORY}/releases/download/v{version}/{name}'
    if value != expected:
        raise ValueError('Обновление ссылается на посторонний файл.')
    return value


class ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        parts = urlsplit(newurl)
        if (parts.scheme != 'https' or parts.hostname not in
                {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise ValueError('Небезопасное перенаправление при загрузке обновления.')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_bytes(url, limit):
    request = Request(url, headers={'User-Agent': 'REGION-OCR-Updater', 'Accept': 'application/vnd.github+json' if url == API_URL else 'application/octet-stream'})
    with build_opener(ReleaseRedirects()).open(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Файл обновления превышает допустимый размер.')
    return data


def release_info(data, current_version):
    if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
        raise ValueError('Релиз не предназначен для установки.')
    version = data.get('tag_name', '')
    number = version_number(version)
    version = '.'.join(map(str, number))
    name = f'REGION-OCR-{version}-Setup.exe'
    assets = {item.get('name'): item for item in data.get('assets', []) if isinstance(item, dict)}
    installer, checksums = assets.get(name), assets.get('SHA256SUMS')
    if not installer or not checksums:
        raise ValueError('В релизе нет установщика или контрольной суммы.')
    size = installer.get('size', 0)
    if not isinstance(size, int) or not 0 < size <= MAX_INSTALLER:
        raise ValueError('Некорректный размер установщика.')
    return {'version': version, 'name': name, 'size': size,
            'installer_url': asset_url(installer.get('browser_download_url'), version, name),
            'checksums_url': asset_url(checksums.get('browser_download_url'), version, 'SHA256SUMS'),
            'available': number > version_number(current_version)}


def verify_installer(data, checksums, name, size):
    matches = [match.group(1).lower() for line in checksums.decode('ascii').splitlines()
               if (match := re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?' + re.escape(name), line))]
    if len(matches) != 1 or len(data) != size or hashlib.sha256(data).hexdigest() != matches[0]:
        raise ValueError('Контрольная сумма обновления не совпадает. Установка отменена.')
    if not data.startswith(b'MZ'):
        raise ValueError('Загруженный файл не является установщиком Windows.')


class UpdateManager:
    def __init__(self, root, current_version, cache_root, *, fetch=fetch_bytes, launch=None, ready=None, shutdown=None, restart_args=()):
        self.root = Path(root).resolve()
        self.cache_root = Path(cache_root).resolve()
        self.fetch, self.launch = fetch, launch or subprocess.Popen
        self.ready, self.shutdown = ready or (lambda: None), shutdown or (lambda: None)
        self.restart_args = tuple(restart_args)
        self.lock = threading.RLock()
        self.release = None
        self.last_check = 0.0
        self.state = {'current_version': current_version, 'latest_version': None,
                      'available': False, 'checking': False, 'installing': False,
                      'error': '', 'message': 'Проверить обновления'}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def refresh(self):
        with self.lock:
            if self.last_check and time.monotonic() - self.last_check >= 6 * 60 * 60:
                return self.check()
            return self.snapshot()

    def check(self):
        with self.lock:
            if self.state['checking'] or self.state['installing'] or time.monotonic() - self.last_check < 60:
                return self.snapshot()
            self.state.update(checking=True, error='', message='Проверка обновлений…')
            threading.Thread(target=self._check, name='release-check', daemon=True).start()
            return self.snapshot()

    def _check(self):
        try:
            release = release_info(json.loads(self.fetch(API_URL, 1024 * 1024)), self.state['current_version'])
            with self.lock:
                self.release = release
                self.state.update(latest_version=release['version'], available=release['available'], error='',
                                  message=f'Доступна версия {release["version"]}' if release['available'] else 'Установлена последняя версия')
        except Exception:
            with self.lock:
                self.state.update(error='Не удалось проверить обновления. Проверьте интернет и повторите.', message='Проверка обновлений недоступна')
        finally:
            with self.lock:
                self.last_check = time.monotonic()
                self.state['checking'] = False

    def install(self):
        with self.lock:
            if self.state['installing']:
                return self.snapshot()
            if self.state['checking'] or not self.release or not self.release['available']:
                raise ValueError('Сначала проверьте наличие новой версии.')
            marker = json.loads((self.root / '.region-ocr-install.json').read_text('utf-8'))
            if marker.get('product') != 'REGION OCR' or Path(marker.get('folder', '')).resolve() != self.root:
                raise ValueError('Обновление доступно только для установленной копии приложения.')
            self.ready()
            self.state.update(installing=True, error='', message='Загрузка обновления…')
            threading.Thread(target=self._install, name='release-install', daemon=True).start()
            return self.snapshot()

    def _install(self):
        target = None
        launched = False
        written = False
        try:
            release = self.release
            sums = self.fetch(release['checksums_url'], 64 * 1024)
            data = self.fetch(release['installer_url'], MAX_INSTALLER)
            verify_installer(data, sums, release['name'], release['size'])
            self.cache_root.mkdir(parents=True, exist_ok=True)
            # Only the verified installer is persisted; no document or credentials leave the machine.
            target = self.cache_root / release['name']
            try:
                output = target.open('xb')
            except FileExistsError:
                with target.open('rb') as existing:
                    reusable = existing.read(MAX_INSTALLER + 1) == data
                if reusable:
                    output = None  # An interrupted handoff can safely retry the verified same bytes.
                else:
                    # Do not overwrite or delete a pre-existing unknown file in the cache.
                    target = self.cache_root / (Path(release['name']).stem + '-' + uuid.uuid4().hex + '.exe')
                    output = target.open('xb')
            if output is not None:
                written = True
                with output:
                    output.write(data)
            self.launch([str(target), '--update', str(self.root), '--wait-pid', str(os.getpid()), *self.restart_args],
                        cwd=str(self.root), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
            launched = True
            with self.lock:
                self.state['message'] = 'Обновление подготовлено. Приложение перезапускается…'
            self.shutdown()
        except Exception as error:
            with self.lock:
                self.state.update(installing=False, error=str(error) if isinstance(error, ValueError) else 'Не удалось подготовить обновление. Повторите попытку.',
                                  message='Обновление не установлено')
        finally:
            if target is not None and written and not launched:
                target.unlink(missing_ok=True)
                try:
                    self.cache_root.rmdir()
                except OSError:
                    pass
