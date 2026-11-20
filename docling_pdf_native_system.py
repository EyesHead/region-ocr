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
                if obj.type == raw.FPDF_PAGEOBJ_PATH and not count:
                    # A white PDF background is painted before its text. A
                    # later covering path must still disqualify the reading.
                    from ctypes import c_int
                    rgba = [c_uint() for _ in range(4)]
                    fill, stroke = c_int(), c_int()
                    if (left <= bbox[0] and top <= bbox[1] and right >= bbox[2] and bottom >= bbox[3]
                            and raw.FPDFPath_GetDrawMode(obj.raw, byref(fill), byref(stroke))
                            and fill.value and not stroke.value
                            and raw.FPDFPageObj_GetFillColor(obj.raw, *(byref(v) for v in rgba))
                            and all(v.value == 255 for v in rgba)):
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


def _tight_source_glyphs(regions, page, bbox):
    """Read tight ink boundaries, including limits just outside an OCR box."""
    from docling.utils.locks import pypdfium2_lock

    with pypdfium2_lock:
        source = regions.pdf[page - 1]
        try:
            if source.get_rotation():
                return []
            height = source.get_height()
            view = source.get_textpage()
            try:
                glyphs = []
                for i in range(view.count_chars()):
                    left, lower, right, upper = view.get_charbox(i)
                    top, bottom = height - upper, height - lower
                    if (left >= right or top >= bottom or right < bbox[0] - 30
                            or left > bbox[2] + 30 or bottom < bbox[1] - 40 or top > bbox[3] + 40):
                        continue
                    value = view.get_text_range(i, 1).strip()
                    if not value:
                        from pypdfium2 import raw
                        unit = raw.FPDFText_GetUnicode(view.raw, i)
                        if 0xD800 <= unit <= 0xDBFF and i + 1 < view.count_chars():
                            value = view.get_text_range(i, 2)
                    if value:
                        glyphs.append(dict(text=value, x0=left, x1=right, top=top, bottom=bottom,
                                           size=bottom - top))
                return glyphs
            finally:
                view.close()
        finally:
            source.close()


def _source_sum_side(sign, signs, glyphs, label, regions, page):
    from docling_pdf_cpu_route import _ink

    width, height = sign['x1'] - sign['x0'], sign['size']
    def gap(glyph, operator):
        return max(0, glyph['top'] - operator['bottom'], operator['top'] - glyph['bottom'])

    candidates = {'^': [], '_': []}
    for g in glyphs:
        x = (g['x0'] + g['x1']) / 2
        if not (sign['x0'] - .18 * width < x < sign['x1'] + .18 * width
                and g['text'] in '0123456789−-' and g['size'] < .65 * height
                and gap(g, sign) < .9 * height):
            continue
        nearby = [other for other in signs if other is not sign
                  and other['x0'] - .18 * width < x < other['x1'] + .18 * width]
        if any(gap(g, other) <= gap(g, sign) for other in nearby):
            continue
        if g['bottom'] <= sign['top']:
            candidates['^'].append(g)
        elif g['top'] >= sign['bottom']:
            candidates['_'].append(g)
        else:
            return None
    nonempty = [(side, line) for side, line in candidates.items() if line]
    if len(nonempty) != 1:
        return None
    side, line = nonempty[0]
    line.sort(key=lambda g: g['x0'])
    if ''.join(g['text'] for g in line).replace('−', '-') != label:
        return None
    top, bottom = min(g['top'] for g in line), max(g['bottom'] for g in line)
    left, right = min(g['x0'] for g in line) - .45, max(g['x1'] for g in line) + .45
    if (bottom - top >= .65 * height or abs((left + right - sign['x0'] - sign['x1']) / 2) > .2 * width
            or max(gap(g, sign) for g in line) > .45 * height):
        return None
    present = (left, top - .45, right, bottom + .45)
    # Mirror the observed limit about the operator. Every pixel on the other
    # side must be blank, including the gap to the operator's actual ink.
    if side == '^':
        absent = (left, sign['bottom'] + .65, right, sign['bottom'] + sign['top'] - top)
    else:
        absent = (left, sign['top'] - (bottom - sign['bottom']), right, sign['top'] - .65)
    bounds = (left, min(present[1], absent[1]), right, max(present[3], absent[3]))
    if absent[3] <= absent[1] or not _painted_text_objects(regions, page, bounds):
        return None
    with regions.crop(page, absent, scale=4) as image:
        if _ink(image).any():
            return None
    with regions.crop(page, present, scale=4) as image:
        sx, sy = image.width / (right - left), image.height / (present[3] - present[1])
        if not _strict_ink_coverage(_ink(image), line, present, sx, sy):
            return None
    return side


def _recover_sum_limits(regions, page, bbox, rendered):
    import re

    soup = BeautifulSoup(rendered, 'html.parser')
    node = soup.find('math')
    if node is None:
        return None
    latex = node.get('data-latex', '')
    ranges = list(re.finditer(r'\\sum_\{([0-9]+-[0-9]+)\}\^\{\1\}', latex))
    tokens = [mo for mo in node.find_all('mo') if mo.get_text() == '∑']
    if not ranges or len(ranges) != len(tokens) or len(tokens) != len(re.findall(r'\\sum(?![A-Za-z])', latex)):
        return None
    glyphs = _tight_source_glyphs(regions, page, bbox)
    signs = sorted((g for g in glyphs if g['text'] == '∑'
                    and bbox[0] <= (g['x0'] + g['x1']) / 2 <= bbox[2]
                    and bbox[1] <= (g['top'] + g['bottom']) / 2 <= bbox[3]),
                   key=lambda g: (round(g['top'], 1), g['x0']))
    if len(signs) != len(ranges):
        return None
    changes = []
    for match, mo, sign in zip(ranges, tokens, signs):
        item = mo.parent
        parts = item.find_all(recursive=False)
        if (item.name not in {'munderover', 'msubsup'} or len(parts) != 3 or parts[0] is not mo
                or any(p.get_text().replace('−', '-') != match[1] for p in parts[1:])):
            return None
        side = _source_sum_side(sign, signs, glyphs, match[1], regions, page)
        if side:
            changes.append((match, item, parts, side))
    if not changes:
        return None
    for match, item, parts, side in reversed(changes):
        latex = latex[:match.start()] + r'\sum' + side + '{' + match[1] + '}' + latex[match.end():]
        parts[1 if side == '^' else 2].decompose()
        item.name = ({'^': 'mover', '_': 'munder'} if item.name == 'munderover'
                     else {'^': 'msup', '_': 'msub'})[side]
    node['data-latex'] = latex
    return latex, str(soup)


def _source_letter_denominator(reader, glyphs, bbox, numerator, regions, page):
    """Locate a single-letter denominator under a painted native fraction rule."""
    import unicodedata
    from docling_pdf_cpu_route import _ink

    pdf_page = reader.page
    found = []
    for rule in pdf_page.rects + pdf_page.lines:
        x0, y0, x1, y1 = (rule[k] for k in ('x0', 'top', 'x1', 'bottom'))
        if (not (bbox[0] <= x0 < x1 <= bbox[2] and bbox[1] < y0 <= y1 < bbox[3])
                or x1 - x0 < 5 or y1 - y0 > 1.8):
            continue
        above, below = [], []
        for g in glyphs:
            if g['x0'] < x0 - .8 or g['x1'] > x1 + .8:
                continue
            if -1 < y0 - g['bottom'] < .6 * g['size']:
                above.append(g)
            elif -1 < g['top'] - y1 < .6 * g['size']:
                below.append(g)
        above.sort(key=lambda g: g['x0'])
        if (unicodedata.normalize('NFKC', ''.join(g['text'] for g in above)) != numerator
                or len(below) != 1 or not below[0]['text'].isalpha()
                or abs((below[0]['x0'] + below[0]['x1'] - x0 - x1) / 2) > .2 * (x1 - x0)):
            continue
        with regions.crop(page, [x0, y0 - .6, x1, y1 + .6], scale=4) as image:
            if max((_ink(image) > 0).mean(axis=1)) < .9:
                continue
        found.append((above, below[0]))
    return found[0] if len(found) == 1 else None


def _recover_source_variables(reader, regions, page, bbox, rendered, api):
    """Correct source-proved identifiers without changing the surrounding math."""
    import re
    import unicodedata
    from collections import defaultdict
    from docling_pdf_cpu_route import _ink

    def norm(value):
        return ''.join(unicodedata.normalize('NFKC', value).split())

    def script_values(node):
        result = defaultdict(list)
        for tag in node.find_all(['msub', 'msup', 'msubsup']):
            parts = tag.find_all(recursive=False)
            base = parts[0]
            if base.name == 'mover':
                base = base.find(recursive=False)
            key = norm(base.get_text())
            if len(key) != 1 or not key.isalpha():
                continue
            for side, entry in zip({'msub':['sub'], 'msup':['sup'], 'msubsup':['sub','sup']}[tag.name], parts[1:]):
                result[key, side].append(entry)
        return result

    def substitute(latex, before, after, expected):
        # Compare complete regenerated expressions. A literal or TeX control
        # sequence is replaced only at the unique position yielding this edit.
        options = []
        for m in re.finditer(r'\\[A-Za-z]+|[^\W\d_]+|[0-9]+', latex):
            if norm(m[0]) != before and not (m[0].startswith('\\') and not before.isascii()):
                continue
            candidate = latex[:m.start()] + after + latex[m.end():]
            output = api.latex_math(candidate, display)
            if output:
                parsed = BeautifulSoup(output, 'html.parser').math
                shapes = Counter(n.name for n in parsed.find_all(structural))
                if shapes == original_shapes and norm(parsed.get_text()) == expected:
                    options.append((candidate, output))
                    if len(options) > 1:
                        return None
        return options[0] if options else None

    def confirmed(g):
        nonlocal tight
        if tight is None:
            tight = _tight_source_glyphs(regions, page, bbox)
        left, top, right, bottom = g['bbox']
        matches = [v for v in tight if v['text'] == g['text']
                   and left <= (v['x0'] + v['x1']) / 2 <= right
                   and top <= (v['top'] + v['bottom']) / 2 <= bottom]
        if len(matches) != 1:
            return False
        v = matches[0]
        bounds = [v['x0'] - .35, v['top'] - .35, v['x1'] + .35, v['bottom'] + .35]
        if not _painted_text_objects(regions, page, bounds):
            return False
        with regions.crop(page, bounds, scale=4) as image:
            sx = image.width / (bounds[2] - bounds[0])
            sy = image.height / (bounds[3] - bounds[1])
            return _strict_ink_coverage(_ink(image), matches, bounds, sx, sy)

    soup = BeautifulSoup(rendered, 'html.parser')
    node = soup.math
    if not node or not node.get('data-latex') or not node.find(['msub', 'msup', 'msubsup', 'mfrac']):
        return None
    latex, display = node['data-latex'], node.get('display', 'block')
    reader.load_page(page)
    glyphs = [g for g in reader.chars if g['text'].strip()
              and bbox[0]-1 <= (g['x0'] + g['x1']) / 2 <= bbox[2]+1
              and bbox[1]-1 <= (g['top'] + g['bottom']) / 2 <= bbox[3]+1]
    if (not glyphs or len(glyphs) > 450 or any(not g.get('native_script_eligible', True)
            or not g.get('upright', True) for g in glyphs)):
        return None
    lines = api.native_cell_lines(glyphs)
    proofs = defaultdict(list)
    for line in lines:
        for proof in line.get('source_scripts', []):
            for side, value in proof['scripts'].items():
                proofs[norm(proof['base']), side].append((value, proof))
    structural = ['msub', 'msup', 'msubsup', 'mfrac', 'munder', 'mover', 'munderover', 'mtable', 'mtr']
    original_shapes = Counter(tag.name for tag in node.find_all(structural))
    changed = False
    tight = None
    for key, entries in script_values(node).items():
        if len(proofs[key]) != len(entries):
            continue
        pairs = {(norm(entry.get_text()), norm(value)) for entry, (value, _) in zip(entries, proofs[key])
                 if norm(entry.get_text()) != norm(value)}
        native_values = {norm(value) for value, _ in proofs[key]}
        if len(lines) > 1 and len(native_values) > 1 and (
                len(pairs) != 1 or any(before in native_values for before, _ in pairs)):
            continue
        for entry, (value, proof) in zip(entries, proofs[key]):
            before, after = norm(entry.get_text()), norm(value)
            if (not 1 <= len(before) <= 3 or len(after) != 1 or before == after
                    or not (before.isalpha() or len(before) == 1 and before.isdigit()) or not after.isalpha()):
                continue
            native = [g for g in proof['glyphs'] if g['text'] == value]
            if len(native) != 1 or not confirmed(native[0]):
                continue
            original = list(entry.contents)
            entry.clear()
            entry.append(after)
            candidate = substitute(latex, before, after, norm(node.get_text()))
            if not candidate:
                entry.clear()
                entry.extend(original)
                continue
            latex, rendered = candidate
            changed = True
    # A raised stroke and a small circle can mark different printed node
    # numbers. Retain a measured source mark as literal text, without giving
    # it the meaning of a degree, exponent value or division operator.
    circles = {'°', '∘', 'º'}
    marks = circles | {'/', '′', '″'}
    scripts = []
    for tag in node.find_all('msup'):
        base, exponent = tag.find_all(recursive=False)
        if base.get_text().isdigit() and exponent.get_text() in marks:
            scripts.append((base.get_text(), exponent))
    if scripts and any(entry.get_text() in circles for _, entry in scripts):
        physical = []
        for base, mark in zip(glyphs, glyphs[1:]):
            size = base['size']
            if (base['text'].isdigit() and mark['text'] in marks and .45 < mark['size']/size < .95
                    and -.15 * size <= mark['x0'] - base['x1'] < .45 * size
                    and .05 * size < base['top'] - mark['top'] < .7 * size
                    and mark['bottom'] < base['bottom']):
                physical.append((base['text'], mark))
        if [digit for digit, _ in physical] == [digit for digit, _ in scripts]:
            for (_, entry), (_, mark) in zip(scripts, physical):
                before, after = entry.get_text(), mark['text']
                if before not in circles or after in circles or not confirmed(dict(
                        text=after, bbox=[mark[k] for k in ('x0','top','x1','bottom')])):
                    continue
                old = list(entry.contents)
                entry.clear()
                entry.append(after)
                candidate = substitute(latex, before, r'{\text{' + after + '}}', norm(node.get_text()))
                if candidate:
                    latex, rendered = candidate
                    changed = True
                else:
                    entry.clear()
                    entry.extend(old)
    fractions = node.find_all('mfrac')
    if len(fractions) == 1:
        upper, lower = fractions[0].find_all(recursive=False)
        numerator, before = norm(upper.get_text()), norm(lower.get_text())
        if numerator.isalpha() and len(before) == 1 and before.isalpha():
            source = _source_letter_denominator(reader, glyphs, bbox, numerator, regions, page)
            if source:
                above, g = source
                after = norm(g['text'])
                if before != after and all(confirmed(dict(text=v['text'], bbox=[
                        v[k] for k in ('x0','top','x1','bottom')])) for v in [*above, g]):
                    old = list(lower.contents)
                    lower.clear()
                    lower.append(after)
                    candidate = substitute(latex, before, after, norm(node.get_text()))
                    if candidate:
                        latex, rendered = candidate
                        changed = True
                    else:
                        lower.clear()
                        lower.extend(old)
    # A plain variable may also be wrong. Require a single native line, the
    # entire remaining expression in order, and a unique source glyph per edit.
    if len(lines) == 1 and len(glyphs) <= 110:
        def comparable(value):
            return norm(value).translate(str.maketrans({'⋅':'·', '⩽':'≤', '⩾':'≥'}))
        source, observed = comparable(lines[0]['text']), comparable(node.get_text())
        if (len(source) == len(observed) and 0 < sum(a != b for a,b in zip(source, observed)) <= 3
                and all(a==b or a.isalpha() and b.isalpha() for a,b in zip(source,observed))):
            offset = 0
            for leaf in node.find_all(string=True):
                atom = comparable(str(leaf))
                after = source[offset:offset+len(atom)]
                offset += len(atom)
                if atom == after or len(atom) != 1 or leaf.parent.name != 'mi':
                    continue
                native = [g for g in glyphs if norm(g['text']) == after]
                if len(native) != 1:
                    continue
                g = native[0]
                if not confirmed(dict(text=g['text'], bbox=[g[k] for k in ('x0','top','x1','bottom')])):
                    continue
                parent = leaf.parent
                leaf.replace_with(after)
                candidate = substitute(latex, atom, after, norm(node.get_text()))
                if candidate:
                    latex, rendered = candidate
                    changed = True
                else:
                    parent.string = str(leaf)
    return (latex, rendered) if changed else None


def recover_formula_identifier(reader, regions, page, bbox, rendered, api):
    """Reconcile a literal LaTeX identifier against a single painted source line."""
    import re
    import unicodedata
    from difflib import SequenceMatcher
    from docling_pdf_cpu_route import _ink

    def key(value):
        return ''.join(unicodedata.normalize('NFKC', value).split())

    if reader is not None and any(tag in rendered for tag in ('<msub', '<msup', '<mfrac')):
        scripts = _recover_source_variables(reader, regions, page, bbox, rendered, api)
        if scripts:
            return scripts
    if reader is not None and r'\sum_' in rendered:
        sums = _recover_sum_limits(regions, page, bbox, rendered)
        if sums:
            return sums

    node = BeautifulSoup(rendered, 'html.parser').find('math')
    if (reader is None or node is None or not node.get('data-latex')
            or any(tag.name not in _MATH_TAGS | {'mtext'} for tag in node.find_all(True))):
        return None
    reading = key(node.get_text())
    if not 4 <= len(reading) <= 160 or not any(c.isalpha() for c in reading):
        return None
    reader.load_page(page)
    glyphs = [g for g in reader.chars if g['text'].strip()
              and bbox[0] <= (g['x0'] + g['x1']) / 2 <= bbox[2]
              and bbox[1] <= (g['top'] + g['bottom']) / 2 <= bbox[3]]
    if (not glyphs or len(glyphs) > 180 or any(not g.get('native_script_eligible', True)
            or not g.get('upright', True) or len(g['text']) != 1 for g in glyphs)):
        return None
    if Counter(reading) == Counter(key(''.join(g['text'] for g in glyphs))):
        return None
    lines = api.native_cell_lines(glyphs)
    if len(lines) != 1:
        return None
    expected = key(lines[0]['text'])
    edits = SequenceMatcher(None, reading, expected, autojunk=False).get_opcodes()
    mismatches = [(reading[a:b], expected[c:d]) for op, a, b, c, d in edits if op != 'equal']
    if (len(mismatches) != 1 or sum(b - a for op, a, b, _, _ in edits if op == 'equal') < 3):
        return None
    before, after = mismatches[0]
    if not (before.isalpha() and after.isalpha() and max(len(before), len(after)) <= 8):
        return None

    latex = node['data-latex']
    # Skip TeX control word names. Only a literal replacement whose complete
    # mathematical reading agrees with the source can be accepted.
    candidates = []
    for match in re.finditer(r'\\[A-Za-z]+|' + re.escape(before), latex):
        if match[0] != before:
            continue
        proposed = latex[:match.start()] + after + latex[match.end():]
        markup = api.latex_math(proposed, node.get('display', 'block'))
        if markup and key(BeautifulSoup(markup, 'html.parser').get_text()) == expected:
            candidates.append((proposed, markup))
    if len(candidates) != 1 or not _painted_text_objects(regions, page, bbox):
        return None
    with regions.crop(page, bbox, scale=3) as image:
        sx, sy = image.width / (bbox[2] - bbox[0]), image.height / (bbox[3] - bbox[1])
        if not _strict_ink_coverage(_ink(image), glyphs, bbox, sx, sy):
            return None
    return candidates[0]
