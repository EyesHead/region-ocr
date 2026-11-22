"""Keep positioned primary prose editable when layout alone called it a table.

This is deliberately not a table reconstruction. No cell grid or text is
invented; incomplete source coverage declines this recovery route.
"""
from hashlib import sha256
import math
import re

from bs4 import BeautifulSoup

from docling_pdf_structure import _raster_record_coverage


def _box(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in value)
            and value[0] < value[2] and value[1] < value[3])


def _overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _source_form_fields(entries, regions, page_no, ink_threshold):
    """Recover printed field lengths only when the surrounding line agrees."""
    if regions is None:
        return []
    candidates = []
    for entry in entries:
        if '__' not in entry['html']:
            continue
        node = BeautifulSoup(entry['html'], 'html.parser').p
        if node is not None and node.string and not node.find() and '__' in node.string:
            candidates.append((entry, node))
    if not candidates:
        return []

    import pypdfium2.raw as raw
    from pypdfium2 import PdfiumError
    import numpy as np

    proofs = []
    page = None
    try:
        page = regions.pdf[page_no-1]
        text = page.get_textpage()
        try:
            height = page.get_height()
            owners = [[] for _ in candidates]
            for i in range(text.count_chars()):
                x0, b, x1, t = text.get_charbox(i, loose=True)
                x, y = (x0+x1)/2, height-(t+b)/2
                rows = [j for j, (entry, _) in enumerate(candidates)
                        if entry['bbox'][0] <= x <= entry['bbox'][2]
                        and entry['bbox'][1] <= y <= entry['bbox'][3]]
                if len(rows) > 1:
                    return []
                if rows:
                    owners[rows[0]].append(i)
            for (entry, node), indices in zip(candidates, owners):
                chars = [chr(raw.FPDFText_GetUnicode(text, i)) for i in indices]
                literal = ''.join(chars).strip()
                ocr = str(node.string).strip()
                if (literal == ocr or literal.startswith('_') or literal.endswith('_')
                        or re.sub('_+', '_', literal) != re.sub('_+', '_', ocr)):
                    continue
                # Each underscore must be a separate, painted PDF character
                # whose tight glyph box contains the visible horizontal stroke.
                previous_end = -math.inf
                for i, char in zip(indices, chars):
                    if char != '_':
                        continue
                    obj = raw.FPDFText_GetTextObject(text, i)
                    if not obj or raw.FPDFTextObj_GetTextRenderMode(obj) != raw.FPDF_TEXTRENDERMODE_FILL:
                        break
                    left, bottom, right, top = text.get_charbox(i)
                    middle = (left+right)/2
                    if not (left < middle < right and middle > previous_end and 0 < top-bottom < right-left):
                        break
                    previous_end = right
                    with regions.crop(page_no, [left, height-top, right, height-bottom]) as glyph:
                        with glyph.convert('L') as gray:
                            pixels = np.asarray(gray)
                            # Ignore the outer columns rounded by the renderer.
                            if gray.width < 3 or not (pixels[:, 1:-1] <= ink_threshold).any(axis=0).all():
                                break
                else:
                    node.string.replace_with(literal)
                    entry['html'] = str(node)
                    proofs.append(dict(record_index=entry['index'],
                        source_runs=[len(run) for run in re.findall('_+', literal)],
                        primary_runs=[len(run) for run in re.findall('_+', ocr)]))
        finally:
            text.close()
    except (PdfiumError, ValueError, IndexError, OverflowError):
        pass  # The primary line remains usable without a native field-length proof.
    finally:
        if page is not None:
            page.close()
    return proofs


def recover_primary_text_region(image, region, records, page, api, *, source_regions=None):
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
        selected.append(dict(bbox=list(bounds), layout_bbox=list(bounds),
                             html=str(soup), record=record, index=index))
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
        ink_threshold, ink = cv2.threshold(np.asarray(gray), 0, 255,
                                          cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
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

    # Detectors may overlap the margins of two successive text lines. Resolve
    # only vertically ordered, horizontally aligned packets separated by a
    # clear scanline inside their common band. Keep the original OCR boxes
    # for coverage, ownership and diagnostics.
    selected.sort(key=lambda entry: (entry['bbox'][1], entry['bbox'][0]))
    separators = []
    for i, lower in enumerate(selected):
        bx, by, br, bb = lower['bbox']
        for upper in selected[:i]:
            ax, ay, ar, ab = upper['bbox']
            if not _overlap(upper['bbox'], lower['bbox']):
                continue
            left, right = max(ax, bx), min(ar, br)
            if (not left < (ax+ar)/2 < right or not left < (bx+br)/2 < right
                    or not (ay+ab)/2 < by < ab < (by+bb)/2
                    or (ab-by)*sy > glyph_height):
                return None
            # Use whole pixels lying inside both vertical bounds.
            first = max(1, math.ceil((by-region[1])*sy))
            stop = min(image.height-1, math.floor((ab-region[1])*sy))
            if first >= stop:
                return None
            x0 = max(0, math.floor((min(ax, bx)-region[0])*sx))
            x1 = min(image.width, math.ceil((max(ar, br)-region[0])*sx))
            top = max(0, math.ceil((ay-region[1])*sy))
            bottom = min(image.height, math.floor((bb-region[1])*sy))
            if (x0 >= x1 or not ink[top:first, x0:x1].any()
                    or not ink[stop:bottom, x0:x1].any()):
                return None
            clear = np.flatnonzero(~ink[first:stop, x0:x1].any(axis=1))
            if not len(clear):
                return None
            cut = region[1]+(first+int(clear[len(clear)//2])+.5)/sy
            upper['layout_bbox'][3] = min(upper['layout_bbox'][3], cut)
            lower['layout_bbox'][1] = max(lower['layout_bbox'][1], cut)
            separators.append(dict(above=upper['index'], below=lower['index'], y=cut))

    # Source boxes define row bands, including side-by-side title/identifier
    # blocks whose tops differ. A pure top/left sort would reverse that row.
    rows = []
    for entry in sorted(selected, key=lambda value: (value['layout_bbox'][1], value['bbox'][0])):
        if rows and entry['layout_bbox'][1] < max(old['layout_bbox'][3] for old in rows[-1]):
            rows[-1].append(entry)
        else:
            rows.append([entry])
    form_fields = _source_form_fields(selected, source_regions, page, ink_threshold)
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
        boundary_repairs=boundary_repairs, row_separators=separators, source_form_fields=form_fields,
        source_rgb_sha256=sha256(image.tobytes()).hexdigest(),
        html_sha256=sha256(fragment.encode()).hexdigest(), **coverage)
    return fragment, proof
