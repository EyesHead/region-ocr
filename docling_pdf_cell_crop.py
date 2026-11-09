"""Raster content bounds inside physical table cells; never changes the grid."""
import math

import numpy as np


def _rule_inset(ink, coordinate, start, stop, axis, direction, scale):
    """Measure a continuous rule at the known cell edge, excluding corners.

    A neighbouring symbol is not a rule: the measured run must cover at least
    three quarters of the side and pass through its physical boundary. Only
    that run determines extra inset; no source pixels are painted or erased.
    """
    side = stop - start
    if side < 12:
        return 3, None
    margin = max(3, math.ceil(side * .08))
    start, stop = math.ceil(start + margin), math.floor(stop - margin)
    limit = ink.shape[1 if axis == 'x' else 0]
    other_limit = ink.shape[0 if axis == 'x' else 1]
    start, stop = max(0, start), min(other_limit, stop)
    tolerance = max(1, .25 * scale)
    if stop - start < 8 or coordinate < -tolerance or coordinate > limit+tolerance:
        return 3, None
    radius = max(6, math.ceil(3 * scale))
    left, right = max(0, math.floor(coordinate - radius)), min(limit, math.ceil(coordinate + radius) + 1)
    stripe = ink[start:stop, left:right] if axis == 'x' else ink[left:right, start:stop]
    occupied = np.mean(stripe, axis=0 if axis == 'x' else 1) >= .75
    indices = np.flatnonzero(occupied)
    if not len(indices):
        return 3, None
    nearest = int(indices[np.argmin(np.abs(indices + left - coordinate))])
    if abs(nearest + left - coordinate) > max(1, .25 * scale):
        return 3, None
    a = b = nearest
    while a and occupied[a-1]:
        a -= 1
    while b+1 < len(occupied) and occupied[b+1]:
        b += 1
    # If a thick rule reaches the initial strip, continue measuring only the
    # same long run. A quarter of the side is a conservative geometric bound.
    maximum = max(radius, math.floor(side / 4))
    def is_rule(position):
        if not 0 <= position < limit:
            return False
        pixels = ink[start:stop, position] if axis == 'x' else ink[position, start:stop]
        return np.mean(pixels) >= .75
    a, b = a + left, b + left
    while coordinate-a < maximum and is_rule(a-1):
        a -= 1
    while b-coordinate < maximum and is_rule(b+1):
        b += 1
    edge = b+1 if direction > 0 else a
    inset = max(3, math.ceil(direction * (edge-coordinate)) + 1)
    return inset, [a, b+1]


def cell_content_crop(image, region, cell_bbox, content_bbox=None):
    """Return ``(pixels, actual_bbox, proof)`` for original content pixels.

    ``region`` and both bboxes use source-page points. A minimum of three
    rendered pixels is excluded from each physical boundary, increased only
    past a measured rule's inward edge and antialiasing allowance. ``bbox``
    and spans supplied by the grid reader are never modified. An optional
    picture bbox is clipped to this interior. A degenerate crop returns None.
    """
    if image is None or len(region) != 4 or len(cell_bbox) != 4:
        return None
    width, height = image.size
    rw, rh = region[2]-region[0], region[3]-region[1]
    if width <= 0 or height <= 0 or rw <= 0 or rh <= 0:
        return None
    sx, sy = width/rw, height/rh
    box = [(cell_bbox[0]-region[0])*sx, (cell_bbox[1]-region[1])*sy,
           (cell_bbox[2]-region[0])*sx, (cell_bbox[3]-region[1])*sy]
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    with image.convert('L') as gray:
        values = np.asarray(gray)
        # A pale rule is still a rule. Compare with the actual paper level,
        # so an off-white scan background is not itself treated as one.
        threshold = max(1, min(250, float(np.percentile(values, 90))-4))
        ink = values < threshold
    measurements = [
        _rule_inset(ink, box[0], box[1], box[3], 'x', 1, sx),
        _rule_inset(ink, box[1], box[0], box[2], 'y', 1, sy),
        _rule_inset(ink, box[2], box[1], box[3], 'x', -1, sx),
        _rule_inset(ink, box[3], box[0], box[2], 'y', -1, sy),
    ]
    insets = [value[0] for value in measurements]
    pixels = [max(0, math.ceil(box[0]+insets[0])), max(0, math.ceil(box[1]+insets[1])),
              min(width, math.floor(box[2]-insets[2])), min(height, math.floor(box[3]-insets[3]))]
    if content_bbox is not None:
        if len(content_bbox) != 4:
            return None
        requested = [math.floor((content_bbox[0]-region[0])*sx), math.floor((content_bbox[1]-region[1])*sy),
                     math.ceil((content_bbox[2]-region[0])*sx), math.ceil((content_bbox[3]-region[1])*sy)]
        pixels = [max(pixels[0], requested[0]), max(pixels[1], requested[1]),
                  min(pixels[2], requested[2]), min(pixels[3], requested[3])]
    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
        return None
    actual = [region[0]+pixels[0]/sx, region[1]+pixels[1]/sy,
              region[0]+pixels[2]/sx, region[1]+pixels[3]/sy]
    proof = dict(method='physical_cell_interior_excluding_measured_edge_rules',
                 source_cell_bbox=list(cell_bbox), source_pixel_bbox=pixels,
                 source_content_bbox=actual, minimum_inset_pixels=3,
                 edge_insets_pixels=insets, edge_rule_pixel_runs=[value[1] for value in measurements])
    return tuple(pixels), actual, proof
