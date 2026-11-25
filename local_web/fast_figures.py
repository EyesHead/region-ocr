"""Source graphics and their text anchors for the lightweight PDF path."""
from __future__ import annotations

from concurrent.futures import CancelledError
from html import escape
from io import BytesIO
from itertools import groupby
from statistics import median
import math
import re

from docling_pdf_forms import FIGURE_CAPTION, FORM_HEADING

from local_web.fast_formulas import (EQUATION_NUMBER, FormulaCrop, attach_fraction_terms, coverage,
                                     equation_number_words, ordinary_text, text_confirms_formula,
                                     text_regions, vector_fractions)
from local_web.fast_image_assets import crop_png, make_image_asset
from local_web.fast_resources import FORMULA_CROP_ATTR, register_image

_EQUATION = EQUATION_NUMBER
_EMPTY_MATH = re.compile(r'^[\s;,:.()—–=+\-]*$')
_MATH = re.compile(r'[=±≈≤≥∑√∫^_α-ω]|[A-Za-zА-Яа-я]\s*[+/×÷]\s*\w')
_LEGEND = re.compile(r'(?:^|;)\s*(?:[A-Za-zА-Яа-яЁё]|\d{1,2})\s*[-—–−)]\s*\S')
_FORMULA_INTRO = re.compile(r'\b(?:по формул[аеы]|по выражению|из соотношения)\s*[:.]?\s*$', re.I)
_FORMULA_SLOT = re.compile(r'<math display="(?:inline|block)" data-formula-slot="(\d+)"></math>')


def _union(boxes):
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _inside(box, outer):
    return outer[0] <= box[0] and outer[1] <= box[1] and box[2] <= outer[2] and box[3] <= outer[3]


def _overlap(a, b):
    return max(0, min(a[2], b[2])-max(a[0], b[0])) / max(1, min(a[2]-a[0], b[2]-b[0]))


def _legend(lines):
    return not lines or (_LEGEND.search(lines[0]) and not any(
        _heading(s) or re.match(r'^\d+(?:\.\d+)+\s', s) for s in lines))


def _heading(text):
    for pattern, kind in ((FIGURE_CAPTION, 'figure'), (FORM_HEADING, 'form')):
        match = pattern.fullmatch(text)
        if match:
            # The shared pattern may backtrack "6.9), ..." to number "6" and title "9), ...".
            title = match.group('title') or ''
            if not re.match(r'^\d+\)\s*[,;]', title):
                return kind, match.group('number')
    return None


def _bind(lines, boxes, em):
    """Associate anchored bodies and join adjacent panels when only their legend intervenes."""
    out = {}
    for index, (text, cap) in enumerate(lines):
        heading = _heading(text)
        if heading is None:
            continue
        after = heading[0] == 'form'
        choices = []
        for box in boxes:
            low, high = (cap[3], box[1]) if after else (box[3], cap[1])
            if not -.3*em <= high-low <= 16*em or _overlap(cap, box) < .4:
                continue
            between = [s for s, b in lines if low <= b[1] and b[3] <= high]
            # Forms may have a short printed title between header and ruled body.
            title = after and len(between) == 1 and len(between[0].split()) < 12 and not _heading(between[0])
            if _legend(between) or title:
                choices.append((high-low, box))
        if not choices:
            continue
        _, body = min(choices)
        parts = {body}
        while True:
            extra = set()
            for box in boxes:
                if box in parts:
                    continue
                low, high = (body[3], box[1]) if after else (box[3], body[1])
                dx = max(0, box[0]-body[2], body[0]-box[2])
                y_overlap = min(box[3], body[3])-max(box[1], body[1])
                side = y_overlap >= .75*min(box[3]-box[1], body[3]-body[1]) and dx <= 2.5*em
                if not side and not (-.3*em <= high-low <= 6*em and _overlap(body, box) >= .5):
                    continue
                between = [s for s, b in lines if low <= b[1] and b[3] <= high and _overlap(body,b)]
                if side or (_legend(between) and (high-low <= 2.5*em or between)):
                    extra.add(box)
            if not extra:
                break
            parts.update(extra)
            body = _union(parts)
        out[index] = body
    return out


def _raster_bodies(image, size, lines, em):
    import cv2
    import numpy as np

    width, height = size
    scale = min(1, 1200 / max(image.size))
    array = np.asarray(image)
    if scale < 1:
        array = cv2.resize(array, (round(image.width*scale), round(image.height*scale)), interpolation=cv2.INTER_AREA)
    if array.ndim == 3:
        array = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    rows, cols = array.shape
    sx, sy = cols/width, rows/height
    _, ink = cv2.threshold(array, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    for text, (l,t,r,b) in lines:
        if len(text.split()) >= 5 or _heading(text):
            ink[max(0,round(t*sy)-1):min(rows,round(b*sy)+2), max(0,round(l*sx)-1):min(cols,round(r*sx)+2)] = 0
    radius = max(1, round(.30*em*(sx+sy)/2))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2*radius+1,2*radius+1))
    _, _, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(ink,kernel), connectivity=8)
    return [((x+radius)/sx,(y+radius)/sy,(x+w-radius)/sx,(y+h-radius)/sy)
            for x,y,w,h,area in stats[1:]
            if w-2*radius >= 4*em*sx and h-2*radius >= 4*em*sy and area >= 12*em*sx*em*sy]


def _equations(rows, images, em, width, row_cells=()):
    found = {}
    for i, row in enumerate(rows):
        text = ' '.join(w[4] for w in row)
        tag = _EQUATION.search(text)
        if not tag or (not _EMPTY_MATH.fullmatch(text[:tag.start()]) and
                       (row[-1][0] < .56*width or tag[0][-1] in '.,;')):
            continue
        number_words = equation_number_words(row) or row[-1:]
        number_box = _union(number_words)
        edge = number_box[0]
        cy = (number_box[1]+number_box[3])/2
        cell = row_cells[i] if row_cells else None
        nearby = [box for box in images if _inside(box,cell)] if cell else images
        refs = [b for b in nearby if b[2] <= edge+.2*em and b[1]-.3*em <= cy <= b[3]+.3*em
                and b[3]-b[1] < 12*em and b[2]-b[0] > .5*em]
        if not refs and len(row) == 1 and _EQUATION.fullmatch(text):
            above = [b for b in nearby if b[2] < edge and 0 < cy-b[3] < 2*em and b[3]-b[1] < 8*em]
            below = [b for b in nearby if b[2] < edge and 0 < b[1]-cy < 2*em and b[3]-b[1] < 8*em]
            if above and below and _overlap(_union(above),_union(below)) >= .6:
                refs = above + below
        if refs:
            # Keep independent prose before/after the printed formula editable.
            left,_,right,_ = _union(refs)
            mixed = [w[:4] for w in row[:-len(number_words)] if left < (w[0]+w[2])/2 < right]
            found[i] = _union(refs + mixed)
    return found


def _png(image):
    buf = BytesIO()
    image.save(buf, 'PNG', compress_level=1)
    return buf.getvalue()


def _crop(page, image, box, size):
    width, height = size
    l, t, r, b = max(0, box[0]-1), max(0, box[1]-1), min(width, box[2]+1), min(height, box[3]+1)
    if image is not None:
        with image.crop((math.floor(l*image.width/width), math.floor(t*image.height/height),
                         math.ceil(r*image.width/width), math.ceil(b*image.height/height))) as section:
            return _png(section)
    return crop_png(page,(l,t,r,b),scale=min(2.5,2600/max(width,height)))


def _resource(assets, mime, data):
    if assets is None:
        from base64 import b64encode
        return 'data:'+mime+';base64,'+b64encode(data).decode('ascii')
    return register_image(assets, mime, data)


def _markup(assets, parts, kind, box, page_no, originals=()):
    mime, data, original_mime, original = parts
    src = _resource(assets,mime,data)
    urls = dict.fromkeys(originals)
    if original and original_mime:
        urls[_resource(assets,original_mime,original)] = None
    elif mime == 'image/jpeg':
        urls[src] = None
    if len(urls) == 1:
        attr = f' data-original-src="{next(iter(urls))}"'
    elif urls:
        attr = ' data-original-srcs="'+' '.join(urls)+'"'
    else:
        attr = ''
    bounds = ','.join(f'{v:.2f}' for v in box)
    return (f'<img alt="{escape(kind, quote=True)}" data-source-page="{page_no}" '
            f'data-source-bbox="{bounds}" src="{src}"{attr}>')


def _formula_markup(assets, markup, crop, table_boxes):
    tag = re.match(r'<(?:math|span|code)(?=[\s>])', markup)
    if tag is None:
        return markup
    bounds = ','.join(f'{v:.2f}' for v in crop.bbox)
    source = f' data-source-page="{crop.page_no}" data-source-bbox="{bounds}"'
    if tag[0] == '<math' and any(coverage(crop.bbox, box) >= .8 for box in table_boxes):
        src = _resource(assets, 'image/png', crop.png)
        source += f' {FORMULA_CROP_ATTR}="{escape(src, quote=True)}"'
    return markup[:tag.end()] + source + markup[tag.end():]


def _rendered_formula(assets, rendered, crop, label, table_boxes=()):
    rendered = rendered.strip() if rendered else ''
    if not rendered:
        source = _markup(assets, ('image/png', crop.png, None, None), label, crop.bbox, crop.page_no)
        return source, 'formula_crops'
    if rendered.startswith(('<math', '<span data-formula-fragment="true">')):
        kind = 'formulas_mathml'
    elif rendered.startswith('<span'):
        kind = 'formulas_latex'
    else:
        kind = 'formulas_text'
    return _formula_markup(assets, rendered, crop, table_boxes), kind


class FormulaBatches:
    """Recognize a bounded batch and fill its source-owned HTML positions."""

    _MAX_CROPS = 16
    _MAX_BYTES = 16 * 1024 * 1024
    _MAX_PAGES = 4

    def __init__(self, reader, assets, stats, cancelled):
        self.reader = reader
        self.assets = assets
        self.stats = stats
        self.cancelled = cancelled
        self.pending = []
        self.pending_bytes = 0
        self.resolved = {}
        self.next_slot = 0
        self.first_page = None
        self.output_start = 0

    def place(self, crop, label):
        size = len(crop.png)
        if len(self.pending) == self._MAX_CROPS or (
                self.pending and self.pending_bytes + size > self._MAX_BYTES):
            self._recognize()
        slot = self.next_slot
        self.next_slot += 1
        self.pending.append((slot, crop, label))
        self.pending_bytes += size
        if self.first_page is None:
            self.first_page = crop.page_no
        display = 'inline' if crop.display in ('inline', False) else 'block'
        return f'<math display="{display}" data-formula-slot="{slot}"></math>'

    def finish_page(self, page_no, output):
        if self.first_page is None:
            self.output_start = len(output)
        elif (page_no - self.first_page + 1 >= self._MAX_PAGES
              or len(self.pending) >= self._MAX_CROPS or self.pending_bytes >= self._MAX_BYTES):
            self.flush(output)

    def _recognize(self):
        if not self.pending:
            return
        if self.cancelled:
            self.cancelled()
        try:
            rendered = list(self.reader([crop for _, crop, _ in self.pending]) or ())
        except CancelledError:
            raise
        except Exception as error:
            print(f'Surya: {type(error).__name__}: {error}', flush=True)
            rendered = []
        if self.cancelled:
            self.cancelled()
        for index, (slot, crop, label) in enumerate(self.pending):
            markup = rendered[index] if index < len(rendered) else None
            if markup and markup.strip():
                self.resolved[slot] = _rendered_formula(self.assets, markup, crop, label)
            else:
                text = crop.text.strip() or '[формула не распознана]'
                fragment = '<span>' + escape(text) + '</span>'
                self.resolved[slot] = _formula_markup(self.assets,fragment,crop,()), 'formulas_text'
        self.pending.clear()
        self.pending_bytes = 0

    def flush(self, output):
        self._recognize()

        def replace(match):
            result = self.resolved.pop(int(match[1]), None)
            if result is None:
                return ''
            markup, kind = result
            self.stats[kind] = self.stats.get(kind, 0) + 1
            return markup

        if self.resolved:
            for index in range(self.output_start, len(output)):
                if 'data-formula-slot=' in output[index]:
                    output[index] = _FORMULA_SLOT.sub(replace, output[index])
        self.resolved.clear()
        self.first_page = None
        self.output_start = len(output)


def _figure_name(text):
    kind, number = _heading(text)
    return ('Форма ' if kind == 'form' else 'Рисунок ') + number


def _ink_box(image, size, band, em):
    """Trim the available formula band to ink, rejecting page rules and empty bands."""
    w,h = size
    l,t,r,b = max(0,band[0]),max(0,band[1]),min(w,band[2]),min(h,band[3])
    if r-l < em or b-t < .4*em:
        return None
    sx,sy = image.width/w,image.height/h
    xx, yy = math.floor(l*sx), math.floor(t*sy)
    with image.crop((xx,yy,math.ceil(r*sx),math.ceil(b*sy))) as sub:
        # A white/gray margin is common to both scans and digital source plots.
        lo,hi = sub.getextrema()
        if hi-lo < 28:
            return None
        with sub.point(lambda v: 255 if v < lo+.58*(hi-lo) else 0) as ink:
            bounds = ink.getbbox()
    if not bounds:
        return None
    a,c,d,e = bounds
    if d-a < .65*em*sx or e-c < .4*em*sy:
        return None
    return ((xx+a)/sx,(yy+c)/sy,(xx+d)/sx,(yy+e)/sy)


def _raster_math(image, size, rows, scores, em, excluded, row_cells=()):
    """Use numbered bands, source cues or low-confidence math; preserve the original OCR string."""
    width,height = size
    lines = [(' '.join(w[4] for w in row), _union(row)) for row in rows]
    found = {}
    for i,(text,box) in enumerate(lines):
        if i in excluded:
            continue
        x,t,r,b = box
        cell = row_cells[i] if row_cells else None
        def ink(band):
            if cell:
                band = (max(band[0],cell[0]+1), max(band[1],cell[1]+1),
                        min(band[2],cell[2]-1), min(band[3],cell[3]-1))
            return _ink_box(image,size,band,em)

        tag = _EQUATION.search(text)
        anchor = _EQUATION.fullmatch(text.strip())
        low_math = scores is not None and min(scores[id(w)] for w in rows[i]) < .87 and _MATH.search(text)
        if tag and not anchor and _EMPTY_MATH.fullmatch(text[:tag.start()]):
            number = equation_number_words(rows[i])
            edge = min(word[0] for word in number)-.25*em if number else r
            residual = ink((0,t-.65*em,edge,b+.65*em))
            minimum = .65*em if number else (len(tag[0])+1)*em*.7
            if residual and residual[2]-residual[0] > minimum:
                found[i] = residual
            continue
        if tag and low_math and not anchor and not ordinary_text(text[:tag.start()]):
            number = equation_number_words(rows[i])
            found[i] = _union(rows[i][:-len(number)]) if number else box
            continue
        if anchor:
            if any(j != i and (not row_cells or row_cells[j] == cell) and q[2] < x and abs((q[1]+q[3]-t-b)/2) < .8*em
                   and len(s.split()) >= 4 and not _MATH.search(s) for j,(s,q) in enumerate(lines)):
                continue
            # OCR often returns the printed number as the only token in the formula band.
            same = [q for j,(s,q) in enumerate(lines) if j != i and (not row_cells or row_cells[j] == cell)
                    and abs((q[1]+q[3]-t-b)/2) < .8*em
                    and q[2] < x and (_MATH.search(s) or _EMPTY_MATH.fullmatch(s))]
            band = (0,t-1.45*em,x-.35*em,b+1.45*em)
            above = [q[3] for s,q in lines if q[3] <= t and ordinary_text(s)]
            below = [q[1] for s,q in lines if q[1] >= b and ordinary_text(s)]
            band = (band[0],max(band[1],max(above,default=0)),band[2],min(band[3],min(below,default=height)))
            residual = ink(band)
            if residual:
                found[i] = _union([residual,*same])
        elif _FORMULA_INTRO.search(text) and i+1 < len(lines) and (not row_cells or row_cells[i+1] == cell):
            next_text, next_box = lines[i+1]
            if re.match(r'^\s*где\b',next_text,re.I) and 1.4*em < next_box[1]-b < 12*em:
                residual = ink((0,b+.25*em,width,next_box[1]-.25*em))
                if residual:
                    found[i] = residual
    return found


def _unread_fractions(image, size, rows, em, occupied):
    """Find isolated fraction bars with unread glyphs immediately above and below."""
    import cv2
    import numpy as np

    width, height = size
    scale = min(1., 1400/max(image.size))
    gray = np.asarray(image)
    if scale < 1:
        gray = cv2.resize(gray, (max(1, round(image.width*scale)), max(1, round(image.height*scale))),
                          interpolation=cv2.INTER_AREA)
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_RGB2GRAY)
    sy, sx = gray.shape[0]/height, gray.shape[1]/width
    ex, ey = em*sx, em*sy
    if min(ex, ey) < 5:
        return []
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    # Work on intact components. Erasing text first could split a table grid into apparent bars.
    _, _, components, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    x, y, w, h, area = components[1:].T
    horizontal = (w >= 1.35*ex) & (w >= 7*h) & (h <= .22*ey) & (area >= .70*w*h)
    glyph = ((h >= .30*ey) & (h <= 1.35*ey) & (w >= .09*ex) & (w <= 1.5*ex)
             & (area >= .035*ex*ey) & (area >= .10*w*h))
    glyphs = components[1:][glyph]
    if not len(glyphs):
        return []
    gx, gy, gw, gh = glyphs[:, :4].T
    line_x, line_y, line_w, _ = components[1:][horizontal, :4].T
    known = [word[:4] for row in rows for word in row] + list(occupied)
    found = []
    for left, top, length, depth in components[1:][horizontal & (w <= 10*ex), :4]:
        right, bottom = left+length, top+depth
        if np.any((line_y != top) & (abs(line_y-top) < 2.4*ey)
                  & (np.minimum(right, line_x+line_w)-np.maximum(left, line_x) >= .55*length)):
            continue
        # Reject a surrounding grid or a vertical ruling next to the candidate.
        if np.any((h > 2.4*ey) & (y < top) & (y+h > bottom)
                  & (x < right+ex) & (x+w > left-ex)):
            continue
        within = (gx >= left-.16*ex) & (gx+gw <= right+.16*ex)
        upper = within & (gy+gh <= top) & (gy+gh >= top-.8*ey)
        lower = within & (gy >= bottom) & (gy <= bottom+.8*ey)
        if not upper.any() or not lower.any():
            continue
        centered = True
        for tier in (upper, lower):
            first, last = gx[tier].min(), (gx+gw)[tier].max()
            if last-first < .22*length or abs(first+last-left-right) > .42*length:
                centered = False
                break
        if not centered:
            continue
        l = min(left, gx[upper | lower].min())
        r = max(right, (gx+gw)[upper | lower].max())
        t, b = gy[upper].min(), (gy+gh)[lower].max()
        # At the same height an adjacent glyph indicates part of a larger expression or prose.
        if np.any((gy < b) & (gy+gh > t) & (gx < r+.55*ex) & (gx+gw > l-.55*ex) & ~(upper | lower)):
            continue
        box = (max(0., (l-.10*ex)/sx), max(0., (t-.10*ey)/sy),
               min(width, (r+.10*ex)/sx), min(height, (b+.10*ey)/sy))
        if any(a[0] < box[2] and box[0] < a[2] and a[1] < box[3] and box[1] < a[3]
               for a in known):
            continue
        known.append(box)
        found.append(box)
    return found


def _candidates(boxes, em, size):
    return [b for b in boxes if b[2]-b[0] >= 4*em and b[3]-b[1] >= 3*em
            and (b[2]-b[0])*(b[3]-b[1]) < .90*size[0]*size[1]]


def _image_parts(page, entries, box, raster, size):

    if len(entries) == 1:
        return make_image_asset(page, entries[0], box)
    return ('image/png', _crop(page,raster,box,size), None, None)


def _word_anchor(rows, lines, box, *, standalone=False, row_cells=()):
    if not rows:
        return None
    # Pick a line in the same column, then the word on the right of an inline picture.
    cy = (box[1]+box[3])/2
    def distance(i):
        b = lines[i][1]
        dy = max(0,b[1]-cy,cy-b[3])
        dx = max(0,box[0]-b[2],b[0]-box[2])
        return (dy+dx*.35,abs((b[1]+b[3])/2-cy))
    cx = (box[0]+box[2])/2
    candidates = [i for i,cell in enumerate(row_cells) if cell and cell[0]<=cx<=cell[2] and cell[1]<=cy<=cell[3]]
    row = rows[min(candidates or range(len(rows)),key=distance)]
    if standalone:
        if box[3] <= min(w[1] for w in row):
            return id(row[0]), False
        if box[1] >= max(w[3] for w in row):
            return id(row[-1]), True
    word = next((w for w in row if w[0]>=box[2]),None)
    if word is not None:
        return id(word),False
    if box[3]<=min(w[1] for w in row):
        return id(row[0]),False
    return id(row[-1]),True


def _edge_key(text):
    return re.sub(r'\d+', '#', ' '.join(text.casefold().split()))


def _edge_lines(lines, height):
    return {_edge_key(text) for text,b in lines if b[3] < .07*height or b[1] > .93*height}


def _cross_page(page_no, lines, rows, bound, size, context, anchors):
    previous = context.get('previous')
    if not previous or previous['page']+1 != page_no:
        return
    height = size[1]
    top = [(i,text,b) for i,(text,b) in enumerate(lines) if i not in bound and b[1] < .19*height
           and _heading(text) and _heading(text)[0]=='figure']
    if len(top) != 1:
        return
    index, text, cap = top[0]
    left,right = previous['span']
    cap_left,cap_right = cap[0]/size[0],cap[2]/size[0]
    overlap = min(right,cap_right)-max(left,cap_left)
    if overlap < .4*min(right-left,cap_right-cap_left):
        return
    repeated = previous['edges'] & _edge_lines(lines,height)
    preceding = [s for s,b in lines if b[3] <= cap[1] and not s.isdecimal() and _edge_key(s) not in repeated]
    trailing = [s for s in previous['following'] if not s.isdecimal() and _edge_key(s) not in repeated]
    if len(preceding)+len(trailing)>14 or not _legend(trailing+preceding):
        return
    graphic_id = previous['graphic_id']
    caption_id = f'fast-caption-{page_no}-{index}'
    old = f'alt="Изображение" id="{graphic_id}"'
    new = (f'alt="{escape(text,quote=True)}" id="{graphic_id}" '
           f'data-caption-ref="{caption_id}" data-caption-page="{page_no}"')
    context['match'] = old,new
    # Mark the original first word on the caption page, preserving reading order
    # even when the layout reader publishes the rest of the caption separately.
    word_id = id(rows[index][0])
    anchors[word_id,False] = anchors.get((word_id,False),'') + f'<span id="{caption_id}" data-source-image="{graphic_id}">'
    anchors[word_id,True] = '</span>'+anchors.get((word_id,True),'')


def page_graphics(page, page_no, rows, objects, image_assets, *, raster=None, scores=None, stats=None,
                  links=None, cancelled=None, formula_text=None, render_formulas=None, table_formula_boxes=(),
                  formula_batches=None, formula_cells=None, horizontal_rules=()):
    """Return graphic anchors and, when requested, replacements for formula tokens."""
    if not rows:
        return {}
    from pypdfium2 import PdfiumError
    row_cells = []
    cell_rows = {}
    if formula_cells:
        split = []
        for row in rows:
            for cell, group in groupby(row, key=lambda word: formula_cells.get(id(word))):
                part = list(group)
                split.append(part)
                row_cells.append(cell)
                cell_rows.setdefault(cell, []).append(part)
        rows = split
    table_formula_boxes = tuple(table_formula_boxes)
    lines = [(' '.join(w[4] for w in row), _union(row)) for row in rows]
    em = max(1,median(b[3]-b[1] for _,b in lines))
    size = page.get_size()
    # Whole-page image + distributed searchable text is a scan with a hidden OCR layer.
    boxes = [b for b in objects if (b[2]-b[0])*(b[3]-b[1]) < .82*size[0]*size[1]]
    captions = {i for i,(s,b) in enumerate(lines) if _heading(s)}
    bound = _bind(lines,_candidates(boxes,em,size),em) if captions else {}
    equations = _equations(rows,boxes,em,size[0],row_cells) if boxes else {}
    check_math = any((i not in equations and (m := _EQUATION.search(s)) and _EMPTY_MATH.fullmatch(s[:m.start()]))
                     or (_FORMULA_INTRO.search(s) and i+1 < len(lines) and re.match(r'^где\b',lines[i+1][0],re.I))
                     for i,(s,b) in enumerate(lines))
    probe = raster
    working = None
    scan_background = len(boxes) != len(objects)
    fractions = []
    if (captions-bound.keys() or check_math) and raster is None:
        bitmap = page.render(scale=min(1.6,1200/max(size)),grayscale=True)
        try:
            working = bitmap.to_pil().convert('L')
        finally:
            bitmap.close()
        probe = working
    try:
        if captions-bound.keys() and probe is not None:
            for i,b in _bind(lines,_raster_bodies(probe,size,lines,em),em).items():
                bound.setdefault(i,b)
        if probe is not None and (raster is not None or check_math):
            equations.update(_raster_math(probe,size,rows,scores,em,equations.keys() | bound.keys(),row_cells))
        if probe is not None and (raster is not None or scan_background):
            fractions = _unread_fractions(probe, size, rows, em, [*boxes, *bound.values(), *equations.values()])
    finally:
        if working is not None:
            working.close()
    marked = []
    for i,b in bound.items():
        # Keep the editable caption on the correct side of the illustration.
        form = _heading(lines[i][0])[0] == 'form'
        word = rows[i][-1] if form else rows[i][0]
        marked.append((b,_figure_name(lines[i][0]),(id(word),form)))
    formula_regions = []
    for i,b in equations.items():
        tag = _EQUATION.search(lines[i][0])
        if any(_inside(b,outer) for outer,_,_ in marked):
            continue
        number = equation_number_words(rows[i])
        word = number[0] if number else next((w for w in rows[i] if w[0]>=b[2]),rows[i][0])
        number_ids = set(map(id, number))
        owned = tuple(w for row in rows for w in row
                      if id(w) not in number_ids and coverage(w, b) >= .80 and not ordinary_text(w[4])
                      and not _EQUATION.fullmatch(w[4]))
        label = 'Формула' + (' ' + tag[0] if tag and not number else '')
        formula_regions.append((b, owned, True, label, (id(word),False)))
    ruled_fractions = set(fractions)
    for box in fractions:
        formula_regions.append((box, (), True, 'Формула', _word_anchor(rows,lines,box,standalone=True,row_cells=row_cells)))
    for box, words, display in vector_fractions(rows, horizontal_rules, em, formula_cells or {}):
        if any(_inside(box, outer) for outer, _, _ in marked):
            continue
        for i, (other, owned, old_display, label, anchor) in enumerate(formula_regions):
            if coverage(box, other) > .50 or coverage(other, box) > .65:
                combined = tuple({id(word): word for word in (*words, *owned)}.values())
                merged_box = _union((other, box))
                formula_regions[i] = (merged_box, combined, old_display, label, anchor)
                ruled_fractions.add(merged_box)
                break
        else:
            formula_regions.append((box, words, display, 'Формула', None))
            ruled_fractions.add(box)
    occupied = [*(b for b,_,_ in marked), *(entry[0] for entry in formula_regions)]
    if formula_text is not None:
        for box, words, display in text_regions(rows, bound.keys() | captions, occupied=occupied):
            if raster is not None:
                scope = cell_rows[formula_cells.get(id(words[0]))] if formula_cells else rows
                extended, pieces = attach_fraction_terms(box, words, scope, raster, size, em)
                if not any(coverage(extended, existing) > .45 for existing in occupied):
                    box, words = extended, pieces
            # A formula can be split across RapidOCR lines. A crop already
            # measured around its number takes priority over partial repeats.
            if any(coverage(box, existing) > .45 or coverage(existing, box) > .75 for existing in occupied):
                continue
            formula_regions.append((box, words, display, 'Формула', None))
            occupied.append(box)

    anchors = {}
    formula_regions.sort(key=lambda entry: (entry[0][1], entry[0][0]))
    ready = []
    claimed = set()
    formula_owners = []

    def place(crop, words, anchor, markup):
        if words and formula_text is not None:
            for index, word in enumerate(words):
                formula_text[id(word)] = markup if index == 0 else ''
        else:
            anchor = anchor or _word_anchor(rows, lines, crop.bbox, standalone=crop.display,row_cells=row_cells)
            if anchor is not None:
                anchors.setdefault(anchor, []).append((crop.bbox, markup))
        formula_owners.append(crop.bbox)

    for box, words, display, label, anchor in formula_regions:
        if cancelled:
            cancelled()
        if any(id(word) in claimed for word in words):
            continue
        text = ' '.join(word[4] for word in words)
        if formula_batches is not None:
            cells = {formula_cells.get(id(word)) for word in words} if formula_cells else set()
            cell = next(iter(cells)) if len(cells) == 1 else None
            rejection = None
            if len(cells) > 1 or cell is not None and not _inside(box, cell):
                rejection = 'formula_ambiguous_cell'
            elif box not in ruled_fractions and not text_confirms_formula(text):
                rejection = 'formula_unconfirmed'
            if rejection:
                if stats is not None:
                    stats[rejection] = stats.get(rejection, 0) + 1
                continue
        try:
            png = _crop(page, raster, box, size)
        except (PdfiumError, ValueError, OSError):
            continue
        claimed.update(map(id, words))
        crop = FormulaCrop(page_no, box, text, display, png)
        if formula_batches is not None:
            place(crop, words, anchor, formula_batches.place(crop, label))
        else:
            ready.append((crop, words, label, anchor))
    rendered = render_formulas([entry[0] for entry in ready]) if ready and render_formulas else [None]*len(ready)
    if rendered is None or len(rendered) != len(ready):
        rendered = [None]*len(ready)
    for (crop, words, label, anchor), markup in zip(ready, rendered, strict=True):
        result, kind = _rendered_formula(image_assets, markup, crop, label, table_formula_boxes)
        if stats is not None:
            stats[kind] = stats.get(kind, 0) + 1
        place(crop, words, anchor, result)
    owners = [*(b for b,_,_ in marked), *formula_owners]
    bottom = [b for b in boxes if b[3] > .72*size[1] and b[3]-b[1] > .12*size[1]
              and b[2]-b[0] > .19*size[0] and len(objects[b])==1 and not any(_inside(b,area) for area in owners)]
    for box, label, anchor in [*marked,*((b,'Изображение',None) for b in sorted(boxes,key=lambda b:(b[1],b[0]))
                                  if not any(_inside(b,outer) for outer in owners))]:
        if cancelled:
            cancelled()
        entries = objects.get(box, ())
        pieces = [b for b in boxes if _inside(b,box)] if len(entries) != 1 else []
        # The composite carries the original geometry. Source components remain
        # downloadable without inserting a second copy of the visible formula.
        originals = []
        for source_box in pieces:
            for obj in objects[source_box]:
                try:
                    preview_mime, preview, orig_mime, orig = _image_parts(page,[obj],source_box,None,size)
                except (ValueError, PdfiumError, OSError):
                    if stats is not None:
                        stats['image_original_unavailable'] = stats.get('image_original_unavailable',0)+1
                    continue
                if orig:
                    originals.append(_resource(image_assets,orig_mime,orig))
                elif preview_mime == 'image/jpeg':
                    originals.append(_resource(image_assets,preview_mime,preview))
                elif stats is not None:
                    stats['image_original_unavailable'] = stats.get('image_original_unavailable',0)+1
        try:
            parts = _image_parts(page, objects.get(box, ()), box, raster, size)
        except (ValueError, PdfiumError, OSError):
            parts = ('image/png', _crop(page,raster,box,size), None, None)
        if stats is not None and len(entries) == 1 and parts[0] == 'image/png' and parts[3] is None:
            stats['image_original_unavailable'] = stats.get('image_original_unavailable',0)+1
        html = _markup(image_assets,parts,label,box,page_no,originals)
        if links is not None and label == 'Изображение' and bottom == [box]:
            graphic_id = f'fast-edge-figure-{page_no}'
            html = html.replace('alt="Изображение"', f'alt="Изображение" id="{graphic_id}"', 1)
            links['next'] = dict(page=page_no,graphic_id=graphic_id,span=(box[0]/size[0],box[2]/size[0]),
                                 edges=_edge_lines(lines,size[1]),following=[s for s,b in lines if b[1]>=box[3]])
        anchor = anchor or _word_anchor(rows,lines,box,row_cells=row_cells)
        if anchor is not None:
            anchors.setdefault(anchor,[]).append((box,html))
    result = {k:''.join(s for b,s in sorted(items,key=lambda item:(item[0][1],item[0][0]))) for k,items in anchors.items()}
    if links is not None:
        _cross_page(page_no,lines,rows,bound,size,links,result)
    return result
