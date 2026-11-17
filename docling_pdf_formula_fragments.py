"""Source geometry for native formulas whose fraction rules become PNGs."""
from io import BytesIO
import re
from statistics import median


def trim_formula_whitespace(image):
    """Remove only empty outer margins, keeping every ink pixel and padding."""
    from PIL import ImageOps
    import numpy as np
    import cv2
    gray=np.asarray(image.convert('L'))
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    yy,xx=np.nonzero(ink)
    if not len(xx):return image.copy(),None
    bounds=[int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)]
    padding=max(4,round(.12*min(bounds[2]-bounds[0],bounds[3]-bounds[1])))
    wanted=[bounds[0]-padding,bounds[1]-padding,bounds[2]+padding,bounds[3]+padding]
    crop=[max(0,wanted[0]),max(0,wanted[1]),min(image.width,wanted[2]),min(image.height,wanted[3])]
    patch=image.crop(crop)
    border=(crop[0]-wanted[0],crop[1]-wanted[1],wanted[2]-crop[2],wanted[3]-crop[3])
    result=ImageOps.expand(patch,border=border,fill='white');patch.close()
    return result,dict(method='all_source_ink_with_empty_outer_margin_crop',source_size=list(image.size),
        source_ink_bbox=bounds,source_crop_bbox=crop,white_padding_added=list(border),
        recognition_size=list(result.size),source_ink_pixels=int(np.count_nonzero(ink)))


def fraction_bar_context(data, entry, source):
    """Prove a short internal rule separates native numerator/denominator.

    A layout label or a horizontal bitmap alone is insufficient. The bar
    must lie inside one physical cell, and positioned mathematical glyphs
    must occupy both sides of that same rule. Adjacent table cells and
    ordinary underlines therefore cannot supply the required evidence.
    """
    if not source or not entry.get('bbox') or not source.get('bbox'):return None
    glyphs=[g for g in source.get('glyphs',[]) if g.get('text','').strip() and g.get('bbox')]
    if not glyphs:return None
    text=' '.join(line.get('text','') for line in source.get('lines',[]))
    if not re.search(r'[=+*/<>≤≥−∑Σ√α-ωΑ-Ω]',text):return None
    from PIL import Image
    import numpy as np
    import cv2
    with Image.open(BytesIO(data)) as image:
        gray=np.asarray(image.convert('L'))
        ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    yy,xx=np.nonzero(ink)
    if not len(xx):return None
    width,height=int(xx.max()-xx.min()+1),int(yy.max()-yy.min()+1)
    if width<5 or width<4*height:return None
    # A fraction rule is a single nearly filled horizontal ink strip.
    if len(xx)/(width*height)<.7:return None
    box=list(map(float,entry['bbox']));cell=list(map(float,source['bbox']))
    if box[0]<cell[0] or box[1]<cell[1] or box[2]>cell[2] or box[3]>cell[3]:return None
    sizes=[float(g.get('size') or g['bbox'][3]-g['bbox'][1]) for g in glyphs]
    sizes=[v for v in sizes if v>0]
    if not sizes:return None
    size=median(sizes)
    if (box[3]-box[1]>.25*size or box[2]-box[0]>.85*(cell[2]-cell[0])
            or box[1]-cell[1]<.3*size or cell[3]-box[3]<.3*size):return None
    above=[];below=[]
    for g in glyphs:
        b=g['bbox'];gs=float(g.get('size') or b[3]-b[1])
        center=(b[0]+b[2])/2
        if not box[0]-.1*size<=center<=box[2]+.1*size:continue
        if -.08*gs<=box[1]-b[3]<=1.2*gs:above.append(g)
        if -.08*gs<=b[1]-box[3]<=1.2*gs:below.append(g)
    # A measured index belongs to its base even when its font box slightly
    # crosses the bar. Never borrow an unproved neighboring script glyph.
    glyph_key=lambda g:(g['text'],tuple(g['bbox']))
    available={glyph_key(g):g for g in glyphs}
    for items,role in ((above,'numerator'),(below,'denominator')):
        owned={glyph_key(g) for g in items}
        for line in source.get('lines',[]):
            for script in line.get('source_scripts',[]):
                group=script.get('glyphs',[])
                if (script.get('method')!='source_glyph_size_text_origin_and_adjacent_base' or not group
                        or glyph_key(group[0]) not in owned
                        or any(glyph_key(g) not in available for g in group)):
                    continue
                boxes=[g['bbox'] for g in group]
                if any(not box[0]-.1*size <= (b[0]+b[2])/2 <= box[2]+.1*size for b in boxes):continue
                if role=='numerator' and max(b[3] for b in boxes)>box[3]+.1*size:continue
                if role=='denominator' and min(b[1] for b in boxes)<box[1]-.1*size:continue
                for g in group:
                    if glyph_key(g) not in owned:
                        items.append(available[glyph_key(g)]);owned.add(glyph_key(g))
    if not above or not below:return None
    literal=lambda items:''.join(g['text'] for g in sorted(items,key=lambda g:g['bbox'][0]))
    numerator,denominator=literal(above),literal(below)
    if re.search('[А-Яа-яЁё]{3,}',numerator+denominator):return None
    return dict(method='native_glyphs_on_both_sides_of_internal_fraction_rule',bbox=box,
        cell_bbox=cell,numerator=numerator,denominator=denominator,
        numerator_glyphs=[dict(text=g['text'],bbox=g['bbox']) for g in above],
        denominator_glyphs=[dict(text=g['text'],bbox=g['bbox']) for g in below],
        rule_ink_bbox=[int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)])


def graft_source_fraction_fragment(fragment, recognized, source, entries, api):
    """Insert one proved OCR fraction while retaining surrounding native HTML.

    The numerator and denominator must occupy complete, separate native
    lines around a measured raster bar. The OCR prefix supplies no text.
    """
    import base64
    from collections import Counter
    from copy import deepcopy
    from hashlib import sha256
    from bs4 import BeautifulSoup

    if not source or source.get('source_text_unreliable') or len(entries) != 1:
        return None
    lines = source.get('lines', [])
    expected = '<br/>'.join(line.get('html', '') for line in lines)
    canonical = lambda value: str(BeautifulSoup(value, 'html.parser'))
    if canonical(expected) != canonical(fragment):
        return None  # Never discard a newer editable fragment or its markup.
    original = BeautifulSoup(fragment, 'html.parser')
    images = original.find_all('img')
    if len(images) != 1 or images[0].get('data-source-cell') != 'true':
        return None
    uri = images[0].get('src', '')
    if not re.match(r'^data:image/[^;]+;base64,', uri):
        return None
    try:
        data = base64.b64decode(uri.split(',', 1)[1], validate=True)
        evidence = fraction_bar_context(data, entries[0], source)
    except (ValueError, OSError):
        return None
    if not evidence:
        return None
    candidate = BeautifulSoup(recognized, 'html.parser')
    fractions = candidate.find_all('mfrac')
    if len(fractions) != 1:
        return None
    fraction = fractions[0]
    terms = fraction.find_all(recursive=False)
    if len(terms) != 2 or fraction.find(['mfrac', 'msqrt', 'mroot', 'mtable']):
        return None
    if any(api.cell_text_key(node.get_text()) != api.cell_text_key(evidence[name])
           for node, name in zip(terms, ('numerator', 'denominator'))):
        return None

    owned_lines = []
    for role in ('numerator', 'denominator'):
        glyphs = evidence[role + '_glyphs']
        left = min(g['bbox'][0] for g in glyphs)
        right = max(g['bbox'][2] for g in glyphs)
        height = max(g['bbox'][3]-g['bbox'][1] for g in glyphs)
        baseline = max(g['bbox'][3] for g in glyphs if g['bbox'][3]-g['bbox'][1]>=height*.9)
        matches = [index for index, line in enumerate(lines)
            if api.cell_text_key(line.get('text', '')) == api.cell_text_key(evidence[role])
            and abs(line.get('x0', left) - left) <= line['size'] * .1
            and abs(line.get('x1', right) - right) <= line['size'] * .1
            and abs(line['y'] - baseline) <= line['size'] * .1
            and not BeautifulSoup(line.get('html', ''), 'html.parser').find('img')]
        if len(matches) != 1:
            return None
        from docling_pdf_native_scripts import script_terms
        from docling_pdf_text_cells import _source_script_math
        native = BeautifulSoup(lines[matches[0]]['html'], 'html.parser')
        approved = _source_script_math(native, source.get('glyphs', []), lines[matches[0]].get('source_scripts', []))
        if (any(id(node) not in approved for node in native.find_all('math'))
                or script_terms(str(native)) != script_terms(str(terms[len(owned_lines)]))):
            return None
        owned_lines.append(matches[0])
    owners = [index for index, line in enumerate(lines)
              if len(BeautifulSoup(line.get('html', ''), 'html.parser').find_all('img')) == 1]
    if (len(owners) != 1 or len(set(owned_lines + owners)) != 3
            or not owned_lines[0] < owners[0] < owned_lines[1]):
        return None
    wrapper = BeautifulSoup('', 'html.parser')
    math = wrapper.new_tag('math', attrs={'display': 'inline', 'xmlns': 'http://www.w3.org/1998/Math/MathML'})
    math.append(deepcopy(fraction))
    owner = BeautifulSoup(lines[owners[0]]['html'], 'html.parser')
    owner.img.replace_with(math)
    result = '<br/>'.join(str(owner) if index == owners[0] else line['html']
                         for index, line in enumerate(lines) if index not in owned_lines)
    payload = lambda value: Counter(api.cell_text_key(BeautifulSoup(value, 'html.parser').get_text()))
    if payload(result) != payload(fragment) or BeautifulSoup(result, 'html.parser').find('img'):
        return None
    bounds = [*evidence['numerator_glyphs'], *evidence['denominator_glyphs'], dict(bbox=evidence['bbox'])]
    proof = dict(method='native_lines_with_exact_ocr_fraction_on_proved_source_bar',
        source_fraction_bbox=[min(g['bbox'][0] for g in bounds), min(g['bbox'][1] for g in bounds),
                              max(g['bbox'][2] for g in bounds), max(g['bbox'][3] for g in bounds)],
        numerator=evidence['numerator'], denominator=evidence['denominator'],
        source_bar_bbox=evidence['bbox'], native_prefix_preserved=True,
        source_html_sha256=sha256(fragment.encode()).hexdigest(),
        ocr_fraction_sha256=sha256(str(fraction).encode()).hexdigest())
    return result, proof


def formula_cell_parts(source, entries):
    """Separate disjoint native prose lines from the formula bands.

    Formula-only recognition never receives a Cyrillic prose line. A line
    containing both prose and an image remains unresolved rather than being
    silently rewritten by a formula recognizer.
    """
    lines=source.get('lines',[])
    prose=[line for line in lines if re.search('[А-Яа-яЁё]{3,}',line.get('text',''))]
    if not prose:return [dict(kind='formula',bbox=source['bbox'])]
    if any('<img' in line.get('html','') for line in prose):return None
    result=[];group=[]
    def flush():
        if not group:return
        visible=[line for line in group if line.get('text','').strip() or '<img' in line.get('html','')]
        if visible:
            top=min(line['y']-line['size'] for line in visible)-2
            bottom=max(line['y'] for line in visible)+2
            bars=[e for e in entries if top<=(e['bbox'][1]+e['bbox'][3])/2<=bottom]
            if bars:
                result.append(dict(kind='formula',bbox=[source['bbox'][0],max(source['bbox'][1],top),
                    source['bbox'][2],min(source['bbox'][3],bottom)]))
            else:result.append(dict(kind='text',html='<br/>'.join(line['html'] for line in visible)))
        group.clear()
    for line in lines:
        if line in prose:
            flush();result.append(dict(kind='text',html=line['html']))
        else:group.append(line)
    flush()
    return result if any(p['kind']=='formula' for p in result) else None
