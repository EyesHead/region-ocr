"""Attach positioned PDF or OCR text to a verified OpenCV grid with merged cells."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from math import isfinite
import logging

from docling_pdf_cell_geometry import Cell, Table, analyze_page

from local_web.fast_scan_tables import find_grids


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionedTable:
    bbox: tuple
    cells: tuple
    words: tuple


def transform_grid(grid, *, x=0, y=0, sx=1, sy=1):
    def moved(box):
        l,t,r,b = box
        return l*sx+x,t*sy+y,r*sx+x,b*sy+y

    return replace(grid, bbox=moved(grid.bbox), mask_bbox=moved(grid.mask_bbox),
        x_grid=tuple(n*sx+x for n in grid.x_grid), y_grid=tuple(n*sy+y for n in grid.y_grid),
        cells=tuple(replace(cell, bbox=moved(cell.bbox),
                            blocks=tuple(replace(block, bbox=moved(block.bbox)) for block in cell.blocks))
                    for cell in grid.cells))


def merged_grids(image, candidates=None):
    if candidates is None:
        candidates = []
        find_grids(image, candidates)
    recovered = []
    pad = max(12, min(image.size)//65)
    for left, top, right, bottom in candidates:
        x,y = max(0,left-pad),max(0,top-pad)
        with image.crop((x,y,min(image.width,right+pad),min(image.height,bottom+pad))) as scope:
            for item in analyze_page(scope, cell_contents=False):
                if item.rows < 2 or item.columns < 2:
                    continue
                recovered.append(transform_grid(item, x=x, y=y))
    return _outer_grids(recovered)


def _intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _overlap_ratio(a,b):
    area = min(_intersection(a,a),_intersection(b,b))
    return _intersection(a,b)/area if area else 0.


def _covered(box, bounds):
    area = _intersection(box, box)
    return area > 0 and any(_intersection(box, other) > .7 * area for other in bounds)


def _outer_grids(grids):
    # A ruled shape inside a measured cell shares the surrounding table's words.
    keep = []
    for grid in sorted(grids, key=lambda g: _intersection(g.bbox, g.bbox), reverse=True):
        if not any(_overlap_ratio(grid.bbox, other.bbox) > .75 for other in keep):
            keep.append(grid)
    return sorted(keep, key=lambda g: (g.bbox[1], g.bbox[0]))


class _CellIndex:
    def __init__(self, grid):
        self.grid = grid
        self.slots = {(r, c): (cell.row, cell.column)
                      for cell in grid.cells
                      for r in range(cell.row, cell.row + cell.rowspan)
                      for c in range(cell.column, cell.column + cell.colspan)}

    def at(self, x, y):
        grid = self.grid
        if not (grid.x_grid[0] <= x <= grid.x_grid[-1] and grid.y_grid[0] <= y <= grid.y_grid[-1]):
            return None
        col = min(bisect_right(grid.x_grid, x) - 1, grid.columns - 1)
        row = min(bisect_right(grid.y_grid, y) - 1, grid.rows - 1)
        return self.slots.get((row, col))


def native_merged_tables(image, words, page_width, page_height, *, covered=(), unresolved=None):
    sx, sy = image.width / page_width, image.height / page_height
    measured = []
    find_grids(image, measured)
    pixel_covered = [(l*sx,t*sy,r*sx,b*sy) for l,t,r,b in covered]
    pending = [box for box in measured if not any(_overlap_ratio(box,grid) > .7 for grid in pixel_covered)]
    result = []
    assigned = set()
    for grid in merged_grids(image, pending):
        owners = defaultdict(list)
        cells = _CellIndex(grid)
        for word in words:
            if id(word) in assigned:
                continue
            left, top, right, bottom = word[:4]
            owner = cells.at((left+right)*sx/2, (top+bottom)*sy/2)
            if owner is not None:
                owners[owner].append(word)
                assigned.add(id(word))
        l,t,r,b = grid.bbox
        result.append(PositionedTable((l/sx, t/sy, r/sx, b/sy), grid.cells,
                                      tuple((key, tuple(value)) for key, value in owners.items())))
    return result


def _simple_grid(grid, scale=(1., 1.)):
    xs = tuple(float(x / grid.scale_x) * scale[0] for x in grid.xs)
    ys = tuple(float(y / grid.scale_y) * scale[1] for y in grid.ys)
    bbox = xs[0], ys[0], xs[-1], ys[-1]
    cells = tuple(Cell(row, col, 1, 1, (left, top, right, bottom), ())
                  for row, (top, bottom) in enumerate(zip(ys, ys[1:]))
                  for col, (left, right) in enumerate(zip(xs, xs[1:])))
    return Table(bbox, len(ys)-1, len(xs)-1, cells, xs, ys, bbox)


def legacy_scan_grids(image, covered=()):
    candidates = []
    grids = []
    for simple in find_grids(image, candidates):
        grid = _simple_grid(simple)
        if not _covered(grid.bbox, covered):
            grids.append(grid)
    known = [*covered, *(grid.bbox for grid in grids)]
    pending = [box for box in candidates if not _covered(box, known)]
    if pending:
        try:
            for grid in merged_grids(image, pending):
                if not _covered(grid.bbox, covered):
                    grids.append(grid)
        except Exception as error:
            _LOG.warning('Не удалось разобрать границы ячеек (%s)', error)
    return _outer_grids(grids)


def _line_fragments(line, tokens):
    """Map adjacent words with one owner to slices of the original RapidOCR line."""
    groups = []
    compact_words = []
    consumed = 0
    for text, owner, box in tokens:
        compact_word = ''.join(text.split())
        compact_words.append(compact_word)
        consumed += len(compact_word)
        if groups and owner == groups[-1][0]:
            old = groups[-1][2]
            box = (min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))
            groups[-1] = owner, consumed, box
        else:
            groups.append((owner, consumed, box))
    if len(groups) == 1:
        owner, _, box = groups[0]
        yield owner, len(line)-len(line.lstrip()), len(line.rstrip()), box
        return
    # Boxed tokens normally reproduce the complete source string. If the OCR
    # skipped punctuation or changed a token, align the boundary between cells
    # while retaining every source character exactly once.
    positions = [i for i, char in enumerate(line) if not char.isspace()]
    measured = ''.join(compact_words)
    source = ''.join(line[i] for i in positions)
    cuts = [end for _, end, _ in groups]
    if measured != source:
        opcodes = SequenceMatcher(None, measured, source).get_opcodes()
        word_breaks = [i for i in range(1, len(positions)) if positions[i] > positions[i-1] + 1]
        for i, cut in enumerate(cuts[:-1]):
            for operation, a, b, c, d in opcodes:
                if a < cut <= b:
                    mapped = round(c + (cut-a) * (d-c) / (b-a))
                    if operation == 'replace' and cut < b:
                        gaps = (gap for gap in word_breaks if c < gap < d)
                        mapped = min(gaps, key=lambda gap: abs(gap-mapped), default=mapped)
                    cuts[i] = mapped
                    break
    cuts[-1] = len(source)
    start = 0
    for (owner, _, box), end in zip(groups, cuts, strict=True):
        if end > start:
            yield owner, positions[start], positions[end-1] + 1, box
        start = end


def scan_grid_blocks(lines, word_rows, line_boxes, physical_grids, legacy_grids=()):
    """Fill measured cells from one page OCR, including lines that cross a table boundary."""
    grids = physical_grids or legacy_grids
    if physical_grids and legacy_grids:
        grids = _outer_grids([*physical_grids, *legacy_grids])
    physical_ids = {id(grid) for grid in physical_grids}
    indices = [_CellIndex(grid) for grid in grids]
    cell_parts = [defaultdict(list) for grid in grids]
    start_at = [None] * len(grids)
    events = []

    def locate(box):
        x, y = (box[0]+box[2])/2, (box[1]+box[3])/2
        for i, index in enumerate(indices):
            owner = index.at(x, y)
            if owner is not None:
                return i, owner
        return None

    for n, line in enumerate(lines):
        if not line.strip():
            continue
        tokens = []
        words = word_rows[n] if n < len(word_rows) else ()
        for text, x, y, w, h in words:
            if not text.strip() or not all(map(isfinite, (x, y, w, h))) or w <= 0 or h <= 0:
                continue
            box = float(x), float(y), float(x+w), float(y+h)
            tokens.append((text, locate(box), box))
        if not tokens and n in line_boxes:
            box = line_boxes[n]
            tokens.append((line, locate(box), box))
        if not any(owner is not None for _, owner, _ in tokens):
            events.append(((n, 0), {'text': line, 'line_id': n}))
            continue
        for owner, start, end, box in _line_fragments(line, tokens):
            if owner is None:
                events.append(((n, start), {'text': line[start:end], 'span': (start,end),
                                             'line_id': n, 'bbox': box}))
                continue
            i, cell = owner
            if start_at[i] is None:
                start_at[i] = n, start
            cell_parts[i][cell].append((n, start, end))

    for i, grid in enumerate(grids):
        parts = cell_parts[i]
        cells = [(cell.row, cell.column, cell.rowspan, cell.colspan,
                  ' '.join(lines[n][a:b] for n, a, b in parts.get((cell.row, cell.column), ())))
                 for cell in grid.cells]
        owned = {n for entries in parts.values() for n, _, _ in entries}
        item = {'cell_parts': dict(parts), 'bbox': grid.bbox, 'line_ids': tuple(sorted(owned)),
                'cell_boxes': {(cell.row, cell.column): cell.bbox for cell in grid.cells}}
        if id(grid) not in physical_ids and all(rs == cs == 1 for _, _, rs, cs, _ in cells):
            rows = [[''] * grid.columns for _ in range(grid.rows)]
            for row, col, _, _, text in cells:
                rows[row][col] = text
            item['rows'] = rows
        else:
            item['table'] = {'rows': grid.rows, 'columns': grid.columns, 'cells': cells}
        if id(grid) in physical_ids:
            item['grid'] = grid
        # A blank form still has a position relative to the surrounding prose.
        if start_at[i] is None:
            _, top, right, bottom = grid.bbox
            after = next((n for n, (x0, y0, _, y1) in line_boxes.items()
                          if y0 >= bottom or (y1 > top and x0 >= right)), len(lines))
            start_at[i] = after, -1
        events.append((start_at[i], item))
    return [item for _, item in sorted(events, key=lambda event: event[0])]
