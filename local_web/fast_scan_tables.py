"""Detect closed rectangular grids in the fast raster path."""
from __future__ import annotations

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


def find_grids(image, candidates=None):
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
        if candidates is not None and len(xs) >= 2 and len(ys) >= 3:
            sx, sy = width/image.width, height/image.height
            candidates.append((int(x/sx), int(y/sy), int(np.ceil((x+w)/sx)), int(np.ceil((y+h)/sy))))
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


def grid_bbox(grid):
    return (grid.xs[0]/grid.scale_x, grid.ys[0]/grid.scale_y,
            grid.xs[-1]/grid.scale_x, grid.ys[-1]/grid.scale_y)


def table_blocks(lines, word_rows, grids, *, line_boxes=None, physical_grids=None):
    if not grids and not physical_grids:
        return None
    from local_web.fast_ruled_tables import _covered, _outer_grids, _simple_grid, scan_grid_blocks

    bounds = [grid.bbox for grid in physical_grids or ()]
    tables = _outer_grids([_simple_grid(grid) for grid in grids if not _covered(grid_bbox(grid), bounds)])
    return {'blocks': scan_grid_blocks(lines, word_rows, line_boxes or {}, physical_grids or (), tables)}
