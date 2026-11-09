"""Source-backed scalar quantities and structural guards before formula OCR.

An OCR reading used here only proposes a candidate.  It never supplies an
accepted formula: raster candidates still go through both equation readers.
Native quantities may be serialized directly when the source glyphs account
for every foreground pixel of the retained image.
"""
from html import escape
from io import BytesIO
import re


_NUMBER = r'[+−-]?\d+(?:[.,]\d+)?'
_UNIT = r'(?:[%‰]|°(?:[CFС])?|[мm](?:[²³]|[23])?)'
_QUANTITY = re.compile(rf'^\s*([≤≥<>≈=]?)\s*({_NUMBER})\s*({_UNIT})\s*$')


def quantity_mathml(value):
    """Serialize a complete scalar with an explicit unit, without guessing it."""
    match = _QUANTITY.fullmatch(value)
    if not match:
        return None
    relation, number, unit = match.groups()
    prefix = '<mo>'+escape(relation)+'</mo>' if relation else ''
    if unit[0] in 'мm' and len(unit) == 2:
        exponent = unit[-1].translate(str.maketrans('²³', '23'))
        suffix = '<msup><mi mathvariant="normal">'+unit[0]+'</mi><mn>'+exponent+'</mn></msup>'
    elif unit[0] in '%‰°':
        suffix = '<mo>'+escape(unit[0])+'</mo>'
        if len(unit) > 1:
            suffix += '<mi mathvariant="normal">'+escape(unit[1:])+'</mi>'
    else:
        suffix = '<mi mathvariant="normal">'+escape(unit)+'</mi>'
    return '<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline">'+prefix+'<mn>'+escape(number)+'</mn>'+suffix+'</math>'


def native_quantity(data, box, glyphs, native_lines):
    """Return exact source text only if all image ink belongs to its glyphs."""
    import numpy as np
    from PIL import Image
    chosen = [g for g in glyphs if g.get('text', '').strip()
              and box[0] <= (g['x0']+g['x1'])/2 < box[2]
              and box[1] <= (g['top']+g['bottom'])/2 < box[3]]
    if not chosen or any(not g.get('upright', True) or '(cid:' in g['text']
                         or '\ufffd' in g['text'] for g in chosen):
        return None
    lines = native_lines(chosen)
    if len(lines) != 1:
        return None
    value = lines[0]['text']
    rendered = quantity_mathml(value)
    if rendered is None:
        return None
    with Image.open(BytesIO(data)) as image:
        ink = np.asarray(image.convert('L')) < 200
    allowed = np.zeros(ink.shape, dtype=bool)
    sx, sy = ink.shape[1]/(box[2]-box[0]), ink.shape[0]/(box[3]-box[1])
    for g in chosen:
        x0=max(0, int((g['x0']-box[0])*sx)-1); x1=min(ink.shape[1], int(__import__('math').ceil((g['x1']-box[0])*sx))+1)
        y0=max(0, int((g['top']-box[1])*sy)-1); y1=min(ink.shape[0], int(__import__('math').ceil((g['bottom']-box[1])*sy))+1)
        allowed[y0:y1, x0:x1] = True
    if np.any(ink & ~allowed):
        return None
    return dict(value=value, html=rendered, method='complete_native_quantity_glyphs_and_source_ink')


def raster_quantity_candidate(data, reader):
    """Ordinary OCR may nominate a scalar, but must never change % into ‰.

    The common OCR spelling ``%0`` merely nominates this raster for the strict
    equation readers.  Its text is deliberately not returned as an accepted
    quantity, because the third circle may be an actual separate zero.
    """
    import numpy as np
    from PIL import Image, ImageOps
    with Image.open(BytesIO(data)) as original:
        image = original.convert('RGB')
    ink = np.asarray(image.convert('L')) < 200
    ys, xs = np.where(ink)
    if not len(xs):
        image.close()
        return None
    cropped = image.crop((int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1))
    image.close()
    readings = []
    for scale in (1., .75):
        probe = cropped.resize((max(1, round(cropped.width*scale)), max(1, round(cropped.height*scale))))
        padded = ImageOps.expand(probe, border=max(2, round(probe.height*.2)), fill='white')
        result = reader(np.asarray(padded)[:, :, ::-1].copy(), use_det=False, use_cls=False, use_rec=True)
        texts = list(result.txts) if result.txts is not None else []
        if len(texts) == 1:
            text = re.sub(r'\s+', '', texts[0])
            if _QUANTITY.fullmatch(text) or re.fullmatch(rf'{_NUMBER}%[oо0]', text):
                readings.append(text)
        padded.close(); probe.close()
    cropped.close()
    return dict(readings=readings, method='ordinary_ocr_quantity_candidate_only') if readings else None


def graphic_outline(data):
    """Reject a closed rule frame whose ink contains no typographic content.

    Rule lengths are measured relative to the actual ink bounds, rather than
    a physical size cutoff which would confuse large letters and small diagrams.
    """
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        gray = np.asarray(image.convert('L'))
    mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    ys, xs = np.where(mask)
    if not len(xs):
        return None
    mask = mask[ys.min():ys.max()+1, xs.min():xs.max()+1]
    height, width = mask.shape
    if min(width, height) < 4:
        return None
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1,max(3,round(width*.7))),np.uint8))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((max(3,round(height*.7)),1),np.uint8))
    hbands = np.flatnonzero((horizontal > 0).sum(axis=1) >= width*.7)
    vbands = np.flatnonzero((vertical > 0).sum(axis=0) >= height*.7)
    if not len(hbands) or not len(vbands) or hbands[-1]-hbands[0] < height*.7 or vbands[-1]-vbands[0] < width*.7:
        return None
    rules = cv2.dilate(cv2.bitwise_or(horizontal,vertical), np.ones((3,3),np.uint8)) > 0
    unaccounted = int(np.count_nonzero((mask > 0) & ~rules))
    # An arrow head at a corner belongs to the frame.  A letter in the centre
    # does not: require a completely empty central field as well as rule ink.
    central = mask[round(height*.2):round(height*.8),round(width*.2):round(width*.8)]
    if np.any(central) or unaccounted > max(1,np.count_nonzero(mask)*.1):
        return None
    return dict(reason='closed_rule_frame_without_typographic_content', method='source_rule_ink_accounting',
                foreground_pixels=int(np.count_nonzero(mask)), nonrule_pixels=unaccounted)


def single_symbol_ink(data):
    """Require one horizontal ink group, independently of its physical size.

    Vertically separated accents/dots may belong to the same glyph even when
    their horizontal boxes have a small gap. Two letters sharing a baseline
    never merge merely because their inter-letter spacing is narrow.
    """
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(BytesIO(data)) as image:gray=np.asarray(image.convert('L'))
    threshold,core=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    ink=gray<(threshold+255)/2
    occupied=np.flatnonzero(ink.any(axis=0));groups=[]
    for x in occupied:
        if groups and x==groups[-1][-1]+1:groups[-1].append(int(x))
        else:groups.append([int(x)])
    bands=[]
    for xs in groups:
        ys=np.flatnonzero(ink[:,xs].any(axis=1))
        bands.append(dict(x0=xs[0],x1=xs[-1]+1,top=int(ys.min()),bottom=int(ys.max()+1)))
    merged=[]
    for band in bands:
        if merged:
            before=merged[-1]
            separate_y=before['bottom']<=band['top'] or band['bottom']<=before['top']
            gap=band['x0']-before['x1']
            height=max(before['bottom'],band['bottom'])-min(before['top'],band['top'])
            if separate_y and gap<=height*.25:
                before.update(x1=band['x1'],top=min(before['top'],band['top']),bottom=max(before['bottom'],band['bottom']))
                continue
        merged.append(dict(band))
    _,_,stats,_=cv2.connectedComponentsWithStats(core,8)
    parts=[dict(x0=int(x),x1=int(x+w),top=int(y),bottom=int(y+h),area=int(area)) for x,y,w,h,area in stats[1:]]
    maximum=max([p['area'] for p in parts],default=0)
    substantial=[p for p in parts if p['area']>=maximum*.2]
    body_groups=max([sum(min(a['bottom'],b['bottom'])-max(a['top'],b['top'])>=
                            .6*min(a['bottom']-a['top'],b['bottom']-b['top'])
                        and max(a['bottom']-a['top'],b['bottom']-b['top'])<=
                            2*min(a['bottom']-a['top'],b['bottom']-b['top'])
                        for b in substantial) for a in substantial],default=0)
    groups=max(len(merged),body_groups)
    return dict(single=groups==1,ink_groups=groups,source_bands=bands,body_components=substantial,
                method='horizontal_ink_ownership_with_displaced_accent_support')


def source_frame_context(data, source_image, source_region, asset, cache=None):
    """Prove a clipped candidate belongs to an otherwise empty rule frame.

    The context follows the actual connected source ink component; no fixed
    page margin or padding is used. An exact RGB subimage match verifies that
    every candidate pixel occurs inside the examined frame context.
    """
    import cv2
    import numpy as np
    from PIL import Image
    import hashlib,math
    if source_image is None or not source_region:return None
    if cache is None:cache={}
    if not cache:
        full=np.asarray(source_image.convert('RGB'))
        gray=cv2.cvtColor(full,cv2.COLOR_RGB2GRAY)
        threshold,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        mask=(gray<(threshold+255)/2).astype('uint8')*255
        _,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
        cache.update(full=full,mask=mask,labels=labels,stats=stats,rules={})
    full,mask,labels,stats=(cache[k] for k in ('full','mask','labels','stats'))
    sx,sy=full.shape[1]/(source_region[2]-source_region[0]),full.shape[0]/(source_region[3]-source_region[1])
    x0=max(0,math.floor((asset[0]-source_region[0])*sx));x1=min(full.shape[1],math.ceil((asset[2]-source_region[0])*sx))
    y0=max(0,math.floor((asset[1]-source_region[1])*sy));y1=min(full.shape[0],math.ceil((asset[3]-source_region[1])*sy))
    touched=[int(index) for index in np.unique(labels[y0:y1,x0:x1]) if index]
    if not touched:return None
    boxes=[stats[index] for index in touched]
    left=min([x0,*(int(b[0]) for b in boxes)]);top=min([y0,*(int(b[1]) for b in boxes)])
    right=max([x1,*(int(b[0]+b[2]) for b in boxes)]);bottom=max([y1,*(int(b[1]+b[3]) for b in boxes)])
    contexts=[]
    if (left,top,right,bottom)!=(x0,y0,x1,y1):contexts.append((left,top,right,bottom,'connected_ink_component'))
    # A frame may be connected to a surrounding table grid. Follow the
    # nearest complete source rule cycle rather than the entire connected grid.
    rule_key=(max(3,round((x1-x0)*.7)),max(3,round((y1-y0)*.7)))
    if rule_key not in cache['rules']:
        horizontal=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((1,rule_key[0]),np.uint8))
        vertical=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((rule_key[1],1),np.uint8))
        cache['rules'][rule_key]=(cv2.connectedComponentsWithStats(horizontal,8)[2][1:],
                                  cv2.connectedComponentsWithStats(vertical,8)[2][1:])
    hs,vs=cache['rules'][rule_key]
    top_rules=[b for b in hs if b[0]<=x0 and b[0]+b[2]>=x1 and b[1]<=y0]
    bottom_rules=[b for b in hs if b[0]<=x0 and b[0]+b[2]>=x1 and b[1]+b[3]>=y1]
    left_rules=[b for b in vs if b[1]<=y0 and b[1]+b[3]>=y1 and b[0]<=x0]
    right_rules=[b for b in vs if b[1]<=y0 and b[1]+b[3]>=y1 and b[0]+b[2]>=x1]
    if top_rules and bottom_rules and left_rules and right_rules:
        tr=max(top_rules,key=lambda b:b[1]);br=min(bottom_rules,key=lambda b:b[1]+b[3])
        lr=max(left_rules,key=lambda b:b[0]);rr=min(right_rules,key=lambda b:b[0]+b[2])
        contexts.append((int(lr[0]),int(tr[1]),int(rr[0]+rr[2]),int(br[1]+br[3]),'nearest_complete_source_rule_cycle'))
    with Image.open(BytesIO(data)) as image:original=np.asarray(image.convert('RGB'))
    oh,ow=original.shape[:2]
    for left,top,right,bottom,basis in contexts:
        context=full[top:bottom,left:right]
        if oh>context.shape[0] or ow>context.shape[1]:continue
        stream=BytesIO();Image.fromarray(context).save(stream,'PNG')
        proof=graphic_outline(stream.getvalue())
        if not proof:continue
        scores=cv2.matchTemplate(cv2.cvtColor(context,cv2.COLOR_RGB2GRAY),cv2.cvtColor(original,cv2.COLOR_RGB2GRAY),cv2.TM_SQDIFF)
        _,_,location,_=cv2.minMaxLoc(scores);mx,my=location
        if not np.array_equal(context[my:my+oh,mx:mx+ow],original):continue
        return dict(reason='candidate_is_clipped_part_of_empty_source_rule_frame',
            method='source_rule_cycle_and_exact_rgb_candidate_ownership',context_basis=basis,
            source_context_bbox=[source_region[0]+left/sx,source_region[1]+top/sy,
                                 source_region[0]+right/sx,source_region[1]+bottom/sy],
            source_components=touched,source_context_sha256=hashlib.sha256(stream.getvalue()).hexdigest(),
            source_candidate_sha256=hashlib.sha256(data).hexdigest(),frame_proof=proof)
    return None


def source_boundary_rule_fragment(data,source_image=None,source_region=None,asset=None,cache=None):
    """Account for a clipped border/arrow fragment in a proven blank form rail.

    A source rule must run between opposite crop edges. All remaining ink
    must be connected to that rule at its endpoint, within a few measured
    stroke widths. This proof is used only together with source column roles;
    it never classifies arbitrary border-touching text on its own.
    """
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(BytesIO(data)) as image:gray=np.asarray(image.convert('L'))
    _,mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    height,width=mask.shape
    for vertical in (False,True):
        view=mask.T if vertical else mask
        h,w=view.shape
        rule=cv2.morphologyEx(view,cv2.MORPH_OPEN,np.ones((1,max(3,round(w*.8))),np.uint8))
        _,_,stats,_=cv2.connectedComponentsWithStats(rule,8)
        for x,y,rw,rh,_ in stats[1:]:
            x,y,rw,rh=map(int,(x,y,rw,rh))
            if not (x==0 and x+rw==w and (y<=rh or y+rh>=h-rh)):
                continue
            _,components,_,_=cv2.connectedComponentsWithStats(view,8)
            owned={int(v) for v in np.unique(components[rule>0]) if v}
            if any(int(v) not in owned for v in np.unique(components[view>0])):
                continue
            remainder=(view>0)&~(rule>0)
            ys,xs=np.where(remainder)
            if len(xs):
                stroke=max(1,rh)
                near_left=xs.max()<6*stroke
                near_right=xs.min()>=w-6*stroke
                near_rule=max(abs(int(ys.min())-y),abs(int(ys.max())-(y+rh-1)))<=6*stroke
                if not (near_rule and (near_left or near_right)):
                    continue
            return dict(method='source_boundary_rule_and_attached_endpoint_ink',
                orientation='vertical' if vertical else 'horizontal',rule_thickness_pixels=rh,
                source_rule_pixels=int(np.count_nonzero(rule)),endpoint_ink_pixels=int(np.count_nonzero(remainder)))
    if source_image is None or not source_region or not asset:return None
    # A cell crop can exclude the actual rule and retain only its attached
    # arrow tip. Prove its ownership against the unchanged surrounding raster.
    import math
    full=(cache or {}).get('full')
    if full is None:full=np.asarray(source_image.convert('RGB'))
    sx,sy=full.shape[1]/(source_region[2]-source_region[0]),full.shape[0]/(source_region[3]-source_region[1])
    x0=max(0,math.floor((asset[0]-source_region[0])*sx));x1=min(full.shape[1],math.ceil((asset[2]-source_region[0])*sx))
    y0=max(0,math.floor((asset[1]-source_region[1])*sy));y1=min(full.shape[0],math.ceil((asset[3]-source_region[1])*sy))
    with Image.open(BytesIO(data)) as image:original=np.asarray(image.convert('RGB'))
    if not np.array_equal(full[y0:y1,x0:x1],original):return None
    source_mask=(cache or {}).get('mask')
    if source_mask is None:
        source_gray=cv2.cvtColor(full,cv2.COLOR_RGB2GRAY)
        threshold,_=cv2.threshold(source_gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        source_mask=(source_gray<(threshold+255)/2).astype('uint8')*255
    local=source_mask[y0:y1,x0:x1]
    ly,lx=np.where(local)
    if not len(lx):return None
    for vertical in (False,True):
        view=source_mask.T if vertical else source_mask
        origin_x,origin_y=(y0,x0) if vertical else (x0,y0)
        px,py=(ly,lx) if vertical else (lx,ly)
        crop_width=height if vertical else width
        rule=cv2.morphologyEx(view,cv2.MORPH_OPEN,np.ones((1,max(3,round(crop_width*.8))),np.uint8))
        _,_,stats,_=cv2.connectedComponentsWithStats(rule,8)
        _,components,_,_=cv2.connectedComponentsWithStats(view,8)
        ids={int(v) for v in np.unique(components[origin_y+py,origin_x+px]) if v}
        for rx,ry,rw,rh,_ in stats[1:]:
            rx,ry,rw,rh=map(int,(rx,ry,rw,rh));stroke=max(1,rh)
            source_x,source_y=origin_x+px,origin_y+py
            if (max(abs(int(source_y.min())-ry),abs(int(source_y.max())-(ry+rh-1)))>6*stroke
                    or not (np.max(np.abs(source_x-rx))<=6*stroke
                            or np.max(np.abs(source_x-(rx+rw-1)))<=6*stroke)):
                continue
            rule_ids={int(v) for v in np.unique(components[ry:ry+rh,rx:rx+rw][rule[ry:ry+rh,rx:rx+rw]>0]) if v}
            if not ids or not ids<=rule_ids:continue
            physical=([source_region[0]+ry/sx,source_region[1]+rx/sy,
                       source_region[0]+(ry+rh)/sx,source_region[1]+(rx+rw)/sy] if vertical else
                      [source_region[0]+rx/sx,source_region[1]+ry/sy,
                       source_region[0]+(rx+rw)/sx,source_region[1]+(ry+rh)/sy])
            return dict(method='clipped_endpoint_attached_to_positioned_source_rule',
                orientation='vertical' if vertical else 'horizontal',source_rule_bbox=physical,
                rule_thickness_pixels=rh,source_components=sorted(ids),exact_candidate_rgb_match=True,
                endpoint_ink_pixels=int(np.count_nonzero(local)))
    return None


def complete_source_component_ownership(data,source_image,source_region,asset,cache=None):
    """An isolated glyph must own every source ink component it intersects.

    A cropped arrow head or table rule is not a letter merely because its
    retained fragment looks like one. This preclassification proof neither
    changes an established Equation route nor consumes a formula budget.
    """
    import cv2
    import numpy as np
    from PIL import Image
    import math,hashlib
    if source_image is None or not source_region:
        return dict(complete=False,reason='source_component_context_unavailable')
    if cache is None:cache={}
    if not cache:
        full=np.asarray(source_image.convert('RGB'));gray=cv2.cvtColor(full,cv2.COLOR_RGB2GRAY)
        threshold,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        mask=(gray<(threshold+255)/2).astype('uint8')*255
        _,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
        cache.update(full=full,mask=mask,labels=labels,stats=stats,rules={})
    full,mask,labels,stats=(cache[k] for k in ('full','mask','labels','stats'))
    sx,sy=full.shape[1]/(source_region[2]-source_region[0]),full.shape[0]/(source_region[3]-source_region[1])
    x0=max(0,math.floor((asset[0]-source_region[0])*sx));x1=min(full.shape[1],math.ceil((asset[2]-source_region[0])*sx))
    y0=max(0,math.floor((asset[1]-source_region[1])*sy));y1=min(full.shape[0],math.ceil((asset[3]-source_region[1])*sy))
    with Image.open(BytesIO(data)) as image:original=np.asarray(image.convert('RGB'))
    if not np.array_equal(full[y0:y1,x0:x1],original):
        return dict(complete=False,reason='source_glyph_pixel_mapping_not_verified')
    ids=[int(v) for v in np.unique(labels[y0:y1,x0:x1][mask[y0:y1,x0:x1]>0]) if v]
    outside=[]
    for index in ids:
        x,y,w,h,area=map(int,stats[index])
        if x<x0 or y<y0 or x+w>x1 or y+h>y1:
            outside.append(dict(component=index,pixels=area,
                bbox=[source_region[0]+x/sx,source_region[1]+y/sy,
                      source_region[0]+(x+w)/sx,source_region[1]+(y+h)/sy]))
    return dict(complete=bool(ids) and not outside,
        reason='source_ink_component_continues_outside_candidate' if outside else
               'complete_source_ink_components' if ids else 'source_candidate_has_no_ink',
        source_components=ids,continuing_components=outside,exact_candidate_rgb_match=True,
        source_candidate_sha256=hashlib.sha256(data).hexdigest(),
        method='complete_positioned_source_component_ownership')


def source_multiline_ink(data):
    """Prove separate multiglyph source rows before accepting multiline math.

    Every occupied row group must contain several substantial components.
    Dots/accents above one letter, the strokes of Xi and fraction bars therefore
    cannot establish this evidence. All foreground row groups are retained.
    """
    import cv2
    import numpy as np
    from PIL import Image
    import hashlib
    with Image.open(BytesIO(data)) as image:gray=np.asarray(image.convert('L'))
    threshold,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    ink=(gray<(threshold+255)/2).astype('uint8')
    projection=ink.any(axis=1)
    starts=np.flatnonzero(projection&~np.r_[False,projection[:-1]])
    ends=np.flatnonzero(projection&~np.r_[projection[1:],False])+1
    if len(starts)<2:return None
    _,_,stats,_=cv2.connectedComponentsWithStats(ink,8)
    rows=[]
    for top,bottom in zip(starts,ends):
        parts=[list(map(int,p)) for p in stats[1:] if p[1]>=top and p[1]+p[3]<=bottom]
        largest=max((p[4] for p in parts),default=0)
        glyphs=[p for p in parts if p[3]>=max(2,(bottom-top)*.35) and p[4]>=largest*.15]
        if len(glyphs)<2:return None
        xs=np.flatnonzero(ink[top:bottom].any(axis=0))
        rows.append(dict(pixel_bbox=[int(xs.min()),int(top),int(xs.max()+1),int(bottom)],
                         glyph_components=len(glyphs),ink_pixels=int(np.count_nonzero(ink[top:bottom]))))
    return dict(method='complete_separate_multiglyph_source_rows',row_count=len(rows),rows=rows,
        source_sha256=hashlib.sha256(data).hexdigest(),source_dimensions=[gray.shape[1],gray.shape[0]],
        source_ink_pixels=int(np.count_nonzero(ink)),assigned_source_ink_pixels=sum(r['ink_pixels'] for r in rows))


def source_permille_quantity(data, reader):
    """Read a scalar only when source topology proves a printed per-mille sign.

    Three similarly small closed circles, the two lower circles on the same
    baseline, and a separate rising slash distinguish ‰ from %, %0 and %o.
    The numeric prefix is read twice from its own crop; the unit is never
    inferred from an OCR spelling such as ``%o``.
    """
    import cv2
    import numpy as np
    from PIL import Image,ImageOps
    from statistics import median
    with Image.open(BytesIO(data)) as original:
        image=original.convert('RGB')
    gray=np.asarray(image.convert('L'))
    threshold,mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    ys,xs=np.where(mask)
    if not len(xs):
        image.close();return None
    inkheight=int(ys.max()-ys.min()+1)
    contours,hierarchy=cv2.findContours(mask,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    holes=[]
    if hierarchy is not None:
        for index,contour in enumerate(contours):
            parent=int(hierarchy[0,index,3])
            if parent<0:continue
            x,y,w,h=cv2.boundingRect(contour);area=float(cv2.contourArea(contour))
            if .2*inkheight<=h<=.65*inkheight and .35<=w/h<=1.5 and area>=w*h*.25:
                holes.append(dict(box=[x,y,x+w,y+h],area=area,parent=parent,cx=x+w/2,cy=y+h/2,height=h))
    proof=None
    # A terminal unit has the rightmost three meaningful small ring holes.
    # Do not enumerate arbitrary triples in a large raster/table candidate.
    for triple in [sorted(holes,key=lambda h:h['cx'])[-3:]] if len(holes)>=3 else []:
        top=sorted(triple,key=lambda h:h['cy'])[0]
        bottom=sorted((h for h in triple if h is not top),key=lambda h:h['cx'])
        ringheight=median(h['height'] for h in triple)
        if (max(h['height'] for h in triple)>1.5*min(h['height'] for h in triple)
                or max(h['area'] for h in triple)>2*min(h['area'] for h in triple)
                or abs(bottom[0]['cy']-bottom[1]['cy'])>ringheight*.3
                or not .6*ringheight<=min(h['cy'] for h in bottom)-top['cy']<=1.8*ringheight
                or not top['cx']<bottom[0]['cx']<bottom[1]['cx']
                or not .6*ringheight<=bottom[1]['cx']-bottom[0]['cx']<=2.5*ringheight):
            continue
        parents={h['parent'] for h in triple}
        boxes=[cv2.boundingRect(contours[p]) for p in parents]
        unit_left=min(b[0] for b in boxes);unit_top=min(b[1] for b in boxes)
        unit_right=max(b[0]+b[2] for b in boxes);unit_bottom=max(b[1]+b[3] for b in boxes)
        unit_width,unit_height=unit_right-unit_left,unit_bottom-unit_top
        if not unit_width or not unit_height or not .8<=unit_width/unit_height<=2:
            continue
        # A full-size zero or letter following % adds a larger or differently
        # aligned hole.  Tiny rasterization pinholes do not count as circles.
        meaningful=[]
        for index,contour in enumerate(contours):
            if hierarchy[0,index,3]<0:continue
            x,y,w,h=cv2.boundingRect(contour)
            if x>=unit_left and cv2.contourArea(contour)>=median(t['area'] for t in triple)*.4:
                meaningful.append(index)
        if len(meaningful)!=3:
            continue
        # Colour-fringed source scans may render the slash lighter than the
        # closed rings. Use the midpoint to the white paper only for locating
        # that stroke; ring topology still uses the independent Otsu mask.
        stroke_mask=(gray<(threshold+255)/2).astype('uint8')*255
        unit=stroke_mask[unit_top:unit_bottom,unit_left:unit_right]
        lines=cv2.HoughLinesP(unit,1,np.pi/180,threshold=max(4,round(unit_height*.3)),
            minLineLength=max(3,round(unit_height*.7)),maxLineGap=max(1,round(unit_height*.08)))
        diagonals=[]
        for line in lines if lines is not None else []:
            ax,ay,bx,by=map(int,line[0]);dx,dy=bx-ax,by-ay
            if dx*dy<0 and abs(dx)>=unit_height*.25 and abs(dy)>=unit_height*.65:
                diagonals.append([ax+unit_left,ay+unit_top,bx+unit_left,by+unit_top])
        if not diagonals:
            continue
        # There may be no unaccounted content to the right of the unit.
        if np.any(mask[:,unit_right:]):
            continue
        prefix=mask[:,:unit_left]
        py,px=np.where(prefix)
        if not len(px) or not .7*unit_height<=py.max()-py.min()+1<=1.5*unit_height:
            continue
        proof=dict(unit_bbox=[unit_left,unit_top,unit_right,unit_bottom],
            ring_holes=[h['box'] for h in sorted(triple,key=lambda h:(h['cy'],h['cx']))],
            diagonal_strokes=diagonals,numeric_bbox=[int(px.min()),int(py.min()),int(px.max()+1),int(py.max()+1)])
        break
    if proof is None:
        image.close();return None
    prefix=image.crop(proof['numeric_bbox']);image.close()
    readings=[];scores=[]
    for scale in (1.,.75):
        probe=prefix.resize((max(1,round(prefix.width*scale)),max(1,round(prefix.height*scale))))
        padded=ImageOps.expand(probe,border=max(2,round(probe.height*.25)),fill='white')
        result=reader(np.asarray(padded)[:,:,::-1].copy(),use_det=False,use_cls=False,use_rec=True)
        text=list(result.txts) if result.txts is not None else []
        confidence=list(result.scores) if result.scores is not None else []
        padded.close();probe.close()
        if len(text)!=1 or not confidence or min(confidence)<.9:
            prefix.close();return None
        value=re.sub(r'\s+','',text[0])
        if not re.fullmatch(_NUMBER,value):
            prefix.close();return None
        readings.append(value);scores.append(float(min(confidence)))
    prefix.close()
    if readings[0]!=readings[1]:return None
    return dict(value=readings[0]+'‰',html=quantity_mathml(readings[0]+'‰'),
        method='source_permille_topology_and_two_numeric_prefix_reads',
        minimum_confidence=min(scores),numeric_reads=readings,**proof)
