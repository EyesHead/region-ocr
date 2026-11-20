"""Read ruled mathematical fractions from native PDF text and vector geometry."""
from collections import Counter
from html import escape
from statistics import median
import re
import unicodedata

from bs4 import BeautifulSoup

from docling_pdf_native_scripts import attach_source_scripts
from docling_pdf_native_system import _painted_text_objects, _strict_ink_coverage, _tight_source_glyphs


class _AmbiguousLayout(ValueError):
    pass


def _atoms(value):
    output = []
    for token in re.findall(r'\d+(?:[,.]\d+)?|.', value):
        if token[0].isdigit():
            tag = 'mn'
        elif token.isalpha():
            tag = 'mi'
        elif token in '+−-=×·⋅÷<>≤≥≠≈≡±∓∞∑∏∫∂∇∈∉∪∩⇒⇔→←↔∝∧∨…⋯′″()[],.!:;/|':
            tag = 'mo'
        else:
            raise _AmbiguousLayout
        fence = ' stretchy="false"' if token in ('(', ')', '[', ']') else ''
        output.append(f'<{tag}{fence}>{escape(token)}</{tag}>')
    return ''.join(output)


def _math_text(text):
    # MathML identifiers already render the ordinary mathematical italic.
    # Keep bold, script and double-struck characters in their original alphabet.
    return ''.join(unicodedata.normalize('NFKC', c) if unicodedata.name(c, '').startswith(
        'MATHEMATICAL ITALIC ') else c for c in text)


def _run(chars):
    if not chars:
        raise _AmbiguousLayout
    chars = sorted(attach_source_scripts(chars, lambda text, display: text), key=lambda c: c['x0'])
    baseline = median(c['matrix'][5] for c in chars)
    if any(abs(c['matrix'][5] - baseline) > .15*c['size'] for c in chars):
        raise _AmbiguousLayout
    result, plain = [], ''
    previous = None
    for char in chars:
        # Keep separately typeset numeric groups from becoming one number.
        if previous is not None and char['x0']-previous['x1'] > .24*char['size']:
            result.append(_atoms(plain))
            plain = ''
        previous = char
        script = char.get('source_script_proof')
        if not script:
            plain += _math_text(char['text'])
            continue
        result.append(_atoms(plain))
        plain = ''
        modes = script['scripts']
        tag = 'msubsup' if len(modes) == 2 else 'm' + next(iter(modes))
        children = [script['base'], *(modes[key] for key in ('sub', 'sup') if key in modes)]
        rendered = ''.join('<mrow>'+_atoms(_math_text(t))+'</mrow>' for t in children)
        result.append(f'<{tag}>{rendered}</{tag}>')
    return ''.join(result) + _atoms(plain)


def _rule_rows(page, box, size):
    rules = []
    for shape in (*page.rects, *page.lines):
        x0, top, x1, bottom = (shape[k] for k in ('x0', 'top', 'x1', 'bottom'))
        if not (box[0]-.3 <= x0 < x1 <= box[2]+.3 and box[1] <= (top+bottom)/2 <= box[3]):
            continue
        if x1-x0 < .65*size or bottom-top > .16*size:
            continue
        if shape['object_type'] == 'line':
            if bottom - top > .05 or not shape.get('stroke'):
                continue
            half = (shape.get('linewidth') or .5)/2
            top, bottom = top-half, bottom+half
        elif not shape.get('fill'):
            continue
        rules.append((x0, top, x1, bottom))
    rows = []
    for rule in sorted(rules, key=lambda r: r[1]+r[3]):
        axis = (rule[1]+rule[3])/2
        if not rows or abs(rows[-1][0]-axis) > .12*size:
            rows.append((axis, []))
        rows[-1][1].append(rule)
    return rows


def _fraction_markup(halves, chars, size):
    # A display-style fraction keeps the type size of its numerator and
    # denominator in MathML table cells, which default to compact math.
    style = 'normal' if median(c['size'] for c in chars) >= .9*size else 'compact'
    display = 'true' if style == 'normal' else 'false'
    return f'<mfrac style="math-style:{style}" displaystyle="{display}">'+''.join(halves)+'</mfrac>'


def _fraction_row(chars, rules, axis, size):
    slots = {}
    spans = []
    for rule in sorted(rules):
        x0, _, x1, _ = rule
        if spans and spans[-1][1] > x0:
            raise _AmbiguousLayout
        owned = [c for c in chars if x0 <= (c['x0']+c['x1'])/2 <= x1]
        upper = [c for c in owned if (c['top']+c['bottom'])/2 < axis-.12*c['size']]
        lower = [c for c in owned if (c['top']+c['bottom'])/2 > axis+.12*c['size']]
        if len(upper)+len(lower) != len(owned) or not upper or not lower:
            raise _AmbiguousLayout
        halves = []
        for band in (upper, lower):
            left, right = min(c['x0'] for c in band), max(c['x1'] for c in band)
            if left < x0-.12*size or right > x1+.12*size or abs(left+right-x0-x1) > .55*size:
                raise _AmbiguousLayout
            halves.append('<mrow>'+_run(band)+'</mrow>')
        slots[x0] = _fraction_markup(halves, owned, size)
        spans.append((x0, x1))
    outside = [c for c in chars if not any(a <= (c['x0']+c['x1'])/2 <= b for a,b in spans)]
    if outside:
        centres = [(c['top']+c['bottom'])/2 for c in outside if c['size'] >= .85*size]
        if not centres or abs(median(centres)-axis) > .25*size:
            raise _AmbiguousLayout
    pieces = []
    for start, end in zip([float('-inf'), *(b for _,b in spans)], [*(a for a,_ in spans), float('inf')]):
        run = [c for c in outside if start < (c['x0']+c['x1'])/2 < end]
        if run:
            pieces.append(_run(run))
        if end in slots:
            pieces.append(slots[end])
    return '<mrow>'+''.join(pieces)+'</mrow>'


def _printed_ink(regions, page_no, glyphs, box, rows, width, height):
    from docling_pdf_cpu_route import _ink
    import numpy as np

    tight = _tight_source_glyphs(regions, page_no, box)
    painted = []
    clipped = 0
    for c in glyphs:
        matches = [g for g in tight if g['text'] == c['text']
                   and c['x0']-.3 <= (g['x0']+g['x1'])/2 <= c['x1']+.3
                   and c['top']-.3 <= (g['top']+g['bottom'])/2 <= c['bottom']+.3]
        if len(matches) != 1:
            raise _AmbiguousLayout
        g = matches[0]
        tight.remove(g)
        left, right = max(0.,g['x0']), min(width,g['x1'])
        if not (left < right and 0 <= g['top'] < g['bottom'] <= height):
            raise _AmbiguousLayout
        if left != g['x0'] or right != g['x1']:
            # A mostly visible dot has a recognizable complete source character.
            if g['text'] not in '⋅·.' or right-left < .75*(g['x1']-g['x0']):
                raise _AmbiguousLayout
            clipped += 1
        if not _painted_text_objects(regions,page_no,[left,g['top'],right,g['bottom']]):
            raise _AmbiguousLayout
        painted.append(dict(g,x0=left,x1=right))
    bars = [r for _,rules in rows for r in rules]
    marks = painted + [dict(x0=x0, top=top, x1=x1, bottom=bottom, size=1.) for x0,top,x1,bottom in bars]
    bounds = [max(0.,min(c['x0'] for c in marks)-.5), max(0.,min(c['top'] for c in marks)-.5),
              min(width,max(c['x1'] for c in marks)+.5), min(height,max(c['bottom'] for c in marks)+.5)]
    # The enlarged PDFium lookup also sees neighboring lines. An unmatched
    # character with ink in the selected row band makes the ownership ambiguous.
    if any(max(g['x0'], box[0], 0.) < min(g['x1'], box[2], width)
           and max(g['top'], bounds[1]) < min(g['bottom'], bounds[3]) for g in tight):
        raise _AmbiguousLayout
    # At a physical page edge the PDF really ends. At an interior layout
    # boundary an immediately adjoining printed token belongs to the same run.
    size = median(c['size'] for c in glyphs)
    for other in tight:
        if not (0 < (other['x0']+other['x1'])/2 < width):
            continue
        nearby = ((0 < box[0] and box[0]-.55*size <= other['x1'] <= box[0]+.3)
                  or (box[2] < width-.3 and box[2]-.3 <= other['x0'] <= box[2]+.55*size))
        if nearby and any(min(other['bottom'],c['bottom']) > max(other['top'],c['top'])
                          and min(abs(other['x1']-c['x0']), abs(other['x0']-c['x1'])) < .7*size
                          for c in painted):
            raise _AmbiguousLayout
    with regions.crop(page_no,bounds,scale=3) as bitmap:
        mask = _ink(bitmap)
        sx, sy = bitmap.width/(bounds[2]-bounds[0]), bitmap.height/(bounds[3]-bounds[1])
        if not _strict_ink_coverage(mask,marks,bounds,sx,sy):
            raise _AmbiguousLayout
        for left,top,right,bottom in bars:
            band = mask[max(0,int((top-bounds[1])*sy)-1):int((bottom-bounds[1])*sy)+2,
                        int((left-bounds[0])*sx)+1:int((right-bounds[0])*sx)-1]
            if not band.size or np.count_nonzero(band.any(axis=0)) < .98*band.shape[1]:
                raise _AmbiguousLayout
    return clipped


def _display_math(pieces):
    from local_web.structured_export import math_to_latex

    body = pieces[0] if len(pieces) == 1 else (
        '<mtable columnalign="center">'+''.join('<mtr><mtd>'+p+'</mtd></mtr>' for p in pieces)+'</mtable>')
    node = BeautifulSoup('<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">'
                         +body+'</math>','html.parser').math
    row_latex = [math_to_latex(BeautifulSoup(p,'html.parser').mrow).replace('⇒', r'\Rightarrow ') for p in pieces]
    node['data-latex'] = (row_latex[0] if len(pieces) == 1 else
                          r'\begin{gathered}' + r' \\ '.join(row_latex) + r'\end{gathered}')
    return node


def recover_native_equation(reader, regions, page_no, bounds, api=None):
    """Rebuild verified text runs, scripts and fractions without model inference."""
    if reader is None or regions is None:
        return None
    page = reader.load_page(page_no)
    if page is None:
        return None
    box = list(bounds)
    chars = [c for c in reader.chars if c['text'].strip()
             and max(0.,box[0]) <= (c['x0']+c['x1'])/2 <= min(page.width,box[2])
             and max(0.,box[1]) <= (c['top']+c['bottom'])/2 <= min(page.height,box[3])]
    if not chars or any(not c.get('native_script_eligible',True) or not c.get('upright',True)
                        or not c.get('matrix') for c in chars):
        return None
    size = median(c['size'] for c in chars)
    rows = _rule_rows(page, box, size)
    if not rows:
        return None
    bands = [[] for _ in rows]
    for c in chars:
        centre = (c['top']+c['bottom'])/2
        distances = sorted((abs(axis-centre),i) for i,(axis,_) in enumerate(rows))
        if distances[0][0] > 1.6*size or (len(distances)>1 and distances[1][0]-distances[0][0] < .15*size):
            return None
        bands[distances[0][1]].append(c)
    try:
        pieces = [_fraction_row(band,rules,axis,size) for band,(axis,rules) in zip(bands,rows)]
        clipped = _printed_ink(regions,page_no,chars,box,rows,page.width,page.height)
    except _AmbiguousLayout:
        return None
    node = _display_math(pieces)
    if Counter(node.get_text()) != Counter(_math_text(''.join(c['text'] for c in chars))):
        return None
    return str(node), dict(method='native_fraction_rules_and_painted_glyphs', page=page_no, bbox=box,
                           rows=len(rows), fractions=len(node.find_all('mfrac')), glyphs=len(chars),
                           scripts=len(node.find_all(['msub','msup','msubsup'])), clipped_glyphs=clipped)


def _painted_subset(reader, regions, page_no, bounds, page):
    from docling_pdf_cpu_route import _ink

    glyphs = [c for c in reader.chars if c['text'].strip() and c.get('matrix') and c['size'] > 0
              and bounds[0] < c['x1'] and c['x0'] < bounds[2]
              and bounds[1] <= (c['top']+c['bottom'])/2 <= bounds[3]]
    if not glyphs:
        return [], []
    nearby = _tight_source_glyphs(regions, page_no, bounds)
    seen = set()
    with regions.crop(page_no, bounds, scale=3) as crop:
        mask = _ink(crop)
        sx, sy = crop.width/(bounds[2]-bounds[0]), crop.height/(bounds[3]-bounds[1])
        for c in glyphs:
            if not c.get('native_script_eligible',True) or not c.get('upright',True):
                continue
            candidates = [g for g in nearby if g['text'] == c['text']
                          and c['x0']-.3 <= (g['x0']+g['x1'])/2 <= c['x1']+.3
                          and c['top']-.3 <= (g['top']+g['bottom'])/2 <= c['bottom']+.3]
            if len(candidates) != 1:
                continue
            g = candidates[0]
            left, top = max(bounds[0],g['x0']), max(bounds[1],g['top'])
            right, bottom = min(bounds[2],g['x1']), min(bounds[3],g['bottom'])
            required = .75 if g['text'] in '⋅·.' else .90
            if (right <= left or bottom <= top or
                    (right-left)*(bottom-top) < required*(g['x1']-g['x0'])*(g['bottom']-g['top'])):
                continue
            if not _painted_text_objects(regions,page_no,[left,top,right,bottom]):
                continue
            glyph_ink = mask[int((top-bounds[1])*sy):int((bottom-bounds[1])*sy)+1,
                             int((left-bounds[0])*sx):int((right-bounds[0])*sx)+1]
            if glyph_ink.any():
                nearby.remove(g)
                seen.add(id(c))
        size = median(c['size'] for c in glyphs)
        rows = []
        for axis, rules in _rule_rows(page,bounds,size):
            visible = []
            for left,top,right,bottom in rules:
                line = mask[max(0,int((top-bounds[1])*sy)-1):int((bottom-bounds[1])*sy)+2,
                            max(0,int((left-bounds[0])*sx)+1):int((right-bounds[0])*sx)-1]
                if line.size and line.any(axis=0).mean() >= .98:
                    visible.append((left,top,right,bottom))
            if visible:
                rows.append((axis, visible))
    return [dict(c, confirmed=id(c) in seen) for c in glyphs], rows


def _first_readable_run(chars):
    # Never concatenate pieces on opposite sides of an unconfirmed sign.
    run = []
    for c in sorted(chars,key=lambda g:g['x0']):
        if c['confirmed']:
            try:
                _atoms(_math_text(c['text']))
            except _AmbiguousLayout:
                if run:
                    break
            else:
                run.append(c)
        elif run:
            break
    return run


def _baseline_pieces(chars):
    confirmed = _first_readable_run(chars)
    if not confirmed:
        return []
    merged = attach_source_scripts(confirmed, lambda text, display: text)
    rows = []
    for c in sorted(merged, key=lambda g: -g['matrix'][5]):
        if not rows or abs(rows[-1][0]['matrix'][5]-c['matrix'][5]) > .12*c['size']:
            rows.append([])
        rows[-1].append(c)
    return [(min(c['top'] for c in row), '<mrow>'+_run(row)+'</mrow>') for row in rows]


def _visible_fraction(band, rule, axis, size, bounds):
    x0, top, x1, bottom = rule
    upper = [c for c in band if (c['top']+c['bottom'])/2 < axis-.12*c['size']]
    lower = [c for c in band if (c['top']+c['bottom'])/2 > axis+.12*c['size']]
    if len(upper)+len(lower) != len(band):
        return None
    halves = []
    for side, available in enumerate((upper,lower)):
        known = _first_readable_run(available)
        if known:
            # A genuinely complete side is centered under or above this rule.
            # For a partial side accept only glyphs owned by its horizontal span.
            l, r = min(c['x0'] for c in known), max(c['x1'] for c in known)
            if l < x0-.12*size or r > x1+.12*size:
                return None
            if len(known) == len(available) and abs(l+r-x0-x1) > .55*size:
                return None
            try:
                halves.append(_run(known))
            except _AmbiguousLayout:
                return None
        else:
            # An empty side is meaningful only if the printed area ends here
            # or actual glyphs on that side could not be read.
            at_edge = (top-bounds[1] if side == 0 else bounds[3]-bottom) < .9*size
            if not (available or at_edge):
                return None
            halves.append('')
    if not any(halves):
        return None
    return _fraction_markup(['<mrow>'+h+'</mrow>' for h in halves],band,size)


def recover_visible_prefix(reader, regions, page_no, bounds, api=None):
    """Publish legible native fragments of an incomplete formula in source order."""
    if reader is None or regions is None:
        return None
    try:
        page = reader.load_page(page_no)
        if page is None:
            return None
        box = [max(0.,bounds[0]),max(0.,bounds[1]),min(page.width,bounds[2]),min(page.height,bounds[3])]
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
        glyphs, rows = _painted_subset(reader,regions,page_no,box,page)
        if not any(c['confirmed'] for c in glyphs):
            return None
        size = median(c['size'] for c in glyphs)
        remaining = list(glyphs)
        fragments = []
        for axis, rules in rows:
            band = [c for c in remaining if abs((c['top']+c['bottom'])/2-axis) <= 1.55*size
                    and axis == min((a for a,_ in rows), key=lambda a: abs((c['top']+c['bottom'])/2-a))]
            ruled = []
            for rule in sorted(rules):
                if ruled and rule[0] < ruled[-1][1]:
                    continue
                owned = [c for c in band if rule[0] <= (c['x0']+c['x1'])/2 <= rule[2]]
                fraction = _visible_fraction(owned,rule,axis,size,box)
                if fraction:
                    ruled.append((rule[0],rule[2],fraction))
                    owned_ids = {id(c) for c in owned}
                    band = [c for c in band if id(c) not in owned_ids]
                    remaining = [c for c in remaining if id(c) not in owned_ids]
            if not ruled:
                continue
            # Only baseline glyphs and their measured scripts may flank fractions.
            main = [c for c in band if abs((c['top']+c['bottom'])/2-axis) <= .72*c['size']
                    and not any(l <= (c['x0']+c['x1'])/2 <= r for l,r,_ in ruled)]
            output = []
            for start,end in zip([float('-inf'),*(r for _,r,_ in ruled)],
                                 [*(l for l,_,_ in ruled),float('inf')]):
                run = [c for c in main if start < (c['x0']+c['x1'])/2 < end]
                confirmed = _first_readable_run(run)
                if confirmed:
                    try:
                        output.append(_run(confirmed))
                    except _AmbiguousLayout:
                        fragments.extend(_baseline_pieces(run))
                output.extend(value for l,_,value in ruled if l == end)
            ids = {id(c) for c in main}
            remaining = [c for c in remaining if id(c) not in ids]
            fragments.append((axis,'<mrow>'+''.join(output)+'</mrow>'))
        # Keep other physical lines separate; a clipped letter cannot join digits.
        loose = []
        for c in sorted(remaining,key=lambda g:g['top']):
            if not loose or c['top']-loose[-1][0]['top'] > .52*size:
                loose.append([])
            loose[-1].append(c)
        for line in loose:
            fragments.extend(_baseline_pieces(line))
        if not fragments:
            return None
        node = _display_math([value for _,value in sorted(fragments,key=lambda t:t[0])])
        if not node.get_text(strip=True):
            return None
        node['data-source-formula-partial'] = 'true'
        return str(node),dict(method='partial_visible_native_equation',partial=True,page=page_no,bbox=box,
                              rows=len(fragments),glyphs=len(node.get_text()),fractions=len(node.find_all('mfrac')),
                              scripts=len(node.find_all(['msub','msup','msubsup'])))
    except (OSError, RuntimeError, ValueError):
        return None
