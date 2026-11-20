"""Read tall prose cells from their measured source lines after a block failure."""

from math import ceil

from bs4 import BeautifulSoup
from PIL import ImageOps


def _threshold(image):
    import cv2
    import numpy as np

    with image.convert('L') as gray:
        return cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]


def measured_cell_interior(image):
    """Exclude verified enclosing rails, returning crop-relative bounds and proof."""
    import cv2

    from docling_pdf_cell_crop import cell_content_crop
    from docling_pdf_table_content import _coverage_without_enclosing_rules

    bounds = (0, 0, image.width, image.height)
    interior = cell_content_crop(image, bounds, bounds)
    if interior is None:
        return None
    pixels, _, proof = interior
    left, top, right, bottom = pixels
    ink = _threshold(image)
    outside = _coverage_without_enclosing_rules(ink, physical_cell=True)
    outside[top:bottom, left:right] = 0
    _, _, components, _ = cv2.connectedComponentsWithStats(outside, 8)
    runs = proof['edge_rule_pixel_runs']

    rail_count = 0
    for x, y, width, height, area in components[1:]:
        if area < 3:
            continue
        vertical_edge = (runs[0] is not None and x + width <= left
                         or runs[2] is not None and x >= right)
        horizontal_edge = (runs[1] is not None and y + height <= top
                           or runs[3] is not None and y >= bottom)
        if not (vertical_edge and height >= .75 * image.height
                or horizontal_edge and width >= .75 * image.width):
            return None
        rail_count += 1
    # The render/measurement rounding can leave one isolated scan row on
    # the inside of a rail. A full-width stroke within four source pixels
    # of the measured boundary and separated from all content by two clear
    # rows is still part of the enclosing grid, not body content.
    fringe = []
    if runs[0] is not None or runs[2] is not None:
        if (top <= 4 and bottom - top > 5 and ink[top, left:right].all()
                and not ink[top + 1:top + 3, left:right].any()):
            fringe.append(top)
            top += 1
        if (image.height - bottom <= 4 and bottom - top > 5 and ink[bottom - 1, left:right].all()
                and not ink[bottom - 3:bottom - 1, left:right].any()):
            bottom -= 1
            fringe.append(bottom)
    pixels = (left, top, right, bottom)
    return pixels, dict(proof, source_pixel_bbox=list(pixels),
                        source_content_bbox=list(pixels), isolated_edge_rule_rows=fringe,
                        excluded_rail_component_count=rail_count + len(fringe))


def _text_bands(image):
    """Confirm ordinary source line bands before sending them to Surya."""
    import cv2
    import numpy as np

    from docling_pdf_primary_cells import _ink_view

    mask = _ink_view(image).astype(bool)
    occupied = mask.sum(axis=1) >= max(2, image.width * .004)
    edges = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
    bands = [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True) if b - a >= 3]
    if not 2 <= len(bands) <= 36:
        return None
    typical = float(np.median([end - start for start, end in bands]))
    lines = []
    for start, end in bands:
        if (lines and start - lines[-1][1] < typical * .6
                and min(end - start, lines[-1][1] - lines[-1][0]) < typical * .45):
            lines[-1] = (lines[-1][0], end)
        else:
            lines.append((start, end))
    if (len(lines) < 2 or any(end - start > 1.7 * typical for start, end in lines)
            or sum(end - start for start, end in lines) > .65 * image.height):
        return None

    # Use the same source bounds as the legacy exact-line reader and check
    # that no significant connected ink is omitted before invoking it.
    remaining = _threshold(image)
    for start, end in lines:
        xs = np.flatnonzero(mask[start:end].any(axis=0))
        if len(xs) < 3:
            return None
        left, right = int(xs[0]), int(xs[-1]) + 1
        remaining[max(0, start - 6):min(image.height, end + 6),
                  max(0, left - 6):min(image.width, right + 6)] = 0
    _, _, missing, _ = cv2.connectedComponentsWithStats(remaining, 8)
    if any(area >= 3 for _, _, _, _, area in missing[1:]):
        return None
    return lines


UNCONFIRMED_LINE = '<span class="table-cell-review" data-ocr-review="true">[Строка не распознана]</span>'


def _line_views(source, bands):
    """Give each band its own source pixels and no neighboring glyphs."""
    import cv2
    import numpy as np

    from docling_pdf_primary_cells import _ink_view

    mask = _ink_view(source)
    for index, (top, bottom) in enumerate(bands):
        foreground = mask[top:bottom]
        xs = np.flatnonzero(foreground.any(axis=0))
        left, right = max(0, int(xs[0]) - 3), min(source.width, int(xs[-1]) + 4)
        upper = bands[index - 1][1] if index else 0
        lower = bands[index + 1][0] if index + 1 < len(bands) else source.height
        bounds = (left, max(upper, top - 3), right, min(lower, bottom + 3))
        _, _, components, _ = cv2.connectedComponentsWithStats(foreground, 8)
        count = sum(area >= 3 for _, _, _, _, area in components[1:])
        # Some neighboring letters touch or share a component. Give the
        # transcription room for that ink without admitting another phrase.
        max_characters = max(4, ceil(count * 1.24) + 1)
        yield bounds, count, max_characters


def _read_line(image, args, page, position, api, max_characters):
    from docling_pdf_cpu_route import POSSIBLE_MATH
    from docling_pdf_primary_cells import _complete_line_reading

    try:
        reading = _complete_line_reading(args, image, page, position, api, allow_fast=False)
    except RuntimeError as error:
        if not str(error).startswith('Local block request failed after retry:'):
            raise
        return None, 'surya_line_request_failed'
    if reading is None:
        return None, 'no_complete_line_response'
    html, replies, methods = reading
    if len(replies) != 1 or methods != ['surya']:
        return None, 'response_not_bound_to_one_source_line'
    parsed = BeautifulSoup(html, 'html.parser')
    if parsed.find(['img', 'svg', 'table', 'math', 'sup', 'sub', 'input', 'script', 'style', 'iframe', 'object']):
        return None, 'unverified_nontext_line_response'
    text = parsed.get_text(' ', strip=True)
    if not text or POSSIBLE_MATH.search(text) or parsed.select_one('.source-latex'):
        return None, 'unverified_nontext_line_response'
    if sum(not char.isspace() for char in text) > max_characters:
        return None, 'text_longer_than_measured_line_ink'
    return (html, replies[0]), None


def recover_vertical_cell_text(image, args, page, position, box, api):
    """Confirm a rotated printed line against the independent primary table reading."""
    if image.height < image.width * 2 or not getattr(args, 'surya_ocr_model', None):
        return None
    import cv2
    import numpy as np
    from PIL import Image

    interior = measured_cell_interior(image)
    if not interior:
        return None
    pixels, _ = interior
    with image.crop(pixels) as source:
        ink = _threshold(source)
        x, y, width, height = cv2.boundingRect(ink)
        if width < 3 or height < width * 4:
            return None
        stats = cv2.connectedComponentsWithStats(ink, 8)[2][1:]
        letters = stats[stats[:, cv2.CC_STAT_AREA] >= 3]
        if len(letters) < 4 or np.median(letters[:, cv2.CC_STAT_WIDTH]) < width / 3:
            return None
        candidates = {}
        for record in getattr(args, 'surya_records', ()):
            rect = record.get('bbox')
            if (record.get('page_no') != page or record.get('label', '').lower() != 'table' or not rect
                    or not (rect[0] <= box[0] < box[2] <= rect[2] and rect[1] <= box[1] < box[3] <= rect[3])):
                continue
            for cell in BeautifulSoup(record.get('html', ''), 'html.parser').find_all(['td', 'th']):
                if (cell.select_one('[data-latex], .source-latex')
                        or any(tag.name not in {'p', 'span', 'b', 'strong', 'i', 'em'}
                               for tag in cell.find_all(True))):
                    continue
                text = ' '.join(cell.get_text(' ', strip=True).split())
                if text:
                    candidates.setdefault(text, set()).add(cell.decode_contents())
        if not candidates:
            return None
        bounds = (max(0, x-4), max(0, y-4), min(source.width, x+width+4), min(source.height, y+height+4))
        with source.crop(bounds) as strip:
            for turn, angle in ((Image.Transpose.ROTATE_270, 90), (Image.Transpose.ROTATE_90, 270)):
                with strip.transpose(turn) as line:
                    reading, _ = _read_line(line, args, page, position, api, ceil(len(letters) * 1.24) + 1)
                if reading:
                    value = ' '.join(BeautifulSoup(reading[0], 'html.parser').get_text(' ', strip=True).split())
                    variants = candidates.get(value, set())
                    if len(variants) == 1:
                        from docling_pdf_table_content import _safe_markup
                        content = _safe_markup(next(iter(variants)), api)
                        return content, dict(method='rotated_line_and_primary_table_agree', page=page,
                            clockwise=angle, source_bbox=list(box), source_pixel_bbox=list(pixels),
                            response_sha256=reading[1])
    return None


def recover_typed_text_lines(image, args, page, position, api):
    """Read verified prose bands independently and disclose any unconfirmed lines."""
    if image.height < 60:
        return None
    interior = measured_cell_interior(image)
    if interior is None:
        return None
    pixels, evidence = interior
    contents, responses, unresolved = [], [], []
    with image.crop(pixels) as source:
        bands = _text_bands(source)
        if bands is None:
            return None
        for index, (bounds, components, max_characters) in enumerate(_line_views(source, bands), start=1):
            with source.crop(bounds) as line:
                reading, reason = _read_line(line, args, page, position, api, max_characters)
                if reading is None:
                    api.diagnostic_event('TYPED_SOURCE_LINE_RETRY', page=page, cell=position, line=index,
                                         reason=reason, method='same_pixels_alternate_white_margin')
                    # Legacy per-line recovery trims around ink itself.
                    # Extra whitespace changes the visible margins within
                    # that trim allowance while fixing the source pixels.
                    with ImageOps.expand(line, border=(7, 6, 7, 6), fill='white') as padded:
                        reading, reason = _read_line(padded, args, page, position, api, max_characters)
            if reading:
                html, digest = reading
                contents.append(html)
                responses.append(digest)
            else:
                contents.append(UNCONFIRMED_LINE)
                responses.append(None)
                top, bottom = bands[index - 1]
                unresolved.append(dict(line=index, pixel_band=[top + pixels[1], bottom + pixels[1]],
                                       reason=reason, foreground_components=int(components),
                                       maximum_characters=max_characters))
    if all(response is None for response in responses):
        return None
    normalized_bands = [[top + pixels[1], bottom + pixels[1]] for top, bottom in bands]
    proof = dict(method='surya_complete_lines_on_measured_cell', source_interior=evidence,
                 source_line_pixel_bands=normalized_bands, response_sha256=responses,
                 unconfirmed_line_count=len(unresolved), unconfirmed_source_lines=unresolved)
    api.diagnostic_event('TYPED_SOURCE_CELL_LINES_RECOVERED', page=page, cell=position, **proof)
    return '<br/>'.join(contents), proof
