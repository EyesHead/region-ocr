"""Read raster table cells with current Cyrillic OCR before image fallback.

Table structure remains sourced from the PDF grid. OCR boxes are local image
coordinates, never fabricated native-PDF glyph coordinates. Drawings retain
their original pixels when text boxes do not explain the non-grid ink.
"""
import base64
from functools import lru_cache
import hashlib
from html import escape
from io import BytesIO
from pathlib import Path
import re
import time

from bs4 import BeautifulSoup

MODEL_ROOT=Path(__file__).resolve().parent/'.cache/docling_models/rapidocr-3.10.0'


@lru_cache(maxsize=1)
def engine():
    from rapidocr import RapidOCR,OCRVersion,LangRec,ModelType
    return RapidOCR(params={'Global.model_root_dir':str(MODEL_ROOT),'Global.use_cls':False,
        'Global.log_level':'warning','Det.ocr_version':OCRVersion.PPOCRV6,'Det.model_type':ModelType.SMALL,
        'Det.limit_type':'max','Det.limit_side_len':960,
        'Rec.ocr_version':OCRVersion.PPOCRV5,'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.CYRILLIC,
        'EngineConfig.onnxruntime.intra_op_num_threads':4,'EngineConfig.onnxruntime.inter_op_num_threads':1})


def ink_without_frame(image):
    import cv2
    import numpy as np
    gray=np.asarray(image.convert('L'))
    mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    h,w=mask.shape;frame=np.zeros_like(mask)
    for horizontal in (True,False):
        kernel=np.ones((1,max(8,round(w*.7))) if horizontal else (max(8,round(h*.7)),1),np.uint8)
        rules=cv2.morphologyEx(mask,cv2.MORPH_OPEN,kernel)
        count,labels,stats,_=cv2.connectedComponentsWithStats(rules,8)
        for i,(x,y,cw,ch,area) in enumerate(stats[1:],1):
            edge=(y<=2 or y+ch>=h-2) if horizontal else (x<=2 or x+cw>=w-2)
            if edge:frame[labels==i]=255
    return cv2.bitwise_and(mask,cv2.bitwise_not(frame))


def components(mask):
    import cv2
    _,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
    return [dict(x=int(x),y=int(y),w=int(w),h=int(h),area=int(area))
        for x,y,w,h,area in stats[1:] if area>2 and h>1 and w>1]


def source_simple_signs(mask):
    """Read bare +/++/− cells from complete measured cross strokes."""
    import cv2
    import numpy as np
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
    pieces=[]
    for i,(x,y,w,h,area) in enumerate(stats[1:],1):
        if area<=2:continue
        local=labels[y:y+h,x:x+w]==i
        if w>=4*h and area>=.7*w*h:
            kind='−'
        else:
            if min(w,h)<4 or not .45<=w/h<=2.2:return None
            rows=np.flatnonzero(local.sum(axis=1)>=.75*w)
            cols=np.flatnonzero(local.sum(axis=0)>=.75*h)
            if not len(rows) or not len(cols):return None
            cy=float(rows.mean());cx=float(cols.mean())
            if not (.2*h<=cy<=.8*h and .2*w<=cx<=.8*w):return None
            if len(rows)>.35*h or len(cols)>.35*w:return None
            cross=np.zeros_like(local);cross[rows,:]=True;cross[:,cols]=True
            if np.count_nonzero(local&~cross)>.15*area:return None
            kind='+'
        pieces.append((int(x),int(y),int(w),int(h),kind))
    if not pieces:return None
    pieces.sort()
    if len(set(p[-1] for p in pieces))!=1:return None
    if max(p[1]+p[3]/2 for p in pieces)-min(p[1]+p[3]/2 for p in pieces)>max(p[3] for p in pieces)*.3:return None
    if len(pieces)>1 and pieces[0][-1]=='−':return None
    text=''.join(p[-1] for p in pieces)
    return dict(text=text,components=[list(p) for p in pieces],method='complete_source_cross_or_dash_strokes')


def missing_text_lines(image,mask,boxes):
    """Recover a line missed by detection inside a proven source table cell.

    Recognition uses measured ink rows, not a second table/layout prediction.
    Single long strokes and non-text-shaped regions are left to the caller.
    """
    import cv2
    import numpy as np
    from statistics import median
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    covered=np.zeros_like(mask)
    for box in boxes:cv2.fillPoly(covered,[np.rint(box).astype('int32')],255)
    covered=cv2.dilate(covered,np.ones((3,3),np.uint8))
    missing=np.where(covered==0,mask,0).astype('uint8')
    parts=components(missing)
    # Once a multi-line text block is established, its last line may consist
    # of only one short syllable/letter. It still owns real source ink.
    minimum=1 if len(boxes)>=2 else 3
    if len(parts)<minimum:return []
    typical=median(p['h'] for p in parts)
    rows=np.flatnonzero(np.any(missing,axis=1));groups=[]
    for y in rows:
        if not groups or y-groups[-1][-1]>max(2,typical*.35):groups.append([])
        groups[-1].append(int(y))
    recovered=[]
    for group in groups:
        y0,y1=group[0],group[-1]+1
        members=[p for p in parts if p['y']<y1 and p['y']+p['h']>y0]
        if len(members)<minimum:continue
        x0=min(p['x'] for p in members);x1=max(p['x']+p['w'] for p in members)
        if y1-y0>2.5*typical or x1-x0<( .4 if minimum==1 else 2)*(y1-y0):continue
        bounds=[max(0,x0-2),max(0,y0-2),min(image.width,x1+2),min(image.height,y1+2)]
        patch=image.crop(bounds)
        try:
            r=engine().recognize_txt([np.asarray(patch)])
            readings=[(r.txts[0],float(r.scores[0]))] if r.txts and r.txts[0].strip() else []
            if readings and readings[0][1]<.6:
                # Serif uppercase abbreviations may have low CTC confidence
                # even when read correctly. Require literal agreement on
                # independently resampled source pixels, not a guessed word
                # or an unqualified lower confidence threshold.
                from collections import Counter
                from PIL import Image,ImageOps
                for factor in (2,4):
                    enlarged=patch.resize((patch.width*factor,patch.height*factor),Image.Resampling.LANCZOS)
                    padded=ImageOps.expand(enlarged,border=16,fill='white');enlarged.close()
                    try:other=engine().recognize_txt([np.asarray(padded)])
                    finally:padded.close()
                    if other.txts and other.txts[0].strip():readings.append((other.txts[0],float(other.scores[0])))
                counts=Counter(value for value,_ in readings)
                consensus=[value for value,n in counts.items() if n>=2]
            else:consensus=[]
        finally:patch.close()
        value=(max((item for item in readings if item[0] in consensus),key=lambda v:v[1])
               if consensus else readings[0] if readings and readings[0][1]>=.6 else None)
        if value:
            a,b,c,d=bounds
            recovered.append((value[0],value[1],[[a,b],[c,b],[c,d],[a,d]]))
    return recovered


def raised_suffix(image,text,box):
    """A suffix is raised only when the actual last ink component is raised."""
    from statistics import median
    import numpy as np
    match=re.search(r'([A-Za-zА-Яа-яЁё]+)(\d)([),.;:]?)$',text)
    if not match:return escape(text),None
    mask=ink_without_frame(image);h,w=mask.shape
    points=np.asarray(box);x0,y0=points.min(axis=0);x1,y1=points.max(axis=0)
    parts=[p for p in components(mask) if x0-2<=p['x']<=x1+2 and y0-2<=p['y']<=y1+2]
    if len(parts)<3:return escape(text),None
    height=max(p['h'] for p in parts)
    body=[p for p in parts if p['h']>=.75*height]
    baseline=median(p['y']+p['h'] for p in body)
    last=max(parts,key=lambda p:p['x']+p['w'])
    # Superscripts in old low-resolution scans can have the same raster
    # height as the base letters. Their measured baseline displacement is
    # the decisive evidence, rather than an assumed smaller font.
    if not (.3*height<=last['h']<=1.05*height and last['y']+last['h']<baseline-.15*height):
        return escape(text),None
    pos=match.start(2)
    html=escape(text[:pos])+'<sup>'+escape(match[2])+'</sup>'+escape(text[pos+1:])
    return html,dict(method='OCR_suffix_and_measured_raised_final_ink_component',text=match[2],
        local_bbox=[last['x'],last['y'],last['x']+last['w'],last['y']+last['h']],baseline=baseline)


@lru_cache(maxsize=2048)
def recognize(data):
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(BytesIO(data)) as original:image=original.convert('RGB')
    try:
        simple=source_simple_signs(ink_without_frame(image))
        if simple:
            return dict(accepted=True,reason='complete_source_sign_geometry',texts=[simple['text']],scores=[1.],
                boxes=[],html=escape(simple['text']),source_scripts=[],confidence=1.,coverage=1.,
                uncovered_ink_pixels=0,formula=False,seconds=0.,source_signs=simple)
        started=time.perf_counter();result=engine()(image);elapsed=time.perf_counter()-started
        texts=list(result.txts or []);scores=list(map(float,result.scores or []))
        mask=ink_without_frame(image);boxes=np.asarray(result.boxes) if texts else np.empty((0,4,2))
        scale_proof=None
        if texts and image.height<=80 and image.width<=600 and sum(scores)/len(scores)<.85:
            # Tiny Cyrillic tokens are easily read as visually similar Latin
            # letters/digits. Re-read their original pixels at useful model
            # resolution; retain actual OCR output, never a spelling table.
            from PIL import ImageOps
            options=[(sum(scores)/len(scores),texts,scores,boxes,None)]
            for factor in (3,4):
                enlarged=image.resize((image.width*factor,image.height*factor),Image.Resampling.LANCZOS)
                probe=ImageOps.expand(enlarged,border=16,fill='white');enlarged.close()
                try:r=engine()(probe)
                finally:probe.close()
                values=list(r.txts or []);conf=list(map(float,r.scores or []))
                if values and len(values)==len(texts):
                    local=(np.asarray(r.boxes,dtype=float)-16)/factor
                    local[:,:,0]=np.clip(local[:,:,0],0,image.width)
                    local[:,:,1]=np.clip(local[:,:,1],0,image.height)
                    options.append((sum(conf)/len(conf),values,conf,local,dict(scale=factor,padding=16)))
            _,texts,scores,boxes,scale_proof=max(options,key=lambda x:x[0])
        if not texts and np.any(mask):
            parts=components(mask)
            if len(parts)==1 and parts[0]['w']>=4*parts[0]['h']:
                p=parts[0];texts=['—'];scores=[1.]
                boxes=np.array([[[p['x'],p['y']],[p['x']+p['w'],p['y']],
                    [p['x']+p['w'],p['y']+p['h']],[p['x'],p['y']+p['h']]]])
            else:
                # The detector can miss isolated numerals/signs in a known
                # source cell. Feed its measured ink directly to the SAME
                # current Cyrillic recognizer instead of retaining a crop.
                from rapidocr.ch_ppocr_rec.typings import TextRecInput
                yy,xx=np.where(mask>0)
                bounds=[max(0,int(xx.min())-2),max(0,int(yy.min())-2),
                        min(image.width,int(xx.max())+3),min(image.height,int(yy.max())+3)]
                patch=image.crop(bounds)
                try:direct=engine().recognize_txt([np.asarray(patch)])
                finally:patch.close()
                texts=list(direct.txts or []);scores=list(map(float,direct.scores or []))
                x0,y0,x1,y1=bounds;boxes=np.asarray([[[x0,y0],[x1,y0],[x1,y1],[x0,y1]]])
        if not texts or not any(t.strip() for t in texts):return dict(accepted=False,reason='no_readable_text',seconds=elapsed)
        extra=missing_text_lines(image,mask,boxes)
        if extra:
            texts.extend(t for t,_,_ in extra);scores.extend(s for _,s,_ in extra)
            boxes=np.concatenate([boxes,np.asarray([b for _,_,b in extra])])
            order=sorted(range(len(texts)),key=lambda i:(float(boxes[i,:,1].mean()),float(boxes[i,:,0].min())))
            texts=[texts[i] for i in order];scores=[scores[i] for i in order];boxes=boxes[order]
        covered=np.zeros_like(mask)
        for box in boxes:cv2.fillPoly(covered,[np.rint(box).astype('int32')],255)
        covered=cv2.dilate(covered,np.ones((3,3),np.uint8))
        ink=int(np.count_nonzero(mask));uncovered=int(np.count_nonzero((mask>0)&(covered==0)))
        coverage=1-uncovered/max(1,ink)
        confidence=sum(scores)/len(scores)
        # Low CTC confidence on a short class label, dash or unit is not
        # evidence of a drawing. Keep the transcription and its confidence
        # in the report; preserve pixels only for unexplained non-text ink.
        accepted=coverage>=.9
        lines=[];scripts=[]
        for text,box in zip(texts,boxes):
            html,proof=raised_suffix(image,text,box);lines.append(html)
            if proof:scripts.append(proof)
        plain=' '.join(texts)
        # Class labels such as W4/XD3 stay text. A word-free expression with
        # mathematical operators/symbols is sent to the formula-only reader.
        word=bool(re.search('[А-Яа-яЁё]{3,}',plain))
        parts=components(mask);height=max((p['h'] for p in parts),default=0)
        shifted=bool(height and any(.3*height<=p['h']<.75*height and p['y']<.15*height for p in parts))
        formula=(not word and (bool(re.search(r'[=∑Σ√∞α-ωΑ-Ω]',plain)) or
            (bool(re.search('[A-Za-z]',plain)) and bool(re.search(r'[_^+*/<>≤≥]',plain))) or
            (shifted and bool(re.search(r'10|[A-Za-z].*\d',plain)))))
        return dict(accepted=accepted,reason='text_boxes_cover_source_ink' if accepted else 'unexplained_nontext_ink',
            texts=texts,scores=scores,boxes=boxes.tolist(),html='<br/>'.join(lines),source_scripts=scripts,
            confidence=confidence,coverage=coverage,uncovered_ink_pixels=uncovered,formula=formula,seconds=elapsed,
            recognition_preprocessing=scale_proof)
    finally:image.close()


def joined_formula_cells(soup,geometry,page,manifest,api,recognizer,source_image,region,*,native_only=False):
    """Read adjacent PNG pieces from one physical formula cell together.

    A full-cell crop supplies the context; it never combines different cells
    or feeds Russian prose/diagrams into the formula-only reader.
    """
    if source_image is None or region is None or manifest.get('formula_reader')!='p2t':return []
    import math
    import numpy as np
    from docling_pdf_repairs import verified_equation,_math_asset_budget
    from docling_pdf_formula_fragments import fraction_bar_context,formula_cell_parts,trim_formula_whitespace
    profile=api.table_profile(str(soup.table));nodes=soup.table.find_all(['td','th'])
    protected={p['source_image_sha256'] for p in geometry.get('source_graphic_assets',[])}
    protected.update(p['source_image_sha256'] for p in manifest.get('source_cell_diagrams',[])
        if p.get('evidence',{}).get('method')!='source_layout_graphic_without_equation')
    weak_graphics={p['source_image_sha256'] for p in manifest.get('source_cell_diagrams',[])
        if p.get('evidence',{}).get('method')=='source_layout_graphic_without_equation'}
    physical={(c['row'],c['column'],c['row_span'],c['column_span']):c for c in geometry.get('cells',[])}
    all_images=soup.table.find_all('img');all_entries=geometry.get('retained_cell_images',[])
    mapped=(len(all_images)==len(all_entries) and all(abs(int(n.get('width',0))-
        round((e['bbox'][2]-e['bbox'][0])*96/72))<=1 for n,e in zip(all_images,all_entries)))
    image_sources={id(n):e for n,e in zip(all_images,all_entries)} if mapped else {}
    if not mapped:
        # Native line objects can be emitted in drawing order while DOM
        # images follow text baseline order (notably nested fractions).
        # Recover a unique width allocation INSIDE each proven source cell;
        # never borrow an asset from a neighbouring row or column.
        for node,position in zip(nodes,profile['cells']):
            local=node.find_all('img')
            entries=[e for e in all_entries if (e.get('row'),e.get('column'))==
                (position['row'],position['column'])]
            if not local or len(local)!=len(entries):continue
            matches=lambda n,e:abs(int(n.get('width',0))-round((e['bbox'][2]-e['bbox'][0])*96/72))<=1
            if all(matches(n,e) for n,e in zip(local,entries)):
                image_sources.update({id(n):e for n,e in zip(local,entries)});continue
            candidates={j:{i for i,e in enumerate(entries) if matches(n,e)} for j,n in enumerate(local)}
            allocation={}
            while candidates and all(candidates.values()):
                forced=next(((j,next(iter(choices))) for j,choices in candidates.items() if len(choices)==1),None)
                if forced is None:break
                j,i=forced;allocation[j]=i;del candidates[j]
                for choices in candidates.values():choices.discard(i)
            if len(allocation)==len(local):
                image_sources.update({id(local[j]):entries[i] for j,i in allocation.items()})
    proofs=[];done=set()
    for node,position in zip(nodes,profile['cells']):
        images=node.find_all('img')
        if not images:continue
        key=tuple(position[k] for k in ('row','column','row_span','column_span'))
        source=physical.get(key)
        if source is None or not source.get('bbox'):continue
        entries=[e for e in geometry.get('retained_cell_images',[]) if
                 (e.get('row'),e.get('column'))==key[:2]]
        if any(e.get('source_column_context',{}).get('graphic_column') or
               e.get('source_column_context',{}).get('dimension_column') for e in entries):continue
        decoded={id(im):base64.b64decode(im['src'].split(',',1)[1]) for im in images
            if im.get('src','').startswith('data:image/')}
        digests={hashlib.sha256(data).hexdigest() for data in decoded.values()}
        if digests&protected:continue
        fraction_proofs=[]
        for im in images:
            entry=image_sources.get(id(im))
            if entry and id(im) in decoded:
                proof=fraction_bar_context(decoded[id(im)],entry,source)
                if proof:fraction_proofs.append(proof)
        source_fraction_bar=bool(fraction_proofs)
        if native_only and not fraction_proofs:continue
        if digests&weak_graphics and not source_fraction_bar:continue
        if len(images)==1 and not source_fraction_bar:
            # A PDF fraction can be a native numerator/denominator with only
            # its horizontal bar stored as PNG. Read the complete physical
            # cell once rather than turn that bar into a minus sign.
            if not node.find('math'):continue
            src=images[0].get('src','')
            if not src.startswith('data:image/'):continue
            from PIL import Image
            import cv2
            with Image.open(BytesIO(base64.b64decode(src.split(',',1)[1]))) as bar:
                gray=np.asarray(bar.convert('L'))
                ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
                sign=source_simple_signs(ink)
                source_fraction_bar=bool(sign and sign['text']=='−')
            if not source_fraction_bar:continue
        plain=node.get_text(' ',strip=True)
        if node.find('table'):continue
        pending=geometry.setdefault('fragmented_formula_pending',[])
        if fraction_proofs:
            for digest in digests:
                if not any((p.get('row'),p.get('column'),p['source_image_sha256'])==
                        (key[0],key[1],digest) for p in pending):
                    pending.append(dict(page=page,row=key[0],column=key[1],
                        source_image_sha256=digest,reason='complete_source_fraction_not_yet_transcribed'))
        parts=(formula_cell_parts(source,entries) if fraction_proofs else
            None if re.search('[А-Яа-яЁё]{3,}',plain) else [dict(kind='formula',bbox=source['bbox'])])
        if not parts:continue
        box=source['bbox'];sx=source_image.width/(region[2]-region[0]);sy=source_image.height/(region[3]-region[1])
        asset=None
        try:
            chunks=[];rendered_parts=[]
            for part in parts:
                if part['kind']=='text':chunks.append(part['html']);continue
                part_box=part['bbox']
                pixels=[max(0,math.floor((part_box[0]-region[0])*sx)),max(0,math.floor((part_box[1]-region[1])*sy)),
                        min(source_image.width,math.ceil((part_box[2]-region[0])*sx)),
                        min(source_image.height,math.ceil((part_box[3]-region[1])*sy))]
                asset=source_image.crop(pixels).convert('RGB')
                # Delete measured outer rules only, retaining the original
                # glyph pixels and gaps between separate source pieces.
                import cv2
                gray=np.asarray(asset.convert('L'));ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
                frame=(ink>0)&(ink_without_frame(asset)==0)
                clean=np.asarray(asset).copy();clean[frame]=255
                from PIL import Image
                asset.close();asset=Image.fromarray(clean)
                source_stream=BytesIO();asset.save(source_stream,'PNG')
                source_digest=hashlib.sha256(source_stream.getvalue()).hexdigest()
                trim_proof=None
                stream=BytesIO();asset.save(stream,'PNG');data=stream.getvalue()
                if not source_fraction_bar:
                    reading=recognize(data)
                    if not (reading.get('formula') or re.search(r'[=+*/<>≤≥∑Σ√]',plain)):
                        raise ValueError('Source cell is not an isolated mathematical region')
                expected=sum(part_box[1]<=(p['bbox'][1]+p['bbox'][3])/2<=part_box[3] for p in fraction_proofs)
                def transcribe():
                    rendered=verified_equation(data,dict(source_page=page,source_bbox=part_box,formula_item=True,
                        source_asset_sha256=source_digest,recognition_preprocessing=trim_proof),recognizer,manifest,api)
                    count=len(BeautifulSoup(rendered,'html.parser').find_all('mfrac'))
                    if count<expected:
                        raise ValueError(f'Complete formula lost source fraction rules: {count} rendered, {expected} proven')
                    return rendered,count
                with _math_asset_budget(manifest,page,api,part_box) as admitted:
                    if not admitted:raise ValueError('Complete source formula deferred by configured budget')
                    try:html,actual=transcribe()
                    except Exception as original:
                        if not fraction_proofs:raise
                        # Keep successful full-cell transcriptions unchanged.
                        # Retry a rejected formula once with empty outer
                        # margins removed, preserving every source ink pixel.
                        trimmed,trim_proof=trim_formula_whitespace(asset);asset.close();asset=trimmed
                        retry=dict(page=page,row=key[0],column=key[1],bbox=part_box,
                            reason=str(original),source_image_sha256=source_digest,preprocessing=trim_proof)
                        manifest.setdefault('fragmented_formula_retries',[]).append(retry)
                        api.diagnostic_event('TABLE_FRAGMENTED_FORMULA_WHITESPACE_RETRY',**retry)
                        stream=BytesIO();asset.save(stream,'PNG');data=stream.getvalue()
                        html,actual=transcribe()
                chunks.append(html);rendered_parts.append(dict(bbox=part_box,
                    source_image_sha256=source_digest,recognition_image_sha256=hashlib.sha256(data).hexdigest(),
                    recognition_preprocessing=trim_proof,fractions_proven=expected,fractions_rendered=actual))
                asset.close();asset=None
            html='<br/>'.join(chunks)
            node.clear();node.append(BeautifulSoup(html,'html.parser'))
            source.update(html=node.decode_contents(),text=node.get_text(' ',strip=True),raster_cell_ocr=True)
            geometry['fragmented_formula_pending']=[p for p in pending if not
                ((p.get('row'),p.get('column'))==key[:2] and p['source_image_sha256'] in digests)]
            manifest['source_cell_diagrams']=[p for p in manifest.get('source_cell_diagrams',[]) if not
                (p['source_image_sha256'] in digests and p.get('evidence',{}).get('method')=='source_layout_graphic_without_equation')]
            proof=dict(page=page,row=key[0],column=key[1],bbox=box,
                method='pix2text_complete_source_cell',source_pieces=len(images),
                source_image_sha256=rendered_parts[0]['source_image_sha256'],recognized_html=html,
                native_fraction_bar=source_fraction_bar,native_fraction_rules=fraction_proofs,
                parts=rendered_parts,source_prose_lines=sum(p['kind']=='text' for p in parts))
            proofs.append(proof);done.add(key[:2])
            api.diagnostic_event('TABLE_FRAGMENTED_FORMULA_CELL_TRANSCRIBED',**proof)
        except Exception as error:
            failure=dict(page=page,row=key[0],column=key[1],bbox=box,
                error_type=type(error).__name__,reason=str(error))
            manifest.setdefault('fragmented_formula_failures',[]).append(failure)
            api.diagnostic_event('TABLE_FRAGMENTED_FORMULA_CELL_REJECTED',**failure)
        finally:
            if asset is not None:asset.close()
    if done:
        geometry['retained_cell_images']=[e for e in geometry.get('retained_cell_images',[]) if
                                        (e.get('row'),e.get('column')) not in done]
    return proofs


def restore_formula_cells(fragment,geometry,page,manifest,api,recognizer=None,source_image=None,source_region=None):
    """Read fragmented native formulas before any individual-image classifier."""
    soup=BeautifulSoup(fragment,'html.parser')
    if soup.table is None:return fragment,[]
    before=api.table_profile(str(soup.table))
    proofs=joined_formula_cells(soup,geometry,page,manifest,api,recognizer,source_image,source_region,native_only=True)
    after=api.table_profile(str(soup.table))
    shape=lambda p:[(c['row'],c['column'],c['row_span'],c['column_span']) for c in p['cells']]
    if shape(before)!=shape(after):raise RuntimeError('Fragmented formula repair changed physical source grid')
    geometry.setdefault('restored_cell_text',[]).extend(proofs)
    manifest.setdefault('raster_cells_transcribed',[]).extend(proofs)
    return str(soup.table),proofs


def overlapping_boundary_fragment(data,image_node,cell):
    """Remove a clipped neighbour stroke already printed by native HTML.

    A slash immediately before a raster unit can be included again as a tiny
    clipped left-edge component. Only the measured fragment is removed; the
    complete preceding native separator remains in the cell.
    """
    prefix=[]
    for node in cell.descendants:
        if node is image_node:break
        if isinstance(node,str):prefix.append(str(node))
    previous=''.join(prefix).rstrip()
    if not previous or previous[-1] not in '/\\,:;=+−–—':return data,None
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(BytesIO(data)) as source:image=source.convert('RGB')
    try:
        mask=ink_without_frame(image);count,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
        if count<4:return data,None
        largest=max(int(s[4]) for s in stats[1:]);height=max(int(s[3]) for s in stats[1:])
        candidates=[i for i,(x,y,w,h,area) in enumerate(stats[1:],1) if x<=1 and
                    w<=max(2,.3*height) and area<.2*largest and h<.7*height]
        if not candidates:return data,None
        pixels=np.asarray(image).copy()
        for i in candidates:pixels[labels==i]=255
        clean=Image.fromarray(pixels);stream=BytesIO();clean.save(stream,'PNG');clean.close()
        return stream.getvalue(),dict(method='clipped_source_neighbour_already_in_native_prefix',
            preceding_separator=previous[-1],removed_components=[stats[i].astype(int).tolist() for i in candidates])
    finally:image.close()


def restore(fragment,geometry,page,manifest,api,recognizer=None,source_image=None,source_region=None):
    soup=BeautifulSoup(fragment,'html.parser');nodes=soup.find_all('img')
    if not nodes:return fragment,[]
    original_profile=api.table_profile(str(soup.table))
    proofs=joined_formula_cells(soup,geometry,page,manifest,api,recognizer,source_image,source_region)
    nodes=soup.find_all('img')
    entries=list(geometry.get('retained_cell_images',[]));mapped=(len(nodes)==len(entries) and
        all(abs(int(n.get('width',0))-round((e['bbox'][2]-e['bbox'][0])*96/72))<=1 for n,e in zip(nodes,entries)))
    mapping={id(n):e for n,e in zip(nodes,entries)} if mapped else {}
    # Reserve the WHOLE consecutive Roman-designation row before generic text
    # OCR can turn I/II/III/IV into quotes or Latin letters. The existing
    # source-geometry/math stage reads indices and renders the numeral intact.
    from docling_pdf_roman_labels import roman_row_plans
    roman_plans=roman_row_plans(list(zip(nodes,entries)),geometry.get('source_roman_references',())) if mapped else {}
    profile=api.table_profile(str(soup.table));cells=soup.table.find_all(['td','th'])
    positions={id(node):cell for node,cell in zip(cells,profile['cells'])}
    converted=set()
    proven_diagrams={p['source_image_sha256'] for p in manifest.get('source_cell_diagrams',[])}
    protected_assets=geometry.get('source_graphic_assets',[])+manifest.get('protected_source_graphic_assets',[])
    for image in nodes:
        cell=image.find_parent(['td','th']);position=positions.get(id(cell))
        if position is None:continue
        source=mapping.get(id(image),{});context=source.get('source_column_context',{})
        if id(image) in roman_plans:
            api.diagnostic_event('SCAN_CELL_ROMAN_RESERVED',page=page,row=position['row'],
                column=position['column'],numeral=roman_plans[id(image)]['proof']['numeral'])
            continue
        if ((context.get('graphic_column') or context.get('dimension_column'))
                and not position.get('header')):
            image['data-cell-content']='source-diagram'
            image['data-diagram-evidence']='source-column-header'
            src=image.get('src','')
            if src.startswith('data:image/'):
                digest=hashlib.sha256(base64.b64decode(src.split(',',1)[1])).hexdigest()
                proof=dict(page=page,row=position['row'],column=position['column'],
                    bbox=source.get('bbox'),source_image_sha256=digest,evidence=context)
                manifest.setdefault('source_cell_diagrams',[]).append(proof)
                api.diagnostic_event('TABLE_SOURCE_DIAGRAM_CONFIRMED',**proof)
            continue
        src=image.get('src','')
        if not src.startswith('data:image/'):continue
        data=base64.b64decode(src.split(',',1)[1]);digest=hashlib.sha256(data).hexdigest()
        if any((p.get('row'),p.get('column'),p['source_image_sha256'])==
                (position['row'],position['column'],digest) for p in geometry.get('fragmented_formula_pending',[])):
            api.diagnostic_event('SCAN_CELL_FRAGMENTED_FORMULA_PENDING',page=page,row=position['row'],
                column=position['column'],source_image_sha256=digest,
                reason='complete_source_formula_required_before_individual_asset_OCR')
            continue
        native_graphic=any(p.get('page')==page and p.get('row')==position['row'] and
            p.get('column')==position['column'] and p['source_image_sha256']==digest for p in protected_assets)
        if digest in proven_diagrams or native_graphic:
            image['data-cell-content']='source-diagram'
            if native_graphic:image['data-diagram-evidence']='native-source-bytes-and-graphic-column'
            continue
        data,boundary_proof=overlapping_boundary_fragment(data,image,cell)
        record=dict(page=page,row=position['row'],column=position['column'],source_image_sha256=digest,
                    model='PP-OCRv6 small detection + PP-OCRv5 Cyrillic mobile recognition',runtime='RapidOCR 3.10.0')
        try:result=recognize(data)
        except Exception as error:
            issue=dict(**record,error_type=type(error).__name__,reason=str(error))
            manifest.setdefault('raster_cell_ocr_failures',[]).append(issue)
            api.diagnostic_event('SCAN_CELL_OCR_FAILED',**issue)
            continue
        if not result['accepted']:
            api.diagnostic_event('SCAN_CELL_OCR_SOURCE_KEEP',**record,**result)
            continue
        html=result['html'];method='cyrillic_text_ocr'
        if result.get('formula'):
            if manifest.get('formula_reader')!='p2t':continue
            from docling_pdf_repairs import verified_equation,_math_asset_budget
            try:
                with _math_asset_budget(manifest,page,api,source.get('bbox')) as admitted:
                    if not admitted:continue
                    html=verified_equation(data,dict(source_page=page,source_bbox=source.get('bbox'),
                        formula_item=True),recognizer,manifest,api)
                method='pix2text_formula_ocr'
            except Exception as error:
                issue=dict(**record,error_type=type(error).__name__,reason=str(error))
                manifest.setdefault('raster_cell_ocr_failures',[]).append(issue)
                api.diagnostic_event('SCAN_CELL_FORMULA_SOURCE_KEEP',**issue)
                continue
        converted.add(id(image));image.replace_with(BeautifulSoup(html,'html.parser'))
        proof=dict(**record,method=method,recognized_html=html,boundary_fragment=boundary_proof,**result)
        if source.get('bbox'):proof['bbox']=source['bbox']
        proofs.append(proof)
        api.diagnostic_event('SCAN_CELL_RASTER_TRANSCRIBED',**proof)
        native=next((c for c in geometry.get('cells',[]) if c['row']==position['row'] and c['column']==position['column']),None)
        if native is not None:
            native.update(html=cell.decode_contents(),text=cell.get_text(' ',strip=True),raster_cell_ocr=True)
            native['raster_cell_ocr_source']=dict(bbox=source.get('bbox'),coverage=result.get('coverage',0),
                confidence=result.get('confidence',0),formula=result.get('formula',False))
    if mapped:geometry['retained_cell_images']=[e for n,e in zip(nodes,entries) if id(n) not in converted]
    geometry.setdefault('restored_cell_text',[]).extend(proofs)
    manifest.setdefault('raster_cells_transcribed',[]).extend(proofs)
    result=str(soup.table);checked=api.table_profile(result)
    shape=lambda p:[(c['row'],c['column'],c['row_span'],c['column_span']) for c in p['cells']]
    if (checked['rows'],checked['columns'],shape(checked))!=(original_profile['rows'],original_profile['columns'],shape(original_profile)):
        raise RuntimeError('Raster cell OCR changed source table structure')
    return result,proofs


def audit(soup,manifest,api):
    """A raster fallback is diagnostic, never a completed editable table."""
    entries={e['ref']:e for e in manifest.get('tables',[])}
    issues=[];tables=[]
    diagrams={p['source_image_sha256'] for p in manifest.get('source_cell_diagrams',[])}
    diagrams.update(p['source_image_sha256'] for p in manifest.get('typed_table_picture_assets',[]))
    for ordinal,table in enumerate(soup.find_all('table')):
        ref=table.get('data-pdf-table-ref');entry=entries.get(ref,{})
        profile=api.table_profile(str(table))
        shapes=[(c['row'],c['column'],c['row_span'],c['column_span']) for c in profile['cells']]
        cells=[c for c in table.find_all(['td','th']) if c.find_parent('table') is table]
        positions={id(node):p for node,p in zip(cells,profile['cells'])}
        raster=[]
        for image in table.find_all('img'):
            if image.find_parent('table') is not table:continue
            cell=image.find_parent(['td','th'])
            if cell is None:continue
            data=image.get('src','')
            digest=hashlib.sha256(base64.b64decode(data.split(',',1)[1])).hexdigest() if data.startswith('data:image/') else hashlib.sha256(data.encode()).hexdigest()
            if digest in diagrams:continue
            position=positions.get(id(cell),{})
            raster.append(dict(source_image_sha256=digest,row=position.get('row'),column=position.get('column'),
                reason='unresolved_raster_cell_content'))
        record=dict(ordinal=ordinal,ref=ref,page=entry.get('page'),rows=profile['rows'],
            columns=profile['columns'],cells=len(shapes),merged_cells=sum(r>1 or c>1 for _,_,r,c in shapes),
            structure_sha256=hashlib.sha256(repr(shapes).encode()).hexdigest(),
            raster_cell_images=len(raster),diagram_images=len(table.find_all('img'))-len(raster))
        tables.append(record)
        issues.extend(dict(**record,**item) for item in raster)
    for figure in soup.select('figure.source-table'):
        if figure.find('img'):
            issues.append(dict(reason='whole_table_raster_fallback',page=None,ref=None))
    manifest['table_content_audit']=dict(tables=tables,issues=issues,
        raster_cell_images=sum(t['raster_cell_images'] for t in tables),
        whole_table_rasters=sum(i['reason']=='whole_table_raster_fallback' for i in issues))
    api.diagnostic_event('TABLE_EDITABLE_CONTENT_AUDIT',structured_tables=len(tables),
        raster_cell_images=manifest['table_content_audit']['raster_cell_images'],
        whole_table_rasters=manifest['table_content_audit']['whole_table_rasters'],issues=len(issues))
    return issues


