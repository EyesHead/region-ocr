"""Content-addressed PDF resources exchanged between the fast reader and the exporter."""
import hashlib
from io import BytesIO
import re

MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MIME_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/jp2': 'jp2',
                   'image/j2k': 'j2k', 'image/tiff': 'tif', 'application/pdf': 'pdf'}
FAST_PATH = re.compile(r'src/fast-([0-9a-f]{64})\.(png|jpg|jp2|j2k|tif|pdf)')
FORMULA_CROP_ATTR = 'data-source-formula-crop'
FORMULA_CROP_PATH = re.compile(r'src/fast-[0-9a-f]{64}\.png')
LEGACY_NAME = re.compile(r'[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp|gif|bmp|tif|tiff)', re.I)
ASSET_TYPES = {'.' + ext: mime for mime, ext in MIME_EXTENSIONS.items()}
ASSET_TYPES.update({'.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
                    '.bmp': 'image/bmp', '.tiff': 'image/tiff'})


def served_asset_name(name):
    return isinstance(name, str) and (LEGACY_NAME.fullmatch(name) or FAST_PATH.fullmatch('src/' + name))


def formula_crop_paths(soup):
    for math in soup.find_all('math', attrs={FORMULA_CROP_ATTR: True}):
        value = math[FORMULA_CROP_ATTR]
        if FORMULA_CROP_PATH.fullmatch(value):
            yield value


def register_image(image_assets, mime, data):
    """Register bytes in a local mapping or send them to the worker transport."""
    data = bytes(data)
    if not data or len(data) > MAX_IMAGE_BYTES or mime not in MIME_EXTENSIONS:
        raise ValueError('Недопустимый тип или размер ресурса PDF')
    path = 'src/fast-' + hashlib.sha256(data).hexdigest() + '.' + MIME_EXTENSIONS[mime]
    if callable(image_assets):
        image_assets(path, data)
    else:
        image_assets.setdefault(path, data)
    return path


def exact_record(path, data):
    """Validate a worker resource before publishing it without re-encoding."""
    match = FAST_PATH.fullmatch(path)
    if not match or not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError('Некорректный путь или размер ресурса PDF')
    digest, ext = match.groups()
    if digest != hashlib.sha256(data).hexdigest():
        raise ValueError('Контрольная сумма ресурса PDF не совпадает с именем')
    record = dict(path=path, sha256=digest, original_bytes=len(data), bytes=len(data),
                  format={'jpg': 'jpeg', 'tif': 'tiff'}.get(ext, ext),
                  encoding='pdf-exact', lossless=True, resized=False, metadata_removed=False)
    if ext == 'pdf':
        from pypdfium2 import PdfDocument, PdfiumError
        if not data.startswith(b'%PDF-'):
            raise ValueError('Некорректный заголовок PDF-фрагмента')
        try:
            with PdfDocument(data) as doc:
                if len(doc) == 0:
                    raise ValueError('В PDF-фрагменте нет страниц')
        except PdfiumError as exc:
            raise ValueError('Не удалось прочитать PDF-фрагмент') from exc
        return record
    from PIL import Image
    try:
        with Image.open(BytesIO(data)) as image:
            expected = {'jpg': 'JPEG', 'png': 'PNG', 'tif': 'TIFF', 'jp2': 'JPEG2000', 'j2k': 'JPEG2000'}[ext]
            if image.format != expected or image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError('Тип или размеры изображения PDF не соответствуют объявленным')
            if ext in ('jp2', 'j2k') and (ext == 'j2k') != data.startswith(b'\xff\x4f'):
                raise ValueError('Некорректный контейнер JPEG 2000')
            record.update(width=image.width, height=image.height)
            image.verify()
    except (OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValueError('Не удалось прочитать изображение PDF') from exc
    return record
