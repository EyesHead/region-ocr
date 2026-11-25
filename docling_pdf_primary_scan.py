"""Reuse a complete positioned primary table on independently measured scan cells."""
from bisect import bisect_right
from collections import Counter
from hashlib import sha256
import math
import re

from bs4 import BeautifulSoup
import cv2

from docling_pdf_logical_rows import _same_region
from docling_pdf_primary import _key
from docling_pdf_table_layout import grid_ink


def measured_header_cells(cells, columns, profiles):
    """Find consecutive primary header rows with the same physical cells and spans."""
    physical = {}
    for cell in cells:
        physical.setdefault(cell['row'], set()).add(_key(cell))
    proposals = {}
    for profile in profiles:
        if profile['columns'] != columns:
            return []
        rows = {}
        for cell in profile['cells']:
            rows.setdefault(cell['row'], []).append(cell)
        headings = []
        for row in range(profile['rows']):
            current = rows.get(row, [])
            if (not current or not all(c['header'] for c in current)
                    or {_key(c) for c in current} != physical.get(row)):
                break
            headings.extend(current)
        proposals[tuple((_key(c), c['html']) for c in headings)] = headings
    return next(iter(proposals.values())) if len(proposals) == 1 else []


def _text_components(image, region, xs, ys, profile):
    """Check ink throughout the measured cells without extracting a cell image."""
    source = grid_ink(image, region, xs)
    if source is None:
        return None
    x_pixels = [(x - region[0]) * source['sx'] for x in xs]
    y_pixels = [(y - region[1]) * source['sy'] for y in ys]
    glyph_height = source['glyph_height'] * source['sy']
    slots = {}
    for cell in profile['cells']:
        key = row, col, rows, cols = _key(cell)
        for r in range(row, row + rows):
            for c in range(col, col + cols):
                slots[r, c] = key
    _, _, components, _ = cv2.connectedComponentsWithStats(source['nongrid'], 8)
    counts = Counter()
    for x, y, width, height, _ in components[1:]:
        row = bisect_right(y_pixels, y + height / 2) - 1
        col = bisect_right(x_pixels, x + width / 2) - 1
        key = slots.get((row, col))
        if key is None:
            if x + width > x_pixels[0] and x < x_pixels[-1] and y + height > y_pixels[0] and y < y_pixels[-1]:
                return None
            continue
        r, c, rs, cs = key
        if (x < x_pixels[c] - 1 or x + width > x_pixels[c + cs] + 1
                or y < y_pixels[r] - 1 or y + height > y_pixels[r + rs] + 1
                or width > 2.5 * glyph_height or height > 1.65 * glyph_height):
            return None
        counts[key] += 1
    for cell in profile['cells']:
        value = re.sub(r'\s+', '', cell['text'])
        count = counts[_key(cell)]
        # Touching letters and diacritics change component counts in prose.
        # For simple digits and range signs require a separate component for
        # each printed character. Every detached mark participates in the veto.
        if re.fullmatch(r'[\d.,+—–−/%-]+', value):
            if count != len(value):
                return None
        elif not .5 * len(value) <= count <= 2.5 * len(value):
            return None
    return counts


def reuse_scan_primary(cells, xs, ys, image, region, page, records, reader, api, *, direction_conflict):
    bounds = [xs[0], ys[0], xs[-1], ys[-1]]
    packets = set()
    for record in records:
        box = record.get('bbox')
        if (record.get('page_no') != page or not isinstance(box, (list, tuple)) or len(box) != 4
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                or api.overlap_ratio(bounds, box) <= .6):
            continue
        if record.get('label') != 'Table' or not _same_region(box, bounds):
            return None
        packets.add((record.get('html', ''), tuple(box)))
    if len(packets) != 1:
        return None
    raw, record_box = next(iter(packets))
    parsed = BeautifulSoup(raw, 'html.parser')
    table = parsed.find('table')
    text_tags = {'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'p', 'br', 'strong', 'em', 'sub', 'sup', 'caption'}
    if table is None or any(node.name not in text_tags for node in table.find_all(True)):
        return None
    table.extract()
    if parsed.get_text(strip=True) or parsed.find(True):
        return None
    try:
        html = api.clean_table_html(str(table))
        profile = api.table_profile(html)
    except (ValueError, TypeError):
        return None
    physical = {_key(c): c for c in cells}
    if (profile['issues'] or (profile['rows'], profile['columns']) != (len(ys) - 1, len(xs) - 1)
            or len(profile['cells']) != len(physical) or set(physical) != {_key(c) for c in profile['cells']}):
        return None
    if any(not reader.is_page_scan(obj, bounds) for obj in reader.graphics(page, bounds)):
        return None
    from docling_pdf_primary_cells import GRAPHIC_HEADING
    if any(c['header'] and GRAPHIC_HEADING.search(c['text']) for c in profile['cells']):
        return None
    counts = _text_components(image, region, xs, ys, profile)
    if counts is None:
        return None
    reading = dict(method='complete_primary_table_on_measured_scan_cells', page=page,
                   source_record_sha256=sha256(raw.encode()).hexdigest(), source_bbox=list(record_box))
    proof = dict(reading, rows=profile['rows'], columns=profile['columns'], cells=len(physical))
    mapped = []
    for cell in profile['cells']:
        key = _key(cell)
        mapped.append(dict(cell, bbox=list(physical[key]['bbox']), source_text_unreliable=True,
                           scan_layout_has_ink=bool(counts[key]), primary_ocr=dict(
                               reading, row=key[0], column=key[1], row_span=key[2], column_span=key[3])))
    api.diagnostic_event('COMPLETE_PRIMARY_SCAN_TABLE_REUSED', **proof)
    header_rows = min((c['row'] for c in mapped if not c['header']), default=profile['rows'])
    return html, dict(basis='raster_cell_rules', bbox=bounds, x_grid=xs, y_grid=ys,
                      source_grid_verified=True, header_rows=header_rows, cells=mapped,
                      primary_scan_grid=proof, retained_cell_images=[], restored_cell_math=[],
                      empty_cell_rule_proofs=[], pending_cell_ocr=[], source_roman_references=[],
                      hidden_ocr_direction_conflict=direction_conflict, page_scan_transcription_required=True,
                      glyph_coverage=0., text_method='primary_surya_on_measured_scan_grid')
