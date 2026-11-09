"""Remove rasterized underline decorations only with retained glyph evidence."""
import base64
from io import BytesIO
import re
from bs4 import BeautifulSoup


def is_snapped_cell_border(obj, cells, *, x_tolerance, y_tolerance):
    """Identify a filled edge strip displaced by the table finder's snapping.

    Matching an axis alone could erase a short symbol near a border. The
    strip must also span one complete physical cell side, with only its own
    thickness allowed at either endpoint. Raster pictures are never rules.
    """
    if obj.get('object_type') != 'rect' or not obj.get('fill'):
        return False
    left, top, right, bottom = (obj[k] for k in ('x0', 'top', 'x1', 'bottom'))
    width, height = right-left, bottom-top
    if 0 < width <= 2 and height >= 4*width:
        margin = min(y_tolerance, max(1, width))
        return any(abs((left+right)/2-x) <= x_tolerance
                   and abs(top-cell[1]) <= margin and abs(bottom-cell[3]) <= margin
                   for cell in cells for x in (cell[0], cell[2]))
    if 0 < height <= 2 and width >= 4*height:
        margin = min(x_tolerance, max(1, height))
        return any(abs((top+bottom)/2-y) <= y_tolerance
                   and abs(left-cell[0]) <= margin and abs(right-cell[2]) <= margin
                   for cell in cells for y in (cell[1], cell[3]))
    return False


def remove_source_underlines(fragment,geometry,page,manifest,api):
    if not geometry:return fragment,[]
    from PIL import Image
    import cv2
    import numpy as np
    soup=BeautifulSoup(fragment,'html.parser');table=soup.table
    if table is None:return fragment,[]
    profile=api.table_profile(str(table));nodes=table.find_all(['td','th'])
    physical={(c['row'],c['column']):c for c in geometry.get('cells',[])}
    entries=list(geometry.get('retained_cell_images',[]));removed=set();proofs=[]
    key=lambda text:re.sub(r'\s+','',text)
    for node,position in zip(nodes,profile['cells']):
        prose=BeautifulSoup(node.decode_contents(),'html.parser')
        for math_node in prose.find_all('math'):math_node.decompose()
        plain=prose.get_text(' ',strip=True)
        if len(re.findall('[А-Яа-яЁё]{3,}',plain))<2:continue
        source=physical.get((position['row'],position['column']),{})
        if source.get('source_text_unreliable'):continue
        images=node.find_all('img');local=[e for e in entries if (e.get('row'),e.get('column'))==(position['row'],position['column'])]
        if len(images)!=len(local):continue
        for image,entry in zip(images,local):
            if not image.get('src','').startswith('data:image/'):continue
            box=entry['bbox'];thickness=box[3]-box[1]
            if thickness<=0 or thickness>2 or box[2]-box[0]<6*thickness:continue
            data=base64.b64decode(image['src'].split(',',1)[1])
            with Image.open(BytesIO(data)) as original:gray=np.asarray(original.convert('L'))
            mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
            yy,xx=np.where(mask>0)
            if len(xx):
                # A neighbouring vertical table border can touch an
                # underline crop. Only edge-connected border pixels may
                # accompany the complete horizontal stroke.
                rows=np.flatnonzero(np.count_nonzero(mask,axis=1)>=.8*gray.shape[1])
                if not len(rows) or rows[-1]-rows[0]+1>max(2,gray.shape[0]*.6):continue
                residual=mask.copy();residual[rows[0]:rows[-1]+1]=0
                edge=max(1,round(gray.shape[0]*.35))
                if np.any(residual[:,edge:-edge]):continue
            y=(box[1]+box[3])/2;glyphs=[]
            for g in source.get('glyphs',[]):
                b=g.get('bbox') or [g.get('x0'),g.get('top'),g.get('x1'),g.get('bottom')]
                if any(v is None for v in b):continue
                height=b[3]-b[1]
                if height>0 and box[0]-.5<=(b[0]+b[2])/2<=box[2]+.5 and b[1]+.65*height<=y<=b[3]+.25*height:
                    glyphs.append((b[0],g.get('text',''),b))
            glyphs.sort();text=''.join(g[1] for g in glyphs).strip()
            if not text or not re.search('[A-Za-zА-Яа-яЁё]',text):continue
            if key(text) not in key(plain):continue
            if len(key(text))<2:
                # A wrapped link can underline only its first letter. Its
                # PDF link annotation provides independent ownership proof.
                links=geometry.get('source_link_rects',[])
                if not any(link[0]-.5<=box[0] and box[2]<=link[2]+.5 and
                           link[1]-.5<=y<=link[3]+.5 for link in links):continue
            if not glyphs or min(g[2][0] for g in glyphs)>box[0]+thickness or max(g[2][2] for g in glyphs)<box[2]-thickness:continue
            proof=dict(page=page,row=position['row'],column=position['column'],bbox=box,
                method='flat_native_underline_beneath_retained_source_glyphs',retained_text=text,
                source_glyphs=[dict(text=g[1],bbox=g[2]) for g in glyphs],ink_pixels=int(len(xx)))
            removed.add(id(entry));image.decompose();proofs.append(proof)
            api.diagnostic_event('TABLE_SOURCE_UNDERLINE_RASTER_REMOVED',**proof)
    if removed:geometry['retained_cell_images']=[e for e in entries if id(e) not in removed]
    manifest.setdefault('source_underline_decorations_removed',[]).extend(proofs)
    return str(table),proofs


def remove_source_rule_fragments(fragment,geometry,page,manifest,api):
    """Remove only long flat raster strips on a physical cell boundary.

    Horizontal strokes are deliberately excluded: a tiny PNG can be the
    fraction bar between native numerator and denominator glyphs. Interior
    vertical strokes can be missing dividers and are not decorations either.
    """
    if not geometry:return fragment,[]
    import hashlib
    import cv2
    import numpy as np
    from PIL import Image
    entries=list(geometry.get('retained_cell_images',[]));originals={}
    for cell in geometry.get('cells',[]):
        local=[e for e in entries if (e.get('row'),e.get('column'))==(cell['row'],cell['column'])]
        images=[image for line in cell.get('lines',[]) for image in BeautifulSoup(line.get('html',''),'html.parser').find_all('img')]
        if len(images)!=len(local):continue
        for image,entry in zip(images,local):
            src=image.get('src','')
            if not src.startswith('data:image/'):continue
            digest=hashlib.sha256(base64.b64decode(src.split(',',1)[1])).hexdigest()
            originals.setdefault(digest,[]).append((cell,entry))
    soup=BeautifulSoup(fragment,'html.parser');table=soup.table
    if table is None:return fragment,[]
    removed=set();proofs=[]
    for image in list(table.find_all('img')):
        node=image.find_parent(['td','th'])
        if node is None or node.find('math'):continue
        src=image.get('src','')
        if not src.startswith('data:image/'):continue
        data=base64.b64decode(src.split(',',1)[1]);digest=hashlib.sha256(data).hexdigest()
        for source,entry in originals.get(digest,[]):
            box=entry.get('bbox');cell_box=source.get('bbox')
            if not box or not cell_box:continue
            width=box[2]-box[0];height=box[3]-box[1]
            if width<=0 or width>2.5 or height<12*width:continue
            tolerance=max(.5,1.75*width)
            edge=min((cell_box[0],cell_box[2]),key=lambda x:abs(x-(box[0]+box[2])/2))
            if abs(edge-(box[0]+box[2])/2)>tolerance:continue
            if (abs(box[1]-cell_box[1])>tolerance or abs(box[3]-cell_box[3])>tolerance
                    or height<.85*(cell_box[3]-cell_box[1])):continue
            glyph_hit=False
            for glyph in source.get('glyphs',[]):
                b=glyph.get('bbox') or [glyph.get('x0'),glyph.get('top'),glyph.get('x1'),glyph.get('bottom')]
                if not glyph.get('text','').strip() or any(v is None for v in b):continue
                if box[0]-.25<=(b[0]+b[2])/2<=box[2]+.25 and box[1]<=(b[1]+b[3])/2<=box[3]:
                    glyph_hit=True;break
            if glyph_hit:continue
            with Image.open(BytesIO(data)) as original:gray=np.asarray(original.convert('L'))
            ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
            yy,xx=np.where(ink>0)
            if not len(xx):continue
            h=int(yy.max()-yy.min()+1)
            rows=[]
            for y in range(int(yy.min()),int(yy.max())+1):
                xs=np.flatnonzero(ink[y])
                if len(xs):rows.append((int(xs[-1]-xs[0]+1),float((xs[0]+xs[-1])/2)))
            # Antialiased line end caps can span the entire tiny PNG width.
            # Measure its persistent vertical stroke instead of treating
            # those cap pixels as the stroke's body thickness.
            thickness=float(np.median([p[0] for p in rows]));centres=np.asarray([p[1] for p in rows])
            if (h<12*thickness or h<.8*gray.shape[0] or len(rows)<.9*h
                    or float(np.percentile(np.abs(centres-np.median(centres)),95))>1
                    or len(xx)<.8*thickness*h):continue
            proof=dict(page=page,row=source['row'],column=source['column'],bbox=box,source_cell_bbox=cell_box,
                source_image_sha256=digest,source_grid_boundary=edge,ink_pixels=int(len(xx)),
                method='flat_vertical_source_rule_spans_physical_cell_boundary')
            image.decompose();removed.add(id(entry));proofs.append(proof)
            api.diagnostic_event('TABLE_SOURCE_GRID_RULE_RASTER_REMOVED',**proof);break
    if removed:geometry['retained_cell_images']=[e for e in entries if id(e) not in removed]
    manifest.setdefault('source_grid_rule_fragments_removed',[]).extend(proofs)
    return str(table),proofs
