"""Repair a confused Cyrillic list marker with a proven positioned source line."""

from collections import Counter
from math import ceil, floor, isfinite
import re

from bs4 import Comment, NavigableString, Tag

from docling_pdf_text_cells import positioned_text_proof


POSITION = ('row', 'column', 'row_span', 'column_span')
MARKER = re.compile(r'^([абв6])\s*\)\s*([А-Яа-яЁё][\s\S]*)$')
BLOCKS = {'blockquote', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'ol', 'p', 'ul'}
BARRIERS = {'br', 'img', 'input', 'math', 'sub', 'sup', 'svg', 'table'}


def _literal(line):
    match = MARKER.fullmatch(' '.join(line.split()))
    return match.groups() if match else (None, None)


def _line_nodes(cell):
    """Keep line boundaries while retaining the exact node holding the marker."""
    current = []

    def visit(parent):
        for node in parent.children:
            if isinstance(node, NavigableString) and not isinstance(node, Comment):
                current.append(node)
            elif isinstance(node, Tag):
                if node.name in BLOCKS or node.name in BARRIERS:
                    if current:
                        yield tuple(current)
                        current.clear()
                if node.name not in BARRIERS:
                    yield from visit(node)
                if node.name in BLOCKS and current:
                    yield tuple(current)
                    current.clear()

    yield from visit(cell)
    if current:
        yield tuple(current)


def _cell_glyphs(glyphs, box):
    x0, y0, x1, y1 = box
    return [glyph for glyph in glyphs if glyph.get('upright', True)
            and x0 < (glyph['x0'] + glyph['x1']) / 2 < x1
            and y0 < (glyph['top'] + glyph['bottom']) / 2 < y1]


def _marker_proof(line, glyphs, mask, region, box, sx, sy):
    same_line = sorted((g for g in glyphs if g.get('text', '').strip()
                       and abs(g['bottom'] - line['y']) <= .30 * line['size']
                       and line['x0'] <= (g['x1'] + g['x0']) / 2 <= line['x1']),
                       key=lambda g: g['x0'])
    if len(same_line) < 3 or [g['text'] for g in same_line[:2]] != ['б', ')']:
        return None
    marker = same_line[0]
    left = max(0, ceil((marker['x0'] - region[0]) * sx))
    top = max(0, ceil((marker['top'] - region[1]) * sy))
    right = min(mask.shape[1], floor((marker['x0'] + .76 * (marker['x1'] - marker['x0']) - region[0]) * sx))
    bottom = min(mask.shape[0], floor((marker['bottom'] - region[1]) * sy))
    # Proof must cover the actual marker as well as the rest of the line.
    if left >= right or top >= bottom or (mask[top:bottom, left:right] > 0).sum() < 3:
        return None
    line_box = [max(box[0], min(g['x0'] for g in same_line) - 2),
                max(box[1], min(g['top'] for g in same_line) - 2),
                min(box[2], max(g['x1'] for g in same_line) + 2),
                min(box[3], max(g['bottom'] for g in same_line) + 2)]
    return positioned_text_proof(line['html'], same_line, mask, region, line_box, sx, sy)


def repair_table_list_markers(soup, geometry, source_image, region, page, source_reader, api,
                              manifest, *, ref=None):
    """Change only the initial 6 in a source-proved Cyrillic б) item."""
    if (soup.table is None or source_reader is None or source_image is None or not geometry
            or not (geometry.get('source_grid_verified') or geometry.get('positioned_source_grid_verified'))
            or len(region) != 4 or not all(isfinite(v) for v in region)
            or region[0] >= region[2] or region[1] >= region[3]):
        return
    profile = api.table_profile(str(soup.table))
    if profile['issues']:
        return
    nodes = [node for node in soup.table.find_all(['td', 'th'])
             if node.find_parent('table') is soup.table]
    if len(nodes) != len(profile['cells']):
        return
    grid = {tuple(cell[k] for k in POSITION): cell.get('bbox') for cell in geometry.get('cells', [])}
    jobs = []
    for node, position in zip(nodes, profile['cells'], strict=True):
        if node.name != 'td' or node.find(['input', 'math', 'table']):
            continue
        box = grid.get(tuple(position[k] for k in POSITION))
        if not box or len(box) != 4 or not all(isfinite(v) for v in box):
            continue
        if (not region[0] <= box[0] < box[2] <= region[2]
                or not region[1] <= box[1] < box[3] <= region[3]):
            continue
        lines = [(_literal(''.join(map(str, parts))), parts) for parts in _line_nodes(node)]
        marked = [(value, parts) for (kind, value), parts in lines if kind == '6']
        if marked:
            jobs.append((position, box, marked, Counter(value for (_, value), _ in lines)))
    if not jobs:
        return

    import cv2
    import numpy as np

    try:
        source_reader.load_page(page)
    except (OSError, ValueError, RuntimeError):
        return
    with source_image.convert('L') as grayscale:
        mask = cv2.threshold(np.asarray(grayscale), 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    sx = source_image.width / (region[2] - region[0])
    sy = source_image.height / (region[3] - region[1])
    for position, box, marked, repetitions in jobs:
        glyphs = _cell_glyphs(source_reader.chars, box)
        source = [(kind, suffix, line) for line in api.native_cell_lines(glyphs)
                  for kind, suffix in [_literal(line['text'])] if kind]
        for suffix, parts in marked:
            matches = [index for index, (_, tail, _) in enumerate(source) if tail == suffix]
            if len(matches) != 1 or repetitions[suffix] != 1:
                continue
            index = matches[0]
            kind, _, line = source[index]
            before = source[index - 1][0] if index else None
            after = source[index + 1][0] if index + 1 < len(source) else None
            if kind != 'б' or (before != 'а' and after != 'в'):
                continue
            proof = _marker_proof(line, glyphs, mask, region, box, sx, sy)
            if proof:
                marker = next(part for part in parts if part.strip())
                indent = len(marker) - len(marker.lstrip())
                marker.replace_with(NavigableString(marker[:indent] + 'б' + marker[indent + 1:]))
                evidence = dict(page=page, ref=ref, row=position['row'], column=position['column'],
                                from_marker='6)', to_marker='б)', **proof)
                manifest.setdefault('source_table_list_markers', []).append(evidence)
                api.diagnostic_event('SOURCE_TABLE_LIST_MARKER_REPAIRED', **evidence)

