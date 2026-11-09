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
    if not above or not below:return None
    literal=lambda items:''.join(g['text'] for g in sorted(items,key=lambda g:g['bbox'][0]))
    numerator,denominator=literal(above),literal(below)
    if re.search('[А-Яа-яЁё]{3,}',numerator+denominator):return None
    return dict(method='native_glyphs_on_both_sides_of_internal_fraction_rule',bbox=box,
        cell_bbox=cell,numerator=numerator,denominator=denominator,
        numerator_glyphs=[dict(text=g['text'],bbox=g['bbox']) for g in above],
        denominator_glyphs=[dict(text=g['text'],bbox=g['bbox']) for g in below],
        rule_ink_bbox=[int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)])


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
