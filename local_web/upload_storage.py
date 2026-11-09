"""Owned source folders reserved at upload, completed by the first download."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shutil
import uuid

if __package__:
    from .file_paths import native_path, normal_path
else:
    from file_paths import native_path, normal_path


def digest(path):
    value = hashlib.sha256()
    with native_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class UploadReservations:
    def __init__(self, manager):
        self.manager = manager

    def load(self, values):
        if not isinstance(values, list):
            raise ValueError('Некорректный список папок загруженных файлов')
        records = {}
        for record in values:
            ident = record.get('job_id', '')
            if (not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident)
                    or ident in records or not re.fullmatch(r'[0-9a-f]{64}', record.get('source_sha256', ''))):
                raise ValueError('Некорректная принадлежность загруженного файла')
            identity = record.get('directory_identity')
            if not isinstance(identity, dict) or any(type(identity.get(k)) is not int for k in ('device', 'inode')):
                raise ValueError('Не установлена принадлежность папки исходника')
            self.manager._folder(record, verify_identity=False)
            records[ident] = record
        self.manager.pending_uploads = records

    def reserve(self, job_id, name, source):
        m = self.manager
        if not re.fullmatch(r'[0-9a-f]{32}', str(job_id)):
            raise ValueError('Некорректный идентификатор загрузки')
        with m.lock:
            m._check_root()
            if job_id in m.pending_uploads:
                return self.folder(job_id)
            base = Path(name).stem + '_ocr'
            used = {p.name.casefold() for p in native_path(m.root).iterdir()}
            used.update(r['folder_name'].casefold() for r in m.pending_uploads.values())
            used.update(r['folder_name'].casefold() for r in m.records.values())
            folder_name, counter = base, 2
            while folder_name.casefold() in used:
                folder_name = f'{base} ({counter})'
                counter += 1
            folder = m.root / folder_name
            native_path(folder).mkdir()
            original = folder / (Path(name).stem + '_source' + Path(name).suffix)
            try:
                with native_path(source).open('rb') as src, native_path(original).open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                identity = native_path(folder).stat()
                record = dict(job_id=job_id, name=name, folder_name=folder_name,
                              directory_identity=dict(device=identity.st_dev, inode=identity.st_ino),
                              source_sha256=digest(original))
                if record['source_sha256'] != digest(source):
                    raise ValueError('Копия исходника не соответствует загруженному файлу')
                m.pending_uploads[job_id] = record
                m._persist()
                return folder
            except Exception:
                m.pending_uploads.pop(job_id, None)
                # This exact direct child was created by this reservation.
                if folder.parent == m.root and native_path(folder).exists():
                    m._check_tree(folder)
                    shutil.rmtree(native_path(folder))
                raise

    def folder(self, job_id):
        return self.manager._folder(self.manager.pending_uploads[job_id])

    def source(self, job_id):
        m = self.manager
        with m.lock:
            pending = m.pending_uploads.get(job_id)
            if pending is not None:
                folder = m._folder(pending)
                name = Path(pending['name'])
                path = folder / (name.stem + '_source' + name.suffix)
                m._check_tree(folder)
                if not native_path(path).is_file():
                    raise FileNotFoundError('Загруженный исходник не найден')
                return path
            saved = next((r for r in m.records.values() if r['job_id'] == job_id), None)
            if saved is None:
                raise KeyError('Папка загруженного исходника не найдена')
            return normal_path(m.target_file(saved['id'], 'source'))

    def validate_delete(self, job_id):
        if job_id not in self.manager.pending_uploads:
            return
        folder = self.folder(job_id)
        if native_path(folder).exists():
            self.manager._check_tree(folder)

    def delete(self, job_id):
        m = self.manager
        if job_id not in m.pending_uploads:
            return
        self.validate_delete(job_id)
        folder = self.folder(job_id)
        if native_path(folder).exists():
            shutil.rmtree(native_path(folder))
        record = m.pending_uploads.pop(job_id)
        try:
            m._persist()
        except Exception:
            m.pending_uploads[job_id] = record
            raise

    def _tree_signature(self, path):
        """Inventory our staged entries before they become user-visible."""
        base = native_path(path)
        if base.is_symlink() or (hasattr(base, 'is_junction') and base.is_junction()):
            raise ValueError('Папка сохранения была изменена ссылкой')
        entries = [base, *base.rglob('*')] if base.is_dir() else [base]
        signature = {}
        for entry in entries:
            if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                raise ValueError('Папка сохранения была изменена ссылкой')
            stat = entry.stat()
            signature[str(entry.relative_to(base))] = (stat.st_dev, stat.st_ino, stat.st_mode,
                stat.st_mtime_ns, digest(entry) if entry.is_file() else None)
        return signature

    def complete(self, package, *, job_id, name, format, log, package_key):
        """Add complete output files without changing the reserved directory."""
        m = self.manager
        reservation = m.pending_uploads[job_id]
        if reservation['name'] != name:
            raise ValueError('Имя исходника не соответствует загрузке')
        target = self.folder(job_id)
        m._check_tree(target)
        original = Path(name).stem + '_source' + Path(name).suffix
        if {p.name for p in native_path(target).iterdir()} != {original}:
            raise ValueError('В папку исходника добавлены сторонние файлы; сохранение не перезаписывает их')
        if digest(target / original) != reservation['source_sha256'] or digest(package / original) != reservation['source_sha256']:
            raise ValueError('Исходник был изменён после загрузки')
        ident = uuid.uuid4().hex
        staging = m.root / ('.stage-' + ident)
        log_path = m.logs_dir / (ident + '.log')
        moved = []
        created_log = False
        try:
            def copy_file(src, dst):
                candidate = native_path(src)
                if candidate.is_symlink() or not normal_path(candidate.resolve()).is_relative_to(package):
                    raise ValueError('Пакет изменился во время сохранения')
                return shutil.copy2(candidate, native_path(dst), follow_symlinks=False)
            shutil.copytree(native_path(package), native_path(staging), copy_function=copy_file)
            m._check_tree(staging)
            for child in native_path(staging).iterdir():
                if child.name == original:
                    continue
                m._folder(reservation)
                destination = target / child.name
                if native_path(destination).exists():
                    raise ValueError('Назначение сохранения изменилось')
                ownership = self._tree_signature(child)
                child.rename(native_path(destination))
                moved.append((destination, ownership))
            with native_path(log_path).open('x', encoding='utf-8') as stream:
                created_log = True
                stream.write(str(log))
            record = dict(id=ident, job_id=job_id, name=name, format=format,
                          folder_name=reservation['folder_name'], package_key=package_key,
                          created_at=datetime.now(timezone.utc).isoformat(),
                          directory_identity=reservation['directory_identity'],
                          size=sum(p.stat().st_size for p in native_path(target).rglob('*') if p.is_file()))
            m.records[ident] = record
            del m.pending_uploads[job_id]
            m._persist()
            return m._public(record)
        except Exception as error:
            m.records.pop(ident, None)
            m.pending_uploads[job_id] = reservation
            m._folder(reservation)
            preserved = False
            for path, ownership in reversed(moved):
                # A user can add/edit a file while an index write is failing.
                # Never remove that content as part of publication rollback.
                try:
                    unchanged = self._tree_signature(path) == ownership
                except (OSError, ValueError):
                    unchanged = False
                if not unchanged:
                    preserved = True
                    continue
                if native_path(path).is_dir():
                    m._check_tree(path)
                    shutil.rmtree(native_path(path))
                else:
                    native_path(path).unlink(missing_ok=True)
            if created_log:
                native_path(log_path).unlink(missing_ok=True)
            if preserved:
                raise OSError('Сохранение не завершено. Изменённые файлы оставлены в папке исходника; '
                              'проверьте её перед повторным скачиванием.') from error
            raise
        finally:
            if staging.parent == m.root and native_path(staging).exists():
                m._check_tree(staging)
                shutil.rmtree(native_path(staging))
