"""Reconstruct ruled tables from measured segments in top-left page coordinates."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import groupby
from math import ceil, floor, inf, isfinite
from statistics import median


@dataclass(frozen=True, slots=True)
class Cell:
    row: int
    column: int
    rowspan: int
    colspan: int
    bbox: tuple[float, float, float, float]
    open_top: bool = False
    open_bottom: bool = False
    synthetic_top: bool = False
    synthetic_bottom: bool = False


@dataclass(frozen=True, slots=True)
class Grid:
    bbox: tuple[float, float, float, float]
    x_grid: tuple[float, ...]
    y_grid: tuple[float, ...]
    cells: tuple[Cell, ...]
    open_top: bool = False
    open_bottom: bool = False
    double_x: tuple[float, ...] = ()
    double_y: tuple[float, ...] = ()
    evidence: str = 'verified'

    @property
    def rows(self):
        return len(self.y_grid) - 1

    @property
    def columns(self):
        return len(self.x_grid) - 1

    def scaled(self, sx, sy):
        """Map detector coordinates into the raster used for OCR word boxes."""
        def box(bounds):
            left, top, right, bottom = bounds
            return left * sx, top * sy, right * sx, bottom * sy

        return replace(self, bbox=box(self.bbox),
                       x_grid=tuple(x * sx for x in self.x_grid),
                       y_grid=tuple(y * sy for y in self.y_grid),
                       double_x=tuple(x * sx for x in self.double_x),
                       double_y=tuple(y * sy for y in self.double_y),
                       cells=tuple(replace(cell, bbox=box(cell.bbox)) for cell in self.cells))


def _reason(diagnostics, code):
    if diagnostics is not None and code not in diagnostics:
        diagnostics.append(code)


class _TooComplex(Exception):
    pass


def _segments(lines, tolerance):
    """Snap collinear rules, including short PDF rectangle corners, before joining their intervals."""
    ordered = []
    for index, (position, start, end) in enumerate(lines):
        if index >= 24_000:
            raise _TooComplex
        if all(map(isfinite, (position, start, end))) and start != end:
            ordered.append((position, min(start, end), max(start, end)))
    ordered.sort()
    merged = []
    first = 0
    while first < len(ordered):
        last = first + 1
        while last < len(ordered) and ordered[last][0] - ordered[first][0] <= tolerance:
            last += 1
        group = ordered[first:last]
        mass = sum(end - start for _, start, end in group)
        axis = sum(position * (end - start) for position, start, end in group) / mass
        spans = sorted((start, end) for _, start, end in group)
        begin, finish = spans[0]
        for start, end in spans[1:]:
            if start - finish <= 1.8 * tolerance:
                finish = max(finish, end)
            else:
                if finish - begin > 3 * tolerance:
                    merged.append((axis, begin, finish))
                begin, finish = start, end
        if finish - begin > 3 * tolerance:
            merged.append((axis, begin, finish))
        first = last
    return merged


class _Components:
    def __init__(self, count):
        self.parent = list(range(count))

    def root(self, index):
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, first, second):
        self.parent[self.root(second)] = self.root(first)


def _intersections(horizontal, vertical, tolerance):
    """Cluster actual crossings before considering any missing edge or line interruption."""
    xs = [x for x, _, _ in vertical]
    offset = len(horizontal)
    components = _Components(offset + len(vertical))
    used_rows = {}
    used_columns = defaultdict(list)
    comparisons = 0
    for index, (y, left, right) in enumerate(horizontal):
        start = bisect_left(xs, left - tolerance)
        stop = bisect_right(xs, right + tolerance)
        comparisons += stop - start
        if comparisons > 240_000:
            raise _TooComplex
        crossing = [i for i in range(start, stop)
                    if vertical[i][1] - tolerance <= y <= vertical[i][2] + tolerance]
        if not crossing:
            continue
        used_rows[index] = {xs[i] for i in crossing}
        for i in crossing:
            components.join(index, offset + i)
            used_columns[i].append(y)

    parts = defaultdict(lambda: ([], []))
    for i, hits in used_rows.items():
        parts[components.root(i)][0].append((*horizontal[i], hits))
    for i, hits in used_columns.items():
        parts[components.root(offset + i)][1].append((*vertical[i], set(hits)))
    return parts.values()


def _outer_edge(rails, x_min, x_max, ruled_y, tolerance, *, top):
    """Close aligned ends of the two side rails only within this connected component."""
    ends = []
    for x in (x_min, x_max):
        crossing = [(a, b) for a, b in rails[x] if a - tolerance <= ruled_y <= b + tolerance]
        if not crossing:
            return None
        ends.append(min(a for a, _ in crossing) if top else max(b for _, b in crossing))
    if all(abs(y - ruled_y) <= tolerance for y in ends):
        return ruled_y, False
    if abs(ends[1] - ends[0]) <= tolerance and all(abs(y - ruled_y) > 3 * tolerance for y in ends):
        return (min(ends) if top else max(ends)), True
    return None


def _walls(spans, breaks, tolerance, *, exterior=False):
    """Classify each edge as present or absent. A rule ending inside an edge is ambiguous."""
    result = []
    cursor = 0
    for left, right in zip(breaks, breaks[1:]):
        while cursor < len(spans) and spans[cursor][1] <= left + tolerance:
            cursor += 1
        if cursor == len(spans) or spans[cursor][0] >= right - tolerance:
            result.append(False)
            continue
        first, last = spans[cursor]
        missing = 0.0
        i = cursor
        while last < right - tolerance and i + 1 < len(spans) and spans[i + 1][0] < right - tolerance:
            i += 1
            a, b = spans[i]
            missing += max(0.0, a - last)
            last = max(last, b)
        # Both ends meet measured rails. A small interruption inside a mostly
        # printed divider does not turn two rectangular cells into one.
        complete = first <= left + tolerance and last >= right - tolerance and missing <= .12 * (right - left)
        if not complete and not exterior:
            return None
        result.append(complete)
    return result


def _make_cells(xs, ys, horizontal, vertical, open_top, open_bottom):
    rows, cols = len(ys) - 1, len(xs) - 1
    tiles = _Components(rows * cols)
    for row in range(rows):
        for col in range(cols):
            ix = row * cols + col
            if col and not vertical[col][row]:
                tiles.join(ix - 1, ix)
            if row and not horizontal[row][col]:
                tiles.join(ix - cols, ix)
    members = defaultdict(list)
    for row in range(rows):
        for col in range(cols):
            ix = row * cols + col
            root = tiles.root(ix)
            # A true divider inside one connected face would disappear from an HTML cell.
            if col and vertical[col][row] and tiles.root(ix - 1) == root:
                return None
            if row and horizontal[row][col] and tiles.root(ix - cols) == root:
                return None
            members[root].append((row, col))

    cells = []
    for slots in members.values():
        rr, cc = zip(*slots)
        r0, r1, c0, c1 = min(rr), max(rr) + 1, min(cc), max(cc) + 1
        if len(slots) != (r1 - r0) * (c1 - c0):
            return None
        at_top = r0 == 0 and any(open_top[c0:c1])
        at_bottom = r1 == rows and any(open_bottom[c0:c1])
        cells.append(Cell(r0, c0, r1 - r0, c1 - c0, (xs[c0], ys[r0], xs[c1], ys[r1]),
                          at_top, at_bottom, at_top, at_bottom))
    return tuple(cells)


def _grid(horizontal, vertical, tolerance, diagnostics):
    by_y = defaultdict(list)
    by_x = defaultdict(list)
    crossing_x = defaultdict(set)
    for y, left, right, hits in horizontal:
        by_y[y].append((left, right))
        crossing_x[y].update(hits)
    by_y = {y: spans for y, spans in by_y.items() if len(crossing_x[y]) >= 2}
    crossing_y = defaultdict(set)
    for x, top, bottom, hits in vertical:
        anchors = hits.intersection(by_y)
        if anchors:
            by_x[x].append((top, bottom))
            crossing_y[x].update(anchors)
    stable = {x for x, anchors in crossing_y.items() if len(anchors) >= 2}
    if len(by_y) < 2 or len(by_x) < 3:
        _reason(diagnostics, 'too_few_ruled_rows_or_columns')
        return None

    extents = (min(a for spans in by_y.values() for a, _ in spans),
               max(b for spans in by_y.values() for _, b in spans))
    # A cell spanning all rows can leave a side rail with only one printed crossing.
    for endpoint in extents:
        side = min(by_x, key=lambda x: abs(x - endpoint))
        if abs(side - endpoint) > tolerance:
            _reason(diagnostics, 'missing_side_rails')
            return None
        stable.add(side)
    xs, ys = sorted(stable), sorted(by_y)
    top = _outer_edge(by_x, xs[0], xs[-1], ys[0], tolerance, top=True)
    bottom = _outer_edge(by_x, xs[0], xs[-1], ys[-1], tolerance, top=False)
    if top is None or bottom is None:
        _reason(diagnostics, 'unaligned_outer_endpoints')
        return None
    if top[1]:
        ys.insert(0, top[0])
    if bottom[1]:
        ys.append(bottom[0])
    # A divider confined to an open edge band touches only one printed horizontal.
    for x, spans in by_x.items():
        if x in stable or not xs[0] < x < xs[-1]:
            continue
        if any((top[1] and abs(a - ys[0]) <= tolerance) or (bottom[1] and abs(b - ys[-1]) <= tolerance)
               for a, b in spans):
            stable.add(x)
    xs = tuple(sorted(stable))
    ys = tuple(ys)
    if min(len(xs), len(ys)) < 3:
        _reason(diagnostics, 'too_few_ruled_rows_or_columns')
        return None
    if (len(xs) - 1) * (len(ys) - 1) > 8192:
        _reason(diagnostics, 'grid_complexity_limit')
        return None
    if any(b - a <= 3 * tolerance for axis in (xs, ys) for a, b in zip(axis, axis[1:])):
        _reason(diagnostics, 'indistinct_grid_axes')
        return None
    if any(a < ys[0] - tolerance or b > ys[-1] + tolerance for x in xs for a, b in by_x[x]):
        _reason(diagnostics, 'unaligned_outer_endpoints')
        return None

    horizontal_walls = [(_walls(sorted(by_y[y]), xs, tolerance, exterior=y in (ys[0], ys[-1]))
                         if y in by_y else [False] * (len(xs) - 1))
                        for y in ys]
    vertical_walls = [_walls(sorted(by_x[x]), ys, tolerance) for x in xs]
    if None in horizontal_walls or None in vertical_walls:
        _reason(diagnostics, 'incomplete_rule')
        return None
    if not all(vertical_walls[0]) or not all(vertical_walls[-1]):
        _reason(diagnostics, 'incomplete_perimeter')
        return None
    # The side rails locate both ends even when only some columns have a printed
    # top or bottom. Keep those cell flags, and reserve a page join for an entirely open edge.
    open_top = [not wall for wall in horizontal_walls[0]]
    open_bottom = [not wall for wall in horizontal_walls[-1]]
    cells = _make_cells(xs, ys, horizontal_walls, vertical_walls, open_top, open_bottom)
    if cells is None:
        _reason(diagnostics, 'nonrectangular_cells')
        return None
    return Grid((xs[0], ys[0], xs[-1], ys[-1]), xs, ys, cells, top[1], bottom[1])


def _collapse_pairs(segments, tolerance, clear_gap, vertical):
    levels = [list(group) for _, group in groupby(sorted(segments, key=lambda item: item[:3]),
                                               key=lambda item: item[0])]
    close = [b[0][0] - a[0][0] <= 3 * tolerance for a, b in zip(levels, levels[1:])]
    merged, doubled = [], []
    index = 0
    while index < len(levels):
        first = levels[index]
        if (index < len(close) and close[index]
                and (index == 0 or not close[index - 1])
                and (index + 1 == len(close) or not close[index + 1])):
            second = levels[index + 1]
            if len(first) == len(second) and all(
                    abs(a - c) <= tolerance and abs(b - d) <= tolerance and len(hits & other_hits) >= 2
                    and clear_gap(first[0][0], second[0][0], max(a, c), min(b, d), vertical)
                    for (_, a, b, hits), (_, c, d, other_hits) in zip(first, second, strict=True)):
                center = (first[0][0] + second[0][0]) / 2
                doubled.append(center)
                merged.extend((center, min(a, c), max(b, d))
                              for (_, a, b, _), (_, c, d, _) in zip(first, second, strict=True))
                index += 2
                continue
        merged.extend(item[:3] for item in first)
        index += 1
    return merged, tuple(doubled)


def _double_grid(rows, columns, tolerance, clear_gap):
    horizontal, double_y = _collapse_pairs(rows, tolerance, clear_gap, False)
    vertical, double_x = _collapse_pairs(columns, tolerance, clear_gap, True)
    if not (double_x or double_y):
        return None
    regions = list(_intersections(horizontal, vertical, tolerance))
    if len(regions) != 1:
        return None
    grid = _grid(*regions[0], tolerance, [])
    if grid is None or set(double_x) - set(grid.x_grid) or set(double_y) - set(grid.y_grid):
        return None
    return replace(grid, double_x=double_x, double_y=double_y)


def vector_grids(horizontal, vertical, *, tolerance=1.1, diagnostics=None, clear_gap=None):
    """Return grids from (axis, start, end) rules in PDF points, or in another shared unit."""
    if not isfinite(tolerance) or tolerance <= 0:
        raise ValueError('tolerance must be finite and positive')
    grids = []
    try:
        h = _segments(horizontal, tolerance)
        v = _segments(vertical, tolerance)
        if not h or not v:
            _reason(diagnostics, 'no_horizontal_rules' if not h else 'no_vertical_rules')
            return []
        for rows, columns in _intersections(h, v, tolerance):
            reasons = []
            grid = _grid(rows, columns, tolerance, reasons)
            if grid is None and clear_gap is not None and 'indistinct_grid_axes' in reasons:
                grid = _double_grid(rows, columns, tolerance, clear_gap)
            if grid is not None:
                grids.append(grid)
            else:
                for reason in reasons:
                    _reason(diagnostics, reason)
    except _TooComplex:
        _reason(diagnostics, 'grid_complexity_limit')
        return []
    if not grids:
        _reason(diagnostics, 'no_verified_grid')
    if len(grids) > 1:
        # A separately ruled inset belongs to an outer cell. Until nested tables
        # are represented, keep its words only in that cell.
        outer = []
        for grid in sorted(grids, key=lambda g: (g.bbox[0],g.bbox[1],-g.bbox[2],-g.bbox[3])):
            l,t,r,b = grid.bbox
            if not any(x <= l and y <= t and r <= u and b <= v for x,y,u,v in (g.bbox for g in outer)):
                outer.append(grid)
        grids = outer
    return sorted(grids, key=lambda grid: (grid.bbox[1], grid.bbox[0]))


def _kernel(size, vertical):
    import cv2

    size = max(1, size | 1)  # Centred odd kernels preserve open endpoints.
    return cv2.getStructuringElement(cv2.MORPH_RECT, (1, size) if vertical else (size, 1))


def partial_raster_grids(image, word_rows):
    """Infer an open grid only where printed dividers and positioned rows agree."""
    import cv2
    import numpy as np

    words = [(x, y, x+w, y+h) for row in word_rows for text, x, y, w, h in row
             if text.strip() and w > 0 and h > 0]
    if not words:
        return []
    em = median(b-t for _, t, _, b in words)
    with image.convert('L') as gray:
        ink = cv2.adaptiveThreshold(np.asarray(gray), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                    cv2.THRESH_BINARY_INV, 15, 9)
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, _kernel(round(2*em), True))
    strokes = _mask_segments(vertical, 2*em, max(2, .6*em), True)
    rails = _segments(strokes, max(1., .4*em))
    rails = [(x, t, b) for x, t, b in rails if b-t >= 3*em]
    found = []
    for axis, start, end in sorted(rails, key=lambda rail: rail[2]-rail[1], reverse=True):
        group = [(x, t, b) for x, t, b in rails
                 if min(end, b)-max(start, t) >= .65*max(end-start, b-t)]
        columns = sorted({x for x, _, _ in group})
        if len(columns) < 2 or any(b-a < 2*em for a, b in zip(columns, columns[1:])):
            continue
        top, bottom = median(t for _, t, _ in group), median(b for _, _, b in group)
        if any(max(top, box[1]) < min(bottom, box[3]) for box in (grid.bbox for grid in found)):
            continue
        grid = _partial_grid(ink, vertical, words, columns, top, bottom, em)
        if grid is not None:
            found.append(grid)
    return found


def _partial_grid(ink, vertical, words, columns, top, bottom, em):
    import cv2

    spacing = median(b-a for a, b in zip(columns, columns[1:]))
    left, right = columns[0] - 1.5*spacing, columns[-1] + 1.5*spacing
    rule_length = columns[-1]-columns[0]
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, _kernel(round(rule_length), False))
    rules = [y for y, a, b in _mask_segments(horizontal, rule_length, max(2, .6*em), False)
             if a <= columns[0] and b >= columns[-1]]
    top = min((y for y in rules if top-2*em <= y <= top), default=top-.5*em)
    candidates = sorted((box for box in words if left < (box[0]+box[2])/2 < right
                         and top <= (box[1]+box[3])/2 <= bottom+.5*em),
                        key=lambda box: box[1]+box[3])
    rows, centers = [], []
    for box in candidates:
        y = (box[1]+box[3])/2
        if not rows or y-centers[-1] > .55*em:
            rows.append([])
            centers.append(y)
        rows[-1].append(box)
    supported = []
    for i, row in enumerate(rows):
        filled = {bisect_right(columns, (l+r)/2) for l, _, r, _ in row}
        crosses = any(l+.2*em < x < r-.2*em for l, _, r, _ in row for x in columns)
        if not crosses and len(filled) >= 3 and 0 in filled and len(columns) in filled:
            supported.append(i)
    if len(supported) < 3 or 2*len(supported) < supported[-1]-supported[0]+1:
        return None
    left = max(0., min(l for row in rows for l, _, _, _ in row)-.5*em)
    right = min(float(ink.shape[1]), max(r for row in rows for _, _, r, _ in row)+.5*em)
    xs = (left, *columns, right)
    mids = [(a+b)/2 for a, b in zip(centers, centers[1:])]
    splits = [min((r for r in rules if a+.25*em <= r <= b-.25*em),
                  key=lambda r: abs(r-mid), default=mid)
              for a, b, mid in zip(centers, centers[1:], mids)]
    ys = (max(0., min(t for _, t, _, _ in rows[0])-.25*em), *splits,
          min(float(ink.shape[0]), max(b for _, _, _, b in rows[-1])+.25*em))
    walls = [[True] * (len(xs)-1) for _ in ys]
    dividers = [[True]*len(rows)]
    for x in columns:
        lo, hi = max(0, round(x-.2*em)), round(x+.2*em)+1
        dividers.append([
            not any(l+.2*em < x < r-.2*em for l, _, r, _ in row)
            and (i in supported or vertical[round(ys[i]):round(ys[i+1]), lo:hi].any())
            for i, row in enumerate(rows)])
    dividers.append([True]*len(rows))
    cells = _make_cells(xs, ys, walls, dividers, [False]*(len(xs)-1), [False]*(len(xs)-1))
    return Grid((left, ys[0], right, ys[-1]), xs, ys, cells, evidence='inferred') if cells else None


def _mask_segments(mask, minimum, thickness, vertical):
    import cv2

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    segments = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        start, length, side, width = (y, h, x, w) if vertical else (x, w, y, h)
        if length >= minimum and width <= thickness and length >= 3 * width:
            segments.append((side + (width - 1) / 2, start, start + length - 1))
    return segments


def _dash_period(ink, axis, start, end, vertical, gap, thickness):
    """Check the original ink for a repeating run/gap pattern before linking dashes."""
    import numpy as np

    if vertical:
        ink = ink.T
    center = round(axis)
    flank = max(3, thickness // 2 + 1)
    length = end - start + 1
    for y in (center - flank, center + flank):
        if 0 <= y < ink.shape[0] and np.count_nonzero(ink[y, start:end + 1]) > .20 * length:
            return 0
    strip = ink[max(0, center - 1):center + 2, start:end + 1]
    line = strip.max(axis=0)
    edges = np.flatnonzero(line[:-1] != line[1:]) + 1
    if len(edges) < 7:
        return 0
    lengths = np.diff(edges)
    light = line[edges[:-1]] == 0
    gaps, marks = lengths[light], lengths[~light]
    if min(len(gaps), len(marks)) < 3:
        return 0
    gap_size, mark_size = np.median(gaps), np.median(marks)
    if gap_size > gap + 1 or mark_size < 2 or mark_size > 6 * gap_size:
        return 0
    for runs, typical in ((gaps, gap_size), (marks, mark_size)):
        if np.count_nonzero(abs(runs - typical) <= max(1, .30 * typical)) < .75 * len(runs):
            return 0
    return float(gap_size)


def _raster_rules(ink, length, gap, thickness, vertical):
    import cv2

    solid_mask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, _kernel(length, vertical))
    solid = _mask_segments(solid_mask, length, thickness, vertical)
    # Closing all glyphs also connects the feet of printed letters. Inspect the
    # original ink, where a dashed rule has regular gaps and a solid rule has none.
    seed = cv2.morphologyEx(ink, cv2.MORPH_OPEN, _kernel(3, vertical))
    linked = cv2.morphologyEx(seed, cv2.MORPH_CLOSE, _kernel(gap + 1, vertical))
    dash_length = max(length, 5 * gap + 1)
    dashed_mask = cv2.morphologyEx(linked, cv2.MORPH_OPEN, _kernel(dash_length, vertical))
    dashed = []
    for axis, start, end in _mask_segments(dashed_mask, dash_length, thickness, vertical):
        period = _dash_period(ink, axis, start, end, vertical, gap, thickness)
        if period:
            dashed.append((axis, start, end, period))
    return solid, dashed


def _snap_dashes(solid, dashed, perpendicular, tolerance):
    if not dashed:
        return solid
    anchors = sorted(perpendicular)
    axes = [axis for axis, _, _ in anchors]
    combined = list(solid)
    comparisons_left = 240_000
    for axis, start, end, gap in dashed:
        # Either measured endpoint may fall inside the dash gap or the width of its crossing rail.
        margin = gap + 2 * tolerance

        def nearest(position):
            nonlocal comparisons_left
            lo, hi = bisect_left(axes, position - margin), bisect_right(axes, position + margin)
            comparisons_left -= hi - lo
            if comparisons_left < 0:
                return position
            candidates = (x for x, a, b in anchors[lo:hi]
                          if a - margin <= axis <= b + margin)
            return min(candidates, key=lambda x: abs(x - position), default=position)

        combined.append((axis, min(start, nearest(start)), max(end, nearest(end))))
    return combined


def raster_grids(image, *, diagnostics=None):
    """Read an already deskewed PIL page. All returned coordinates refer to the input pixels."""
    import cv2
    import numpy as np

    width, height = image.size
    if min(width, height) < 24:
        _reason(diagnostics, 'image_too_small')
        return []
    scale = min(1.0, 1650 / max(width, height))
    target = max(1, round(width * scale)), max(1, round(height * scale))
    with image.convert('L') as gray:
        original = np.asarray(gray)
        pixels = original
        if target != (width, height):
            pixels = cv2.resize(pixels, target, interpolation=cv2.INTER_AREA)
    w, h = target
    ink = cv2.adaptiveThreshold(pixels, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 9)
    gap = max(2, min(w, h) // 230)
    thickness = max(4, min(w, h) // 160)
    h_length = max(9, w // 40)
    h_solid, h_dashes = _raster_rules(ink, h_length, gap, thickness, False)
    horizontal = h_solid + [rule[:3] for rule in h_dashes]
    if len(horizontal) < 2:
        _reason(diagnostics, 'no_horizontal_rules')
        return []
    # A short vertical may separate just one dense header or span just one data row.
    # Cap the vertical opening at half of the shortest distinguishable ruled band.
    levels = sorted({y for y, a, b in horizontal if b - a >= h_length * 2})
    shortest = min((b - a for a, b in zip(levels, levels[1:]) if b - a >= 7), default=inf)
    v_length = max(3, int(min(max(7, h // 76), shortest / 2)))
    v_solid, v_dashes = _raster_rules(ink, v_length, gap, thickness, True)
    tolerance = max(1.65, min(w, h) / 530)
    vertical = _snap_dashes(v_solid, v_dashes, horizontal, tolerance)
    horizontal = _snap_dashes(h_solid, h_dashes, vertical, tolerance)
    sx, sy = width / w, height / h
    residual = {}

    def clear_gap(first, last, start, end, vertical):
        if not residual:
            # Check the original raster so small strokes cannot disappear during line detection.
            window = round(15 * max(sx, sy)) | 1
            source_ink = cv2.adaptiveThreshold(original, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                               cv2.THRESH_BINARY_INV, window, 9)
            minimum_rule = 2 * ceil(3 * tolerance) + 1
            h_rules = cv2.morphologyEx(source_ink, cv2.MORPH_OPEN,
                                       _kernel(round(max(h_length, minimum_rule) * sx), False))
            v_rules = cv2.morphologyEx(source_ink, cv2.MORPH_OPEN,
                                       _kernel(round(max(v_length, minimum_rule) * sy), True))
            for transposed, parallel, transverse in ((False, h_rules, v_rules), (True, v_rules, h_rules)):
                # Ragged antialiased edges can be one pixel away from the continuous rule.
                edges = cv2.dilate(parallel, _kernel(3, not transposed))
                crossings = cv2.dilate(transverse, _kernel(3, transposed))
                mask = cv2.bitwise_or(edges, crossings)
                remaining = cv2.bitwise_and(source_ink, cv2.bitwise_not(mask))
                residual[transposed] = (remaining.T, mask.T) if transposed else (remaining, mask)
        rows, rules = residual[vertical]
        top, bottom = floor(first) + 1, ceil(last)
        left, right = ceil(start), floor(end) + 1
        return (top < bottom and left < right and (rules[top:bottom, left:right] == 0).any()
                and not rows[top:bottom, left:right].any())

    return vector_grids(((y * sy, a * sx, b * sx) for y, a, b in horizontal),
                        ((x * sx, a * sy, b * sy) for x, a, b in vertical),
                        tolerance=tolerance * max(sx, sy), diagnostics=diagnostics, clear_gap=clear_gap)
