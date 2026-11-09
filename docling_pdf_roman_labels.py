"""Source-geometry recognition of consecutive Roman table designations.

An upright I in a category label is visually identical to a vertical bar.
Do not ask an algebra reader to guess its meaning in isolation: an actual
source row of consecutive I/II/III/IV/... designations proves the numeral.
Their entire lower indices still pass independent mathematical OCR.
"""
from io import BytesIO
from statistics import median

import cv2
import numpy as np
from PIL import Image
from bs4 import BeautifulSoup
import re


def _roman_glyph(mask):
    h,w=mask.shape
    if h<4:
        return None
    rows=[np.where(row)[0] for row in mask]
    if any(not len(xs) for xs in rows):
        return None
    spans=np.array([xs[-1]-xs[0]+1 for xs in rows])
    centers=np.array([(xs[0]+xs[-1])/2 for xs in rows])
    edge=max(1,h//5)
    top,bottom=float(np.mean(spans[:edge])),float(np.mean(spans[-edge:]))
    middle=float(np.mean(spans[h//3:2*h//3]))
    if w/h <= .28 and max(centers)-min(centers) <= max(1,w*.5):
        # Reject a one-sided digit-1 flag or a T/L with unequal end widths.
        if abs(top-bottom)<=max(1,.3*w) and min(spans)>=.45*w:
            return 'I'
    if .35 <= w/h <= 1.3 and np.max(abs(centers-w/2)) <= .2*w:
        if top>=.65*w and bottom<=.4*w and top>=2*bottom and middle<top:
            return 'V'
        if top>=.65*w and bottom>=.65*w and middle<=.6*min(top,bottom):
            return 'X'
    return None


def split_roman_label(data):
    """Allocate every source foreground component to numeral or lower index."""
    with Image.open(BytesIO(data)) as original:
        image=original.convert('RGB')
    gray=np.asarray(image.convert('L'))
    mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
    parts=[tuple(map(int,s)) for s in stats[1:] if s[4]>=1]
    if len(parts)<2:
        image.close();return None
    height=max(p[3] for p in parts)
    top=min(p[1] for p in parts if p[3]>=.75*height)
    main=sorted((p for p in parts if p[3]>=.75*height and abs(p[1]-top)<=.15*height),key=lambda p:p[0])
    sub=[p for p in parts if p not in main]
    if not main or not sub:
        image.close();return None
    baseline=median(p[1]+p[3] for p in main)
    right=max(p[0]+p[2] for p in main)
    if any(p[0]<right-1 or p[1]<top+.3*height or p[1]+p[3]<baseline+.05*height for p in sub):
        image.close();return None
    letters=[]
    for x,y,w,h,_ in main:
        letter=_roman_glyph(mask[y:y+h,x:x+w]>0)
        if not letter:
            image.close();return None
        letters.append(letter)
    word=''.join(letters)
    # Canonical subtractive spelling, without an arbitrary document budget.
    value=sum(({'I':1,'V':5,'X':10}[c] * (-1 if i+1<len(word)
        and {'I':1,'V':5,'X':10}[c]<{'I':1,'V':5,'X':10}[word[i+1]] else 1)) for i,c in enumerate(word))
    n=value;canonical=''
    for amount,text in ((10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')):
        times,n=divmod(n,amount);canonical+=text*times
    if canonical!=word:
        image.close();return None
    box=[max(0,min(p[0] for p in sub)-1),max(0,min(p[1] for p in sub)-1),
         min(image.width,max(p[0]+p[2] for p in sub)+1),min(image.height,max(p[1]+p[3] for p in sub)+1)]
    crop=image.crop(box);stream=BytesIO();crop.save(stream,'PNG');crop.close()
    proof=dict(numeral=word,value=value,numeral_components=[list(p) for p in main],
        subscript_components=[list(p) for p in sub],subscript_pixel_bbox=box,
        source_size=list(image.size),allocated_foreground_components=len(parts))
    image.close()
    return dict(data=stream.getvalue(),proof=proof)


def reference_roman_rows(profiles,columns):
    """Keep full primary-OCR rows of flat indexed Roman designations."""
    result=[]
    for reference,profile in enumerate(profiles):
        groups={}
        for cell in profile['cells']:
            groups.setdefault(cell['row'],[]).append(cell)
        for row,cells in sorted(groups.items()):
            visible=[c for c in cells if c['text'].strip() or '<img' in c['html']]
            # A bad merged header can make span repair pad an otherwise
            # complete body row with an empty comparison cell. It carries no
            # source content and cannot change the actual designation columns.
            if not visible or max(c['column']+c['column_span'] for c in visible)!=columns:
                continue
            labels=[]
            for cell in cells:
                if '<math' not in cell['html']:
                    continue
                soup=BeautifulSoup(cell['html'],'html.parser')
                maths=soup.find_all('math')
                if len(maths)!=1 or cell['column_span']!=1 or cell['row_span']!=1:
                    continue
                node=maths[0]
                scripts=node.find_all('msub')
                if len(scripts)!=1 or node.find(['msup','msubsup','mfrac','mtable','mover','munder','msqrt','mroot']):
                    continue
                children=scripts[0].find_all(True,recursive=False)
                if len(children)!=2 or children[1].find(['msub','msup','msubsup','mfrac','mtable']):
                    continue
                index=children[1].get_text('',strip=True)
                if not re.fullmatch(r'[A-Za-zА-Яа-яЁё0-9,]+',index):
                    continue
                scripts[0].replace_with(children[0].extract())
                word=node.get_text('',strip=True)
                if not re.fullmatch('[IVX]+',word):
                    continue
                node.extract()
                if soup.get_text(strip=True):
                    continue
                labels.append(dict(column=cell['column'],numeral=word,index=index))
            if len(labels)>=3:
                result.append(dict(reference=reference,row=row,labels=sorted(labels,key=lambda c:c['column'])))
    return result


def roman_row_plans(asset_pairs,reference_rows=()):
    """Require an actual contiguous row with consecutive Roman values."""
    import base64
    candidates={}
    for node,entry in asset_pairs:
        if entry.get('basis')!='nonempty_scan_cell_without_positioned_text':
            continue
        raw=base64.b64decode(node['src'].split(',',1)[1],validate=True)
        label=split_roman_label(raw)
        if label:
            candidates.setdefault(entry['row'],[]).append((node,entry,label))
    plans={}
    for row,group in candidates.items():
        group.sort(key=lambda value:value[1]['column'])
        if len(group)<3 or not any(set(p[2]['proof']['numeral']) & set('VX') for p in group):
            continue
        if any(b[1]['column']!=a[1]['column']+1 or b[2]['proof']['value']!=a[2]['proof']['value']+1
               for a,b in zip(group,group[1:])):
            continue
        peers=[dict(column=e['column'],bbox=list(map(float,e['bbox'])),numeral=p['proof']['numeral'])
               for _,e,p in group]
        for node,entry,label in group:
            label['proof'].update(method='source_consecutive_roman_designations',row=row,source_peers=peers)
            plans[id(node)]=label
    if plans:
        source_rows={}
        for node,entry in asset_pairs:
            if id(node) in plans:
                source_rows.setdefault(entry['row'],[]).append((entry,plans[id(node)]))
        source_rows=[sorted(group,key=lambda x:x[0]['column']) for _,group in sorted(source_rows.items())]
        matches={}
        for reference in sorted({r['reference'] for r in reference_rows}):
            rows=sorted((r for r in reference_rows if r['reference']==reference),key=lambda r:r['row'])
            if len(rows)!=len(source_rows):continue
            allocations=[]
            for source_row,ref in zip(source_rows,rows):
                actual=[(e['column'],p['proof']['numeral']) for e,p in source_row]
                expected=[(c['column'],c['numeral']) for c in ref['labels']]
                if actual!=expected:break
                allocations.extend((p,dict(c,primary_reference=reference,primary_row=ref['row']))
                    for (_,p),c in zip(source_row,ref['labels']))
            else:
                key=tuple(c['index'] for p,c in allocations)
                matches[key]=allocations
        if len(matches)==1:
            for plan,proposal in next(iter(matches.values())):
                plan['primary_literal']=proposal
        # Ordinary OCR suppresses isolated one-letter words. Supply genuine
        # neighbouring index strips from this same source table, in source
        # cell order. Nothing is duplicated, guessed or added to the image.
        ordered=sorted(plans.values(),key=lambda p:(p['proof']['row'],p['proof']['source_peers'][0]['column'],
            p['proof']['value']))
        pieces=[]
        for plan in ordered:
            with Image.open(BytesIO(plan['data'])) as image:
                pieces.append(image.convert('RGB'))
        height=max(p.height for p in pieces)
        context=Image.new('RGB',(sum(p.width for p in pieces),height),'white')
        x=offset=0;ranges=[]
        for plan,piece in zip(ordered,pieces):
            context.paste(piece,(x,height-piece.height))
            count=len(plan['proof']['subscript_components'])
            ranges.append(dict(row=plan['proof']['row'],value=plan['proof']['value'],
                character_offset=offset,component_count=count,pixel_bbox=[x,height-piece.height,x+piece.width,height]))
            plan['literal_context']=dict(offset=offset,count=count)
            offset+=count;x+=piece.width;piece.close()
        stream=BytesIO();context.save(stream,'PNG');context.close()
        for plan in ordered:
            plan['literal_context'].update(data=stream.getvalue(),total=offset,
                proof=dict(method='source_index_strips_in_source_cell_order',ranges=ranges))
    return plans
