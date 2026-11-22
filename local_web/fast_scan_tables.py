"""Recognize only complete, text-filled rectangular grids in the fast raster path."""
from __future__ import annotations

from statistics import median
from typing import NamedTuple

import cv2
import numpy as np


class Grid(NamedTuple):
    xs: tuple[int, ...]
    ys: tuple[int, ...]
    ink: np.ndarray
    scale_x: float
    scale_y: float


def _centers(counts):
    indices = np.flatnonzero(counts)
    if not len(indices):
        return ()
    # Collapse the thickness of a rule and pairs of closely spaced header rules.
    return tuple(round(float(np.median(run))) for run in np.split(indices, np.flatnonzero(np.diff(indices) > 3) + 1))


def _closed_grid(horizontal, vertical, xs, ys):
    for y in ys:
        band = horizontal[max(0, y-2):y+3]
        for left, right in zip(xs, xs[1:]):
            if np.count_nonzero(band[:, left:right].any(axis=0)) < .88 * (right-left):
                return False
    for x in xs:
        band = vertical[:, max(0, x-2):x+3]
        for top, bottom in zip(ys, ys[1:]):
            if np.count_nonzero(band[top:bottom].any(axis=1)) < .88 * (bottom-top):
                return False
    return True


def find_grids(image):
    factor = min(1.0, 1400 / max(image.size))
    with image.convert("L") as gray:
        pixels = np.asarray(gray)
        if factor < 1:
            pixels = cv2.resize(pixels, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    height, width = pixels.shape
    if min(height, width) < 45:
        return []
    ink = cv2.threshold(pixels, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(15, width//22)), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((max(12, height//44), 1), np.uint8))
    net = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    _, _, regions, _ = cv2.connectedComponentsWithStats(net, connectivity=8)
    grids = []
    # Checking each cell is useful for document tables but costly for graph paper.
    # Limit the total number of cell checks per page, including rejected candidates.
    remaining_cells = 1024
    for x, y, w, h, _ in regions[1:]:
        if w < width*.18 or h < height*.025:
            continue
        xs = tuple(x+i for i in _centers(np.count_nonzero(vertical[y:y+h, x:x+w], axis=0) > .55*h))
        ys = tuple(y+i for i in _centers(np.count_nonzero(horizontal[y:y+h, x:x+w], axis=1) > .55*w))
        # At least a header and two separately ruled data bands. A merge or a
        # partial separator leaves that region for the ordinary text route.
        if len(xs) < 3 or len(ys) < 4:
            continue
        cell_count = (len(xs) - 1) * (len(ys) - 1)
        if cell_count > remaining_cells:
            continue
        remaining_cells -= cell_count
        if _closed_grid(horizontal, vertical, xs, ys):
            grids.append(Grid(xs, ys, ink, width/image.width, height/image.height))
    return grids


def _cell_fragments(source_line, positioned):
    """Align recognition boxes to the original text without inventing word breaks."""
    fragments = []
    cursor = 0
    for text, row, col in positioned:
        start = cursor
        while start < len(source_line) and source_line[start].isspace():
            start += 1
        if not text or not source_line.startswith(text, start):
            return None
        end = start + len(text)
        if fragments and fragments[-1][:2] == [row, col]:
            fragments[-1][2] += source_line[cursor:end]
        else:
            if fragments and (start == cursor or row != fragments[-1][0] or col < fragments[-1][1]):
                return None
            fragments.append([row, col, source_line[start:end]])
        cursor = end
    return None if source_line[cursor:].strip() else fragments


def _region_rows(grid, lines, word_rows):
    xs, ys, ink, sx, sy = grid
    cells = [[[] for _ in xs[1:]] for _ in ys[1:]]
    residual = ink[ys[0]:ys[-1]+1, xs[0]:xs[-1]+1].copy()
    heights = []
    owned = set()
    for index, (source_line, words) in enumerate(zip(lines, word_rows, strict=True)):
        found = []
        for text, x, y, w, h in words:
            left, top = x*sx, y*sy
            right, bottom = left+w*sx, top+h*sy
            if right <= xs[0] or bottom <= ys[0] or left >= xs[-1] or top >= ys[-1]:
                continue
            col = int(np.searchsorted(xs, (left+right)/2)-1)
            row = int(np.searchsorted(ys, (top+bottom)/2)-1)
            if not (0 <= col < len(xs)-1 and 0 <= row < len(ys)-1):
                return None
            inside = max(0, min(right, xs[col+1])-max(left, xs[col])) * max(0, min(bottom, ys[row+1])-max(top, ys[row]))
            if inside < .9*(right-left)*(bottom-top):
                return None
            found.append((text, row, col))
            heights.append(bottom-top)
            l = max(0, int(left)-xs[0]-1)
            t = max(0, int(top)-ys[0]-1)
            residual[t:int(np.ceil(bottom))-ys[0]+2, l:int(np.ceil(right))-xs[0]+2] = 0

        if not found:
            continue
        if len(found) != len(words):
            return None
        fragments = _cell_fragments(source_line, found)
        if fragments is None:
            return None
        owned.add(index)
        for row, col, text in fragments:
            cells[row][col].append(text)

    if not owned:
        return None
    # A column-first OCR pass can read a footer between table columns. The
    # table is emitted at its first owned line, ahead of this measured footer.
    for index in range(min(owned), max(owned)+1):
        if index not in owned and (not word_rows[index] or any(y*sy < ys[-1] for _, _, y, _, _ in word_rows[index])):
            return None
    if not all(any(c for c in row) for row in cells):
        return None
    glyph = max(2, median(heights))
    for row, (top, bottom) in enumerate(zip(ys, ys[1:])):
        for col, (left, right) in enumerate(zip(xs, xs[1:])):
            # Exclude grid-rule ink. Substantial remaining objects outside the
            # OCR word rectangles can be unrecognized symbols or missing text.
            patch = residual[top-ys[0]+3:bottom-ys[0]-2, left-xs[0]+3:right-xs[0]-2]
            if patch.size == 0:
                return None
            if not cells[row][col] and np.count_nonzero(patch) > max(3, .15*glyph**2):
                return None
            _, _, components, _ = cv2.connectedComponentsWithStats(patch, connectivity=8)
            if any(area > .32*glyph**2 or max(w, h) > 1.8*glyph for _, _, w, h, area in components[1:]):
                return None
    return owned, [[" ".join(cell) for cell in row] for row in cells]


def table_blocks(lines, word_rows, grids):
    if len(lines) != len(word_rows):
        return None
    owned_indices = set()
    starts = {}
    for grid in grids:
        result = _region_rows(grid, lines, word_rows)
        if result is None:
            continue
        owned, rows = result
        if owned_indices & owned:
            return None
        owned_indices.update(owned)
        starts[min(owned)] = rows
    if not starts:
        return None
    blocks = []
    for index, line in enumerate(lines):
        if index in starts:
            blocks.append({"rows": starts[index]})
        elif index not in owned_indices and line.strip():
            blocks.append({"text": line})
    return {"blocks": blocks}
