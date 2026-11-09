"""Source-pixel evidence for mixed contents of scanned PDF table cells.

The hidden OCR layer locates a candidate; it does not prove its contents or
typography. No document names, variable dictionaries or OCR spelling repairs
are used here. Unresolved script expressions remain small source images.
"""

import math
import re
from io import BytesIO
from html import escape
from statistics import median


def split_mixed_cell_ink(data, mixed_html, source_bbox, prose_templates=None):
    """Locate alternating prose/math words using real source whitespace.

    The retained cell HTML proposes the reading order and the number of
    physical word groups. It does not validate the formula. Every returned
    math PNG must pass the normal independent readers *and* agree with its
    proposed formula before this split may replace a source cell image.
    Ambiguous whitespace requires source prose templates learned from other
    verified cells; without that evidence it raises a diagnostic error.
    Multiline ownership must be established separately, never flattened here.
    """
    import hashlib
    import cv2
    import numpy as np
    from bs4 import BeautifulSoup
    from PIL import Image

    parsed = BeautifulSoup(mixed_html, 'html.parser')
    math_nodes = [node for node in parsed.find_all('math') if node.find_parent('math') is None]
    if not math_nodes or parsed.find(['table', 'img', 'br']):
        raise ValueError('Mixed-cell proposal must contain inline prose and math without tables, images or line breaks')
    proposals = []
    for index, node in enumerate(math_nodes):
        proposals.append(str(node))
        node.replace_with(parsed.new_string(f' __PDF_MATH_SLOT_{index}__ '))
    sequence = []
    for token in parsed.get_text(' ', strip=True).split():
        match = re.fullmatch(r'__PDF_MATH_SLOT_(\d+)__', token)
        if match:
            sequence.append(dict(kind='math', proposed_html=proposals[int(match[1])], physical=True))
        else:
            sequence.append(dict(kind='text', text=token, physical=bool(re.search(r'[\w\d%‰°±−–—=+<>≤≥]', token))))
    if not any(p['kind'] == 'text' and p['physical'] for p in sequence):
        raise ValueError('Mixed-cell proposal has no prose word outside its formula')
    physical_count = sum(p['physical'] for p in sequence)
    with Image.open(BytesIO(data)) as source:
        image = source.convert('RGB')
    try:
        gray = np.asarray(image.convert('L'))
        threshold,ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        if not np.any(ink):
            raise ValueError('Mixed-cell source crop has no ink')
        _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        glyph_heights = [int(h) for _, _, w, h, _ in stats[1:] if w > 1 and h > 1]
        if not glyph_heights:
            raise ValueError('Mixed-cell source has no measurable source glyphs')
        glyph_height = median(glyph_heights)

        def anchored_partition():
            """Allocate complete source ink components around validated prose.

            Templates come only from geometrically unambiguous mixed cells
            in this same table. Their mathematical contents must pass the
            normal verifier before the caller can commit an HTML result.
            Different white boundary pixels owning the same source components
            represent one allocation, rather than several competing splits.
            """
            if not prose_templates:
                raise ValueError('Mixed-cell whitespace has no unique split and no source prose templates')
            _,labels,components,_=cv2.connectedComponentsWithStats(ink,8)
            text_indices=[i for i,a in enumerate(sequence) if a['physical'] and a['kind']=='text']
            matches={}
            for index in text_indices:
                word=sequence[index]['text'];owners={}
                for template in prose_templates.get(word,[]):
                    with Image.open(BytesIO(template['data'])) as original:
                        sample=np.asarray(original.convert('L'))
                    sample_mask=cv2.threshold(sample,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
                    ty,tx=np.where(sample_mask)
                    if not len(tx):continue
                    sample=sample[ty.min():ty.max()+1,tx.min():tx.max()+1]
                    sh,sw=sample.shape
                    if sh>gray.shape[0] or sw>gray.shape[1]:continue
                    sm=cv2.threshold(sample,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]>0
                    scores=cv2.matchTemplate(gray,sample,cv2.TM_CCOEFF_NORMED)
                    peaks=(scores>=.8)&(scores==cv2.dilate(scores,np.ones((3,3),np.uint8)))
                    for y,x in zip(*np.where(peaks)):
                        y,x=int(y),int(x)
                        patch=gray[y:y+sh,x:x+sw]
                        pm=cv2.threshold(patch,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]>0
                        if not np.any(pm):continue
                        # One raster pixel accounts for subpixel placement and
                        # antialiasing of the same source font, without making
                        # a different letter shape interchangeable.
                        near_source=cv2.dilate(pm.astype('uint8'),np.ones((3,3),np.uint8))>0
                        near_template=cv2.dilate(sm.astype('uint8'),np.ones((3,3),np.uint8))>0
                        cover_a=float(np.count_nonzero(sm&near_source)/np.count_nonzero(sm))
                        cover_b=float(np.count_nonzero(pm&near_template)/np.count_nonzero(pm))
                        if min(cover_a,cover_b)<.98:continue
                        ids=frozenset(int(v) for v in np.unique(labels[y:y+sh,x:x+sw][pm]) if v)
                        if not ids:continue
                        # Never steal a connected part of a neighbouring
                        # formula or a longer word outside this template.
                        if any(components[v,0]<x-1 or components[v,1]<y-1
                               or components[v,0]+components[v,2]>x+sw+1
                               or components[v,1]+components[v,3]>y+sh+1 for v in ids):continue
                        left=min(int(components[v,0]) for v in ids)
                        right=max(int(components[v,0]+components[v,2]) for v in ids)
                        candidate=dict(ids=ids,center=(left+right)/2,word=word,
                            template_sha256=template['source_sha256'],score=float(scores[y,x]),
                            symmetric_ink_coverage=[cover_a,cover_b])
                        if ids not in owners or candidate['score']>owners[ids]['score']:owners[ids]=candidate
                if not owners:raise ValueError('No complete source prose-template match for '+repr(word))
                matches[index]=sorted(owners.values(),key=lambda q:q['center'])
            solutions=[]
            def allocate(position,last_x,used,chosen):
                if len(solutions)>1:return
                if position==len(text_indices):solutions.append(dict(chosen));return
                index=text_indices[position]
                for candidate in matches[index]:
                    if candidate['center']<=last_x or candidate['ids']&used:continue
                    chosen[index]=candidate
                    allocate(position+1,candidate['center'],used|candidate['ids'],chosen)
                    chosen.pop(index,None)
            allocate(0,float('-inf'),frozenset(),{})
            if len(solutions)!=1:raise ValueError('Source prose templates have multiple ink-component allocations')
            chosen=solutions[0];assignment={}
            for index,candidate in chosen.items():
                for component in candidate['ids']:assignment[component]=index+1
            math_indices=[i for i,a in enumerate(sequence) if a['kind']=='math']
            ranges={}
            for index in math_indices:
                before=[chosen[i]['center'] for i in text_indices if i<index]
                after=[chosen[i]['center'] for i in text_indices if i>index]
                ranges[index]=(max(before,default=float('-inf')),min(after,default=float('inf')))
            for component in range(1,len(components)):
                if component in assignment:continue
                x,_,w,_,_=components[component];center=x+w/2
                owners=[i for i,(left,right) in ranges.items() if left<center<right]
                if len(owners)!=1:raise ValueError('A source ink component has no unique mathematical slot')
                assignment[component]=owners[0]+1
            if len(assignment)!=len(components)-1:
                raise ValueError('Mixed-cell component partition would drop source ink')
            lookup=np.zeros(len(components),dtype='int32')
            for component,owner in assignment.items():lookup[component]=owner
            core_owners=lookup[labels]
            _,nearest=cv2.distanceTransformWithLabels((ink==0).astype('uint8'),cv2.DIST_L2,5,
                                                      labelType=cv2.DIST_LABEL_CCOMP)
            owner_lookup=np.zeros(int(nearest.max())+1,dtype='int32')
            for component in np.unique(nearest[ink>0]):
                owners=np.unique(core_owners[(nearest==component)&(ink>0)])
                if len(owners)!=1:raise ValueError('Antialias pixels have ambiguous source-component ownership')
                owner_lookup[int(component)]=int(owners[0])
            all_owners=owner_lookup[nearest]
            visible=gray<(threshold+255)/2
            source_rgb=np.asarray(image)
            parts=[];sx=image.width/(source_bbox[2]-source_bbox[0]);sy=image.height/(source_bbox[3]-source_bbox[1])
            for index,atom in enumerate(sequence):
                if not atom['physical']:
                    parts.append({k:v for k,v in atom.items() if k!='physical'});continue
                owned=(all_owners==index+1)&visible
                ys,xs=np.where(owned)
                if not len(xs):raise ValueError('A proposed mixed-cell group owns no source ink')
                left,top,right,bottom=int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)
                pixels=source_rgb[top:bottom,left:right].copy()
                pixels[all_owners[top:bottom,left:right]!=index+1]=255
                stream=BytesIO();Image.fromarray(pixels).save(stream,'PNG');part_data=stream.getvalue()
                parts.append(dict(**{k:v for k,v in atom.items() if k!='physical'},data=part_data,
                    source_sha256=hashlib.sha256(part_data).hexdigest(),pixel_bbox=[left,top,right,bottom],
                    bbox=[source_bbox[0]+left/sx,source_bbox[1]+top/sy,source_bbox[0]+right/sx,source_bbox[1]+bottom/sy]))
            if np.any((ink>0)&(all_owners==0)):
                raise ValueError('Mixed-cell component partition leaves unowned source pixels')
            return dict(parts=parts,proof=dict(method='source_prose_template_component_partition',
                source_sha256=hashlib.sha256(data).hexdigest(),source_bbox=list(source_bbox),
                physical_groups=physical_count,source_ink_components=len(components)-1,
                source_ink_pixels=int(np.count_nonzero(ink)),assigned_source_ink_pixels=int(np.count_nonzero((ink>0)&(all_owners>0))),
                unassigned_source_ink_pixels=0,prose_templates=[{k:v for k,v in chosen[i].items() if k!='ids'} for i in text_indices]))

        def ink_runs(projection):
            starts = np.flatnonzero(projection & ~np.r_[False, projection[:-1]])
            ends = np.flatnonzero(projection & ~np.r_[projection[1:], False])+1
            return [(int(a), int(b)) for a, b in zip(starts, ends)]

        rows = ink_runs(np.any(ink, axis=1))
        if any(right[0]-left[1] >= glyph_height/2 for left, right in zip(rows, rows[1:])):
            raise ValueError('Mixed-cell source has separate text rows; a one-line split is not proved')
        prose_words={a['text'] for a in sequence if a['physical'] and a['kind']=='text'}
        if prose_templates and all(prose_templates.get(word) for word in prose_words):
            return anchored_partition()
        columns = ink_runs(np.any(ink, axis=0))
        gaps = [(right[0]-left[1], index, left[1], right[0])
                for index, (left, right) in enumerate(zip(columns, columns[1:]))]
        needed = physical_count-1
        if len(gaps) < needed:
            raise ValueError('Mixed-cell source has fewer white gaps than proposed word groups')
        ranked = sorted(gaps, reverse=True)
        selected = ranked[:needed]
        if len(ranked) > needed and selected[-1][0] <= ranked[needed][0]:
            if prose_templates:return anchored_partition()
            raise ValueError('Mixed-cell whitespace does not uniquely separate the proposed prose/math groups')
        selected.sort(key=lambda part: part[1])
        cuts = [round((left+right)/2) for _, _, left, right in selected]
        bounds = [columns[0][0], *cuts, columns[-1][1]]
        if len(bounds) != physical_count+1:
            raise ValueError('Mixed-cell partition does not cover every proposed group')
        top, bottom = rows[0][0], rows[-1][1]
        sx = image.width/(source_bbox[2]-source_bbox[0])
        sy = image.height/(source_bbox[3]-source_bbox[1])
        parts, group_index = [], 0
        for atom in sequence:
            if not atom['physical']:
                parts.append({key:value for key,value in atom.items() if key != 'physical'})
                continue
            left, right = bounds[group_index:group_index+2]
            group_index += 1
            if right <= left:
                raise ValueError('Mixed-cell source partition has an empty group')
            crop = image.crop((left, top, right, bottom))
            try:
                stream = BytesIO()
                crop.save(stream, 'PNG')
                part_data = stream.getvalue()
            finally:
                crop.close()
            parts.append(dict(**{key:value for key,value in atom.items() if key != 'physical'},
                data=part_data, source_sha256=hashlib.sha256(part_data).hexdigest(),
                pixel_bbox=[left, top, right, bottom],
                bbox=[source_bbox[0]+left/sx, source_bbox[1]+top/sy,
                      source_bbox[0]+right/sx, source_bbox[1]+bottom/sy]))
        return dict(parts=parts, proof=dict(method='unique_source_white_gap_partition',
            source_sha256=hashlib.sha256(data).hexdigest(), source_bbox=list(source_bbox),
            physical_groups=physical_count, source_glyph_height_pixels=glyph_height,
            selected_white_gaps_pixels=[gap[0] for gap in selected],
            largest_internal_gap_pixels=max((gap[0] for gap in ranked[needed:]), default=0)))
    finally:
        image.close()


def connected_scan_graphics(ink, sx, sy, font):
    """Find connected drawings even when old OCR boxes cover all their ink.

Require an extended component with two long, separated horizontal strokes.
Ordinary letters, isolated table rules and fraction bars cannot establish
this evidence by themselves. Coordinates returned are local raster pixels.
"""
    import cv2

    # A bogus OCR word spanning a drawing can have a 20-30 pt font box.
    # Bound this hint by the actual cell height; typography is not evidence.
    font = min(font, max(4, ink.shape[0]/sy*.3))
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    found = []
    for x, y, w, h, area in stats[1:]:
        if (w / sx < max(36, 6 * font) or h / sy < max(6, font)
                or w / sx < 2.5 * h / sy or area / (sx * sy) < 15):
            continue
        component = ink[y:y+h, x:x+w]
        lines = cv2.HoughLinesP(component, 1, math.pi / 180, max(12, w // 8),
                                minLineLength=w * .55, maxLineGap=max(2, round(sx)))
        levels = []
        for a, b, c, d in (() if lines is None else lines[:, 0]):
            if abs(d-b) <= max(2, abs(c-a) * .025):
                levels.append((b+d) / 2)
        if levels and max(levels)-min(levels) >= h * .4:
            found.append((int(x), int(y), int(w), int(h)))
    return found


def positioned_word_runs(glyphs):
    """Keep source spelling and glyph membership, including flattened scripts."""
    lines = []
    for char in sorted(glyphs, key=lambda c: (c['top'], c['x0'])):
        if not char['text'] or len(char['text']) != 1:
            continue
        line = next((row for row in reversed(lines)
                     if abs(median(c['bottom'] for c in row)-char['bottom'])
                     <= .5 * max(char['size'], median(c['size'] for c in row))), None)
        if line is None:
            lines.append([char])
        else:
            line.append(char)
    runs = []
    lines.sort(key=lambda row: median(c['bottom'] for c in row))
    centers_y = [median((c['top']+c['bottom'])/2 for c in row) for row in lines]
    for line_index, line in enumerate(lines):
        line.sort(key=lambda c: c['x0'])
        group = []
        def finish():
            if not group:
                return
            text = ''.join(c['text'] for c in group)
            # This is only a candidate gate, never proof of an index. It also
            # covers units, Greek variables and mixed-alphabet source tokens.
            if (2 <= len(text) <= 24 and any(c.isalpha() for c in text)
                    and re.fullmatch(r'[^\W_]+', text)):
                limits = [(centers_y[line_index-1]+centers_y[line_index])/2 if line_index else None,
                          (centers_y[line_index+1]+centers_y[line_index])/2 if line_index+1<len(lines) else None]
                runs.append(dict(text=text, glyphs=list(group), line=line, vertical_limits=limits))
            group.clear()
        for char in line:
            if (not char['text'].isalnum() or (group and
                    (char['x0']-group[-1]['x1'] > .45 * char['size']
                     or (all(c['text'].isdigit() for c in group) and char['text'].isalpha())))):
                finish()
            if char['text'].isalnum():
                group.append(char)
        finish()
    return runs


def source_script_evidence(mask, image_region, cell_bbox, run, sx, sy):
    """Prove smaller raised/lowered ink, independently of the old font boxes.

    Components are assigned by source glyph centers so an adjacent bracket or
    line does not enter the expression crop. Detached dots join their stems.
    The returned bbox is measured source ink in PDF points, not guessed glyphs.
    """
    import cv2

    glyphs = run['glyphs']
    font = median(c['size'] for c in glyphs)
    box = [max(cell_bbox[0]+1, min(c['x0'] for c in glyphs)-font*.6),
           max(cell_bbox[1]+1, min(c['top'] for c in glyphs)-font*.6),
           min(cell_bbox[2]-1, max(c['x1'] for c in glyphs)+font*.6),
           min(cell_bbox[3]-1, max(c['bottom'] for c in glyphs)+font*.6)]
    upper, lower = run.get('vertical_limits', [None, None])
    if upper is not None:
        box[1] = max(box[1], upper-font*.15)
    if lower is not None:
        box[3] = min(box[3], lower+font*.15)
    x0, y0 = max(0, math.floor((box[0]-image_region[0])*sx)), max(0, math.floor((box[1]-image_region[1])*sy))
    x1, y1 = min(mask.shape[1], math.ceil((box[2]-image_region[0])*sx)), min(mask.shape[0], math.ceil((box[3]-image_region[1])*sy))
    if x1 <= x0 or y1 <= y0:
        return None
    first, last = glyphs[0], glyphs[-1]
    centers = lambda c: (c['x0']+c['x1'])/2
    others = [c for c in run['line'] if id(c) not in {id(g) for g in glyphs} and c['text'].strip()]
    left = max((centers(c) for c in others if centers(c) < centers(first)), default=None)
    right = min((centers(c) for c in others if centers(c) > centers(last)), default=None)
    left = (left+centers(first))/2 if left is not None else box[0]
    right = (right+centers(last))/2 if right is not None else box[2]
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask[y0:y1, x0:x1], 8)
    parts = []
    for x, y, w, h, area in stats[1:]:
        center = image_region[0]+(x0+x+w/2)/sx
        if area/(sx*sy) < .12 or not left <= center <= right:
            continue
        # A clipped neighboring row/rule is not evidence for script placement.
        if y == 0 or y+h >= y1-y0 or w/sx > font*3:
            continue
        parts.append([image_region[0]+(x0+x)/sx, image_region[1]+(y0+y)/sy,
                      image_region[0]+(x0+x+w)/sx, image_region[1]+(y0+y+h)/sy])
    # Join only vertically separated components sharing their horizontal ink
    # (i/j dots, accents). Adjacent letters are kept as separate observations.
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(parts):
            for j, b in enumerate(parts[i+1:], i+1):
                overlap = min(a[2], b[2])-max(a[0], b[0])
                if (overlap >= .5 * min(a[2]-a[0], b[2]-b[0])
                        and min(a[3], b[3]) <= max(a[1], b[1])
                        and max(a[1], b[1])-min(a[3], b[3]) < font*.45):
                    parts[i] = [min(a[0],b[0]), min(a[1],b[1]), max(a[2],b[2]), max(a[3],b[3])]
                    parts.pop(j)
                    merged = True
                    break
            if merged:
                break
    parts = sorted(p for p in parts if p[3]-p[1] >= font*.25)
    if len(parts) < 2:
        return None
    base = parts[0]
    height = base[3]-base[1]
    if height < font*.35:
        return None
    kinds = set()
    for part in parts[1:]:
        if part[0] < base[2]-height*.25 or part[3]-part[1] > height*.9:
            continue
        if part[3] > base[3]+height*.15 and part[1] > base[1]+height*.22:
            kinds.add('sub')
        if part[1] < base[1]-height*.15 and part[3] < base[3]-height*.22:
            kinds.add('sup')
    if not kinds:
        return None
    return dict(bbox=[max(cell_bbox[0]+1, min(p[0] for p in parts)-.6),
                      max(cell_bbox[1]+1, min(p[1] for p in parts)-.6),
                      min(cell_bbox[2]-1, max(p[2] for p in parts)+.6),
                      min(cell_bbox[3]-1, max(p[3] for p in parts)+.6)],
                scripts=sorted(kinds), basis='smaller_displaced_source_ink',
                ink_components=parts)


def upright_script_labels(math_node):
    """Keep a recognized operator-name index as an upright identifier label.

    For example, OCR may spell a printed upright label using a LaTeX operator
    macro. This changes markup/typography only; no characters are inferred.
    """
    for script in math_node.find_all(['msub','msup','msubsup']):
        children=script.find_all(recursive=False)
        for index in children[1:]:
            operators=([index] if index.name=='mo' else index.find_all('mo'))
            for operator in operators:
                value=operator.get_text()
                if len(value)>1 and value.isalpha():
                    operator.name='mi'
                    operator.attrs={'mathvariant':'normal'}
                    latex=math_node.get('data-latex','')
                    math_node['data-latex']=re.sub(r'([_^]\s*\{?\s*)\\'+re.escape(value)+r'\b',
                        lambda m:m[1]+r'\mathrm{'+value+'}',latex)


def restore_scan_table_scripts(fragment, geometry, source_image, region, glyphs,
                               recognizer, image_html, diagnostic=None, page_no=None):
    """Repair only source-proven tokens in matching cells of a verified grid.

    Existing MathML, prose, numbers and cell spans are retained. Recognition
    failure or budget exhaustion preserves the exact token as a source image.
    The caller owns the recognizer and its request/time budgets.
    """
    import cv2
    import numpy as np
    from bs4 import BeautifulSoup, NavigableString

    diagnostic = diagnostic or (lambda *args, **fields: None)
    cells = (geometry or {}).get('cells', [])
    if not cells:
        return fragment, []
    soup = BeautifulSoup(fragment, 'html.parser')
    table = soup.find('table')
    if table is None:
        return fragment, []
    # Walk the logical HTML grid; never bind by a guessed flat cell index.
    slots, nodes = {}, {}
    rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
    for y, row in enumerate(rows):
        x = 0
        for node in row.find_all(['td', 'th'], recursive=False):
            while (y,x) in slots:
                x += 1
            rs, cs = int(node.get('rowspan', 1)), int(node.get('colspan', 1))
            nodes[y,x,rs,cs] = node
            for yy in range(y,y+rs):
                for xx in range(x,x+cs):
                    slots[yy,xx] = True
            x += cs
    sx, sy = source_image.width/(region[2]-region[0]), source_image.height/(region[3]-region[1])
    mask = cv2.threshold(np.asarray(source_image.convert('L')), 0, 255,
                         cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    repairs = []
    for cell in sorted(cells, key=lambda c: (c['row'], c['column'])):
        if cell.get('primary_ocr'):
            continue
        node = nodes.get(tuple(cell[k] for k in ('row','column','row_span','column_span')))
        if node is None or node.find('img'):
            continue
        box = cell['bbox']
        local = [c for c in glyphs if box[0] <= (c['x0']+c['x1'])/2 < box[2]
                 and box[1] <= (c['top']+c['bottom'])/2 < box[3]]
        for run in positioned_word_runs(local):
            # A coefficient may touch the variable in old OCR (0,55dH).
            # Preserve that coefficient outside the expression crop.
            pattern = re.compile(r'(?<![^\W\d])'+re.escape(run['text'])+r'(?!\w)')
            target = next(((n, match) for n in node.find_all(string=True)
                           if isinstance(n, NavigableString) and not n.find_parent(['math','sub','sup'])
                           and (match := pattern.search(str(n)))), None)
            if target is None:
                continue
            evidence = source_script_evidence(mask, region, box, run, sx, sy)
            if evidence is None:
                continue
            b = evidence['bbox']
            pixels = (max(0,math.floor((b[0]-region[0])*sx)), max(0,math.floor((b[1]-region[1])*sy)),
                      min(source_image.width,math.ceil((b[2]-region[0])*sx)), min(source_image.height,math.ceil((b[3]-region[1])*sy)))
            asset = source_image.crop(pixels)
            rendered, reason = None, 'recognizer_unavailable_or_budget'
            fields = dict(page=page_no, row=cell['row'], column=cell['column'],
                          source_text=run['text'], **evidence)
            try:
                if recognizer is not None:
                    stream = BytesIO()
                    asset.save(stream, 'PNG')
                    diagnostic('SCAN_SCRIPT_MATH_REQUEST', **fields)
                    candidate = recognizer(stream.getvalue(), dict(formula_item=True, inline=True,
                        width=asset.width, height=asset.height, source_script_kind=evidence['scripts'],
                        media_name=f'PDF page {page_no}, table symbol at {list(b)}'))
                    parsed = BeautifulSoup(candidate or '', 'html.parser')
                    math_node = parsed.find('math')
                    # Do not accept a flattened reading, extra punctuation,
                    # duplicated roots or altered source digits as a repair.
                    value = math_node.get_text('',strip=True) if math_node else ''
                    valid = (math_node is not None and len(parsed.find_all('math')) == 1
                             and re.fullmatch(r'[^\W_]+', value)
                             and len(value) <= max(len(run['text'])+1, len(run['text'])*1.5)
                             and re.findall(r'\d+', value) == re.findall(r'\d+', run['text'])
                             and all(math_node.find(['msub','msubsup','mmultiscripts'] if kind == 'sub'
                                                   else ['msup','msubsup','mmultiscripts'])
                                     for kind in evidence['scripts']))
                    if valid:
                        math_node['display'] = 'inline'
                        upright_script_labels(math_node)
                        rendered, reason = str(math_node), 'source_script_regional_ocr'
                    else:
                        reason = 'ocr_did_not_preserve_source_script'
            except Exception as exc:
                reason = 'regional_ocr_failed: '+str(exc)[:300]
            if rendered is None:
                image = BeautifulSoup(image_html(asset, b[2]-b[0], 'formula'), 'html.parser').img
                image['data-source-cell'] = 'true'
                image['alt'] = 'Обозначение с индексом в исходнике'
                rendered = str(image)
            asset.close()
            old, match = target
            text = str(old)
            old.replace_with(BeautifulSoup(escape(text[:match.start()])+rendered+escape(text[match.end():]), 'html.parser'))
            repair = dict(**fields, method='mathml' if '<math' in rendered else 'source_image', reason=reason)
            repairs.append(repair)
            diagnostic('SCAN_SCRIPT_MATH_RESTORED', **repair)
    return (str(table), repairs) if repairs else (fragment, [])
