"""Read missing mixed-table text on exact physical cell crops."""
from copy import deepcopy
import hashlib
import math
import re
from time import monotonic

from bs4 import BeautifulSoup

from docling_pdf_ocr import output_within_request_budget

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
        key = hashlib.sha256(f'primary-cell:{crop.size}:'.encode()+crop.convert('RGB').tobytes()).hexdigest()
        model = getattr(args, 'surya_ocr_model', None)
        if model is None:
            return None
        from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
        started = monotonic()
        api.diagnostic_event('PRIMARY_TABLE_CELL_REQUEST', page=page, row=cell['row'], column=cell['column'],
                             bbox=box, source_pixel_bbox=list(pixels), max_tokens=2048)
        outputs = api.local_surya_backend(model).generate([BatchInputItem(image=crop,
            prompt_type=PROMPT_TYPE_BLOCK, max_tokens=2048,
            metadata=dict(page=page, element='source_table_cell', row=cell['row'], column=cell['column']))])
        if len(outputs) != 1 or not output_within_request_budget(outputs[0], 2048):
            raise RuntimeError('Primary source-cell OCR did not finish')
        saved = dict(image_sha256=key, raw=outputs[0].raw,
                     response_sha256=hashlib.sha256(outputs[0].raw.encode()).hexdigest(),
                     token_count=outputs[0].token_count, elapsed=round(monotonic()-started, 3))
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
    """Read a different source view without altering an earlier model response."""
    model = getattr(args, 'surya_ocr_model', None)
    if model is None:
        return None
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
    key = hashlib.sha256(f'primary-{kind}:{crop.size}:'.encode()+crop.convert('RGB').tobytes()).hexdigest()
    started = monotonic()
    api.diagnostic_event('PRIMARY_SOURCE_DETAIL_REQUEST', page=page, row=cell['row'], column=cell['column'], kind=kind)
    outputs = api.local_surya_backend(model).generate([BatchInputItem(image=crop, prompt_type=PROMPT_TYPE_BLOCK,
        max_tokens=max_tokens, metadata=dict(page=page, element='source_table_cell', row=cell['row'],
                                           column=cell['column'], reading=kind))])
    if len(outputs) != 1 or not output_within_request_budget(outputs[0], max_tokens):
        return None
    return dict(image_sha256=key, raw=outputs[0].raw,
        response_sha256=hashlib.sha256(outputs[0].raw.encode()).hexdigest(),
        token_count=outputs[0].token_count, elapsed=round(monotonic()-started, 3))


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


def _complete_line_reading(args,crop,page,cell,api, *, allow_fast=False):
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
    boxes=[]
    for top,bottom in joined:
        xs=np.flatnonzero(mask[top:bottom].any(axis=0))
        if not len(xs):return None
        boxes.append((max(0,int(xs.min())-6),max(0,top-6),
                      min(crop.width,int(xs.max())+7),min(crop.height,bottom+6)))
    fast={}
    if allow_fast:
        from docling_pdf_grid_ocr import grid_batch_enabled, read_confirmed_lines, source_cell_ink
        if grid_batch_enabled(args) and not ((source_cell_ink(crop) != 0) & (mask == 0)).any():
            copies=[crop.crop(box) for box in boxes]
            started=monotonic()
            try:
                fast=read_confirmed_lines(copies,line_ink=source_cell_ink)
            except Exception as error:
                api.diagnostic_event('GRID_CELL_LINE_BATCH_FAILED',page=page,error=str(error))
            finally:
                for copy in copies:copy.close()
            stats=getattr(args,'grid_cell_line_ocr',None)
            if stats is None:
                stats=args.grid_cell_line_ocr={'batches':0,'candidates':0,'accepted_lines':0,'seconds':0.}
            stats['batches']+=1
            stats['candidates']+=len(boxes)
            stats['accepted_lines']+=len(fast)
            stats['seconds']=round(stats['seconds']+monotonic()-started,4)
            api.diagnostic_event('GRID_CELL_LINE_BATCH',page=page,row=cell['row'],
                                 column=cell['column'],accepted=len(fast),candidates=len(boxes))
    contents=[];responses=[];methods=[]
    for index, box in enumerate(boxes):
        if index in fast:
            contents.append(fast[index])
            responses.append(hashlib.sha256(fast[index].encode()).hexdigest())
            methods.append('occular')
            continue
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
            methods.append('surya')
        finally:line.close()
    return '<br/>'.join(contents),responses,methods


def _read_cell(args, source_image, region, cell, page, api):
    result=_read_cell_unverified(args,source_image,region,cell,page,api)
    parsed=BeautifulSoup(result[0],'html.parser') if result else None
    # An absent editable response can mean a drawing. Prefix/prose recovery
    # is justified only by an explicitly textual source-column role.
    if result is None and cell.get('_source_cell_role') != 'text':
        return None
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
            complete=_complete_line_reading(args,crop,page,cell,api,
                allow_fast=cell.get('_source_cell_role')=='text' and not parsed.find(['math','sup','sub']))
            if complete is None:return None
            content,responses,methods=complete;proof=dict(result[1])
            proof.update(complete_source_line_responses=responses,source_text_lines=len(responses))
            if 'occular' in methods:
                proof['complete_source_line_methods'] = methods
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


def graft_complete_primary_cells(native, source_image, region, page, args, manifest, api):
    """Reuse complete primary packets on their proved physical source cells."""
    from collections import Counter
    from docling_pdf_table_content import (_retained_blocks, _source_crop, _render_typed,
        _typed_payload_digest, _is_source_graphic_header, _has_source_authority,
        _native_formula_fragments, _require_native_formula_atoms,
        review_primary_cell_conflict, TableContentError)

    if not native or source_image is None or not getattr(args, 'surya_records', None):
        return None
    fragment, original = native
    if not original.get('source_grid_verified'):
        return None
    soup = BeautifulSoup(fragment, 'html.parser')
    if soup.table is None:
        return None
    profile = api.table_profile(fragment)
    if profile['issues']:
        return None
    expected = Counter(tuple(cell.get(k) for k in POSITION) for cell in original.get('cells', []))
    actual = Counter(tuple(cell[k] for k in POSITION) for cell in profile['cells'])
    if expected != actual or len(original.get('x_grid', [])) != profile['columns']+1:
        return None
    xs, ys = original.get('x_grid', []), original.get('y_grid', [])
    if (len(ys) != profile['rows']+1 or any(not math.isfinite(v) for v in xs+ys)
            or any(b <= a for axis in (xs, ys) for a, b in zip(axis, axis[1:]))):
        return None
    epsilon = max((region[2]-region[0])/source_image.width, (region[3]-region[1])/source_image.height)+1e-7
    for cell in original['cells']:
        row, col, rs, cs = (cell[k] for k in POSITION)
        wanted = [xs[col], ys[row], xs[col+cs], ys[row+rs]]
        if len(cell.get('bbox', [])) != 4 or any(abs(a-b) > epsilon for a, b in zip(cell['bbox'], wanted)):
            return None
    geometry = deepcopy(original)
    physical = {tuple(cell[k] for k in POSITION): cell for cell in geometry['cells']}
    nodes = {tuple(cell[k] for k in POSITION): node for cell, node in zip(profile['cells'],
        (n for n in soup.table.find_all(['td', 'th']) if n.find_parent('table') is soup.table))}
    graphic_columns = set()
    for position in profile['cells']:
        key = tuple(position[k] for k in POSITION)
        if (position.get('header') and position['column_span'] < profile['columns']
                and _is_source_graphic_header(nodes[key].get_text(' ', strip=True))):
            graphic_columns.update(range(position['column'], position['column']+position['column_span']))
    candidates, owners = {}, {}
    for key, cell in physical.items():
        node = nodes[key]
        if ((_has_source_authority(node, cell, original) and not _native_formula_fragments(node, cell))
                or node.find('table') or node.select_one('[data-cell-content="source-diagram"]')
                or (cell['row'] >= geometry.get('header_rows', 1)
                    and cell['column_span'] < profile['columns']
                    and graphic_columns.intersection(range(cell['column'], cell['column']+cell['column_span'])))):
            continue
        packets = _retained_blocks(args, page, cell['bbox'])
        if not packets:
            continue
        crop = None
        try:
            crop = _source_crop(source_image, region, cell['bbox'])
            rendered = _render_typed(crop, packets, cell['bbox'][2]-cell['bbox'][0],
                page, {k: cell[k] for k in POSITION}, {}, api, physical_cell=True, require_coverage=True)
            source_fragments = _native_formula_fragments(node, cell)
            if source_fragments:
                _require_native_formula_atoms(node.decode_contents(), rendered, api, cell=cell)
            proof = dict(method='complete_primary_packets_on_verified_physical_cell',
                page=page, **{k: cell[k] for k in POSITION}, source_bbox=list(cell['bbox']),
                native_formula_reassembled=source_fragments,
                source_image_sha256=hashlib.sha256(crop.convert('RGB').tobytes()).hexdigest(),
                rendered_payload_sha256=_typed_payload_digest(rendered),
                packets=[{k: packet[k] for k in ('source_bbox', 'source_record_sha256', 'source_payload_sha256')}
                         for packet in packets])
        except TableContentError:
            continue  # Missing content follows the normal targeted recovery path.
        finally:
            if crop is not None:
                crop.close()
        candidates[key] = rendered, proof
        for packet in proof['packets']:
            identity = (tuple(packet['source_bbox']), packet['source_record_sha256'])
            owners.setdefault(identity, []).append(key)
    reused = []
    for key, (rendered, proof) in candidates.items():
        if any(len(owners[(tuple(packet['source_bbox']), packet['source_record_sha256'])]) != 1
               for packet in proof['packets']):
            continue  # Adjacent/overlapping cells may not both own a packet.
        cell, node = physical[key], nodes[key]
        if not proof.get('native_formula_reassembled'):
            review_primary_cell_conflict(node.decode_contents(), rendered, page, cell, cell['bbox'], manifest, api)
        review_primary_cell_conflict(cell.get('pending_primary_html', ''), rendered, page, cell, cell['bbox'], manifest, api)
        node.clear()
        node.append(BeautifulSoup(rendered, 'html.parser'))
        cell.update(html=rendered, text=node.get_text(' ', strip=True),
                    primary_ocr=proof, primary_packet_graft=proof)
        cell.pop('pending_primary_html', None)
        cell.pop('typed_content_recovery_required', None)
        reused.append(proof)
        api.diagnostic_event('PRIMARY_TABLE_COMPLETE_PACKETS_REUSED', page=page,
                             row=key[0], column=key[1], packets=len(proof['packets']))
    if not reused:
        return None
    positions = {(proof['row'], proof['column']) for proof in reused}
    geometry['retained_cell_images'] = [entry for entry in geometry.get('retained_cell_images', [])
                                       if (entry['row'], entry['column']) not in positions]
    manifest.setdefault('complete_primary_source_cells_reused', []).extend(reused)
    return str(soup.table), geometry


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
    grafted = graft_complete_primary_cells(native, source_image, region, page, args, manifest, api)
    if grafted:
        native = grafted
        fragment, original = native
        cells = original['cells']
    # Physical spans define the initial header band even if the PDF text layer
    # did not recognize a single header word. Do not trust an inferred count
    # which can accidentally include body data on sparse tables.
    header_rows = max((c['row'] + c['row_span'] for c in cells if c['row'] == 0), default=1)
    cursor = 0
    while cursor < header_rows:
        header_rows = max([header_rows] + [c['row'] + c['row_span'] for c in cells if c['row'] == cursor])
        cursor += 1
    if header_rows >= rows:
        return grafted
    geometry = deepcopy(original)
    physical = {tuple(c[k] for k in POSITION): c for c in geometry['cells']}
    soup = BeautifulSoup(fragment, 'html.parser')
    profile = api.table_profile(fragment)
    nodes = {tuple(c[k] for k in POSITION): n for c, n in zip(profile['cells'],
        (node for node in soup.table.find_all(['td', 'th']) if node.find_parent('table') is soup.table))}
    if set(nodes) != set(physical):
        return None
    from docling_pdf_text_cells import positioned_text_proof
    from docling_pdf_table_content import _has_source_authority

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
            _has_source_authority(node, cell, original) or cell.get('primary_ocr')))

    def has_payload(key):
        node = nodes[key]
        return bool(node.get_text(strip=True) or node.find(['img', 'math']))

    proposals, unresolved = {}, []
    from docling_pdf_grid_ocr import grid_batch_enabled, read_confirmed_cells, source_cell_ink
    use_batch = grid_batch_enabled(args)
    fast, source_proofs = {}, {}

    def prepare_batch(keys, role):
        if not use_batch:
            return
        eligible = []
        for key in keys:
            cell, node = physical[key], nodes[key]
            if (node.find(['math', 'sup', 'sub', 'table', 'input', 'svg'])
                    or node.select_one('[data-cell-content="source-diagram"]')):
                continue
            assets = [asset for asset in original.get('retained_cell_images', [])
                      if (asset.get('row'), asset.get('column')) == key[:2]]
            images = node.find_all('img')
            if images or assets:
                if (len(images) != 1 or len(assets) != 1
                        or images[0].get('data-source-cell') != 'true'
                        or node.get_text(strip=True)
                        or assets[0].get('basis') not in {
                            'nonempty_scan_cell_without_positioned_text',
                            'visible_scan_pixels_without_hidden_ocr_authority'}
                        or assets[0].get('mixed_source_html')
                        or assets[0].get('source_column_context', {}).get('graphic_column')
                        or assets[0].get('source_column_context', {}).get('dimension_column')
                        or any((asset.get('row'), asset.get('column')) == key[:2]
                               for asset in original.get('source_graphic_assets', []))):
                    continue
                if (assets[0].get('basis') == 'visible_scan_pixels_without_hidden_ocr_authority'
                        and (not original.get('page_scan_transcription_required')
                             or not cell.get('scan_layout_has_ink')
                             or assets[0].get('bbox') != list(cell['bbox']))):
                    continue
            elif cell.get('source_text_unreliable'):
                continue
            prior = BeautifulSoup(cell.get('pending_primary_html') or '', 'html.parser')
            if prior.find(['math', 'sup', 'sub', 'table', 'input', 'svg', 'img']):
                continue
            positioned = source_reading(cell)
            if positioned:
                source_proofs[key] = positioned
            else:
                reference = prior.get_text(' ', strip=True)
                if not reference and not cell.get('source_text_unreliable'):
                    reference = node.get_text(' ', strip=True)
                eligible.append((cell, role, reference))
        if not eligible:
            return
        started = monotonic()
        try:
            readings = read_confirmed_cells(source_image, region, eligible, page, line_ink=source_cell_ink)
        except Exception as error:
            api.diagnostic_event('GRID_CELL_BATCH_FAILED', page=page, role=role, error=str(error))
            return
        if role == 'header':
            readings = {key: reading for key, reading in readings.items()
                        if any(pattern.search(BeautifulSoup(reading[0], 'html.parser').get_text(' ', strip=True))
                               for pattern in (TEXT_HEADING, GRAPHIC_HEADING))}
        fast.update(readings)
        ledger = manifest.setdefault('grid_table_ocr', {'batches': 0, 'candidates': 0, 'accepted_cells': 0, 'seconds': 0.})
        ledger['batches'] += 1
        ledger['candidates'] += len(eligible)
        ledger['seconds'] = round(ledger['seconds'] + monotonic() - started, 4)
        api.diagnostic_event('GRID_CELL_BATCH', page=page, role=role,
                             candidates=len(eligible), accepted=len(readings))

    def read_cell(cell, role):
        key = tuple(cell[k] for k in POSITION)
        try:
            return (source_proofs.get(key) or fast.get(key) or source_reading(cell)
                    or _read_cell(args, source_image, region,
                                  dict(cell, _source_cell_role=role), page, api))
        except RuntimeError as error:
            if not (str(error).startswith('Local block request failed after retry:')
                    or str(error) == 'Primary source-cell OCR did not finish'):
                raise
            # One unresolved crop must not discard already proved cells or
            # prevent later independent source cells from being recovered.
            api.diagnostic_event('PRIMARY_TABLE_CELL_RECOVERY_UNAVAILABLE', page=page,
                row=cell['row'], column=cell['column'], error_type=type(error).__name__, error=str(error))
            return None

    headers = [key for key, c in physical.items()
               if c['row'] < header_rows and not proved_text(key) and has_payload(key)]
    prepare_batch(headers, 'header')
    for key in headers:
        c = physical[key]
        reading = read_cell(c, 'header')
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
    if graphic_columns and len(graphic_columns) < cols:
        # Unnamed/value columns may contain dimensioned drawings. Only a
        # textual header authorizes prose OCR; other cells keep their typed
        # source payload for the content resolver.
        body_text = [key for key, c in physical.items()
                     if c['row'] >= header_rows and not proved_text(key) and has_payload(key)
                     and set(range(c['column'], c['column']+c['column_span'])).issubset(text_columns)]
        prepare_batch(body_text, 'text')
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
                    reading = read_cell(c, 'text')
                if reading is None:
                    continue  # A wide drawing is never converted by row position.
            else:
                occupied = set(range(c['column'], c['column']+c['column_span']))
                reading = read_cell(c, 'text') if occupied.issubset(text_columns) else source_reading(c)
            if reading is None:
                unresolved.append(dict(row=c['row'], column=c['column'], bbox=list(c['bbox']), role='body'))
                continue
            proposals[key] = reading
    if unresolved:
        manifest.setdefault('unresolved_primary_text_cells', []).extend(dict(page=page, **item) for item in unresolved)
    if not proposals and original.get('header_rows') == header_rows:
        return grafted
    for key, (content, proof) in proposals.items():
        node = nodes[key]
        node.clear(); node.append(BeautifulSoup(content, 'html.parser'))
        if key[0] < header_rows: node.name = 'th'
        physical[key].update(html=content, text=node.get_text(' ', strip=True))
        if proof.get('basis') == 'positioned_pdf_text_covers_source_cell_ink':
            physical[key]['positioned_source_text'] = proof
        else:
            physical[key]['primary_ocr'] = proof
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
    geometry['text_method'] = ('verified_source_cell_crops' if fast else 'primary_surya_source_cell_crops')
    result = str(soup.table)
    checked = api.table_profile(result)
    if checked['issues'] or {tuple(c[k] for k in POSITION) for c in checked['cells']} != set(physical):
        return grafted
    if fast:
        manifest['grid_table_ocr']['accepted_cells'] += len(fast.keys() & proposals.keys())
    for key, (_, proof) in proposals.items():
        if proof.get('basis') == 'positioned_pdf_text_covers_source_cell_ink':
            manifest.setdefault('positioned_source_text_cells_recovered', []).append(
                dict(page=page, row=key[0], column=key[1], **proof))
        else:
            manifest.setdefault('primary_source_cell_crops_preserved', []).append(proof)
    api.diagnostic_event('PRIMARY_TABLE_SOURCE_CELLS_RECOVERED', page=page,
                         cells=len(proposals), graphic_columns=sorted(graphic_columns))
    return result, geometry
