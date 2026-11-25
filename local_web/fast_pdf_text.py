"""Read a usable PDF text layer once, keeping word positions for the fast export."""

from contextlib import contextmanager
from functools import lru_cache
import math
import os
import re
import unicodedata

from local_web.fast_formulas import EQUATION_NUMBER, formula_evidence, ordinary_text

MIN_TEXT_LENGTH = 30
MIN_ALNUM_RATIO = 0.60
_TABLE_TITLE = re.compile(r'^\s*(?:таблица|табл\.|table)\s*(?:[А-ЯA-Z][.\-]?)?\s*\d', re.I)
# UTF-8 Cyrillic decoded as Windows-1251 or Latin-1 produces these repeating pairs.
_CONTINUATIONS = bytes(range(0x80, 0xc0))
_BROKEN_RUN = re.compile(
    r'(?:[РС][' + re.escape(_CONTINUATIONS.decode('cp1251', errors='ignore')) + r']){3,}|'
    r'(?:[ÐÑ][' + re.escape(_CONTINUATIONS.decode('latin1') + _CONTINUATIONS.decode('cp1252', errors='ignore')) +
    r']){3,}|(?i:\(cid:\d+\))')


@lru_cache(maxsize=2048)
def _letter_or_digit(ch):
    return ch.isdecimal() or (ch.isalpha() and
                              (ch.isascii() or unicodedata.name(ch, '').startswith(('CYRILLIC ', 'LATIN '))))


def usable_text(text, min_text_length=MIN_TEXT_LENGTH):
    compact = ''.join(text.split())
    count = len(compact)
    if count < min_text_length or not count or compact.count('\ufffd') > 0.03 * count:
        return False
    if sum(map(_letter_or_digit, compact)) < MIN_ALNUM_RATIO * count:
        return False
    broken = sum(m.end() - m.start() for m in _BROKEN_RUN.finditer(compact))
    return broken < 0.20 * count


@contextmanager
def open_text_document(source):
    document = None
    try:
        import pymupdf

        if isinstance(source, (str, os.PathLike)):
            document = pymupdf.open(os.fspath(source))
        else:
            if isinstance(source, (bytes, bytearray, memoryview)):
                data = bytes(source)
            elif hasattr(source, 'getvalue'):
                data = source.getvalue()
            else:
                position = source.tell()
                try:
                    source.seek(0)
                    data = source.read()
                finally:
                    source.seek(position)
            document = pymupdf.open(stream=data, filetype='pdf')
    except Exception as error:
        # Opening an optional reader can also raise native MuPDF exceptions.
        print(f'PyMuPDF: текстовый слой недоступен ({error}); используем RapidOCR.', flush=True)
    try:
        yield document
    finally:
        if document is not None:
            document.close()


def _formula_rows(rows):
    """MuPDF can emit each typeset operand and operator as its own text line."""
    pending = []
    def finish():
        if len(pending) > 1:
            words = [word for row in pending for word in row]
            if formula_evidence(' '.join(word[0] for word in words), standalone=True):
                return [words]
        return pending

    for row in rows:
        value = ' '.join(word[0] for word in row)
        if ordinary_text(value) or EQUATION_NUMBER.fullmatch(value):
            yield from finish()
            pending = []
            yield row
            continue
        if pending:
            _, x, y, w, h = pending[-1][-1]
            _, nx, ny, _, nh = row[0]
            em = max(h, nh)
            if not (-.35*em <= nx-x-w <= 2.4*em and abs((y+h/2)-(ny+nh/2)) <= .60*em):
                yield from finish()
                pending = []
        pending.append(row)
    yield from finish()


def page_text(document, page_no, page_size):
    """Return the same line/word representation used by the positioned OCR exporter, in PDF points."""
    import pymupdf

    try:
        page = document[page_no - 1]
        flags = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_USE_CID_FOR_UNKNOWN_UNICODE
        words = page.get_text('words', flags=flags)
        rect = page.rect
        if not words or rect.is_empty or rect.is_infinite:
            return None
        rotation = page.rotation_matrix if page.rotation else None
        width, height = page_size
        sx, sy = width / rect.width, height / rect.height
        rows = {}
        for x0, y0, x1, y1, text, block, line, _ in words:
            if rotation is not None:
                x0, y0, x1, y1 = pymupdf.Rect(x0, y0, x1, y1) * rotation
            left, top = max(0., (x0 - rect.x0) * sx), max(0., (y0 - rect.y0) * sy)
            right, bottom = min(width, (x1 - rect.x0) * sx), min(height, (y1 - rect.y0) * sy)
            if not text.strip() or right <= left or bottom <= top:
                continue
            rows.setdefault((block, line), []).append((text, left, top, right - left, bottom - top))
    except (pymupdf.mupdf.FzErrorBase, OSError, RuntimeError, ValueError, IndexError) as error:
        print(f'PyMuPDF: страница {page_no} ({error}); используем RapidOCR.', flush=True)
        return None

    word_rows = list(_formula_rows(rows.values()))
    lines = [' '.join(word[0] for word in row) for row in word_rows]
    if not usable_text(' '.join(lines)):
        return None
    boxes = [(min(w[1] for w in row), min(w[2] for w in row),
              max(w[1] + w[3] for w in row), max(w[2] + w[4] for w in row), text, 1.0)
             for text, row in zip(lines, word_rows)]
    return dict(source_lines=lines, words=boxes, word_rows=word_rows,
                blocks=[{'line_id': index, 'text': text} for index, text in enumerate(lines)])


def _coverage_rows(rectangles, width, height):
    """Union of rectangles on a 48×48 grid in PDF coordinates; no pixels are decoded."""
    rows = [0] * 48
    for l, t, r, b in rectangles:
        left = max(0, min(48, math.floor(48 * l / width)))
        right = max(left, min(48, math.ceil(48 * r / width)))
        mask = ((1 << (right - left)) - 1) << left
        for row in range(max(0, math.floor(48 * t / height)), min(48, math.ceil(48 * b / height))):
            rows[row] |= mask
    return rows


def covers_scanned_body(reading, objects, width, height):
    """A header, stamp or caption on its own does not transcribe a large underlying scan."""
    area = width * height
    if not objects or sum((r-l)*(b-t) for l,t,r,b in objects) < .25 * area:
        return True
    raster = _coverage_rows(objects, width, height)
    raster_area = sum(row.bit_count() for row in raster)
    if raster_area < .25 * 48**2:
        return True
    # A moderate inset can contain the whole scanned body; a narrow banner cannot.
    if raster_area < .45 * 48**2 and sum(bool(row) for row in raster[5:-5]) < 13:
        return True

    words = ((x, y, x+w, y+h) for row in reading['word_rows'] for _, x, y, w, h in row)
    text = _coverage_rows(words, width, height)
    # Exclude running headers and footers. Require measured lines at several heights:
    # a single diagonal watermark can have a bounding box almost as large as the page.
    bands = {int(12 * (t+b) / (2*height)) for _,t,_,b,_,_ in reading['words']
             if .10 * height < (t+b)/2 < .90 * height}
    if len(bands) < 3:
        return False
    overlap = [(a & b).bit_count() for a, b in zip(raster, text)]
    first = next(i for i, row in enumerate(raster) if row)
    last = max(i for i, row in enumerate(raster) if row)
    inset = (last - first + 1) // 4
    covered = [i for i in range(5, 43) if overlap[i]]
    if (covered and covered[-1]-covered[0]+1 >= .35*(last-first+1)
            and sum(overlap[5:-5]) >= .035 * raster_area
            and sum(overlap[first+inset:last-inset+1]) >= .015 * raster_area):
        return True
    # A large illustration next to a substantial digital text column is also a native page.
    outside = sum((b & ~a).bit_count() for a, b in zip(raster[5:-5], text[5:-5]))
    return raster_area < .80 * 48**2 and outside >= .12 * raster_area


def raster_table_hint(page, reading):
    if sum((r-l)*(b-t) for l,t,r,b in page.objects) < .10 * page.width * page.height:
        return False
    if any(_TABLE_TITLE.match(line) for line in reading['source_lines']):
        return True
    # An uncaptioned table may contain only names. Look for a repeated column start across rows.
    bands = {}
    for row in reading['word_rows']:
        for text, x, y, w, h in row:
            band = math.floor((y + h/2) / 5)
            bands.setdefault(band, []).append((x, x+w))
    starts = {}
    for band, fields in bands.items():
        fields.sort()
        for (_, right), (left, _) in zip(fields, fields[1:]):
            if left - right > .035 * page.width:
                column = round(left / (.035 * page.width))
                starts.setdefault(column, set()).add(band)
                if len(starts[column]) >= 3:
                    return True
    return False


def bind_tables(page, reading, *, balanced=False, infer_partial=False):
    """Bind source words to vector grids, or inspect pixels when a bitmap may contain a table."""
    raster_tables = []
    physical = page.grids_for_text(native=True)
    if (not physical or balanced and page.reasons) and page.possible_raster_table:
        from local_web.fast_ruled_tables import _simple_grid, legacy_scan_grids, transform_grid
        from local_web.fast_scan_tables import find_grids

        image = page.image()
        sx, sy = page.pixel_scale
        if balanced:
            covered = [grid.scaled(1/sx, 1/sy).bbox for grid in physical]
            try:
                raster_tables = [transform_grid(grid, sx=sx, sy=sy) for grid in legacy_scan_grids(image, covered)]
            except Exception as error:
                if not physical:
                    raise
                page.reasons.append(f'legacy_grid_error ({type(error).__name__})')
            if not physical and not raster_tables and infer_partial:
                from local_web.balanced_table_grid import partial_raster_grids

                word_pixels = [[(text, x/sx, y/sy, w/sx, h/sy) for text, x, y, w, h in row]
                               for row in reading['word_rows']]
                physical = [grid.scaled(sx, sy) for grid in partial_raster_grids(image, word_pixels)]
        else:
            raster_tables = [_simple_grid(grid, (sx, sy)) for grid in find_grids(image)]
    if physical or raster_tables:
        from local_web.fast_ruled_tables import scan_grid_blocks

        boxes = {i: word[:4] for i, word in enumerate(reading['words'])}
        reading['blocks'] = scan_grid_blocks(reading['source_lines'], reading['word_rows'], boxes,
                                             physical, raster_tables)
