"""Keep positioned primary prose editable when layout alone called it a table.

This is deliberately not a table reconstruction. No cell grid or text is
invented; incomplete source coverage declines this recovery route.
"""
from hashlib import sha256
import math

from bs4 import BeautifulSoup

from docling_pdf_structure import _raster_record_coverage


def _box(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in value)
            and value[0] < value[2] and value[1] < value[3])


def _overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def recover_primary_text_region(image, region, records, page, api):
    """Return (HTML, proof) only for completely covered, non-grid text regions.

    The caller must already have ruled out a usable table candidate, a source
    grid, graphics and source-cell/packet ownership fallbacks. This helper also
    rejects ambiguous/non-text primary blocks, visible residual ink and
    vertical source rules. Small packet-boundary differences require measured
    glyph-scale agreement and a clear source edge. It never calls OCR or writes
    to a cache.
    """
    if not _box(region) or not records or image.width <= 0 or image.height <= 0:
        return None
    allowed = {'p', 'br', 'b', 'strong', 'i', 'em', 'span', 'small', 'sup', 'sub', 'hr'}
    selected = []
    for index, record in enumerate(records):
        if record.get('page_no') != page:
            continue
        bounds = record.get('bbox')
        if not _box(bounds):
            # Without coordinates it is impossible to establish ownership.
            return None
        if not _overlap(region, bounds):
            continue
        if record.get('label') not in {'Text', 'SectionHeader', 'Title', 'TableOfContents'}:
            return None
        soup = BeautifulSoup(record.get('html', ''), 'html.parser')
        if (len(soup.find_all('p')) != 1 or not soup.get_text(strip=True)
                or any(tag.name not in allowed for tag in soup.find_all())
                or soup.get_text('', strip=True) != soup.p.get_text('', strip=True)):
            return None
        # OCR formatting is retained, but arbitrary markup attributes are not.
        for tag in soup.find_all():
            tag.attrs = {}
        if any(_overlap(bounds, old['bbox']) for old in selected):
            return None
        selected.append(dict(bbox=list(bounds), html=str(soup), record=record, index=index))
    if not selected:
        return None

    class ExistingCrop:
        def crop(self, page_no, bounds):
            return image.copy()

    coverage = _raster_record_coverage(ExistingCrop(), page, region,
                                     [entry['record'] for entry in selected])
    if (not coverage or not coverage['covered'] or coverage['x_grid']
            or coverage['grid_intersections']):
        return None

    # Layout and primary OCR predict their rectangles independently. A packet
    # ending just beyond a clear crop edge is not proof of neighbouring text.
    # Do not admit an arbitrary overlap: bound every overhang by the measured
    # source glyph height and reject visible ink touching the affected edge.
    import cv2
    import numpy as np

    with image.convert('L') as gray:
        ink = cv2.threshold(np.asarray(gray), 0, 255,
                            cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    sx = image.width/(region[2]-region[0])
    sy = image.height/(region[3]-region[1])
    glyph_height = coverage['source_glyph_height_pixels']
    edge_ink = (ink[:, 0], ink[0, :], ink[:, -1], ink[-1, :])
    boundary_repairs = []
    for entry in selected:
        box = entry['bbox']
        overhang = [max(0., region[0]-box[0]), max(0., region[1]-box[1]),
                    max(0., box[2]-region[2]), max(0., box[3]-region[3])]
        if any(value*scale > glyph_height for value, scale in zip(overhang, (sx, sy, sx, sy))):
            return None
        if any(value > 0 and np.any(edge) for value, edge in zip(overhang, edge_ink)):
            return None
        if any(overhang):
            boundary_repairs.append(dict(record_index=entry['index'],
                source_bbox=list(box), overhang_points=overhang,
                source_glyph_height_pixels=glyph_height, affected_source_edges_clear=True))

    # Source boxes define row bands, including side-by-side title/identifier
    # blocks whose tops differ. A pure top/left sort would reverse that row.
    rows = []
    for entry in sorted(selected, key=lambda value: (value['bbox'][1], value['bbox'][0])):
        if rows and entry['bbox'][1] < max(old['bbox'][3] for old in rows[-1]):
            rows[-1].append(entry)
        else:
            rows.append([entry])
    width = region[2] - region[0]
    output = ['<div class="primary-text-region">']
    ordered = []
    bottom = region[1]
    for row in rows:
        row.sort(key=lambda entry: entry['bbox'][0])
        top = min(entry['bbox'][1] for entry in row)
        gap = max(0, top-bottom)/width*100
        output.append(f'<div style="display:flex;align-items:flex-start;margin-top:{gap:.5f}%">')
        right = region[0]
        for entry in row:
            left, _, end, _ = entry['bbox']
            left, end = max(region[0], left), min(region[2], end)
            offset = max(0, left-right)/width*100
            percent = (end-left)/width*100
            align = ('left' if entry['record'].get('label') == 'TableOfContents'
                     or len(selected) == 1 and entry['record'].get('label') == 'Text' else 'center')
            output.append(f'<div style="flex:0 1 {percent:.5f}%;min-width:0;'
                          f'margin-left:{offset:.5f}%;text-align:{align}">{entry["html"]}</div>')
            right = end
            ordered.append(entry)
        output.append('</div>')
        bottom = max(entry['bbox'][3] for entry in row)
    output.append('</div>')
    fragment = ''.join(output)
    proof = dict(method='retained_positioned_primary_text', page=page,
        bbox=list(region), record_indexes=[entry['index'] for entry in ordered],
        record_sha256=[sha256(entry['record']['html'].encode()).hexdigest() for entry in ordered],
        record_bboxes=[entry['bbox'] for entry in ordered],
        record_labels=[entry['record'].get('label') for entry in ordered],
        boundary_repairs=boundary_repairs,
        source_rgb_sha256=sha256(image.tobytes()).hexdigest(),
        html_sha256=sha256(fragment.encode()).hexdigest(), **coverage)
    return fragment, proof
