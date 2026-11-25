"""Recover ruled scan tables and their cell contents in original raster coordinates."""

from dataclasses import dataclass
from math import ceil
from statistics import median
from typing import Literal

import cv2
import numpy as np
from PIL import Image


Box = tuple[int, int, int, int]
MAX_PAGE_PIXELS = 16_500_000
MAX_GRID_SLOTS = 4_096
MAX_RULE_BANDS = 256
MAX_CELL_COMPONENTS = 2_048


@dataclass(frozen=True)
class Block:
    kind: Literal['text', 'picture']
    bbox: Box


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    rowspan: int
    colspan: int
    bbox: Box
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Table:
    bbox: Box
    rows: int
    columns: int
    cells: tuple[Cell, ...]
    x_grid: tuple[int, ...]
    y_grid: tuple[int, ...]
    mask_bbox: Box


@dataclass(frozen=True)
class _Part:
    bbox: Box
    area: int
    label: int

    @property
    def width(self):
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self):
        return self.bbox[3] - self.bbox[1]


def _runs(values):
    edges = np.flatnonzero(np.diff(np.r_[False, values, False]))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _axis_bands(rules, minimum, thickness):
    counts = np.count_nonzero(rules, axis=1)
    bands = []
    for start, end in _runs(counts >= minimum):
        if end - start <= thickness:
            bands.append((start, end))
            continue
        # A short underline or stem may be attached to a real rule and widen
        # the projected band. Isolate the thin peak with the longest support.
        local = counts[start:end]
        peaks = _runs(local >= .8 * max(local))
        if len(peaks) == 1 and peaks[0][1] - peaks[0][0] <= thickness:
            bands.append((start + peaks[0][0], start + peaks[0][1]))
    return bands


def _linked_bands(rules, ink, bands, crossings, junctions, gap):
    supported = []
    for band, row in zip(bands, junctions, strict=True):
        ends = [cross for cross, junction in zip(crossings, row, strict=True) if junction]
        supported.append(any(_side(rules, ink, band, first, second, gap) is True
                             for first, second in zip(ends, ends[1:])))
    return np.array(supported, dtype=bool)


def _grid_axes(horizontal, vertical, ink, minimum, thickness, gap):
    ys = _axis_bands(horizontal, minimum, thickness)
    xs = _axis_bands(vertical.T, minimum, thickness)
    if max(len(xs), len(ys)) > MAX_RULE_BANDS or min(len(xs), len(ys)) < 2:
        return None
    crossings = horizontal & vertical
    junctions = np.array([[np.any(crossings[y0:y1, x0:x1]) for x0, x1 in xs]
                          for y0, y1 in ys], dtype=bool)
    while len(xs) >= 2 and len(ys) >= 2:
        real_x = _linked_bands(vertical.T, ink.T, xs, ys, junctions.T, gap)
        real_y = _linked_bands(horizontal, ink, ys, xs, junctions, gap)
        if real_x.all() and real_y.all():
            if any(b[0] - a[1] < minimum-thickness for bands in (xs, ys)
                   for a, b in zip(bands, bands[1:])):
                return None
            return xs, ys
        # Text may touch rails at both ends. A structural axis needs a line
        # between two perpendicular junctions.
        xs = [band for band, real in zip(xs, real_x, strict=True) if real]
        ys = [band for band, real in zip(ys, real_y, strict=True) if real]
        junctions = junctions[real_y][:, real_x]
    return None


def _center(band):
    return (band[0] + band[1] - 1) // 2


def _side(rules, ink, band, first, second, gap):
    start, end = _center(first), _center(second) + 1
    evidence = np.any(rules[band[0]:band[1], start:end], axis=0)
    source = np.any(ink[band[0]:band[1], start:end], axis=0)
    holes = _runs(~evidence)
    if not holes or (np.mean(evidence) >= .85 and all(b - a <= gap for a, b in holes)):
        if np.mean(source) >= .85 and all(b - a <= 2 * gap for a, b in _runs(~source)):
            return True
        return None
    # A neighboring rail may project a few pixels past a junction. Long partial
    # runs between junctions cannot prove a merged cell or an intact divider.
    interior = slice(first[1]-start, second[0]-start)
    source_side = source[interior]
    stub = max(4, gap + 1)
    if (np.any(evidence[interior]) or np.all(source_side[:stub])
            or np.all(source_side[-stub:])):
        return None
    return False


def _tiles(horizontal, vertical):
    """Find connected slots and verify that each resulting cell is rectangular."""
    rows, columns = vertical.shape[0], horizontal.shape[1]
    visited = np.zeros((rows, columns), dtype=bool)
    tiles = []
    for row in range(rows):
        for column in range(columns):
            if visited[row, column]:
                continue
            pending = [(row, column)]
            visited[row, column] = True
            top, left, bottom, right = row, column, row, column
            count = 0
            while pending:
                r, c = pending.pop()
                top, left = min(top, r), min(left, c)
                bottom, right = max(bottom, r), max(right, c)
                count += 1
                neighbors = ((r - 1, c, horizontal[r, c]), (r + 1, c, horizontal[r + 1, c]),
                             (r, c - 1, vertical[r, c]), (r, c + 1, vertical[r, c + 1]))
                for nr, nc, wall in neighbors:
                    if not wall and not visited[nr, nc]:
                        visited[nr, nc] = True
                        pending.append((nr, nc))
            bottom, right = bottom + 1, right + 1
            if (count != (bottom - top) * (right - left)
                    or np.any(horizontal[top+1:bottom, left:right])
                    or np.any(vertical[top:bottom, left+1:right])):
                return None
            tiles.append((top, left, bottom, right))
    return tiles if len(tiles) >= 2 else None


def _grid(horizontal, vertical, ink, minimum, thickness, gap):
    axes = _grid_axes(horizontal, vertical, ink, minimum, thickness, gap)
    if axes is None:
        return None
    xs, ys = axes
    if (len(ys) - 1) * (len(xs) - 1) > MAX_GRID_SLOTS:
        return None
    walls = []
    for rules, source, bands, crossings in ((horizontal, ink, ys, xs), (vertical.T, ink.T, xs, ys)):
        sides = np.zeros((len(bands), len(crossings) - 1), dtype=bool)
        for index, band in enumerate(bands):
            for slot, (first, second) in enumerate(zip(crossings, crossings[1:])):
                present = _side(rules, source, band, first, second, gap)
                if present is None or (index in (0, len(bands) - 1) and not present):
                    return None
                sides[index, slot] = present
            if not sides[index].any():
                return None
        walls.append(sides)
    tiles = _tiles(walls[0], walls[1].T)
    return (xs, ys, tiles) if tiles else None


def _component_boxes(mask):
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if len(stats) > MAX_CELL_COMPONENTS + 1:
        return None, None
    parts = []
    for label, (x, y, w, h, area) in enumerate(stats[1:].tolist(), 1):
        if area >= 4 and (x == 0 or y == 0 or x+w == mask.shape[1] or y+h == mask.shape[0]):
            return None, None
        parts.append(_Part((x, y, x+w, y+h), area, label))
    return parts, labels


def _envelope(parts):
    return (min(part.bbox[0] for part in parts), min(part.bbox[1] for part in parts),
            max(part.bbox[2] for part in parts), max(part.bbox[3] for part in parts))


def _groups(parts, axis, gap):
    """Group whole components along one axis without splitting any ink."""
    groups = []
    stop = 0
    for part in sorted(parts, key=lambda part: part.bbox[axis]):
        if not groups or part.bbox[axis] > stop + gap:
            groups.append([])
            stop = 0
        groups[-1].append(part)
        stop = max(stop, part.bbox[axis + 2])
    return groups


def _picture_shape(part, labels, glyph):
    x0, y0, x1, y1 = part.bbox
    width, height = part.width, part.height
    density = part.area / (width * height)
    # A well-separated outline much larger than local glyphs does not need
    # orthogonal strokes. At smaller sizes, expect a measured corner shape.
    if min(width, height) >= 4 * glyph and density <= .4:
        return True
    if density > .42 or max(width, height) > 5 * min(width, height):
        return False
    pixels = (labels[y0:y1, x0:x1] == part.label).astype(np.uint8)
    across = cv2.morphologyEx(pixels, cv2.MORPH_OPEN,
                              np.ones((1, max(3, ceil(width * .54))), np.uint8))
    down = cv2.morphologyEx(pixels, cv2.MORPH_OPEN,
                            np.ones((max(3, ceil(height * .54)), 1), np.uint8))
    return bool(np.any(across & down))


def _text_line(parts, glyph):
    letters = [p for p in parts if .48 * glyph <= p.height <= 1.6 * glyph and p.width <= 1.55 * glyph]
    if len(letters) < 3:
        return False
    baseline = median(p.bbox[3] for p in letters)
    aligned = [p for p in letters if abs(p.bbox[3] - baseline) <= .32 * glyph]
    return len(aligned) >= max(3, .7 * len(letters))


def _band_blocks(band, anchors, glyph):
    """Separate a clearly isolated side caption from the picture and its labels."""
    groups = _groups(band, 0, max(3, round(glyph * .65)))
    picture = _envelope([part for part in band if part.label in anchors])
    result = []
    for group in groups:
        bounds = _envelope(group)
        distance = max(picture[0] - bounds[2], bounds[0] - picture[2], 0)
        if any(p.label in anchors for p in group):
            kind = 'picture'
        elif distance > 0 and _text_line(group, glyph):
            kind = 'text'
        elif distance <= 2 * glyph:
            kind = 'picture'
        else:
            return None
        if result and result[-1][0] == kind == 'picture':
            result[-1][1].extend(group)
        else:
            result.append((kind, group))
    return [(kind, _envelope(parts)) for kind, parts in result]


def _overlaps(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _block_boxes(parts, labels, glyph, interior):
    if sum(p.area for p in parts) <= 1:
        return ()
    anchors = set()
    for part in parts:
        if min(part.width, part.height) < max(19, 2.5 * glyph):
            continue
        if not _picture_shape(part, labels, glyph):
            return None
        anchors.add(part.label)

    regions, pending_text = [], []
    for band in _groups(parts, 1, max(1, round(glyph * .12))):
        band_anchors = anchors.intersection(part.label for part in band)
        if not band_anchors:
            pending_text.extend(band)
            continue
        if pending_text:
            regions.append(('text', _envelope(pending_text)))
            pending_text = []
        split = _band_blocks(band, band_anchors, glyph)
        if split is None:
            return None
        regions.extend(split)
    if pending_text:
        regions.append(('text', _envelope(pending_text)))

    if len(regions) > 32:
        return None
    left, top, right, bottom = interior
    padding = max(2, min(5, round(.15 * glyph)))
    blocks = []
    for index, (kind, bbox) in enumerate(regions):
        x0, y0, x1, y1 = bbox
        neighbors = [box for _, box in regions[index+1:]]
        neighbors.extend((b.bbox[0]-left, b.bbox[1]-top, b.bbox[2]-left, b.bbox[3]-top) for b in blocks)
        for margin in range(padding, -1, -1):
            crop = (max(0, x0-margin), max(0, y0-margin),
                    min(right-left, x1+margin), min(bottom-top, y1+margin))
            if not any(_overlaps(crop, other) for other in neighbors):
                break
        if any(_overlaps(crop, other) for other in neighbors):
            return None
        blocks.append(Block(kind, (left+crop[0], top+crop[1], left+crop[2], top+crop[3])))
    return tuple(blocks)


def _table(mask, origin, grid, glyph_floor, glyph_ceiling, cell_contents=True):
    xs, ys, tiles = grid
    x, y = origin
    cells, samples, heights = [], [], []
    for row, column, bottom, right in tiles:
        box = (x+_center(xs[column]), y+_center(ys[row]),
               x+_center(xs[right]), y+_center(ys[bottom]))
        if not cell_contents:
            cells.append(Cell(row, column, bottom-row, right-column, box, ()))
            continue
        interior = (x+xs[column][1], y+ys[row][1], x+xs[right][0], y+ys[bottom][0])
        l, t, r, b = interior
        parts, labels = _component_boxes(mask[t:b, l:r])
        if parts is None:
            return None
        heights.extend(p.height for p in parts if glyph_floor <= p.height <= glyph_ceiling
                       and p.area >= 5 and .08 * p.height <= p.width <= 1.6 * p.height)
        samples.append((row, column, bottom, right, box, interior, parts))
        del labels
    glyph = median(heights) if heights else glyph_floor

    for row, column, bottom, right, box, interior, parts in samples:
        l, t, r, b = interior
        # Reproduce labels only for cells with a candidate picture. Retaining a
        # label image for every cell would keep an extra page raster in memory.
        labels = None
        if any(min(p.width, p.height) >= max(19, 2.5 * glyph) for p in parts):
            _, labels = _component_boxes(mask[t:b, l:r])
        blocks = _block_boxes(parts, labels, glyph, interior)
        if blocks is None:
            return None
        cells.append(Cell(row, column, bottom-row, right-column, box, blocks))
    x_grid = tuple(x + _center(band) for band in xs)
    y_grid = tuple(y + _center(band) for band in ys)
    bounds = (x_grid[0], y_grid[0], x_grid[-1], y_grid[-1])
    mask_bbox = (x + xs[0][0], y + ys[0][0], x + xs[-1][1], y + ys[-1][1])
    return Table(bounds, len(ys)-1, len(xs)-1, tuple(cells), x_grid, y_grid, mask_bbox)


def analyze_page(image: Image.Image, *, cell_contents=True) -> tuple[Table, ...]:
    """Recover ruled cells in source pixels and optionally classify their text and picture regions."""
    width, height = image.size
    short = min(width, height)
    if short < 64 or width * height > MAX_PAGE_PIXELS:
        return ()
    with image.convert('L') as gray:
        values = np.asarray(gray)
    threshold, _ = cv2.threshold(values, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    threshold = max(threshold, min(235, threshold + 28))
    mask = cv2.threshold(values, threshold, 255, cv2.THRESH_BINARY_INV)[1]

    minimum = max(13, min(49, 2 * (round(short * .013) // 2) + 1))
    thickness = max(4, round(short * .004))
    gap = max(2, min(4, round(short * .0015)))
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((1, 2*gap+1), np.uint8))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2*gap+1, 1), np.uint8))
    horizontal = cv2.morphologyEx(horizontal, cv2.MORPH_OPEN, np.ones((1, minimum), np.uint8))
    vertical = cv2.morphologyEx(vertical, cv2.MORPH_OPEN, np.ones((minimum, 1), np.uint8))
    _, labels, stats, _ = cv2.connectedComponentsWithStats(horizontal | vertical, connectivity=8)

    glyph_floor = max(5, round(short * .005))
    glyph_ceiling = max(22, round(short * .023))
    tables = []
    candidates = np.flatnonzero(np.minimum(stats[1:, cv2.CC_STAT_WIDTH], stats[1:, cv2.CC_STAT_HEIGHT])
                                >= 2 * minimum) + 1
    for label in candidates:
        x, y, w, h, _ = stats[label].tolist()
        if x == 0 or y == 0 or x+w == width or y+h == height:
            continue
        own = labels[y:y+h, x:x+w] == label
        grid = _grid(horizontal[y:y+h, x:x+w] & own, vertical[y:y+h, x:x+w] & own,
                     mask[y:y+h, x:x+w], minimum, thickness, gap)
        if grid is not None:
            table = _table(mask, (x, y), grid, glyph_floor, glyph_ceiling, cell_contents)
            if table is not None:
                tables.append(table)
    # A small closed figure wholly inside a table belongs to its cell. Crossing
    # candidate rectangles have ambiguous ownership and are left to recovery.
    independent = []
    for i, candidate in enumerate(tables):
        a, b, c, d = candidate.bbox
        if any(_overlaps(candidate.bbox, other.bbox)
               and not (a <= other.bbox[0] and b <= other.bbox[1]
                        and other.bbox[2] <= c and other.bbox[3] <= d)
               for j, other in enumerate(tables) if i != j):
            continue
        independent.append(candidate)
    return tuple(sorted(independent, key=lambda table: (table.bbox[1], table.bbox[0])))
