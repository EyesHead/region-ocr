"""Managed local result folders, their complete logs and native folder opening.

Only folders recorded by this manager may be removed or opened. The converter's
private packages and original inputs remain untouched by saving or deletion.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid

if __package__:
    from .file_paths import native_path, normal_path
    from .upload_storage import UploadReservations
else:
    from file_paths import native_path, normal_path
    from upload_storage import UploadReservations


FORMATS = {'html': '.html', 'markdown': '.md', 'json': '.json', 'plaintext': '.txt'}


def _is_link(path):
    candidate = native_path(path)
    return candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction())


def _identity(path):
    stat = native_path(path).stat()
    return {'device': stat.st_dev, 'inode': stat.st_ino}


def _source_name(value):
    name = str(value)
    if (not name or len(name) > 180 or name in {'.', '..'} or name != name.strip(' .')
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)):
        raise ValueError('Некорректное имя исходного файла')
    stem = Path(name).stem
    if not stem or stem.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        raise ValueError('Некорректное имя исходного файла')
    return name


class SavedResults:
    """Persistent inventory for one explicitly selected ocr_output directory."""

    def __init__(self, root: Path):
        requested = normal_path(root).absolute()
        for parent in [*requested.parents, requested]:
            if _is_link(parent):
                raise ValueError('Каталог сохранённых результатов не может проходить через ссылку')
        self.root = normal_path(native_path(requested).resolve())
        native_path(self.root).mkdir(parents=True, exist_ok=True)
        self.index_dir = self.root / '.index'
        self.logs_dir = self.index_dir / 'logs'
        self.index = self.index_dir / 'results.json'
        self.lock = threading.RLock()
        self.records = {}
        self.removed_result_ids = set()
        self.pending_uploads = {}
        self.uploads = UploadReservations(self)
        self._check_root()
        native_path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        self._load()

    def _check_root(self):
        if _is_link(self.root) or normal_path(native_path(self.root).resolve()) != self.root:
            raise ValueError('Каталог сохранённых результатов был заменён ссылкой')
        for path in (self.index_dir, self.logs_dir, self.index):
            if _is_link(path):
                raise ValueError('Служебные файлы сохранённых результатов не могут быть ссылками')

    def _load(self):
        if not native_path(self.index).is_file():
            return
        try:
            value = json.loads(native_path(self.index).read_text(encoding='utf-8'))
            if value.get('schema_version') != 1 or not isinstance(value.get('results'), list):
                raise ValueError('Некорректная схема списка')
            records = {}
            for record in value['results']:
                ident = record['id']
                if not re.fullmatch(r'[0-9a-f]{32}', ident) or ident in records:
                    raise ValueError('Некорректный идентификатор результата')
                _source_name(record['name'])
                if record['format'] not in FORMATS or not isinstance(record.get('package_key'), str):
                    raise ValueError('Некорректный формат результата')
                if (not isinstance(record.get('created_at'), str) or not isinstance(record.get('job_id'), str)
                        or type(record.get('size')) is not int or record['size'] < 0):
                    raise ValueError('Некорректные метаданные сохранённого результата')
                self._folder(record, verify_identity=False)
                identity = record.get('directory_identity')
                if not isinstance(identity, dict) or not all(isinstance(identity.get(key), int) for key in ('device', 'inode')):
                    raise ValueError('Не установлена принадлежность папки результата')
                records[ident] = record
            self.records = records
            removed = value.get('removed_result_ids', [])
            if (not isinstance(removed, list) or any(not isinstance(ident, str)
                    or not re.fullmatch(r'[0-9a-f]{32}', ident) for ident in removed)):
                raise ValueError('Некорректный список отсутствующих результатов')
            self.removed_result_ids = set(removed)
            self.uploads.load(value.get('pending_uploads', []))
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError('Не удалось прочитать список сохранённых результатов; исходный индекс оставлен без изменений') from error

    def _persist(self):
        self._check_root()
        temporary = self.index_dir / ('.results-' + uuid.uuid4().hex + '.tmp')
        try:
            native_path(temporary).write_text(json.dumps({'schema_version': 1, 'results': list(self.records.values()), 'pending_uploads': list(self.pending_uploads.values()), 'removed_result_ids': sorted(self.removed_result_ids)}, ensure_ascii=False, indent=2), encoding='utf-8')
            native_path(temporary).replace(native_path(self.index))
        finally:
            native_path(temporary).unlink(missing_ok=True)

    def _record(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise KeyError('Сохранённый результат не найден')
        try:
            return self.records[ident]
        except KeyError:
            raise KeyError('Сохранённый результат не найден') from None

    def _folder(self, record, *, verify_identity=True):
        self._check_root()
        name = record['folder_name']
        base = Path(_source_name(record['name'])).stem + '_ocr'
        if not isinstance(name, str) or not re.fullmatch(re.escape(base) + r'(?: \((?:[2-9]|[1-9]\d+)\))?', name):
            raise ValueError('Некорректная папка сохранённого результата')
        path = self.root / name
        if _is_link(path):
            raise ValueError('Папка результата была заменена ссылкой')
        resolved = normal_path(native_path(path).resolve())
        if resolved.parent != self.root:
            raise ValueError('Папка результата выходит за пределы ocr_output')
        if native_path(path).exists():
            if not native_path(path).is_dir():
                raise ValueError('Вместо папки результата найден файл')
            if verify_identity and _identity(path) != record.get('directory_identity'):
                raise ValueError('Папка результата была заменена; удаление и открытие отклонены')
        return path

    @staticmethod
    def _check_tree(folder):
        if _is_link(folder):
            raise ValueError('Пакет результата не может быть ссылкой')
        root = normal_path(native_path(folder).resolve())
        if not native_path(root).is_dir():
            raise FileNotFoundError('Папка результата не найдена')
        for entry in native_path(root).rglob('*'):
            if _is_link(entry) or not normal_path(entry.resolve()).is_relative_to(root):
                raise ValueError('Пакет содержит ссылку или путь за пределами своей папки')
        return root

    def _public(self, record):
        result = {key: record[key] for key in ('id', 'name', 'format', 'folder_name', 'created_at', 'size', 'job_id')}
        result['path'] = str(self.root / record['folder_name'])
        try:
            result['exists'] = native_path(self._folder(record)).is_dir()
        except (OSError, ValueError) as error:
            result['exists'] = False
            result['error'] = str(error)
        return result

    def list(self):
        with self.lock:
            self._check_root()
            return [self._public(record) for record in reversed(list(self.records.values()))]

    def prune_missing(self, *, protected_job_ids=()):
        """Forget only externally removed deliveries, never existing data.

        Persist forgotten result IDs with the same atomic index update so a
        retained private package cannot resurrect the deleted history entry.
        A running/scheduled job and all of its earlier deliveries are excluded.
        """
        protected = set(protected_job_ids)
        with self.lock:
            self._check_root()
            removed = []
            for record in self.records.values():
                if record['job_id'] in protected:
                    continue
                # _folder rejects replacement directories, files and links;
                # permission errors are not evidence that a folder is absent.
                folder = self._folder(record)
                try:
                    native_path(folder).stat()
                except FileNotFoundError:
                    log = self.logs_dir / (record['id'] + '.log')
                    if _is_link(log):
                        raise ValueError('Файл лога был заменён ссылкой')
                    removed.append(record)
            if not removed:
                return []
            previous_records = self.records.copy()
            previous_removed = self.removed_result_ids.copy()
            for record in removed:
                self.records.pop(record['id'])
                self.removed_result_ids.add(record['id'])
            try:
                self._persist()
            except Exception:
                self.records = previous_records
                self.removed_result_ids = previous_removed
                raise
            for record in removed:
                self._check_root()
                log = self.logs_dir / (record['id'] + '.log')
                if not _is_link(log):
                    native_path(log).unlink(missing_ok=True)
            return [record['id'] for record in removed]

    def get(self, ident):
        with self.lock:
            return self._public(self._record(ident))

    def reserve_source(self, job_id, name, source):
        return self.uploads.reserve(job_id, _source_name(name), source)

    def upload_source(self, job_id):
        return self.uploads.source(job_id)

    def save(self, package: Path, *, job_id, name, format, log: str):
        name = _source_name(name)
        if format not in FORMATS:
            raise ValueError('Неизвестный формат результата')
        original_package = normal_path(package).absolute()
        package = self._check_tree(original_package)
        source = Path(name)
        expected = {source.stem + '_source' + source.suffix, source.stem + '_converted' + FORMATS[format],
                    source.stem + '_converted.html', 'src'}
        children = {entry.name for entry in native_path(package).iterdir()}
        if children != expected or not native_path(package / 'src').is_dir():
            raise ValueError('Пакет должен содержать исходник, выбранный результат, HTML и папку src')
        for filename in expected - {'src'}:
            if not native_path(package / filename).is_file():
                raise ValueError('В пакете отсутствует исходник или выбранный результат')
        package_key = hashlib.sha256(os.path.normcase(str(package)).encode('utf-8')).hexdigest()
        with self.lock:
            self._check_root()
            for record in self.records.values():
                if record['package_key'] == package_key:
                    if record['name'] != name or record['format'] != format or record['job_id'] != job_id:
                        raise ValueError('Этот пакет уже связан с другим результатом')
                    if native_path(self._folder(record)).is_dir():
                        return self._public(record)
            if job_id in self.pending_uploads:
                return self.uploads.complete(package, job_id=job_id, name=name, format=format,
                                             log=log, package_key=package_key)
            ident = uuid.uuid4().hex
            base = source.stem + '_ocr'
            used = {entry.name.casefold() for entry in native_path(self.root).iterdir()}
            folder_name, number = base, 2
            while folder_name.casefold() in used:
                folder_name = f'{base} ({number})'
                number += 1
            target = self.root / folder_name
            staging = self.root / ('.stage-' + ident)
            log_path = self.logs_dir / (ident + '.log')
            published = False
            try:
                # Recheck every copied file; source paths are never interpreted
                # as shell commands and a late symlink cannot escape the package.
                def copy_file(src, dst):
                    candidate = normal_path(src)
                    if _is_link(candidate) or not normal_path(native_path(candidate).resolve()).is_relative_to(package):
                        raise ValueError('Пакет изменился во время сохранения')
                    return shutil.copy2(native_path(src), native_path(dst), follow_symlinks=False)
                shutil.copytree(native_path(package), native_path(staging), copy_function=copy_file)
                self._check_tree(staging)
                size = sum(entry.stat().st_size for entry in native_path(staging).rglob('*') if entry.is_file())
                if _is_link(log_path):
                    raise ValueError('Файл лога не может быть ссылкой')
                native_path(log_path).write_text(str(log), encoding='utf-8')
                if target.parent != self.root or native_path(target).exists():
                    raise ValueError('Назначение сохранения изменилось')
                native_path(staging).rename(native_path(target))
                published = True
                record = dict(id=ident, name=name, format=format, folder_name=folder_name,
                              created_at=datetime.now(timezone.utc).isoformat(), size=size,
                              job_id=str(job_id), package_key=package_key, directory_identity=_identity(target))
                self.records[ident] = record
                self._persist()
                return self._public(record)
            except Exception:
                self.records.pop(ident, None)
                cleanup = target if published else staging
                # Both targets are freshly created, direct children of this root.
                if cleanup.parent == self.root and native_path(cleanup).exists() and not _is_link(cleanup):
                    shutil.rmtree(native_path(cleanup))
                native_path(log_path).unlink(missing_ok=True)
                raise

    def read_log(self, ident):
        with self.lock:
            self._record(ident)
            self._check_root()
            path = self.logs_dir / (ident + '.log')
            if _is_link(path):
                raise ValueError('Файл лога был заменён ссылкой')
            if not native_path(path).is_file():
                raise FileNotFoundError('Лог сохранённого результата не найден')
            return native_path(path).read_text(encoding='utf-8')

    def backfill_html(self, ident, *, package, canonical):
        """Add only missing HTML when an old delivery's exact lineage is proven."""
        from bs4 import BeautifulSoup

        with self.lock:
            record = self._record(ident)
            folder = self._folder(record)
            target = folder / (Path(record['name']).stem + '_converted.html')
            if native_path(target).exists() or _is_link(target):
                return False
            package = self._check_tree(normal_path(package).absolute())
            package_key = hashlib.sha256(os.path.normcase(str(package)).encode('utf-8')).hexdigest()
            if package_key != record['package_key']:
                raise ValueError('Старый пакет не соответствует сохранённому результату')
            self._check_tree(folder)
            selected = self.target_file(ident, 'converted')
            private_selected = package / selected.name
            if not native_path(private_selected).is_file():
                raise ValueError('Исходный выбранный результат не найден')
            if hashlib.sha256(selected.read_bytes()).digest() != hashlib.sha256(native_path(private_selected).read_bytes()).digest():
                raise ValueError('Сохранённый результат изменён после конвертации')
            canonical = normal_path(canonical).absolute()
            manifest_path = canonical.parent / 'manifest.json'
            if _is_link(canonical) or _is_link(manifest_path):
                raise ValueError('Канонический документ был заменён ссылкой')
            content = native_path(canonical).read_bytes()
            manifest = json.loads(native_path(manifest_path).read_text(encoding='utf-8'))
            if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
                    or manifest.get('html_sha256') != hashlib.sha256(content).hexdigest()):
                raise ValueError('Не подтверждён канонический HTML старого результата')
            soup = BeautifulSoup(content.decode('utf-8'), 'html.parser')
            if soup.body is None:
                raise ValueError('Канонический HTML не содержит body')
            assets = {}
            manifest_assets = manifest.get('assets')
            if not isinstance(manifest_assets, list):
                raise ValueError('Некорректный манифест изображений старого результата')
            for asset in manifest_assets:
                if not isinstance(asset, dict):
                    raise ValueError('Некорректный манифест изображений старого результата')
                path, digest = asset.get('path'), asset.get('sha256')
                if (not isinstance(path, str) or not re.fullmatch(r'src/[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp|gif|bmp|tif|tiff)', path, re.I)
                        or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                        or path in assets):
                    raise ValueError('Некорректный манифест изображений старого результата')
                assets[path] = digest
            for image in soup.find_all('img'):
                path = str(image.get('src', ''))
                if path not in assets:
                    raise ValueError('Изображение HTML отсутствует в манифесте')
                saved_asset = self.asset(ident, path[4:])
                if hashlib.sha256(saved_asset.read_bytes()).hexdigest() != assets[path]:
                    raise ValueError('Изображение сохранённого результата не соответствует HTML')
            # Exclusive creation preserves files added by the user or another
            # startup; source, selected export, images and log are never written.
            self._folder(record)
            created = False
            try:
                with native_path(target).open('xb') as stream:
                    created = True
                    stream.write(content)
            except FileExistsError:
                return False
            except OSError:
                if created:
                    native_path(target).unlink(missing_ok=True)
                raise
            created_identity = _identity(target)
            previous_size = record['size']
            try:
                record['size'] = sum(entry.stat().st_size for entry in native_path(folder).rglob('*') if entry.is_file())
                self._persist()
            except Exception:
                record['size'] = previous_size
                if not _is_link(target) and native_path(target).exists() and _identity(target) == created_identity:
                    native_path(target).unlink()
                raise
            return True

    def delete(self, ident):
        with self.lock:
            record = self._record(ident)
            folder = self._folder(record)
            log_path = self.logs_dir / (ident + '.log')
            if _is_link(log_path):
                raise ValueError('Файл лога был заменён ссылкой')
            if native_path(folder).exists():
                self._check_tree(folder)
                # _folder establishes the exact direct root child and identity.
                shutil.rmtree(native_path(folder))
            native_path(log_path).unlink(missing_ok=True)
            del self.records[ident]
            try:
                self._persist()
            except Exception:
                self.records[ident] = record
                raise

    def validate_delete_job(self, job_id):
        """Check every owned delivery before removing any version of a job."""
        with self.lock:
            self._check_root()
            self.uploads.validate_delete(job_id)
            ids = [record['id'] for record in self.records.values() if record['job_id'] == job_id]
            for ident in ids:
                folder = self._folder(self._record(ident))
                if native_path(folder).exists():
                    self._check_tree(folder)
                if _is_link(self.logs_dir / (ident + '.log')):
                    raise ValueError('Файл лога был заменён ссылкой')
            return ids

    def delete_job(self, job_id):
        """Remove all saved formats/versions belonging to one upload, not a name."""
        with self.lock:
            for ident in self.validate_delete_job(job_id):
                self.delete(ident)
            self.uploads.delete(job_id)

    def open_folder(self, ident):
        with self.lock:
            folder = self._folder(self._record(ident))
            if not native_path(folder).is_dir():
                raise FileNotFoundError('Сохранённая папка не найдена')
            if os.name == 'nt':
                os.startfile(str(folder))
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(folder)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.get(ident)

    def target_file(self, ident, target):
        """Resolve a fixed document role inside an owned result; never a caller path."""
        with self.lock:
            record = self._record(ident)
            folder = self._folder(record)
            source = Path(record['name'])
            names = {'source': source.stem + '_source' + source.suffix,
                     'converted': source.stem + '_converted' + FORMATS[record['format']],
                     'html': source.stem + '_converted.html'}
            if not isinstance(target, str) or target not in names:
                raise ValueError('Неизвестный файл результата')
            path = folder / names[target]
            if _is_link(path) or normal_path(native_path(path).resolve()).parent != folder:
                raise ValueError('Файл результата был заменён ссылкой')
            if not native_path(path).is_file():
                raise FileNotFoundError('Файл результата не найден. Подготовьте экспорт заново.')
            return native_path(path)

    def asset(self, ident, name):
        with self.lock:
            folder = self._folder(self._record(ident))
            if not re.fullmatch(r'[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp|gif|bmp|tif|tiff)', name, re.I):
                raise FileNotFoundError('Изображение не найдено')
            directory = folder / 'src'
            path = directory / name
            if (_is_link(directory) or _is_link(path)
                    or normal_path(native_path(path).resolve()).parent != directory):
                raise ValueError('Изображение было заменено ссылкой')
            if not native_path(path).is_file():
                raise FileNotFoundError('Изображение не найдено')
            return native_path(path)

    def open_target(self, ident, target):
        with self.lock:
            if target == 'folder':
                return self.open_folder(ident)
            if not isinstance(target, str) or target not in {'source', 'converted'}:
                raise ValueError('Неизвестный файл для открытия')
            path = normal_path(self.target_file(ident, target))
            if os.name == 'nt':
                os.startfile(str(path))
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.get(ident)
