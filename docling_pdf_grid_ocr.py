"""Batch literal text reads for cells already owned by a measured PDF grid."""

from hashlib import sha256
from html import escape
from math import ceil, floor, isfinite
import os
import re

from docling_pdf_cpu_route import POSSIBLE_MATH, adaptive_enabled, occular_reader


POSITION = ('row', 'column', 'row_span', 'column_span')
MAX_CELL_PIXELS = 2_000_000
MAX_BATCH_PIXELS = 4_000_000
DECIMAL = re.compile(r'\d+(?:[.,]\d+)*')
NUMBERED = re.compile(r'^\s*\d{1,2}\s+(?!мм\b|см\b|м\b|кг\b|кН\b)[А-Яа-яЁё]{3}', re.I)
SIGNED_NUMBER = re.compile(r'(?<!\w)[−+–-]\s*\d')


def grid_batch_enabled(args):
    return (adaptive_enabled(getattr(args, 'device', None))
            and os.environ.get('REGION_OCR_GRID_TABLES', '0') == '1')


def _cell_pixels(image, region, bounds):
    if len(bounds) != 4 or any(not isfinite(n) for n in bounds):
        return None
    if (not (region[0] <= bounds[0] < bounds[2] <= region[2])
            or not (region[1] <= bounds[1] < bounds[3] <= region[3])):
        return None
    sx = image.width / (region[2] - region[0])
    sy = image.height / (region[3] - region[1])
    pixels = (max(0, ceil((bounds[0] - region[0] + .7) * sx)),
              max(0, ceil((bounds[1] - region[1] + .7) * sy)),
              min(image.width, floor((bounds[2] - region[0] - .7) * sx)),
              min(image.height, floor((bounds[3] - region[1] - .7) * sy)))
    return pixels if pixels[2] - pixels[0] >= 6 and pixels[3] - pixels[1] >= 6 else None


def source_cell_ink(crop):
    """Exclude residual frame ink at an edge and keep interior strokes."""
    import cv2
    import numpy as np

    with crop.convert('L') as gray:
        mask = (np.asarray(gray) < 180).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    for index, (x, y, w, h, _) in enumerate(stats[1:], 1):
        horizontal = (y <= 2 or y + h >= crop.height - 2) and w >= .8 * crop.width and h <= 3
        vertical = (x <= 2 or x + w >= crop.width - 2) and h >= .8 * crop.height and w <= 3
        if horizontal or vertical:
            mask[labels == index] = 0
    return mask


def _line_samples(crop, ink):
    import cv2
    import numpy as np

    occupied = ink.sum(axis=1) >= max(2, crop.width * .004)
    edges = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
    bands = [[int(a), int(b)] for a, b in zip(edges[::2], edges[1::2], strict=True) if b - a >= 2]
    if not bands or len(bands) > 16:
        return None
    typical = np.median([b - a for a, b in bands])
    merged = []
    for top, bottom in bands:
        if (merged and top - merged[-1][1] < typical * .6
                and min(bottom - top, merged[-1][1] - merged[-1][0]) < typical * .45):
            merged[-1][1] = bottom
        else:
            merged.append([top, bottom])

    if any(b - a < 3 or b - a > typical * 2.5 for a, b in merged):
        return None
    covered = np.zeros_like(ink)
    rgb = np.array(crop.convert('RGB'))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Keep interior source pixels; the same frame-removal mask as legacy OCR
    # identifies rail ink that should never be read as a numeral.
    rgb[(gray < 180) & ~ink] = 255
    lines, counts = [], []
    for i, (top, bottom) in enumerate(merged):
        upper = (top + merged[i-1][1]) // 2 if i else 0
        lower = (bottom + merged[i+1][0]) // 2 if i+1 < len(merged) else ink.shape[0]
        top, bottom = max(upper, top - 2), min(lower, bottom + 2)
        xs = np.flatnonzero(ink[top:bottom].any(axis=0))
        if not len(xs):
            return None
        left, right = int(xs[0]), int(xs[-1]) + 1
        if right - left < 1.4 * (bottom - top):
            return None
        line = np.ascontiguousarray(rgb[top:bottom, left:right])
        mask = ink[top:bottom, left:right].astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        glyphs = sum(area >= 3 and height >= 3 for _, _, _, height, area in stats[1:])
        if glyphs < 3 or any(area >= 12 and (w >= .85 * mask.shape[1] or h > 2.3 * typical)
                            for _, _, w, h, area in stats[1:]):
            return None
        covered[top:bottom, left:right] = True
        lines.append(line)
        counts.append(glyphs)
    if (ink & ~covered).sum() > max(2, .01 * ink.sum()):
        return None
    return lines, counts


def _literal_text(values, checks, glyphs, role, prior):
    texts = [text.strip() for text, _ in values]
    if (texts != [text.strip() for text, _ in checks]
            or any(not isfinite(score) or score < .87 for _, score in (*values, *checks))):
        return None
    for text, count in zip(texts, glyphs, strict=True):
        length = sum(not c.isspace() for c in text)
        if (not text or POSSIBLE_MATH.search(text) or '\ufffd' in text
                or re.search(r'\s-\s', text)
                or length < .65 * count or length > 2.1 * count):
            return None
    literal = ' '.join(texts)
    if not re.search(r'[А-Яа-яЁё]{3}', literal):
        return None
    prior_numbers = DECIMAL.findall(prior)
    if prior_numbers and ' '.join(prior.split()) != ' '.join(literal.split()):
        return None  # Equal digits alone cannot prove a sign or unit.
    for line in texts:
        numbers = DECIMAL.findall(line)
        if numbers and (role == 'header' or (not prior_numbers and (
                len(numbers) != 1 or SIGNED_NUMBER.search(line) or NUMBERED.match(line) is None))):
            return None
    return texts


def _padded_views(lines):
    import numpy as np

    return ([np.pad(line, ((4, 4), (4, 4), (0, 0)), constant_values=255) for line in lines],
            [np.pad(line, ((6, 6), (7, 4), (0, 0)), constant_values=255) for line in lines])


def read_confirmed_cells(image, region, candidates, page, *, line_ink, reader=None):
    """Read line batches from explicitly textual cells without guessing ownership."""
    import numpy as np

    prepared, first, second = [], [], []
    batch_pixels = 0
    for cell, role, prior in candidates:
        box = cell.get('bbox', ())
        pixels = _cell_pixels(image, region, box)
        if not pixels or (pixels[2] - pixels[0]) * (pixels[3] - pixels[1]) > MAX_CELL_PIXELS:
            continue
        with image.crop(pixels) as crop:
            ink = np.asarray(line_ink(crop), dtype=bool)
            sample = _line_samples(crop, ink)
            if sample is None:
                continue
            lines, counts = sample
            sample_pixels = sum(2 * (line.shape[0] + 12) * (line.shape[1] + 11) for line in lines)
            if batch_pixels + sample_pixels > MAX_BATCH_PIXELS:
                continue
            batch_pixels += sample_pixels
            start = len(first)
            a, b = _padded_views(lines)
            first.extend(a)
            second.extend(b)
            digest = sha256(crop.tobytes()).hexdigest()
            prepared.append((cell, role, prior, pixels, counts, start, len(first), digest))
    if not prepared:
        return {}

    reader = reader or occular_reader()
    texts, scores = reader.recognize_lines(first + second)
    answers = list(zip(texts, scores, strict=True))
    if len(answers) != len(first) + len(second):
        raise ValueError('Occular returned an incomplete batch')
    offset = len(first)
    proposals = {}
    for cell, role, prior, pixels, counts, start, end, digest in prepared:
        values, checks = answers[start:end], answers[start+offset:end+offset]
        literal = _literal_text(values, checks, counts, role, prior)
        if literal is None:
            continue
        key = tuple(cell[k] for k in POSITION)
        payload = '<br/>'.join(escape(line) for line in literal)
        proposals[key] = (payload, dict(method='occular_on_verified_source_cell',
            page=page, row=key[0], column=key[1], source_bbox=list(cell['bbox']),
            source_pixel_bbox=list(pixels), image_sha256=digest,
            line_count=len(literal), min_confidence=round(min(s for _, s in (*values, *checks)), 4)))
    return proposals


def read_confirmed_lines(crops, *, line_ink, reader=None):
    """Keep individual literal lines when neighboring lines need Surya."""
    import numpy as np

    indexes, lines, counts = [], [], []
    for index, crop in enumerate(crops):
        sample = _line_samples(crop, np.asarray(line_ink(crop), dtype=bool))
        if sample is None or len(sample[0]) != 1:
            continue
        indexes.append(index)
        lines.append(sample[0][0])
        counts.append(sample[1][0])
    if not lines:
        return {}
    first, second = _padded_views(lines)
    reader = reader or occular_reader()
    texts, scores = reader.recognize_lines(first + second)
    answers = list(zip(texts, scores, strict=True))
    if len(answers) != 2 * len(lines):
        raise ValueError('Occular returned an incomplete line batch')
    confirmed = {}
    for i, (index, count) in enumerate(zip(indexes, counts, strict=True)):
        result = _literal_text([answers[i]], [answers[i + len(lines)]], [count], 'text', '')
        if result:
            confirmed[index] = escape(result[0])
    return confirmed

