"""Locate an extra repeated OCR cell using source rules and source pixels."""
from hashlib import sha256
import re


def _key(node):
    if node.name != 'td' or node.find(['img', 'math', 'svg', 'table']):
        return None
    try:
        unit_span = all(int(node.get(attr, 1)) == 1 for attr in ('rowspan', 'colspan'))
    except (TypeError, ValueError):
        return None
    if not unit_span:
        return None
    if not 0 < len(node.get_text(strip=True)) <= 24:
        return None
    return re.sub(r'\s+', ' ', node.decode_contents()).strip()


def remove_source_proven_duplicate(nodes, cells, aligned_rows, nongrid, region, sx, sy, radius):
    """Return a reduced row only when its position is independently determined.

    An overlong row can have several repeated runs. Do not cut its last cell:
    compare the competing boundary assignments to already aligned source-cell
    pixels. No character is recognized, generated or replaced here.
    """
    import cv2
    import numpy as np

    if len(nodes) != len(cells)+1 or any(c['column_span'] != 1 or c['row_span'] != 1 for c in cells):
        return None, None
    if len(aligned_rows) < 2:
        return None, None
    candidates = {}
    keys = [_key(node) for node in nodes]
    for index in range(1, len(nodes)):
        if keys[index] and keys[index] == keys[index-1]:
            reduced = nodes[:index]+nodes[index+1:]
            candidates.setdefault(tuple(_key(node) or str(node) for node in reduced), (index, reduced))
    if not candidates:
        return None, None

    def pattern(cell):
        box = cell['bbox']
        pad = radius+1
        left = max(0, round((box[0]-region[0])*sx)+pad)
        right = min(nongrid.shape[1], round((box[2]-region[0])*sx)-pad)
        top = max(0, round((box[1]-region[1])*sy)+pad)
        bottom = min(nongrid.shape[0], round((box[3]-region[1])*sy)-pad)
        mask = nongrid[top:bottom, left:right]
        ys, xs = np.nonzero(mask)
        if not len(xs):
            return None
        mask = mask[ys.min():ys.max()+1, xs.min():xs.max()+1]
        width = min(180, max(1, round(mask.shape[1]/mask.shape[0]*32)))
        glyph = cv2.resize(mask, (width, 32), interpolation=cv2.INTER_AREA) > 96
        result = np.zeros((40, 192), np.uint8)
        result[4:36, 4:4+width] = glyph
        return result

    def similarity(left, right):
        if left is None or right is None:
            return 0.0
        kernel = np.ones((3, 3), np.uint8)
        a = np.count_nonzero(left & cv2.dilate(right, kernel))/max(1, np.count_nonzero(left))
        b = np.count_nonzero(right & cv2.dilate(left, kernel))/max(1, np.count_nonzero(right))
        return float(min(a, b))

    if len(candidates) == 1:
        index, reduced = next(iter(candidates.values()))
        # A sole repeated run is not itself evidence: the repeated values
        # may be legitimate and a distinct trailing value may be the error.
        templates = {}
        for other_nodes, other_cells in aligned_rows:
            for node, cell in zip(other_nodes, other_cells):
                key = _key(node)
                if key and cell['column_span'] == cell['row_span'] == 1:
                    value = pattern(cell)
                    if value is not None:
                        templates.setdefault(key, []).append(value)
        scores = []
        for i in range(index, len(cells)):
            measured = pattern(cells[i])
            matches = sorted((similarity(measured, value)
                              for value in templates.get(_key(reduced[i]), [])), reverse=True)
            if len(matches) < 2 or matches[1] < .70:
                return None, None
            scores.append(matches[1])
            if _key(reduced[i]) != _key(nodes[i]):
                alternate = sorted((similarity(measured, value)
                                    for value in templates.get(_key(nodes[i]), [])), reverse=True)
                if len(alternate) < 2 or matches[1]-alternate[1] < .15:
                    return None, None
        method = 'unique_duplicate_run_confirmed_by_aligned_source_cell_pixels'
    else:
        alternatives = list(candidates.values())
        differing = [i for i in range(len(cells))
                     if len({_key(row[i]) for _, row in alternatives}) > 1]
        templates = {}
        for other_nodes, other_cells in aligned_rows:
            for node, cell in zip(other_nodes, other_cells):
                key = _key(node)
                if key and cell['column_span'] == cell['row_span'] == 1:
                    value = pattern(cell)
                    if value is not None:
                        templates.setdefault(key, []).append(value)
        measured = {i: pattern(cells[i]) for i in differing}
        rankings = []
        for index, row in alternatives:
            scores = []
            for i in differing:
                matches = sorted((similarity(measured[i], value)
                                  for value in templates.get(_key(row[i]), [])), reverse=True)
                if len(matches) < 2:
                    return None, None
                scores.append(matches[1])  # Two independently aligned source cells must support it.
            rankings.append((min(scores, default=0), sum(scores)/max(1, len(scores)), index, row, scores))
        rankings.sort(key=lambda value: value[:2], reverse=True)
        best, runner_up = rankings[:2]
        if best[0] < .70 or best[1]-runner_up[1] < .15:
            return None, None
        _, _, index, reduced, scores = best
        method = 'duplicate_run_disambiguated_by_aligned_source_cell_pixels'
    proof = dict(reference_cell=index, reason='duplicate_ocr_cell_outside_measured_source_row',
                 source_cells=len(cells), reference_cells=len(nodes), method=method,
                 payload_sha256=sha256(nodes[index].decode_contents().encode()).hexdigest(),
                 source_pixel_scores=scores, retained_payloads_unchanged=True)
    return reduced, proof
