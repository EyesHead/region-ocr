"""Source-grid evidence that must precede equation OCR, including on scans."""
import math
from functools import lru_cache
from io import BytesIO
import re


def raster_grid(image, region):
    """Find long thin rules and their junctions in the table's source crop.

    Short partial-row rules matter: a wide merged neighbor must not hide two
    numeric cells in a narrow column. A fraction bar has no grid junctions.
    """
    import cv2
    import numpy as np
    gray = np.asarray(image.convert('L'))
    mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    height, width = mask.shape
    sx, sy = (region[2]-region[0])/width, (region[3]-region[1])/height
    horizontal, vertical = [], []
    for is_vertical in (False, True):
        minimum = max(12, round((6 if is_vertical else 10)/(sy if is_vertical else sx)))
        kernel = np.ones((minimum, 1) if is_vertical else (1, minimum), np.uint8)
        rules = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        _, _, stats, _ = cv2.connectedComponentsWithStats(rules, 8)
        for x, y, w, h, _ in stats[1:]:
            if (is_vertical and (h < minimum or w > max(8, h*.15))
                    or not is_vertical and (w < minimum or h > max(8, w*.15))):
                continue
            if is_vertical:
                vertical.append([region[0]+(x+w/2)*sx, region[1]+y*sy, region[1]+(y+h)*sy])
            else:
                horizontal.append([region[0]+x*sx, region[0]+(x+w)*sx, region[1]+(y+h/2)*sy])
    connected = []
    for left, right, y in horizontal:
        # Require actual grid junctions at both ends, not a long underline.
        if all(any(abs(v[0]-x) <= 3 and v[1]-3 <= y <= v[2]+3 for v in vertical)
               for x in (left, right)):
            connected.append([left, right, y])
    connected_vertical = []
    for x, top, bottom in vertical:
        if all(any(abs(h[2]-y) <= 3 and h[0]-3 <= x <= h[1]+3 for h in horizontal)
               for y in (top, bottom)):
            connected_vertical.append([x, top, bottom])
    return dict(horizontal=connected, vertical=vertical, connected_vertical=connected_vertical,
                region=list(region))


def crossed_grid(asset, geometry, grid):
    """Return source proof only for a crop crossing a real cell separator."""
    x0, y0, x1, y1 = map(float, asset)
    width, height = x1-x0, y1-y0
    if width <= 0 or height <= 0:
        return None
    rails = (geometry or {}).get('x_grid', [])
    for left, right, y in (grid or {}).get('horizontal', ()):
        if (y0+max(.7, height*.08) < y < y1-max(.7, height*.08)
                and left <= x0+2 and right >= x1-2
                and right-left >= width*.85 and rails
                and all(any(abs(x-rail) <= 3 for rail in rails) for x in (left, right))):
            return dict(reason='raster_grid_rule_crosses_candidate', source_bbox=list(map(float, asset)),
                        rule=[left, y, right, y], connected_to_column_rails=True)
    # Rotated tables and accidentally merged cells can cross column rules
    # instead of row rules.  Require multiple complete rules with real grid
    # junctions; a fraction, minus sign or lone mathematical stem cannot pass.
    cuts = [[x, top, bottom] for x, top, bottom in (grid or {}).get('connected_vertical', ())
            if x0+max(.7,width*.02) < x < x1-max(.7,width*.02)
            and top <= y0+max(2,height*.1) and bottom >= y1-max(2,height*.1)]
    if len(cuts) >= 2:
        return dict(reason='raster_grid_columns_cross_candidate', source_bbox=list(map(float, asset)),
                    rules=cuts, connected_to_row_rails=True)
    # Explicit source-grid row boundaries are useful even for vector tables.
    cells = (geometry or {}).get('cells', [])
    overlaps = []
    for cell in cells:
        box = cell.get('bbox')
        if not box:
            continue
        area = max(0, min(x1, box[2])-max(x0, box[0])) * max(0, min(y1, box[3])-max(y0, box[1]))
        if area/(width*height) >= .2:
            overlaps.append((cell.get('row'), cell.get('column'), box))
    if len({(row, col) for row, col, _ in overlaps}) > 1:
        return dict(reason='candidate_spans_separate_source_cells', source_bbox=list(map(float, asset)),
                    cells=[dict(row=row, column=col, bbox=box) for row, col, box in overlaps])
    return None


def recognition_grid_copy(data, asset, grid):
    """Remove only detached, source-proven table rules from an OCR copy.

    The retained image and source bbox remain unchanged.  No long stroke is
    removed merely for looking like a rule: it must map to a complete table
    rule with actual junctions outside this crop, and touch no nongrid ink.
    Fraction bars, overlines, underlines and strokes touching a letter survive.
    """
    import cv2
    import numpy as np
    from PIL import Image
    if not grid:
        return data, None
    with Image.open(BytesIO(data)) as image:
        rgb=np.asarray(image.convert('RGB')).copy()
    height,width=rgb.shape[:2]
    x0,y0,x1,y1=map(float,asset)
    sx,sy=width/(x1-x0),height/(y1-y0)
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    candidates=[]
    for left,right,y in grid.get('horizontal',()):
        if not (left<x0 and right>x1 and y0<=y<=y1):continue
        rules=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((1,max(3,round(width*.8))),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(rules,8)
        for index in range(1,count):
            bx,by,bw,bh,_=map(int,stats[index])
            if by-1 <= (y-y0)*sy <= by+bh+1:
                candidates.append((labels==index,dict(orientation='horizontal',rule=[left,y,right,y])))
    for x,top,bottom in grid.get('connected_vertical',()):
        if not (top<y0 and bottom>y1 and x0<=x<=x1):continue
        rules=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((max(3,round(height*.8)),1),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(rules,8)
        for index in range(1,count):
            bx,by,bw,bh,_=map(int,stats[index])
            if bx-1 <= (x-x0)*sx <= bx+bw+1:
                candidates.append((labels==index,dict(orientation='vertical',rule=[x,top,x,bottom])))
    removed=np.zeros(mask.shape,dtype=bool);proofs=[]
    for selected,proof in candidates:
        neighboring=cv2.dilate(selected.astype('uint8'),np.ones((3,3),np.uint8))>0
        if np.any((mask>0) & neighboring & ~selected):
            continue
        removed|=selected;proofs.append(proof)
    if not np.any(removed):
        return data,None
    rgb[removed]=255
    output=BytesIO();Image.fromarray(rgb).save(output,'PNG')
    return output.getvalue(),dict(method='detached_source_grid_rules_in_recognition_copy',
        removed_pixels=int(np.count_nonzero(removed)),rules=proofs)


@lru_cache(maxsize=1)
def _numeric_reader():
    # Already installed for page orientation. No formula model is called for
    # source-proven separate cells, and no new dependency is introduced.
    from rapidocr import RapidOCR
    return RapidOCR(params={'Global.log_level': 'error',
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1})


def _numeric_ink_only(image, value):
    """One typographic component per numeric character, with a common baseline.

    A border, circle, arrow or other unrecognized drawing leaves extra/oversize
    ink and disqualifies text substitution, even if OCR reads its number well.
    """
    import cv2
    import numpy as np
    from statistics import median
    colors=np.asarray(image.convert('RGB')).astype('int16')
    if np.count_nonzero(colors.max(axis=2)-colors.min(axis=2)>30) > colors.shape[0]*colors.shape[1]*.005:
        return False
    ink = (np.asarray(image.convert('L')) < 200).astype('uint8')
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    parts = sorted((list(map(int, s)) for s in stats[1:] if s[4] >= max(3, int(ink.sum()*.003))), key=lambda s:s[0])
    if len(parts) != len(value) or not parts:
        return False
    digits = [p for char, p in zip(value, parts) if char.isdigit()]
    if not digits:
        return False
    height, baseline = median(p[3] for p in digits), median(p[1]+p[3] for p in digits)
    return (all(.7*height <= p[3] <= 1.3*height and .1*p[3] <= p[2] <= 1.1*p[3] and p[4]/(p[2]*p[3]) < .85
                and abs(p[1]+p[3]-baseline) <= .25*height for p in digits)
            and all(a[0]+a[2] <= b[0]+1 for a,b in zip(parts,parts[1:])))


def plain_numeric_cell(data):
    """Return only a fully ink-accounted, repeatedly read scalar cell value.

    Fractions, scripts, drawings and multi-line cells fail the ink/baseline
    proof. This path is for retained source DATA cells, before equation OCR.
    """
    import numpy as np
    from PIL import Image, ImageOps
    with Image.open(BytesIO(data)) as original:
        image = original.convert('RGB')
    ink = np.asarray(image.convert('L')) < 200
    ys, xs = np.where(ink)
    if not len(xs):
        return None
    trimmed = image.crop((int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1))
    readings, scores = [], []
    for scale in (1.0, .75):
        probe = trimmed.resize((max(8,round(trimmed.width*scale)),max(8,round(trimmed.height*scale))))
        padded = ImageOps.expand(probe,border=8,fill='white')
        result = _numeric_reader()(np.asarray(padded)[:,:,::-1].copy(),use_det=False,use_cls=False,use_rec=True)
        texts=list(result.txts) if result.txts is not None else []
        confidence=list(result.scores) if result.scores is not None else []
        if len(texts)!=1 or not confidence or min(confidence)<.95:
            return None
        value=re.sub(r'\s+','',texts[0])
        if not re.fullmatch(r'[+−-]?\d+(?:[.,]\d+)?',value) or not _numeric_ink_only(padded,value):
            return None
        readings.append(value);scores.append(float(min(confidence)))
        padded.close();probe.close()
    image.close();trimmed.close()
    return dict(value=readings[0],minimum_confidence=min(scores)) if readings[0]==readings[1] else None


def split_numeric_cells(soup, node, entry, geometry, grid, data, api):
    """Undo a false rowspan only when rules and repeated numeric OCR agree.

    A grid rejection alone never invents values: ambiguous text stays in its
    original raster. Other cells, rowspans and rows remain unchanged.
    """
    import numpy as np
    from PIL import Image, ImageOps
    profile = api.table_profile(str(soup.table))
    owner = next((c for c in profile['cells']
                  if c['row'] == entry.get('row') and c['column'] == entry.get('column')), None)
    if (not owner or owner['column_span'] != 1 or owner['row_span'] < 2
            or node.parent.name not in {'td', 'th'} or node.parent.get_text(strip=True)
            or len(node.parent.find_all('img')) != 1 or node.parent.find('math')):
        return None
    box = list(map(float, entry['bbox']))
    cuts = sorted({round(line[2], 2) for line in (grid or {}).get('horizontal', ())
                   if box[1]+.7 < line[2] < box[3]-.7
                   and line[0] <= box[0]+2 and line[1] >= box[2]-2})
    if len(cuts) != owner['row_span']-1:
        return None
    ys = [box[1], *cuts, box[3]]
    with Image.open(BytesIO(data)) as original:
        image = original.convert('RGB')
    sx, sy = image.width/(box[2]-box[0]), image.height/(box[3]-box[1])
    values, scores = [], []
    for top, bottom in zip(ys, ys[1:]):
        strip = image.crop((math.floor(1.5*sx), math.floor((top-box[1]+.6)*sy),
                            math.ceil(image.width-1.5*sx), math.floor((bottom-box[1]-.6)*sy)))
        if strip.width < 4 or strip.height < 4:
            return None
        readings = []
        for scale in (1.0, .75):
            probe = strip.resize((max(8, round(strip.width*scale)), max(8, round(strip.height*scale))))
            padded = ImageOps.expand(probe, border=8, fill='white')
            array = np.asarray(padded)[:, :, ::-1].copy()
            result = _numeric_reader()(array, use_det=False, use_cls=False, use_rec=True)
            texts = list(result.txts) if result.txts is not None else []
            confidence = list(result.scores) if result.scores is not None else []
            if len(texts) != 1 or not confidence or min(confidence) < .95:
                return None
            value = re.sub(r'\s+', '', texts[0])
            if not re.fullmatch(r'[+−-]?\d+(?:[.,]\d+)?', value):
                return None
            if scale == 1.0 and not _numeric_ink_only(padded, value):
                return None
            padded.close(); probe.close()
            readings.append(value); scores.append(float(min(confidence)))
        strip.close()
        if readings[0] != readings[1]:
            return None
        values.append(readings[0])
    image.close()
    rows = soup.table.find_all('tr')
    cells = list(zip(profile['cells'], soup.table.find_all(['td', 'th'])))
    cell = node.parent
    cell.clear(); cell.attrs.pop('rowspan', None); cell.append(values[0])
    for offset, value in enumerate(values[1:], 1):
        new = soup.new_tag(cell.name); new.string = value
        following = next((tag for spec, tag in cells if spec['row'] == owner['row']+offset
                          and spec['column'] > owner['column']), None)
        if following:
            following.insert_before(new)
        else:
            rows[owner['row']+offset].append(new)
    checked = api.table_profile(str(soup.table))
    if checked['issues']:
        raise RuntimeError('Source numeric cell split produced an invalid table grid')
    return dict(row=owner['row'], column=owner['column'], source_rowspan=owner['row_span'],
                values=values, minimum_confidence=min(scores), source_rules=cuts,
                method='raster_cell_separators_and_two_matching_numeric_reads')
