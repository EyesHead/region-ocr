"""Recover a visible PDF equation system if its complete source can be proved."""
from collections import Counter
from html import escape
import math
from statistics import median

from bs4 import BeautifulSoup, NavigableString


_OPERATORS = frozenset('+-=' + '\u2212\u22c5\u00d7\u00b7\u00f7/\u2264\u2265<>\u00b1\u2213\u22ef\u2026')
_SEPARATOR = frozenset('.\u2026\u22ef')
_MATH_TAGS = frozenset({'mrow', 'mi', 'mn', 'mo', 'msub', 'msup', 'msubsup'})


def _painted_text_objects(regions, page_no, bbox):
    """Rule out a hidden text layer, pictures, and paths within this exact region."""
    from ctypes import byref, c_uint

    from docling.utils.locks import pypdfium2_lock
    from pypdfium2 import raw

    with pypdfium2_lock:
        page = regions.pdf[page_no - 1]
        try:
            if page.get_rotation():
                return 0
            height = page.get_height()
            count = 0
            for obj in page.get_objects():
                left, lower, right, upper = obj.get_bounds()
                top, bottom = height - upper, height - lower
                if (max(left, bbox[0]) >= min(right, bbox[2])
                        or max(top, bbox[1]) >= min(bottom, bbox[3])):
                    continue
                if obj.type != raw.FPDF_PAGEOBJ_TEXT or raw.FPDFTextObj_GetTextRenderMode(obj.raw) != 0:
                    return 0
                rgba = [c_uint() for _ in range(4)]
                if (not raw.FPDFPageObj_GetFillColor(obj.raw, *(byref(v) for v in rgba))
                        or rgba[-1].value != 255 or min(v.value for v in rgba[:3]) >= 245):
                    return 0
                count += 1
            return count
        finally:
            page.close()


def _strict_ink_coverage(mask, glyphs, bbox, sx, sy):
    """Account for crop edges and isolated strokes, which cell proof may ignore."""
    import cv2
    import numpy as np

    cover = np.zeros_like(mask)
    height, width = mask.shape
    for glyph in glyphs:
        pad = max(.45, glyph['size'] * .08)
        x0 = max(0, math.floor((glyph['x0'] - bbox[0] - pad) * sx))
        y0 = max(0, math.floor((glyph['top'] - bbox[1] - pad) * sy))
        x1 = min(width, math.ceil((glyph['x1'] - bbox[0] + pad) * sx))
        y1 = min(height, math.ceil((glyph['bottom'] - bbox[1] + pad) * sy))
        if x0 >= x1 or y0 >= y1 or not mask[y0:y1, x0:x1].any():
            return False
        cover[y0:y1, x0:x1] = 255
    missing = cv2.bitwise_and(mask, cv2.bitwise_not(cover))
    _, _, components, _ = cv2.connectedComponentsWithStats(missing, 8)
    return not any(area >= 3 for _, _, _, _, area in components[1:])


def _equation_content(line):
    """Reuse source-proved subscripts and encode only the remaining literal atoms."""
    content = []
    parsed = BeautifulSoup(line['html'], 'html.parser')
    for node in parsed.contents:
        if getattr(node, 'name', None) == 'math':
            if any(child.name not in _MATH_TAGS for child in node.find_all(True)):
                return None
            content.extend(str(child) for child in node.children)
        elif isinstance(node, NavigableString):
            for char in node:
                if char.isspace():
                    continue
                tag = ('mo' if char in _OPERATORS else 'mn' if char.isdecimal()
                       else 'mi' if char.isalpha() else None)
                if tag is None:
                    return None
                content.append(f'<{tag}>{escape(char)}</{tag}>')
        else:
            return None
    return ''.join(content)


def recover_native_system(reader, regions, page, bbox, image, api):
    """Return a complete native left-braced equation system, or decline recovery."""
    from docling_pdf_cpu_route import _ink
    from docling_pdf_text_cells import positioned_text_proof

    if (reader is None or len(bbox) != 4 or not all(math.isfinite(v) for v in bbox)
            or bbox[2] <= bbox[0] or bbox[3] <= bbox[1] or not image.width or not image.height):
        return None
    reader.load_page(page)
    if reader.text_method not in {'pdfminer_source_glyphs', 'pdfium_source_glyphs'}:
        return None
    glyphs = [g for g in reader.chars if g.get('text', '').strip()
              and bbox[0] <= (g['x0'] + g['x1']) / 2 <= bbox[2]
              and bbox[1] <= (g['top'] + g['bottom']) / 2 <= bbox[3]]
    if not 10 <= len(glyphs) <= 512:
        return None
    braces = [g for g in glyphs if g['text'] == '{']
    if len(braces) != 1:
        return None
    brace = braces[0]
    lines = api.native_cell_lines([g for g in glyphs if g is not brace])
    compact = lambda text: ''.join(text.split())
    literals = [compact(line['text']) for line in lines]
    separators = [i for i, text in enumerate(literals) if len(text) >= 4 and set(text) <= _SEPARATOR]
    if len(separators) != 1 or not 0 < separators[0] < len(lines) - 1:
        return None
    separator = separators[0]
    equations = [line for i, line in enumerate(lines) if i != separator]
    if not 2 <= len(equations) <= 12 or any(
            line['text'].count('=') != 1 or not line.get('source_scripts') for line in equations):
        return None
    font_size = median(line['size'] for line in equations)
    starts = [line['x0'] for line in equations]
    brace_centre = (brace['top'] + brace['bottom']) / 2
    if (max(starts) - min(starts) > .45 * font_size or brace['x0'] >= min(starts)
            or brace['x1'] > min(starts) + .15 * font_size
            or min(starts) - brace['x1'] > font_size
            or not equations[0]['y'] < brace_centre < equations[-1]['y']):
        return None
    objects = _painted_text_objects(regions, page, bbox)
    if not objects:
        return None
    scripts = [proof for line in lines for proof in line.get('source_scripts', [])]
    mask = _ink(image)
    sx, sy = image.width / (bbox[2] - bbox[0]), image.height / (bbox[3] - bbox[1])
    positioned = positioned_text_proof('{<br/>' + '<br/>'.join(line['html'] for line in lines),
                                      glyphs, mask, bbox, bbox, sx, sy, source_scripts=scripts)
    if (positioned is None or positioned['glyph_ink_support'] != 1.
            or not _strict_ink_coverage(mask, glyphs, bbox, sx, sy)):
        return None

    rows = []
    for i, line in enumerate(lines):
        if i == separator:
            rows.append(f'<mtr><mtd columnalign="center"><mo>{escape(literals[i])}</mo></mtd></mtr>')
        else:
            body = _equation_content(line)
            if body is None:
                return None
            rows.append(f'<mtr><mtd><mrow>{body}</mrow></mtd></mtr>')
    html = ('<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">'
            '<mrow><mo fence="true" stretchy="false">{</mo><mtable columnalign="left">'
            + ''.join(rows) + '</mtable></mrow></math>')
    rendered = compact(BeautifulSoup(html, 'html.parser').get_text())
    if rendered != '{' + ''.join(literals) or Counter(rendered) != Counter(
            compact(''.join(g['text'] for g in glyphs))):
        return None
    return html, dict(method='painted_pdf_glyphs_and_source_ink_for_aligned_equation_system',
                      page=page, bbox=list(bbox), rows=len(equations), glyphs=len(glyphs),
                      script_count=len(scripts), separator_glyph_count=len(literals[separator]),
                      painted_text_objects=objects, ink_coverage=positioned['ink_coverage'],
                      glyph_ink_support=positioned['glyph_ink_support'])
