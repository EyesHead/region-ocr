"""Original formula crops belong to the private HTML comparison view."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import re
import shutil

if __package__:
    from .assets import _read_reference, MAX_IMAGE_PIXELS
    from .file_paths import native_path, normal_path
else:
    from assets import _read_reference, MAX_IMAGE_PIXELS
    from file_paths import native_path, normal_path


PREVIEW_NAME = 'formula-preview.html'
CROP_DIR = 'formula-src'
DIGEST = re.compile(r'[0-9a-f]{64}')


class FormulaComparisons:
    """Temporarily detach comparison images before packaging the editable document."""

    def __init__(self, soup):
        self.items = []
        self.images = {}
        self.warnings = []
        for group in soup.select('div[data-formula-comparison="true"]'):
            page = group.get('data-pdf-page', '')
            details = group.find_all('details', attrs={'data-preview-only': 'formula-source'}, recursive=False)
            if not re.fullmatch(r'[1-9][0-9]*', page) or len(details) != 1:
                raise ValueError('Не удалось определить страницу или оригинал формулы для предпросмотра')
            details = details[0].extract()
            images = details.find_all('img')
            if len(images) != 1 or not images[0].get('src', '').lower().startswith('data:image/png;base64,'):
                raise ValueError('В предпросмотре формулы ожидается один исходный кроп PNG')
            image = images[0]
            data = _read_reference(image['src'], None, None)
            from PIL import Image
            with Image.open(BytesIO(data)) as raster:
                if (raster.format != 'PNG' or raster.width * raster.height > MAX_IMAGE_PIXELS
                        or getattr(raster, 'n_frames', 1) != 1):
                    raise ValueError('Некорректный PNG исходной формулы')
                raster.verify()
            digest = hashlib.sha256(data).hexdigest()
            self.images.setdefault(digest, data)
            image['src'] = f'{CROP_DIR}/{digest}.png'
            self.items.append((group, details))
            if group.get('data-formula-warning') == 'clipped':
                self.warnings.append(f'Страница PDF {page}. Формула обрезана у края исходного листа. '
                                     'Оригинал доступен в предпросмотре.')
            elif group.get('data-formula-warning') == 'clipped-uncertain':
                self.warnings.append(f'Страница PDF {page}. Формула обрезана у края листа. '
                                     'Проверьте распознанные знаки по оригиналу в предпросмотре.')
            elif group.get('data-formula-warning'):
                self.warnings.append(f'Страница PDF {page}. Проверьте распознавание формулы по оригиналу в предпросмотре.')

    def write(self, soup, replay):
        """Use the externalized ordinary images in both views and retain only math in the canonical."""
        if not self.items:
            return None
        folder = native_path(replay / CROP_DIR)
        folder.mkdir(exist_ok=True)
        for digest, data in self.images.items():
            _owned_file(replay, CROP_DIR, digest + '.png').write_bytes(data)
        for group, details in self.items:
            group.append(details)
        preview = str(soup).encode('utf-8')
        _owned_file(replay, PREVIEW_NAME).write_bytes(preview)
        for group, details in self.items:
            details.decompose()
            group.unwrap()
        return {'html_sha256': hashlib.sha256(preview).hexdigest(), 'images': sorted(self.images)}


def _owned_file(root, *parts):
    """Resolve only fixed file roles, rejecting a substituted file or directory."""
    root = normal_path(root).absolute()
    current = root
    for part in parts:
        current = current / part
        path = native_path(current)
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Файл или каталог предпросмотра заменён ссылкой')
    path = native_path(current)
    if not normal_path(path.resolve()).is_relative_to(root):
        raise ValueError('Некорректный путь предпросмотра')
    return path


def preview_info(canonical):
    manifest = _owned_file(normal_path(canonical).parent, 'manifest.json')
    if not manifest.is_file():
        return None
    return json.loads(manifest.read_text('utf-8')).get('formula_preview')


def _checked(path, expected):
    if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
        raise ValueError('Некорректная контрольная сумма предпросмотра')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('Предпросмотр формулы изменён или отсутствует')
    return path


def preview_file(root, info):
    return _checked(_owned_file(root, PREVIEW_NAME), info['html_sha256'])


def preview_image(root, info, digest):
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest) or not info or digest not in info['images']:
        raise FileNotFoundError('Оригинал формулы не найден')
    return _checked(_owned_file(root, CROP_DIR, digest + '.png'), digest)


def copy_preview(canonical, target, info):
    root = normal_path(canonical).parent
    shutil.copyfile(preview_file(root, info), native_path(target / PREVIEW_NAME))
    native_path(target / CROP_DIR).mkdir()
    for digest in info['images']:
        origin = preview_image(root, info, digest)
        shutil.copyfile(origin, native_path(target / CROP_DIR / origin.name))


def migration_content(canonical, info, soup):
    """Carry comparison evidence into another canonical when its document schema changes."""
    from bs4 import BeautifulSoup

    if not info:
        return str(soup)
    root = normal_path(canonical).parent
    preview = BeautifulSoup(preview_file(root, info).read_text('utf-8'), 'html.parser')
    for image in preview.select('details[data-preview-only="formula-source"] img'):
        match = re.fullmatch(r'formula-src/([a-f0-9]{64})\.png', image.get('src', ''))
        if not match:
            raise ValueError('Некорректная ссылка на оригинал формулы')
        data = preview_image(root, info, match[1]).read_bytes()
        image['src'] = 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
    return str(preview)
