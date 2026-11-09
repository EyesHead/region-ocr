"""Read missing mixed-table text on exact physical cell crops with primary Surya."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from time import monotonic

from bs4 import BeautifulSoup

SCHEMA = 1
POSITION = ('row', 'column', 'row_span', 'column_span')
GRAPHIC_HEADING = re.compile(r'изображен|обозначен|эскиз|схема|черт[её]ж|drawing|sketch|diagram|symbol', re.I)
TEXT_HEADING = re.compile(r'^\s*(?:наименован|назван|описан|name\b|description\b)', re.I)
NOTE_START = re.compile(r'^\s*(?:п\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]|notes?)\b', re.I)


def _cell_text_ink(source_image, region, box):
    """A full-width row must be supported by separate glyph-like components.

    Such rows may be group labels or table notes, but can also be complete
    drawings. A VLM transcription by itself cannot decide which one it is.
    """
    import cv2
    import numpy as np
    sx, sy = source_image.width/(region[2]-region[0]), source_image.height/(region[3]-region[1])
    bounds = (max(0, math.ceil((box[0]-region[0]+1.5)*sx)),
              max(0, math.ceil((box[1]-region[1]+1.5)*sy)),
              min(source_image.width, math.floor((box[2]-region[0]-1.5)*sx)),
              min(source_image.height, math.floor((box[3]-region[1]-1.5)*sy)))
    crop = source_image.crop(bounds)
    try: mask = (np.asarray(crop.convert('L')) < 180).astype('uint8')
    finally: crop.close()
    if not mask.size or np.count_nonzero(mask) < 12:
        return False
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    components = [s for s in stats[1:] if s[cv2.CC_STAT_AREA] >= 2]
    if len(components) < 4:
        return False
    heights = [s[cv2.CC_STAT_HEIGHT] for s in components if s[cv2.CC_STAT_HEIGHT] >= 3]
    if not heights:
        return False
    typical = float(np.median(heights))
    covered = sum(int(s[cv2.CC_STAT_AREA]) for s in components
                  if s[cv2.CC_STAT_WIDTH] <= max(8, typical*3)
                  and s[cv2.CC_STAT_HEIGHT] <= max(8, typical*2.5))
    return covered / np.count_nonzero(mask) >= .97


def _whole_table_proposals(args, image, region, physical, header_rows, graphic_columns, page, api):
    """A unique physical prose cell can own multiple primary display rows.

    Source rails, rather than the model's header/row guesses, own the content.
    This route requires one owner in each eligible column and matching source
    text-line bands. Multiple source owners are never guessed from row order.
    """
    owners = {}
    for key, cell in physical.items():
        if cell['row'] >= header_rows and not graphic_columns.intersection(range(key[1], key[1]+key[3])):
            owners.setdefault((key[1], key[3]), []).append((key, cell))
    if not owners or any(len(values) != 1 for values in owners.values()):
        return {}
    key = hashlib.sha256(f'primary-table:{SCHEMA}:{image.size}:'.encode()+image.convert('RGB').tobytes()).hexdigest()
    root = Path(getattr(args, 'primary_table_recovery_cache', None) or args.ocr_cache_dir/'primary_cells')
    path = root/('whole-'+key+'.json')
    if path.exists():
        saved = json.loads(path.read_text('utf-8'))
        if (saved.get('schema') != SCHEMA or saved.get('image_sha256') != key
                or hashlib.sha256(saved.get('raw', '').encode()).hexdigest() != saved.get('response_sha256')):
            raise ValueError('Invalid primary table response cache')
    else:
        model = getattr(args, 'surya_ocr_model', None)
        if model is None: return {}
        from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
        started = monotonic()
        api.diagnostic_event('PRIMARY_TABLE_CROP_REQUEST', page=page, bbox=region, max_tokens=4096)
        outputs = api.local_surya_backend(model).generate([BatchInputItem(image=image,
            prompt_type=PROMPT_TYPE_BLOCK, max_tokens=4096, metadata=dict(page=page, element='source_table_crop'))])
        if len(outputs) != 1 or outputs[0].error or outputs[0].token_count >= 4096:
            raise RuntimeError('Primary table crop OCR did not finish')
        saved = dict(schema=SCHEMA, image_sha256=key, raw=outputs[0].raw,
                     response_sha256=hashlib.sha256(outputs[0].raw.encode()).hexdigest(),
                     token_count=outputs[0].token_count, elapsed=round(monotonic()-started, 3))
        root.mkdir(parents=True, exist_ok=True)
        api.write_atomic(path, json.dumps(saved, ensure_ascii=False, indent=2))
    from surya.inference.parsers import clean_block_html
    parsed = BeautifulSoup(clean_block_html(saved['raw']), 'html.parser')
    if len(parsed.find_all('table')) != 1: return {}
    primary = api.table_profile(str(parsed.table))
    if primary['issues'] or primary['columns'] != len({x for c in physical.values()
            for x in range(c['column'], c['column']+c['column_span'])}): return {}
    proposals = {}
    for (column, span), [(position, cell)] in owners.items():
        readings = [c for c in primary['cells'] if not c.get('header') and c['text'].strip()
                    and c['column'] == column and c['column_span'] == span]
        if not readings or any(BeautifulSoup(c['html'], 'html.parser').find(['img','svg','table']) for c in readings):
            continue
        # Distinct source line bands reject hallucinated extra display rows.
        import numpy as np
        sx, sy = image.width/(region[2]-region[0]), image.height/(region[3]-region[1])
        b = cell['bbox']
        bounds = (max(0, math.ceil((b[0]-region[0]+1.5)*sx)),
                  max(0, math.ceil((b[1]-region[1]+1.5)*sy)),
                  min(image.width, math.floor((b[2]-region[0]-1.5)*sx)),
                  min(image.height, math.floor((b[3]-region[1]-1.5)*sy)))
        band_image = image.crop(bounds)
        try: ink = np.asarray(band_image.convert('L')) < 180
        finally: band_image.close()
        if not ink.size: continue
        occupied = np.count_nonzero(ink, axis=1) >= max(2, ink.shape[1]*.004)
        bands = [];start = None
        for y, value in enumerate(list(occupied)+[False]):
            if value and start is None: start = y
            elif not value and start is not None:
                if y-start >= 3: bands.append((start,y))
                start = None
        if len(bands) != len(readings): continue
        content = '<br/>'.join(c['html'] for c in readings)
        proof = dict(method='primary_table_prose_on_unique_physical_source_cell',
            source_bbox=list(cell['bbox']), primary_table_bbox=list(region),
            response_sha256=saved['response_sha256'], image_sha256=key,
            source_text_line_bands=len(bands), primary_display_rows=len(readings),
            page=page, row=position[0], column=position[1])
        proposals[position] = content, proof
    return proposals


def _read_cell_unverified(args, source_image, region, cell, page, api):
    box = cell['bbox']
    sx, sy = source_image.width/(region[2]-region[0]), source_image.height/(region[3]-region[1])
    # Exclude the physical grid rail, preserving every interior text pixel.
    pixels = (max(0, math.ceil((box[0]-region[0]+.7)*sx)),
              max(0, math.ceil((box[1]-region[1]+.7)*sy)),
              min(source_image.width, math.floor((box[2]-region[0]-.7)*sx)),
              min(source_image.height, math.floor((box[3]-region[1]-.7)*sy)))
    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
        return None
    crop = source_image.crop(pixels)
    try:
        key = hashlib.sha256(f'primary-cell:{SCHEMA}:{crop.size}:'.encode()+crop.convert('RGB').tobytes()).hexdigest()
        root = Path(getattr(args, 'primary_table_recovery_cache', None) or args.ocr_cache_dir/'primary_cells')
        path = root/(key+'.json')
        if path.exists():
            saved = json.loads(path.read_text('utf-8'))
            if (saved.get('schema') != SCHEMA or saved.get('image_sha256') != key
                    or hashlib.sha256(saved.get('raw', '').encode()).hexdigest() != saved.get('response_sha256')):
                raise ValueError('Invalid primary cell response cache')
            api.diagnostic_event('PRIMARY_TABLE_CELL_CACHE_HIT', page=page, row=cell['row'], column=cell['column'], key=key)
        else:
            model = getattr(args, 'surya_ocr_model', None)
            if model is None:
                return None  # Snapshot replay never starts a new engine implicitly.
            from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
            started = monotonic()
            api.diagnostic_event('PRIMARY_TABLE_CELL_REQUEST', page=page, row=cell['row'], column=cell['column'],
                                 bbox=box, source_pixel_bbox=list(pixels), max_tokens=2048)
            outputs = api.local_surya_backend(model).generate([BatchInputItem(image=crop,
                prompt_type=PROMPT_TYPE_BLOCK, max_tokens=2048,
                metadata=dict(page=page, element='source_table_cell', row=cell['row'], column=cell['column']))])
            if len(outputs) != 1 or outputs[0].error or outputs[0].token_count >= 2048:
                raise RuntimeError('Primary source-cell OCR did not finish')
            saved = dict(schema=SCHEMA, image_sha256=key, raw=outputs[0].raw,
                         response_sha256=hashlib.sha256(outputs[0].raw.encode()).hexdigest(),
                         token_count=outputs[0].token_count, elapsed=round(monotonic()-started, 3))
            root.mkdir(parents=True, exist_ok=True)
            api.write_atomic(path, json.dumps(saved, ensure_ascii=False, indent=2))
            api.diagnostic_event('PRIMARY_TABLE_CELL_RESPONSE', page=page, row=cell['row'], column=cell['column'],
                                 key=key, token_count=saved['token_count'], elapsed=saved['elapsed'])
        from surya.inference.parsers import clean_block_html
        parsed = BeautifulSoup(clean_block_html(saved['raw']), 'html.parser')
        if parsed.find('table'):
            # The model sometimes wraps one physical-cell crop in an imagined
            # one-column table. Retain its ordered text, never its guessed grid.
            tables = parsed.find_all('table')
            if len(tables) != 1 or api.table_profile(str(tables[0])).get('columns') != 1:
                return None
            paragraphs = [n.decode_contents() for n in tables[0].find_all(['td', 'th'])
                          if n.get_text(strip=True)]
            if len(paragraphs) > 1:
                # Sparse cells tempt the model to continue a list beyond the
                # source. Reject those extra rows using the original ink.
                import numpy as np
                ink = np.asarray(crop.convert('L'))[3:-3, 3:-3] < 180
                if not ink.size: return None
                occupied = np.count_nonzero(ink, axis=1) >= max(2, ink.shape[1]*.004)
                bands = []; start = None
                for y, value in enumerate(list(occupied)+[False]):
                    if value and start is None: start = y
                    elif not value and start is not None:
                        if y-start >= 3: bands.append((start, y))
                        start = None
                if len(bands) != len(paragraphs): return None
            parsed = BeautifulSoup('<br/>'.join(paragraphs), 'html.parser')
        if (not parsed.get_text(strip=True) or parsed.find(['img', 'svg', 'table', 'script', 'style'])
                or '\ufffd' in parsed.get_text()):
            return None
        return str(parsed), dict(method='primary_surya_on_exact_source_cell', source_bbox=list(box),
            source_pixel_bbox=list(pixels), response_sha256=saved['response_sha256'], image_sha256=key,
            page=page, row=cell['row'], column=cell['column'])
    finally:
        crop.close()


def _detail_response(args, crop, page, cell, api, kind, max_tokens=256):
    """Cache a different source view; never edit an earlier model response."""
    key = hashlib.sha256(f'primary-{kind}:{SCHEMA}:{crop.size}:'.encode()+crop.convert('RGB').tobytes()).hexdigest()
    root = Path(getattr(args, 'primary_table_recovery_cache', None) or args.ocr_cache_dir/'primary_cells')
    path = root/(kind+'-'+key+'.json')
    if path.exists():
        saved = json.loads(path.read_text('utf-8'))
        if (saved.get('schema') != SCHEMA or saved.get('image_sha256') != key
                or hashlib.sha256(saved.get('raw', '').encode()).hexdigest() != saved.get('response_sha256')):
            raise ValueError('Invalid primary source-detail response cache')
    else:
        model = getattr(args, 'surya_ocr_model', None)
        if model is None:
            return None
        from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
        started = monotonic()
        api.diagnostic_event('PRIMARY_SOURCE_DETAIL_REQUEST',page=page,row=cell['row'],column=cell['column'],kind=kind)
        outputs=api.local_surya_backend(model).generate([BatchInputItem(image=crop,prompt_type=PROMPT_TYPE_BLOCK,
            max_tokens=max_tokens,metadata=dict(page=page,element='source_table_cell',row=cell['row'],column=cell['column'],reading=kind))])
        if len(outputs)!=1 or outputs[0].error or outputs[0].token_count>=max_tokens:
            return None
        saved=dict(schema=SCHEMA,image_sha256=key,raw=outputs[0].raw,
            response_sha256=hashlib.sha256(outputs[0].raw.encode()).hexdigest(),
            token_count=outputs[0].token_count,elapsed=round(monotonic()-started,3))
        root.mkdir(parents=True,exist_ok=True);api.write_atomic(path,json.dumps(saved,ensure_ascii=False,indent=2))
    return saved


def _ink_view(crop):
    import cv2
    import numpy as np
    mask=(np.asarray(crop.convert('L'))<180).astype('uint8')
    count, labels, stats, _=cv2.connectedComponentsWithStats(mask,8)
    for index in range(1,count):
        x,y,w,h,area=stats[index]
        if (w>=.8*crop.width and h<=max(3,crop.height*.06)
                or h>=.8*crop.height and w<=max(3,crop.width*.01)):
            mask[labels==index]=0
    return mask


def _prefix_view(crop):
    """Locate the first printed token using a source whitespace boundary."""
    import numpy as np
    mask=_ink_view(crop)
    occupied=mask.sum(axis=1)>=max(2,crop.width*.004)
    bands=[];start=None
    for y,value in enumerate(list(occupied)+[False]):
        if value and start is None:start=y
        elif not value and start is not None:
            if y-start>=3:bands.append((start,y))
            start=None
    if not bands:return None
    top,bottom=bands[0];xs=np.flatnonzero(mask[top:bottom].any(axis=0));runs=[]
    for x in xs:
        if not runs or x>runs[-1][-1]+1:runs.append([int(x)])
        else:runs[-1].append(int(x))
    if not runs:return None
    end=0
    while end+1<len(runs) and runs[end+1][0]-runs[end][-1]-1<=max(3,(bottom-top)*.4):end+=1
    left,right=runs[0][0],runs[end][-1]+1
    if right-left>16*(bottom-top):return None
    box=(max(0,left-5),max(0,top-5),min(crop.width,right+5),min(crop.height,bottom+5))
    image=crop.crop(box)
    scaled=image.resize((image.width*3,image.height*3))
    image.close()
    return scaled,(left,top,right,bottom)


def _literal_prefix(saved):
    from surya.inference.parsers import clean_block_html
    parsed=BeautifulSoup(clean_block_html(saved['raw']),'html.parser')
    text=parsed.get_text(' ',strip=True)
    if not text and len(parsed.find_all('ol'))==1 and parsed.ol.has_attr('start'):
        text=str(parsed.ol['start'])
    return text if re.fullmatch(r'\d+(?:\.\d+)*(?:[.)])?',text) else None


def _arrow_prefix(crop, box):
    """Reject a source arrowhead before a numerical model proposal is used."""
    import cv2
    import numpy as np
    left,top,right,bottom=box
    view=crop.crop((left,top,right,bottom))
    try:mask=(np.asarray(view.convert('L'))<180).astype('uint8')
    finally:view.close()
    if not mask.size:return False
    _,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
    real=[s for s in stats[1:] if s[cv2.CC_STAT_AREA]>=max(5,int(mask.sum())*.12)]
    if len(real)!=1:return False
    h,w=mask.shape;stem=np.flatnonzero(mask.sum(axis=0)>=h*.6)
    if not len(stem):return False
    a,b=int(stem.min()),int(stem.max())
    extension=max(2,int(h*.12));upper=mask[:max(1,int(h*.55))]
    left_ink=np.count_nonzero(upper[:,:max(0,a-extension)])
    right_ink=np.count_nonzero(upper[:,min(w,b+extension+1):])
    return bool(left_ink>=max(3,mask.sum()*.035) and right_ink>=max(3,mask.sum()*.035))


def _prefix_response(args,view,page,cell,api):
    saved=_detail_response(args,view,page,cell,api,'literal-prefix',128)
    number=_literal_prefix(saved) if saved else None
    if number is None and saved and not BeautifulSoup(saved['raw'],'html.parser').get_text(strip=True):
        # BLOCK can encode a bare glyph as an empty default ordered list.
        # The implicit HTML marker is never taken as source evidence.
        # Repeat only the very same source pixels, separated by whitespace.
        # A literal "1 1 1" avoids the model's empty <ol><li> convention;
        # no synthetic numeral, expected sequence or row number is added.
        from PIL import Image
        gap=max(12,view.width)
        repeated=Image.new('RGB',(view.width*3+gap*2,view.height),'white')
        try:
            for index in range(3):repeated.paste(view,(index*(view.width+gap),0))
            reply=_detail_response(args,repeated,page,cell,api,'literal-prefix-repeat',256)
        finally:repeated.close()
        if reply:
            text=BeautifulSoup(reply['raw'],'html.parser').get_text(' ',strip=True)
            tokens=text.split()
            if len(tokens)==1 and len(text)%3==0:
                size=len(text)//3;tokens=[text[i*size:(i+1)*size] for i in range(3)]
            if (len(tokens)==3 and len(set(tokens))==1
                    and re.fullmatch(r'\d+(?:\.\d+)*(?:[.)])?',tokens[0])):
                number,saved=tokens[0],reply
    return number,saved


def _reading_is_short(parsed, crop):
    """Detect a partial prose response using independent source ink components."""
    import cv2
    mask=_ink_view(crop)
    _,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
    count=sum(s[cv2.CC_STAT_AREA]>=3 and s[cv2.CC_STAT_HEIGHT]>=3 for s in stats[1:])
    chars=sum(not c.isspace() for c in parsed.get_text())
    return count>=12 and chars/count<.65


def _complete_line_reading(args,crop,page,cell,api):
    """Cover every original text-line band when a whole-cell read is partial."""
    import numpy as np
    from surya.inference.parsers import clean_block_html
    mask=_ink_view(crop);occupied=mask.sum(axis=1)>=max(2,crop.width*.004)
    bands=[];start=None
    for y,value in enumerate(list(occupied)+[False]):
        if value and start is None:start=y
        elif not value and start is not None:
            if y-start>=3:bands.append([start,y])
            start=None
    if not bands:return None
    typical=float(np.median([b-a for a,b in bands]));joined=[]
    for band in bands:
        if joined and (band[1]-band[0]<typical*.45 or joined[-1][1]-joined[-1][0]<typical*.45) and band[0]-joined[-1][1]<typical*.6:
            joined[-1][1]=band[1]
        else:joined.append(band)
    contents=[];responses=[]
    for top,bottom in joined:
        xs=np.flatnonzero(mask[top:bottom].any(axis=0))
        if not len(xs):return None
        box=(max(0,int(xs.min())-6),max(0,top-6),min(crop.width,int(xs.max())+7),min(crop.height,bottom+6))
        line=crop.crop(box)
        try:
            scaled=line.resize((line.width*2,line.height*2))
            try:saved=_detail_response(args,scaled,page,cell,api,'complete-line',2048)
            finally:scaled.close()
            if saved is None:return None
            parsed=BeautifulSoup(clean_block_html(saved['raw']),'html.parser')
            if parsed.find('table'):
                profile=api.table_profile(str(parsed.table))
                if profile.get('issues') or profile['rows']!=1 or profile['columns']!=1:return None
                payload=parsed.table.find(['td','th']).decode_contents();parsed=BeautifulSoup(payload,'html.parser')
            if parsed.find(['img','svg','table','script','style']) or not parsed.get_text(strip=True):return None
            for ordered in list(parsed.find_all('ol')):
                children=ordered.find_all('li',recursive=False)
                if len(children)!=1:return None
                payload=children[0].decode_contents();text=children[0].get_text(' ',strip=True)
                if not re.match(r'^\s*\d+(?:\.\d+)*(?:[.)]|\s)',text):
                    prefix=_prefix_view(line)
                    if prefix is None:return None
                    view,_=prefix
                    try:number,prefix_saved=_prefix_response(args,view,page,cell,api)
                    finally:view.close()
                    if prefix_saved is None:return None
                    if number:
                        payload='<span>'+number+' </span>'+payload
                    else:
                        word=BeautifulSoup(clean_block_html(prefix_saved['raw']),'html.parser').get_text(' ',strip=True)
                        if not word or not text.casefold().startswith(word.casefold()):return None
                ordered.replace_with(BeautifulSoup(payload,'html.parser'))
            if _reading_is_short(parsed,line):return None
            contents.append(str(parsed));responses.append(saved['response_sha256'])
        finally:line.close()
    return '<br/>'.join(contents),responses


def _read_cell(args, source_image, region, cell, page, api):
    result=_read_cell_unverified(args,source_image,region,cell,page,api)
    parsed=BeautifulSoup(result[0],'html.parser') if result else None
    # An OCR-generated singleton list can silently turn any printed number
    # into its implicit marker "1". Only literal source pixels can restore it.
    singleton=(parsed.find('ol') if parsed and len(parsed.find_all('ol'))==1
               and len(parsed.ol.find_all('li',recursive=False))==1 else None)
    if result and cell.get('_source_cell_role')=='header':
        return result
    if singleton and re.match(r'^\s*\d+(?:\.\d+)*(?:[.)]|\s)',singleton.li.get_text(' ',strip=True)):
        payload=singleton.li.decode_contents();singleton.replace_with(BeautifulSoup(payload,'html.parser'))
        result=(str(parsed),result[1]);singleton=None
    sx,sy=source_image.width/(region[2]-region[0]),source_image.height/(region[3]-region[1]);b=cell['bbox']
    pixels=(max(0,math.ceil((b[0]-region[0]+.7)*sx)),max(0,math.ceil((b[1]-region[1]+.7)*sy)),
            min(source_image.width,math.floor((b[2]-region[0]-.7)*sx)),min(source_image.height,math.floor((b[3]-region[1]-.7)*sy)))
    crop=source_image.crop(pixels)
    try:
        if result and not singleton:
            if not _reading_is_short(parsed,crop):return result
            complete=_complete_line_reading(args,crop,page,cell,api)
            if complete is None:return None
            content,responses=complete;proof=dict(result[1])
            proof.update(complete_source_line_responses=responses,source_text_lines=len(responses))
            return content,proof
        prefix=_prefix_view(crop)
        if prefix is None:return None
        prefix_image,prefix_box=prefix
        if _arrow_prefix(crop,prefix_box):
            prefix_image.close();return None
        try:number,saved=_prefix_response(args,prefix_image,page,cell,api)
        finally:prefix_image.close()
        if number is None:return None
        payload=singleton.li.decode_contents() if singleton else None
        # A leading arrow can be the misread printed strokes of e.g. "11".
        # Read the prose independently after masking only the verified token.
        if payload is None or re.match(r'^\s*[^\wА-Яа-яЁё<(]',singleton.li.get_text(' ',strip=True)):
            from PIL import ImageDraw
            prose=crop.copy()
            try:
                left,top,right,bottom=prefix_box
                ImageDraw.Draw(prose).rectangle((max(0,left-2),max(0,top-2),min(prose.width,right+2),min(prose.height,bottom+2)),fill='white')
                mask=_ink_view(prose)
                import numpy as np
                ys,xs=np.nonzero(mask)
                if not len(xs):return None
                bounds=(max(0,int(xs.min())-7),max(0,int(ys.min())-7),min(prose.width,int(xs.max())+8),min(prose.height,int(ys.max())+8))
                tight=prose.crop(bounds)
                try:
                    scaled=tight.resize((tight.width*2,tight.height*2))
                    try:prose_saved=_detail_response(args,scaled,page,cell,api,'literal-prose',2048)
                    finally:scaled.close()
                finally:tight.close()
            finally:prose.close()
            if prose_saved is None:return None
            from surya.inference.parsers import clean_block_html
            rest=BeautifulSoup(clean_block_html(prose_saved['raw']),'html.parser')
            if rest.find(['img','svg','table','script','style']) or not rest.get_text(strip=True):return None
            for ordered in list(rest.find_all('ol')):
                items=ordered.find_all('li',recursive=False)
                if len(items)!=1:return None
                ordered.replace_with(BeautifulSoup(items[0].decode_contents(),'html.parser'))
            payload=str(rest)
        output=str(BeautifulSoup('<span>'+number+' </span>'+payload,'html.parser'))
        proof=dict(result[1]) if result else dict(method='primary_surya_on_exact_source_cell',source_bbox=list(b),source_pixel_bbox=list(pixels),page=page,row=cell['row'],column=cell['column'])
        proof.update(literal_prefix=number,literal_prefix_bbox=list(prefix_box),literal_prefix_response=saved['response_sha256'])
        api.diagnostic_event('PRIMARY_SOURCE_LITERAL_PREFIX',page=page,row=cell['row'],column=cell['column'],number=number)
        return output,proof
    finally:crop.close()


def recover_missing_mixed_text(native, source_image, region, page, args, manifest, api, *, reader=None):
    """Recover missing text cells on a proved physical mixed-table grid.

    Header crops identify illustration columns. Those columns' body pixels
    never reach text OCR. The physical rows, columns and spans remain intact.
    Forms are excluded by the caller before this table route.
    """
    if not native:
        return None
    fragment, original = native
    cells = original.get('cells') or []
    rows, cols = len(original.get('y_grid', []))-1, len(original.get('x_grid', []))-1
    if (not original.get('source_grid_verified') or rows < 2 or cols < 2 or not cells):
        return None
    # Physical spans define the initial header band even if the PDF text layer
    # did not recognize a single header word. Do not trust an inferred count
    # which can accidentally include body data on sparse tables.
    header_rows = max((c['row'] + c['row_span'] for c in cells if c['row'] == 0), default=1)
    cursor = 0
    while cursor < header_rows:
        header_rows = max([header_rows] + [c['row'] + c['row_span'] for c in cells if c['row'] == cursor])
        cursor += 1
    if header_rows >= rows:
        return None
    geometry = deepcopy(original)
    physical = {tuple(c[k] for k in POSITION): c for c in geometry['cells']}
    soup = BeautifulSoup(fragment, 'html.parser')
    profile = api.table_profile(fragment)
    nodes = {tuple(c[k] for k in POSITION): n for c, n in zip(profile['cells'], soup.table.find_all(['td', 'th']))}
    if set(nodes) != set(physical):
        return None
    from docling_pdf_text_cells import owns_positioned_text, positioned_text_proof

    source_mask = None
    def source_reading(cell):
        nonlocal source_mask
        if reader is None or original.get('hidden_ocr_direction_conflict') or cell.get('source_text_unreliable'):
            return None
        reader.load_page(page)
        box = cell['bbox']
        glyphs = [g for g in reader.chars if box[0] <= (g['x0']+g['x1'])/2 <= box[2]
                  and box[1] <= (g['top']+g['bottom'])/2 <= box[3]]
        if not glyphs:
            return None
        content = '<br/>'.join(line['html'] for line in api.native_cell_lines(glyphs))
        if source_mask is None:
            import cv2
            import numpy as np
            gray = np.asarray(source_image.convert('L'))
            source_mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
        sx, sy = source_image.width/(region[2]-region[0]), source_image.height/(region[3]-region[1])
        proof = positioned_text_proof(content, glyphs, source_mask, region, box, sx, sy)
        return (content, proof) if proof else None

    def proved_text(key):
        cell, node = physical[key], nodes[key]
        return bool(node.get_text(strip=True) and not node.find('img') and (
            owns_positioned_text(node, cell, original) or cell.get('primary_ocr')))

    def has_payload(key):
        node = nodes[key]
        return bool(node.get_text(strip=True) or node.find(['img', 'math']))

    proposals, unresolved = {}, []
    try:
        for key, c in physical.items():
            if c['row'] < header_rows:
                if proved_text(key) or not has_payload(key):
                    continue
                reading = source_reading(c) or _read_cell(args, source_image, region, dict(c,_source_cell_role='header'), page, api)
                if reading is None:
                    unresolved.append(dict(row=c['row'], column=c['column'], bbox=list(c['bbox']), role='header'))
                    continue
                proposals[key] = reading
        graphic_columns, text_columns = set(), set()
        for key, c in physical.items():
            if c['row'] >= header_rows:
                continue
            content = proposals[key][0] if key in proposals else nodes[key].decode_contents() if proved_text(key) else ''
            label = BeautifulSoup(content, 'html.parser').get_text(' ', strip=True)
            if TEXT_HEADING.search(label):
                text_columns.update(range(key[1], key[1]+key[3]))
            elif GRAPHIC_HEADING.search(label):
                graphic_columns.update(range(key[1], key[1]+key[3]))
        graphic_columns -= text_columns
        if not graphic_columns or len(graphic_columns) == cols:
            if unresolved:
                manifest.setdefault('unresolved_primary_text_cells', []).extend(dict(page=page, **item) for item in unresolved)
            return None
        aggregate = _whole_table_proposals(args, source_image, region, physical, header_rows,
                                           graphic_columns, page, api)
        for key, c in physical.items():
            if c['row'] < header_rows:
                continue
            if proved_text(key) or not has_payload(key):
                continue
            full_width = c['column'] == 0 and c['column_span'] == cols
            if graphic_columns.intersection(range(c['column'], c['column']+c['column_span'])):
                if not full_width:
                    continue
                reading = source_reading(c)
                if reading is None and _cell_text_ink(source_image, region, c['bbox']):
                    reading = _read_cell(args, source_image, region, c, page, api)
                if reading is None:
                    continue  # A wide drawing is never converted by row position.
            else:
                reading = source_reading(c) or aggregate.get(key) or _read_cell(args, source_image, region, c, page, api)
            if reading is None:
                unresolved.append(dict(row=c['row'], column=c['column'], bbox=list(c['bbox']), role='body'))
                continue
            proposals[key] = reading
    except Exception as error:
        api.diagnostic_event('PRIMARY_TABLE_CELL_RECOVERY_UNAVAILABLE', page=page,
                             error_type=type(error).__name__, error=str(error))
        return None
    if unresolved:
        manifest.setdefault('unresolved_primary_text_cells', []).extend(dict(page=page, **item) for item in unresolved)
    if not proposals and original.get('header_rows') == header_rows:
        return None
    for key, (content, proof) in proposals.items():
        node = nodes[key]
        node.clear(); node.append(BeautifulSoup(content, 'html.parser'))
        if key[0] < header_rows: node.name = 'th'
        physical[key].update(html=content, text=node.get_text(' ', strip=True))
        if proof.get('basis') == 'positioned_pdf_text_covers_source_cell_ink':
            physical[key]['positioned_source_text'] = proof
            manifest.setdefault('positioned_source_text_cells_recovered', []).append(dict(page=page, row=key[0], column=key[1], **proof))
        else:
            physical[key]['primary_ocr'] = proof
            manifest.setdefault('primary_source_cell_crops_preserved', []).append(proof)
        if key[0] + key[2] == rows and key[1] == 0 and key[3] == cols and NOTE_START.match(node.get_text(' ', strip=True)):
            node['data-table-note'] = 'true'
    for key, node in nodes.items():
        if key[0] < header_rows:
            node.name = 'th'
            physical[key]['header'] = True
        else:
            physical[key]['header'] = False
    header = soup.table.find('thead',recursive=False)
    if header is None:
        header = soup.new_tag('thead')
        caption = soup.table.find('caption',recursive=False)
        if caption:caption.insert_after(header)
        else:soup.table.insert(0,header)
    header_nodes=[row for row in soup.table.find_all('tr') if row.find_parent('table') is soup.table][:header_rows]
    for row in header_nodes:
        if row.parent is not header:
            parent=row.parent;header.append(row.extract())
            if parent.name in {'thead','tbody'} and not parent.find('tr'):parent.decompose()
    positions = {key[:2] for key in proposals}
    geometry['retained_cell_images'] = [c for c in geometry.get('retained_cell_images', [])
                                        if (c['row'], c['column']) not in positions]
    geometry['header_rows'] = header_rows
    geometry['text_method'] = 'primary_surya_source_cell_crops'
    result = str(soup.table)
    if api.table_profile(result)['issues']:
        return None
    api.diagnostic_event('PRIMARY_TABLE_SOURCE_CELLS_RECOVERED', page=page,
                         cells=len(proposals), graphic_columns=sorted(graphic_columns))
    return result, geometry
