"""Distinguish repeated glyph stems from vertical rules on a ruled scan."""
from bisect import bisect_left
import math

import cv2
import numpy as np
from pdfplumber.utils.clustering import cluster_objects


def _printed_stem(label, labels, stats, left, right):
    x, y, width, height, _ = map(int, stats[label])
    if width < max(3, .20 * height) or width > 1.5 * height:
        return False
    glyph = labels[y:y + height, x:x + width] == label
    widths = np.count_nonzero(glyph, axis=1)
    # The flag of a numeral or serif widens part of a stem. A detached
    # straight dash has nearly constant stroke width along its length.
    if widths.max() - np.median(widths) < max(2, .12 * height):
        return False
    neighbors = labels[y:y + height, max(0, x - 2 * height):x + width + 2 * height]
    for other in np.unique(neighbors):
        if other == 0 or other == label:
            continue
        ox, oy, ow, oh, _ = map(int, stats[other])
        if (ox + ow <= left or ox >= right) and .55 * height <= oh <= 1.6 * height:
            if abs((oy + oh) - (y + height)) <= .35 * height:
                return True
    return False


def _row_bands(strokes, horizontal, x, sy):
    levels = sorted({e['top'] for e in horizontal if e['x0'] < x - 2 and x + 2 < e['x1']})
    bands = set()
    for edge in strokes:
        stop = bisect_left(levels, edge['top'])
        if (stop == 0 or stop == len(levels) or edge['top'] - levels[stop - 1] <= 2 * sy
                or levels[stop] - edge['bottom'] <= 2 * sy):
            return set()
        bands.add((levels[stop - 1], levels[stop]))
    return bands


def _text_only_band(top, bottom, left, right, mask, labels, stats, region, sx, sy):
    first = max(0, math.ceil((top - region[1]) / sy) + 2)
    last = min(mask.shape[0], math.floor((bottom - region[1]) / sy) - 2)
    margin = max(4, round(3 / sx))
    stripe = mask[:, max(0, left - margin):min(mask.shape[1], right + margin)]
    # A thick or doubled horizontal rail can enter the row interior. Step
    # only past ink that actually crosses both sides of the candidate axis.
    while first < last and stripe[first].all():
        first += 1
    while last > first and stripe[last - 1].all():
        last -= 1
    ink = labels[first:last, left:right]
    if not ink.size or ink[0].any() or ink[-1].any():
        return False
    components = np.unique(ink[ink != 0])
    return bool(len(components)) and all(_printed_stem(label, labels, stats, left, right) for label in components)


def remove_text_axes(edges, mask, region, sx, sy, snap_tolerance):
    """Remove an axis only if all its segments belong to separate printed glyphs."""
    vertical = [edge for edge in edges if edge['orientation'] == 'v']
    horizontal = [edge for edge in edges if edge['orientation'] == 'h']
    rejected, proofs = set(), []
    labels = stats = None
    for strokes in cluster_objects(vertical, 'x0', snap_tolerance):
        if len(strokes) < 2:
            continue
        positions = [e['x0'] for e in strokes]
        if max(positions) - min(positions) > 2 * sx:
            continue  # Nearby parallel tracks can snap to one apparent column.
        x = sum(positions) / len(positions)
        bands = _row_bands(strokes, horizontal, x, sy)
        if len(bands) < 2:
            continue
        if labels is None:
            _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        radius = max(1, round(.5 / sx))
        left = max(0, round((min(positions) - region[0]) / sx) - radius)
        right = min(mask.shape[1], round((max(positions) - region[0]) / sx) + radius + 1)
        if all(_text_only_band(top, bottom, left, right, mask, labels, stats, region, sx, sy)
               for top, bottom in bands):
            rejected.update(positions)
            proofs.append(dict(x=x, text_bands=len(bands), vertical_segments=len(strokes)))
    return [e for e in edges if e['orientation'] != 'v' or e['x0'] not in rejected], proofs
