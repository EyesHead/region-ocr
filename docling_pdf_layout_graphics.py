"""Reserve explicitly captioned layout objects before page OCR and table inference."""
from contextlib import contextmanager
from dataclasses import replace
from math import ceil, floor
import re

from docling_pdf_forms import FIGURE_CAPTION, FORM_HEADING


_OBJECTS = {'table', 'document_index', 'picture', 'form'}
_TEXT = {'caption', 'section_header', 'text', 'title'}


def _box(cluster):
    b = cluster.bbox
    return b.l, b.t, b.r, b.b


def _inside(inner, outer):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


_GRAPHIC_NAME = r'(?:ф\s*о\s*р\s*м\s*а|р\s*и\s*с(?:\s*у\s*н\s*о\s*к|\s*\.))'
_NAMED_GRAPHIC = re.compile(rf'{_GRAPHIC_NAME}\s*[:—–-]\s*\S.*', re.I)
_NUMBERED_TITLE = re.compile(rf'(?i:{_GRAPHIC_NAME})\s*(?:[A-Za-zА-Яа-яЁё]\s*\.\s*)?\d+(?:[.-]\d+)*'
                             r'\s+[А-ЯЁA-Z]\S*.*')


def _caption(text, heading=False):
    return (FORM_HEADING.fullmatch(text) or FIGURE_CAPTION.fullmatch(text)
            or _NAMED_GRAPHIC.fullmatch(text)
            or heading and _NUMBERED_TITLE.fullmatch(text)
            or re.fullmatch(rf'{_GRAPHIC_NAME}\s*[:.]?', text, re.I))


def _same_object(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    return intersection > .92 * max((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))


def _leading_rule(ink, box):
    """Find an outer horizontal border below text included by the detector."""
    import numpy as np
    l, t, r, b = box
    crop = ink[t:b, l:r]
    wide = np.flatnonzero(np.count_nonzero(crop, axis=1) >= .7*(r-l))
    if not wide.size or wide[0] == 0:
        return None
    top = int(wide[0])
    occupied = np.flatnonzero(crop[:top+1].any(axis=1))
    bands = np.split(occupied, np.flatnonzero(np.diff(occupied) > 1)+1)
    # At most two detached lines can precede a form. A connected rail
    # indicates that the candidate line lies inside a table cell.
    if not 2 <= len(bands) <= 3:
        return None
    return l, t+int(bands[-1][0]), r, b


def _preceding_lines(ink, body, text_boxes):
    """Read up to two physical lines in the object's column, nearest first."""
    import numpy as np

    left, top, right, _ = body
    # The title can be wider than a narrow drawing. Use its full layout box
    # when it overlaps the drawing and ends within a few line heights of it.
    for x0, y0, x1, y1 in text_boxes:
        if (0 <= top-y1 <= 5*(y1-y0)
                and min(right, x1)-max(left, x0) >= .5*min(right-left, x1-x0)):
            left, right = min(left, x0), max(right, x1)
    strip = ink[:top, left:right]
    if not strip.size:
        return
    rows = np.flatnonzero(strip.any(axis=1))
    if not rows.size:
        return
    limit = top
    for run in reversed(np.split(rows, np.where(np.diff(rows) > 1)[0]+1)):
        y0, y1 = int(run[0]), int(run[-1])+1
        # Layout boxes may start just inside the table's top border.
        if y1 == top:
            continue
        if limit-y1 > 5*(y1-y0):
            break
        columns = np.flatnonzero(strip[y0:y1].any(axis=0))
        yield left+int(columns[0]), y0, left+int(columns[-1])+1, y1
        limit = y0


def _touching_heading(ink, body, headings):
    """Separate an offset caption descender from an upper drawing stroke."""
    import numpy as np
    candidates = set()
    l, top, r, _ = body
    for x0, y0, x1, y1 in headings:
        height = y1-y0
        if height <= 0 or abs(y1-top) > .5*height:
            continue
        # In a full-width projection, ink at different x positions can
        # join the caption and the drawing even when they share no pixels.
        start = max(0, y0-height//4)
        strip = ink[start:min(len(ink), y1+height//4+1), x0:x1]
        rows = np.flatnonzero(strip.any(axis=1))
        if not rows.size:
            continue
        for run in np.split(rows, np.flatnonzero(np.diff(rows)>1)+1):
            t, b = start+int(run[0]), start+int(run[-1])+1
            if t < y0-height//4 or b > top+.3*height or min(b,y1)-max(t,y0) < .6*height:
                continue
            cols = np.flatnonzero(strip[run].any(axis=0))
            candidates.add((x0+int(cols[0]), t, x0+int(cols[-1])+1, b))
    if len(candidates) == 1:
        return candidates.pop()


def _separate_heading(ink, body, line, touching):
    """Retain the first drawing pixel outside a tightly located title."""
    import numpy as np
    l, t, r, b = body
    cl, ct, cr, cb = line
    if t >= cb or min(r, cr) <= max(l, cl):
        return body, False
    if t < ct and line != touching:
        return None, False
    t = max(t, ct)
    x0, x1 = max(l, cl), min(r, cr)
    upper = ink[t:cb, l:r].copy()
    shared = upper[:, x0-l:x1-l]
    if not shared.any():
        return body, False
    if line != touching:
        return None, False
    shared[:] = 0
    rows = np.flatnonzero(upper.any(axis=1))
    top = t+int(rows[0]) if rows.size else cb
    return (l, top, r, b), top < cb


def _read_line(image, box, backend, page):
    from bs4 import BeautifulSoup
    from surya.inference.schema import BatchInputItem

    # Provide a white margin without sampling adjacent lines.
    from PIL import ImageOps
    with image.crop(box) as crop, ImageOps.expand(crop, max(2, crop.height//4), 'white') as padded:
        outputs = backend.generate([BatchInputItem(image=padded, prompt_type='block', max_tokens=256,
            metadata={'purpose': 'layout_graphic_caption', 'page': page})])
    if len(outputs) != 1 or outputs[0] is None or outputs[0].error or not outputs[0].raw:
        return ''
    soup = BeautifulSoup(outputs[0].raw, 'html.parser')
    if soup.find(['table', 'img', 'math']):
        return ''
    return soup.get_text(' ', strip=True)


def _native_line(page, line, ink, pixels):
    """Use already parsed, painted PDF words only if they account for the ink."""
    import numpy as np
    parsed = getattr(page, 'parsed_page', None)
    if parsed is None or parsed.bitmap_resources:
        return None
    l, t, r, b = line
    covered = np.zeros((b-t, r-l), dtype=bool)
    words = []
    for cell in parsed.word_cells:
        box = cell.to_bounding_box().to_top_left_origin(page.size.height)
        x0, y0, x1, y1 = pixels((box.l, box.t, box.r, box.b))
        if not (l-1 <= (x0+x1)/2 <= r+1 and t-1 <= (y0+y1)/2 <= b+1):
            continue
        if (getattr(cell, 'rendering_mode', None) != 0 or cell.rgba.a != 255
                or min(cell.rgba.r, cell.rgba.g, cell.rgba.b) >= 245
                or not cell.text.strip() or '\ufffd' in cell.text):
            return None
        xa, ya = max(l, x0-1), max(t, y0-1)
        xb, yb = min(r, x1+1), min(b, y1+1)
        if xa >= xb or ya >= yb or not ink[ya:yb, xa:xb].any():
            return None
        covered[ya-t:yb-t, xa-l:xb-l] = True
        words.append((x0, cell.text))
    if not words or np.any(ink[t:b, l:r][~covered]):
        return None
    return ' '.join(word for _, word in sorted(words))


def _nearby_bounds(ink, box, margin):
    """Extend a clipped detector edge to the first whitespace beside it."""
    h, w = ink.shape
    l, t, r, b = box
    while l > max(0, box[0]-margin) and ink[t:b, l].any():
        l -= 1
    while r < min(w, box[2]+margin) and ink[t:b, r-1].any():
        r += 1
    while t > max(0, box[1]-margin) and ink[t, l:r].any():
        t -= 1
    while b < min(h, box[3]+margin) and ink[b-1, l:r].any():
        b += 1
    return l, t, r, b


@contextmanager
def captioned_layout_input(work, page, scale, backend, proofs, diagnostic):
    from docling_core.types.doc import DocItemLabel

    layout = page.predictions.layout
    objects = [c for c in (layout.clusters if layout else ())
               if c.label in _OBJECTS and c.confidence >= .5]
    if not objects:
        yield work
        return
    import numpy as np
    import cv2

    rect = work.rect
    width, height = work.image.size
    def pixels(box):
        l, t, r, b = box
        return (max(0, floor((l-rect.l)*scale)), max(0, floor((t-rect.t)*scale)),
                min(width, ceil((r-rect.l)*scale)), min(height, ceil((b-rect.t)*scale)))

    def points(box):
        l, t, r, b = box
        return [l/scale+rect.l, t/scale+rect.t, r/scale+rect.l, b/scale+rect.t]

    with work.image.convert('L') as gray:
        ink = cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    text_boxes = [pixels(_box(c)) for c in layout.clusters if c.label in _TEXT and c.confidence >= .5]
    heading_boxes = [pixels(_box(c)) for c in layout.clusters
                     if c.label in {'caption', 'section_header', 'title'} and c.confidence >= .5]
    objects.sort(key=lambda c: -c.bbox.area())
    groups = []
    for obj in objects:
        body = pixels(_box(obj))
        if body[2] <= body[0] or body[3] <= body[1]:
            continue
        aliases = next((group for group in groups if _same_object(body, pixels(_box(group[0])))), None)
        if aliases is None:
            groups.append([obj])
        else:
            aliases.append(obj)
    accepted, reads = [], {}
    for group in groups:
        boxes = [pixels(_box(c)) for c in group]
        body = min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)
        if any(_inside(body, prev[1]) for prev in accepted):
            continue
        inner = _leading_rule(ink, body)
        for candidate in ([inner, body] if inner else [body]):
            touching = _touching_heading(ink, candidate, heading_boxes)
            lines = _preceding_lines(ink, candidate, text_boxes)
            if touching:
                # The model supplies the line's reading column. A full
                # projection can discard that line as part of the table.
                from itertools import chain
                lines = chain((touching,), (other for other in lines if other[3] <= touching[1]))
            for index, line in enumerate(lines):
                if index >= 2 or (candidate == inner and line[1] < body[1]):
                    break
                if line not in reads:
                    reads[line] = (_native_line(page, line, ink, pixels)
                                   or _read_line(work.image, line, backend, page.page_no))
                text = reads[line]
                if _caption(text, heading=any(_inside(line, box) for box in heading_boxes)):
                    if (min(line[2], body[2]) <= max(line[0], body[0])
                            and not FORM_HEADING.fullmatch(text)):
                        break
                    if text.casefold().startswith('р') and any(
                        0 <= line[1]-above[3] <= max(candidate[1]-line[3], 2.5*(line[3]-line[1]))
                        and min(line[2], above[2])-max(line[0], above[0]) >= .5*(line[2]-line[0])
                        for prior in objects if prior not in group for above in [pixels(_box(prior))]):
                        # A caption printed directly below a preceding drawing
                        # can already belong to that drawing.
                        break
                    margin = ceil((line[3]-line[1])*2)
                    bounded = _nearby_bounds(ink, candidate, margin)
                    bounded, overlaps = _separate_heading(ink, bounded, line, touching)
                    if bounded:
                        accepted.append((group, bounded, line, text, index, overlaps))
                    break
                if not text or re.match(r'^(?:(?:продолжение|окончание)\s+)?табл(?:ица|ицы|\.)', text, re.I):
                    break
            if accepted and accepted[-1][0] is group:
                break
    # A single title between two adjacent columns does not identify its owner.
    selected = [entry for entry in accepted if sum(other[2] == entry[2] for other in accepted) == 1]
    if not selected:
        yield work
        return
    masked = work.image.copy()
    try:
        for group, body, line, text, intervening, overlaps in selected:
            obj = group[0]
            masked.paste('white', body)
            if overlaps:
                with work.image.crop(line) as caption:
                    masked.paste(caption, line)
            bbox = points(body)
            proof = dict(page=page.page_no, bbox=bbox, caption_bbox=points(line),
                         caption_text=text, intervening_lines=intervening, overlaps_caption_tail=overlaps)
            proofs.append(proof)
            diagnostic('LAYOUT_GRAPHIC_OCR_SKIPPED', **proof)
            # The postprocessor and TableFormer both consume this layout.
            obj.label = DocItemLabel.PICTURE
            obj.bbox = obj.bbox.model_copy(update=dict(zip(('l','t','r','b'), bbox)))
            layout.clusters = [c for c in layout.clusters if c is obj or (
                c not in group and not _inside(pixels(_box(c)), body))]
        yield replace(work, image=masked)
    finally:
        masked.close()
