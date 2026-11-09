"""External, metadata-free image assets with conservative lossless compression.

The default never reduces dimensions or introduces another lossy encoding pass.
Legacy data URIs are accepted only as input; every emitted reference is a file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
RASTER_MIMES = frozenset({'image/png', 'image/jpeg', 'image/jpg', 'image/gif',
                          'image/webp', 'image/bmp', 'image/tiff', 'image/x-icon'})


def _jpeg_without_metadata(data: bytes) -> bytes:
    """Remove APP/COM records without changing JPEG scans or quantization.

    Adobe APP14 describes the JPEG colour transform and is required for correct
    decoding; it is not a user metadata record. Malformed streams are rejected.
    """
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('Некорректный JPEG')
    output = bytearray(data[:2])
    position = 2
    while position < len(data):
        marker_start = position
        if data[position] != 0xff:
            raise ValueError('Некорректная структура JPEG')
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position >= len(data):
            raise ValueError('Оборванный JPEG')
        marker = data[position]
        position += 1
        if marker == 0xd9:
            output.extend(b'\xff\xd9')
            return bytes(output)
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            output.extend(data[marker_start:position])
            continue
        if position + 2 > len(data):
            raise ValueError('Оборванный JPEG')
        length = int.from_bytes(data[position:position + 2], 'big')
        end = position + length
        if length < 2 or end > len(data):
            raise ValueError('Некорректная длина JPEG-сегмента')
        if not (0xe0 <= marker <= 0xef or marker == 0xfe) or marker == 0xee:
            output.extend(data[marker_start:end])
        position = end
        if marker == 0xda:
            # Preserve entropy-coded scan bytes, including stuffing/restart
            # markers, until the next real marker (progressive JPEG included).
            scan_start = position
            while position < len(data):
                if data[position] != 0xff:
                    position += 1
                    continue
                next_position = position + 1
                while next_position < len(data) and data[next_position] == 0xff:
                    next_position += 1
                if next_position >= len(data):
                    raise ValueError('Оборванный JPEG scan')
                following = data[next_position]
                if following == 0 or 0xd0 <= following <= 0xd7:
                    position = next_position + 1
                    continue
                break
            output.extend(data[scan_start:position])
    raise ValueError('JPEG не содержит конечного маркера')


def _png_bytes(image):
    output = BytesIO()
    # Build an untagged copy so Pillow cannot propagate EXIF/ICC/comments.
    clean = image.copy()
    clean.info.clear()
    try:
        options = {}
        if clean.mode == 'P':
            maximum = clean.getextrema()[1]
            options['bits'] = next(bits for bits in (1, 2, 4, 8) if maximum < 2 ** bits)
        clean.save(output, format='PNG', optimize=True, compress_level=9, **options)
        return output.getvalue()
    finally:
        clean.close()


def store_image_bytes(data: bytes, assets_dir: Path, *, name: str = '') -> dict:
    """Write the smallest verified lossless candidate and return a portable ref.

    PNG and exact palette PNG are compared. For an unrotated JPEG the original
    coefficient stream, stripped of metadata, is another candidate. WebP and
    other optional native encoders are deliberately not activated by this layer.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Изображение пустое или превышает 128 МБ')
    warnings = []
    try:
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise ValueError('Изображение превышает 80 млн пикселей')
            frames = getattr(original, 'n_frames', 1)
            if frames > 1:
                raise ValueError('Многостраничное/анимированное вложенное изображение требует отдельных кропов; оно не уплощено до первого кадра')
            orientation = original.getexif().get(274, 1)
            original_format = original.format
            oriented = ImageOps.exif_transpose(original)
            try:
                if oriented.mode in {'I', 'I;16', 'I;16B', 'I;16L'}:
                    mode = 'I'
                elif oriented.mode == 'F':
                    raise ValueError('Изображение с плавающей точкой нельзя без потерь представить как браузерный PNG')
                else:
                    mode = 'RGBA' if 'A' in oriented.getbands() or 'transparency' in original.info else ('L' if oriented.mode in {'1', 'L'} else 'RGB')
                image = oriented.convert(mode)
            finally:
                oriented.close()
            if original.info.get('icc_profile'):
                warnings.append('Встроенный цветовой профиль удалён; размеры и декодированные значения пикселей сохранены.')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Не удалось прочитать растровое изображение: ' + (name or type(exc).__name__)) from exc

    try:
        if image.mode == 'I' and not (0 <= image.getextrema()[0] <= image.getextrema()[1] <= 65535):
            raise ValueError('32-битное изображение нельзя без потерь представить как 16-битный PNG')
        if image.mode == 'RGBA' and image.getextrema()[-1] == (255, 255):
            opaque = image.convert('RGB')
            image.close()
            image, mode = opaque, 'RGB'
        reference = image.tobytes()
        candidates = [('png', _png_bytes(image), 'lossless-png')]
        colours = image.getcolors(maxcolors=256)
        if mode in {'RGB', 'RGBA', 'L'} and colours is not None and len(colours) <= 256:
            palette = image.quantize(colors=max(1, len(colours)), method=Image.Quantize.FASTOCTREE,
                                     dither=Image.Dither.NONE) if mode == 'RGBA' else image.convert('P', palette=Image.Palette.ADAPTIVE, colors=max(1, len(colours)), dither=Image.Dither.NONE)
            try:
                restored = palette.convert(mode)
                try:
                    if restored.tobytes() == reference:
                        candidates.append(('png', _png_bytes(palette), 'lossless-palette-png'))
                finally:
                    restored.close()
            finally:
                palette.close()
        if original_format == 'JPEG' and orientation in {None, 1} and mode in {'RGB', 'L'}:
            try:
                stripped = _jpeg_without_metadata(data)
                with Image.open(BytesIO(stripped)) as check:
                    decoded = check.convert(mode)
                    try:
                        if decoded.size == image.size and decoded.tobytes() == reference:
                            candidates.append(('jpg', stripped, 'jpeg-original-coefficients'))
                    finally:
                        decoded.close()
            except (ValueError, OSError):
                pass  # PNG remains a verified, metadata-free fallback.
        extension, encoded, encoding = min(candidates, key=lambda candidate: len(candidate[1]))
        sha256 = hashlib.sha256(encoded).hexdigest()
        filename = 'img-' + sha256[:24] + '.' + extension
        folder = Path(assets_dir)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / filename
        if not target.exists():
            target.write_bytes(encoded)
        return dict(path='src/' + filename, sha256=sha256, width=image.width, height=image.height,
                    original_bytes=len(data), bytes=len(encoded), format='jpeg' if extension == 'jpg' else 'png',
                    encoding=encoding, lossless=True, resized=False, metadata_removed=True, warnings=warnings)
    finally:
        image.close()


def _read_reference(value: str, source: Path, resource_root: Path | None):
    if value.lower().startswith('data:'):
        header, separator, payload = value.partition(',')
        mime = header[5:].split(';', 1)[0].lower()
        if not separator or mime not in RASTER_MIMES or not header.lower().endswith(';base64'):
            raise ValueError('Неподдерживаемое встроенное изображение')
        if len(payload) > MAX_IMAGE_BYTES * 4 // 3 + 4096:
            raise ValueError('Встроенное изображение превышает 128 МБ')
        try:
            return base64.b64decode(re.sub(r'\s+', '', payload), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('Некорректное встроенное изображение') from exc
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme != 'file':
        raise ValueError('Внешние изображения не скачиваются автоматически')
    if parsed.netloc:
        raise ValueError('Сетевые пути изображений не разрешены')
    root = Path(resource_root).resolve() if resource_root else Path(source).resolve().parent
    raw = unquote(parsed.path)
    if parsed.scheme == 'file' and re.match(r'^/[A-Za-z]:/', raw):
        raw = raw[1:]
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError('Путь изображения выходит за пределы каталога документа')
    if not candidate.is_file() or candidate.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('Файл изображения отсутствует или превышает 128 МБ')
    return candidate.read_bytes()


def externalize_images(soup, source: Path, assets_dir: Path, *, resource_root: Path | None = None) -> dict:
    """Replace document image sources with deterministic relative asset paths."""
    assets, warnings, seen = [], [], set()
    for image in soup.find_all('img'):
        value = str(image.get('src', '')).strip()
        image.attrs.pop('srcset', None)
        if not value:
            continue
        try:
            record = store_image_bytes(_read_reference(value, source, resource_root), assets_dir,
                                       name=image.get('alt', '') or 'изображение')
            image['src'] = record['path']
            # Intrinsic crop pixels belong to the asset manifest. Existing
            # width/height/style describe the document's display geometry.
            warnings.extend(record.pop('warnings'))
            if record['path'] not in seen:
                assets.append(record)
                seen.add(record['path'])
        except ValueError as exc:
            image.attrs.pop('src', None)
            image['data-image-unavailable'] = 'true'
            if not image.get('alt'):
                image['alt'] = 'Изображение недоступно'
            warnings.append(str(exc))
    return dict(assets=assets, warnings=list(dict.fromkeys(warnings)))
