"""Read low-resolution indexed atoms with complete source-pixel ownership.

Only a flat single-baseline equation with independently separated lower-index
components is eligible. Every indexed atom AND the complete remaining equation
must match the original Surya structure in both CodeFormula readings. No result
is repaired by changing characters, values, alphabets or mathematical structure.
"""
import hashlib
from io import BytesIO
import math
from statistics import median
import re

from bs4 import BeautifulSoup
import cv2
import numpy as np
from PIL import Image,ImageChops,ImageOps


def _png_view(image,scale=1):
    paper=Image.new('RGB',image.size,'white')
    bounds=ImageChops.difference(image,paper).getbbox();paper.close()
    if bounds is None:return None
    crop=image.crop(bounds)
    padded=ImageOps.expand(crop,border=12,fill='white');crop.close()
    if scale!=1:
        enlarged=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
        padded.close();padded=enlarged
    stream=BytesIO();padded.save(stream,'PNG');padded.close()
    return stream.getvalue()


def verify_indexed_atoms(rendered,metadata,recognizer,device,readings,signature,latex_math):
    soup=BeautifulSoup(rendered,'html.parser')
    root=soup.math
    if root is None or root.find(['msup','msubsup','mfrac','mroot','msqrt','mtable','mover','munder','munderover']):
        return None
    nodes=root.find_all('msub')
    if not nodes:return None
    expected=[]
    for node in nodes:
        children=node.find_all(True,recursive=False)
        if (len(children)!=2 or children[0].name!='mi' or len(children[0].get_text())!=1
                or not children[0].get_text().isalpha() or children[1].find(['msub','msup','mfrac'])
                or not re.fullmatch('[A-Za-zΑ-Ωα-ω0-9,]+',children[1].get_text('',strip=True))):
            return None
        expected.append(signature('<math>'+str(node)+'</math>'))
    for node in nodes:
        node.replace_with(node.find(True,recursive=False).extract())
    body_signature=signature(str(root))
    from docling_pdf_formula_retry import native_image
    image=native_image(recognizer,metadata)
    if image is None:return None
    try:
        gray=np.asarray(image.convert('L'))
        threshold,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        # In a tiny native image a thin serif can consist only of gray
        # antialiasing pixels. Use the contrast midpoint toward actual paper
        # for connected components, rather than discarding those strokes.
        paper=float(np.percentile(gray,95))
        mask=(gray < (threshold+paper)/2).astype('uint8')*255
        count,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
        parts=[dict(id=i,x=int(x),y=int(y),w=int(w),h=int(h),area=int(area))
               for i,(x,y,w,h,area) in enumerate(stats) if i and area>1]
        if not parts:return None
        height=max(p['h'] for p in parts)
        baseline=median(p['y']+p['h'] for p in parts if p['h']>=.65*height)
        lower=[p for p in parts if p['y']+p['h']>baseline+.15*height
               and p['h']>=.35*height and p['y']>=baseline-.6*height]
        lower.sort(key=lambda p:p['x'])
        groups=[]
        for part in lower:
            if groups and part['x']-max(p['x']+p['w'] for p in groups[-1])<=.7*height:
                groups[-1].append(part)
            else:groups.append([part])
        if len(groups)!=len(expected):return None
        lower_ids={p['id'] for p in lower}
        allocations=[]
        for group in groups:
            core_left=min(p['x'] for p in group)
            # Connected components locate dark cores. Keep their one-pixel
            # antialiasing fringe too; otherwise dots/serifs can remain in the
            # body and be hallucinated as a second mathematical index.
            box=[max(0,min(p['x'] for p in group)-1),max(0,min(p['y'] for p in group)-1),
                 min(image.width,max(p['x']+p['w'] for p in group)+1),
                 min(image.height,max(p['y']+p['h'] for p in group)+1)]
            bases=[p for p in parts if p['id'] not in lower_ids and p['h']>=.65*height
                   and abs(p['y']+p['h']-baseline)<=.15*height
                   and p['x']+p['w']<=core_left+.05*height]
            if not bases:return None
            base=max(bases,key=lambda p:p['x']+p['w'])
            if np.any(labels[box[1]:box[3],box[0]:box[2]]==base['id']):return None
            atom=[min(base['x'],box[0]),min(base['y'],box[1]),max(base['x']+base['w'],box[2]),
                  max(base['y']+base['h'],box[3])]
            # Another full-height body glyph inside the atom disproves its
            # proposed allocation, even if OCR could ignore it as stray ink.
            allowed={base['id'],*(p['id'] for p in group)}
            if any(p['id'] not in allowed and p['h']>=.65*height
                   and min(p['x']+p['w'],atom[2])>max(p['x'],atom[0])
                   and min(p['y']+p['h'],atom[3])>max(p['y'],atom[1]) for p in parts):
                return None
            allocations.append(dict(index_bbox=box,atom_bbox=atom,base_component=base['id'],
                                    index_components=[p['id'] for p in group]))
        # Rectangular pieces are disjoint. All remaining source pixels stay
        # in the body view; nothing is erased from the union of the views.
        assigned=np.zeros(mask.shape,dtype=bool)
        body=image.copy()
        for item in allocations:
            l,t,r,b=item['index_bbox']
            if np.any(assigned[t:b,l:r]):body.close();return None
            assigned[t:b,l:r]=True;body.paste('white',(l,t,r,b))
        body_raw=None
        for scale in (1,2):
            data=_png_view(body,scale)
            if data is None:continue
            raw=readings(data,device)
            candidates=[latex_math(value,'inline') for value in raw]
            if len(candidates)==2 and all(c and signature(c)==body_signature for c in candidates):
                body_raw=raw;break
        body.close()
        if body_raw is None:return None
        atoms=[]
        for expected_atom,item in zip(expected,allocations):
            atom=image.crop(item['atom_bbox'])
            scale=max(1,min(4,math.ceil(56/max(1,atom.height))))
            data=_png_view(atom,scale);atom.close()
            raw=readings(data,device)
            candidates=[latex_math(value,'inline') for value in raw]
            if len(candidates)!=2 or not all(c and signature(c)==expected_atom for c in candidates):
                return None
            atoms.append(dict(**item,scale=scale,codeformula=list(raw)))
        stream=BytesIO();image.save(stream,'PNG')
        return dict(method='source_separated_indexed_atoms_and_complete_body',
            native_asset_sha256=hashlib.sha256(stream.getvalue()).hexdigest(),source_size=list(image.size),
            baseline=baseline,body_codeformula=list(body_raw),atoms=atoms,
            source_ink_pixels=int(np.count_nonzero(mask)),
            index_ink_pixels=int(np.count_nonzero(mask[assigned])),
            body_ink_pixels=int(np.count_nonzero(mask[~assigned])),unassigned_ink_pixels=0)
    finally:
        image.close()
