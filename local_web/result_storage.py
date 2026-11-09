"""Remember the selected output directory without moving older deliveries."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import uuid

if __package__:
    from .file_paths import native_path, normal_path
    from .saved_results import SavedResults
else:
    from file_paths import native_path, normal_path
    from saved_results import SavedResults


class ResultStorage:
    """Route saved IDs to their original root; new saves use the selected root."""
    _record_methods = {'get', 'read_log', 'open_folder', 'open_target', 'target_file',
                       'asset', 'backfill_html', 'delete'}

    def __init__(self, data_root, default_root, *, application_root):
        self.data_root = normal_path(data_root).resolve()
        self.default_root = normal_path(default_root).resolve()
        self.application_root = normal_path(application_root).resolve()
        self.settings_file = self.data_root / 'storage-settings.json'
        self.managers = {}
        self.unavailable_roots = {}
        if self.settings_file.is_symlink():
            raise ValueError('Файл настроек не может быть ссылкой')
        if self.settings_file.is_file():
            settings = json.loads(self.settings_file.read_text(encoding='utf-8'))
            roots = settings.get('output_roots')
            selected = settings.get('output_dir')
            if (settings.get('schema') != 1 or not isinstance(roots, list)
                    or not all(isinstance(root, str) for root in roots)
                    or not isinstance(selected, str) or selected not in roots):
                raise ValueError('Некорректные настройки папки результатов')
        else:
            roots = [str(self.default_root)]
            selected = roots[0]
        self.remembered_roots = list(dict.fromkeys(roots))
        self.selected_root = normal_path(selected).resolve()
        known_ids = set()
        for root in roots:
            try:
                # A missing old directory can be a disconnected drive. Do not
                # recreate historic folders or discard their recorded paths.
                if self.settings_file.is_file() and not native_path(root).is_dir():
                    raise FileNotFoundError('Папка временно недоступна')
                manager = SavedResults(root)
            except (OSError, ValueError) as error:
                self.unavailable_roots[str(normal_path(root).resolve())] = str(error)
                continue
            if known_ids.intersection(manager.records):
                raise ValueError('В папках результатов повторяются идентификаторы документов')
            known_ids.update(manager.records)
            self.managers[str(manager.root)] = manager
        self.active = self.managers.get(str(self.selected_root))

    @property
    def root(self):
        return self.selected_root

    @property
    def storage_error(self):
        if self.active is None:
            return 'Папка сохранения недоступна. Выберите другую папку перед сохранением результатов.'
        return ''

    @property
    def records(self):
        return {ident: record for manager in self.managers.values() for ident, record in manager.records.items()}

    @property
    def pending_uploads(self):
        return {ident: record for manager in self.managers.values() for ident, record in manager.pending_uploads.items()}

    @property
    def removed_result_ids(self):
        return set().union(*(manager.removed_result_ids for manager in self.managers.values()))

    @staticmethod
    def _check_tree(folder):
        return SavedResults._check_tree(folder)

    def _manager(self, ident):
        for manager in self.managers.values():
            if ident in manager.records:
                return manager
        raise KeyError('Сохранённый результат не найден')

    def __getattr__(self, name):
        if name in self._record_methods:
            def call(ident, *args, **kwargs):
                return getattr(self._manager(ident), name)(ident, *args, **kwargs)
            return call
        # Non-routing attributes remain compatible with the single-root manager.
        if name in {'index', 'index_dir', 'logs_dir', 'uploads', 'reserve_source', 'save'}:
            if self.active is None:
                raise ValueError(self.storage_error)
            return getattr(self.active, name)
        raise AttributeError(name)

    def list(self):
        values = [record for manager in self.managers.values() for record in manager.list()]
        return sorted(values, key=lambda record: record.get('created_at', ''), reverse=True)

    def upload_source(self, job_id):
        for manager in self.managers.values():
            if job_id in manager.pending_uploads or any(r['job_id'] == job_id for r in manager.records.values()):
                return manager.upload_source(job_id)
        raise FileNotFoundError('Исходный файл не найден')

    def validate_delete_job(self, job_id):
        self._reconnect()
        if self.unavailable_roots:
            raise ValueError('Одна из прежних папок результатов недоступна. Подключите её, чтобы полностью удалить документ и его логи.')
        return [ident for manager in self.managers.values() for ident in manager.validate_delete_job(job_id)]

    def delete_job(self, job_id):
        self.validate_delete_job(job_id)
        for manager in self.managers.values():
            manager.delete_job(job_id)

    def prune_missing(self, *, protected_job_ids=()):
        self._reconnect()
        return [ident for manager in self.managers.values()
                for ident in manager.prune_missing(protected_job_ids=protected_job_ids)]

    def _reconnect(self):
        """Retry inactive catalogs on explicit refresh/deletion, not every poll."""
        for root in self.remembered_roots:
            key = str(normal_path(root).resolve())
            if key in self.managers:
                continue
            try:
                if not native_path(root).is_dir():
                    raise FileNotFoundError('Папка временно недоступна')
                manager = SavedResults(root)
                if set(manager.records).intersection(self.records):
                    raise ValueError('В папках результатов повторяются идентификаторы документов')
            except (OSError, ValueError) as error:
                self.unavailable_roots[key] = str(error)
                continue
            self.managers[key] = manager
            self.unavailable_roots.pop(key, None)
            if manager.root == self.selected_root:
                self.active = manager

    def settings(self):
        return {'output_dir': str(self.root), 'storage_error': self.storage_error}

    def change_root(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Укажите полный путь к папке сохранения результатов')
        requested = normal_path(value.strip()).expanduser()
        if not requested.is_absolute():
            raise ValueError('Укажите полный путь к папке сохранения результатов')
        for entry in (*requested.parents, requested):
            candidate_path = native_path(entry)
            if candidate_path.is_symlink() or (hasattr(candidate_path, 'is_junction') and candidate_path.is_junction()):
                raise ValueError('Папка сохранения не может проходить через ссылку')
        requested = requested.resolve()
        if requested == Path(requested.anchor):
            raise ValueError('Выберите отдельную папку, а не корень диска')
        if requested != self.default_root:
            for protected in (self.data_root, self.application_root):
                if requested.is_relative_to(protected) or protected.is_relative_to(requested):
                    raise ValueError('Выберите папку вне служебных файлов приложения')
        for root in self.remembered_roots:
            remembered = normal_path(root).resolve()
            if requested != remembered and (requested.is_relative_to(remembered) or remembered.is_relative_to(requested)):
                raise ValueError('Папки сохранения не должны находиться одна внутри другой')
        # SavedResults rejects links; check the caller's original path before
        # resolving it so a symlink cannot be hidden by normalization above.
        candidate = self.managers.get(str(requested)) or SavedResults(normal_path(value.strip()).expanduser())
        if candidate is not self.active and set(candidate.records).intersection(self.records):
            other_ids = {ident for manager in self.managers.values() if manager is not candidate for ident in manager.records}
            if set(candidate.records).intersection(other_ids):
                raise ValueError('В выбранной папке повторяются идентификаторы сохранённых документов')
        # Verify actual write access before changing the current selection.
        with tempfile.TemporaryFile(dir=native_path(candidate.root)):
            pass
        managers = dict(self.managers)
        managers[str(candidate.root)] = candidate
        roots = list(dict.fromkeys([*self.remembered_roots, str(candidate.root)]))
        settings = {'schema': 1, 'output_dir': str(candidate.root), 'output_roots': roots}
        if self.settings_file.is_symlink():
            raise ValueError('Файл настроек не может быть ссылкой')
        temporary = self.data_root / ('.storage-settings-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.settings_file)
        finally:
            temporary.unlink(missing_ok=True)
        self.managers, self.active = managers, candidate
        self.selected_root = candidate.root
        self.remembered_roots = roots
        self.unavailable_roots.pop(str(candidate.root), None)
        return self.settings()
