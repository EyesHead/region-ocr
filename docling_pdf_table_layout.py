"""Logical tables backed by source rails, empty bands and repeated headers."""
from collections import Counter
from hashlib import sha256
from io import BytesIO
import re
from statistics import median

from bs4 import BeautifulSoup
import cv2
import numpy as np
from PIL import Image,ImageOps


def grid_ink(image,region,xs):
    """Separate only measured table-rule components from the comparison ink."""
    gray=np.asarray(image.convert('L'))
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    height,width=ink.shape
    sx,sy=width/(region[2]-region[0]),height/(region[3]-region[1])
    _,_,stats,_=cv2.connectedComponentsWithStats(ink,8)
    heights=[int(h) for x,y,w,h,area in stats[1:] if 1<h<height*.8 and 1<w<width]
    if not heights:return None
    glyph_height=median(heights)
    radius=max(1,round(float(np.median(cv2.distanceTransform(ink,cv2.DIST_L2,3)[ink>0]))))
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(3,round(3*glyph_height))),np.uint8))
    vertical=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(3,round(3*glyph_height)),1),np.uint8))
    projected=np.array([(x-region[0])*sx for x in xs])
    rules=np.zeros_like(ink);full=[]
    count,labels,boxes,_=cv2.connectedComponentsWithStats(horizontal,8)
    for i,(x,y,w,h,area) in enumerate(boxes[1:],1):
        if min(abs(projected-x))>2*radius+2 or min(abs(projected-(x+w)))>2*radius+2:continue
        rules[labels==i]=255
        if w>=.94*(xs[-1]-xs[0])*sx:
            full.append(region[1]+(y+h/2)/sy)
    for x in projected:
        l=max(0,round(x)-2*radius-1);r=min(width,round(x)+2*radius+2)
        rules[:,l:r]=cv2.bitwise_or(rules[:,l:r],vertical[:,l:r])
    # Only original-ink components connected to a measured rule may supply
    # its one-stroke antialiasing fringe. Mere proximity is insufficient:
    # a separate dot/letter inside the band must continue to block deletion.
    _,ink_labels,_,_=cv2.connectedComponentsWithStats(ink,8)
    touching=np.unique(ink_labels[rules>0]);touching=touching[touching!=0]
    near=cv2.dilate(rules,np.ones((2*radius+1,2*radius+1),np.uint8))
    fringe=(ink>0)&(near>0)&np.isin(ink_labels,touching)
    rules[fringe]=255
    nongrid=cv2.bitwise_and(ink,cv2.bitwise_not(rules))
    return dict(ink=ink,nongrid=nongrid,rules=rules,glyph_height=glyph_height/sy,
                stroke_radius=radius,full_width_rules=sorted(full),sx=sx,sy=sy)


def empty_rule_bands(fragment,geometry,image,region,glyphs,api):
    """Delete zero-payload rows only between two close full-width source rules."""
    soup=BeautifulSoup(fragment,'html.parser');table=soup.table
    if table is None:return fragment,None
    profile=api.table_profile(str(table));xs=geometry.get('x_grid',[]);ys=geometry.get('y_grid',[])
    if profile['issues'] or profile['rows']!=len(ys)-1 or len(xs)-1!=profile['columns']:return fragment,None
    rows=table.find_all('tr');nodes=table.find_all(['td','th'])
    candidates=[r for r,row in enumerate(rows) if not row.get_text(strip=True) and not row.find(['img','math','svg','table'])]
    if not candidates:return fragment,None
    source=grid_ink(image,region,xs)
    if source is None:return fragment,None
    removed=[]
    for row in candidates:
        top,bottom=ys[row:row+2]
        if bottom-top>=source['glyph_height']:continue
        if any(not any(abs(y-rule)<=max(1/source['sy'],source['stroke_radius']/source['sy'])
                       for rule in source['full_width_rules']) for y in (top,bottom)):continue
        if any(g.get('text','').strip() and top<=(g['top']+g['bottom'])/2<bottom
               and xs[0]<=(g['x0']+g['x1'])/2<=xs[-1] for g in glyphs):continue
        y0=max(0,round((top-region[1])*source['sy']));y1=min(image.height,round((bottom-region[1])*source['sy'])+1)
        x0=max(0,round((xs[0]-region[0])*source['sx']));x1=min(image.width,round((xs[-1]-region[0])*source['sx'])+1)
        residual=int(np.count_nonzero(source['nongrid'][y0:y1,x0:x1]))
        if residual:continue
        removed.append(dict(row=row,bbox=[xs[0],top,xs[-1],bottom],nongrid_ink_pixels=0,positioned_glyphs=0))
    if not removed:return fragment,None
    deleted={r['row'] for r in removed}
    before=[n.decode_contents() for n in nodes if n.get_text(strip=True) or n.find(['img','math','svg','table'])]
    groups={i:(row.find_parent(['thead','tbody','tfoot']).name if row.find_parent(['thead','tbody','tfoot']) else 'tbody')
            for i,row in enumerate(rows) if i not in deleted}
    placements=[]
    for cell,node in zip(profile['cells'],nodes):
        start,end=cell['row'],cell['row']+cell['row_span']
        kept=[i for i in range(start,end) if i not in deleted]
        if not kept:
            if node.get_text(strip=True) or node.find(['img','math','svg','table']):return fragment,None
            continue
        node.extract()
        if len(kept)==1:node.attrs.pop('rowspan',None)
        else:node['rowspan']=str(len(kept))
        placements.append((kept[0],cell['column'],node))
    for old in rows:old.decompose()
    for group in table.find_all(['thead','tbody','tfoot'],recursive=False):group.decompose()
    for old_row in groups:
        name=groups[old_row];group=table.find(name,recursive=False)
        if group is None:group=soup.new_tag(name);table.append(group)
        tr=soup.new_tag('tr')
        for _,_,node in sorted((p for p in placements if p[0]==old_row),key=lambda p:p[1]):tr.append(node)
        group.append(tr)
    after=[n.decode_contents() for n in table.find_all(['td','th']) if n.get_text(strip=True) or n.find(['img','math','svg','table'])]
    result=str(table);checked=api.table_profile(result)
    if before!=after or checked['issues'] or checked['columns']!=profile['columns']:return fragment,None
    return result,dict(method='empty_band_between_close_complete_source_rules',removed=removed,
        source_glyph_height=source['glyph_height'],source_rows=profile['rows'],logical_rows=checked['rows'],payloads_unchanged=True)


def compact_row_geometry(geometry,proof):
    """Keep logical source rows aligned after dropping a physical rule gap."""
    deleted={r['row'] for r in proof['removed']}
    ys=geometry.get('y_grid',[])
    if len(ys)!=proof['source_rows']+1:return
    geometry.setdefault('physical_y_grid',list(ys))
    # Assign the zero-content rail to the preceding row's source envelope;
    # all following source coordinates remain physical PDF coordinates.
    geometry['y_grid']=[y for i,y in enumerate(ys) if i not in deleted]
    cells=[]
    for cell in geometry.get('cells',[]):
        start=cell['row'];end=start+cell['row_span']
        kept=[i for i in range(start,end) if i not in deleted]
        if not kept:continue
        c=dict(cell)
        c.setdefault('physical_row',start);c.setdefault('physical_row_span',cell['row_span'])
        c['row']=kept[0]-sum(r<kept[0] for r in deleted)
        c['row_span']=len(kept);cells.append(c)
    if 'cells' in geometry:geometry['cells']=cells
    for name in ('retained_cell_images','restored_cell_math','restored_cell_text'):
        for item in geometry.get(name,[]):
            if 'row' in item:item['row']-=sum(r<item['row'] for r in deleted)


def set_table(item,entry,fragment,api):
    from docling_core.types.doc import TableCell,TableData
    profile=api.table_profile(fragment)
    item.data=TableData(num_rows=profile['rows'],num_cols=profile['columns'],table_cells=[
        TableCell(text=c['text'],row_span=c['row_span'],col_span=c['column_span'],
            start_row_offset_idx=c['row'],end_row_offset_idx=c['row']+c['row_span'],
            start_col_offset_idx=c['column'],end_col_offset_idx=c['column']+c['column_span'],column_header=c['header'])
        for c in profile['cells']])
    entry.update(html=fragment,selected=profile)


def compact_source_rule_rows(doc,routes,manifest,reader,regions,api):
    if reader is None:return
    items={t.self_ref:t for t in doc.tables};proofs=[]
    for entry in manifest.get('tables',[]):
        fragment=routes.get(entry['ref'],'');parsed=BeautifulSoup(fragment,'html.parser')
        table=parsed.table
        if table is None or entry['ref'] not in items:continue
        geometry=entry.get('pdf_geometry') or entry.get('join_geometry') or {}
        ys=geometry.get('y_grid',[]);xs=geometry.get('x_grid',[])
        count=len(table.thead.find_all('tr')) if table.thead else 0
        if count and len(ys)>count and len(xs)>1:
            entry['source_header_bbox']=[xs[0],ys[0],xs[-1],ys[count]]
        if not ys or not xs or not any(not r.get_text(strip=True) and not r.find(['img','math','svg','table']) for r in table.find_all('tr')):continue
        bounds=[xs[0],ys[0],xs[-1],ys[-1]];image=regions.crop(entry['page'],bounds)
        try:
            reader.load_page(entry['page'])
            result,proof=empty_rule_bands(fragment,geometry,image,bounds,reader.chars,api)
        finally:image.close()
        if proof:
            compact_row_geometry(geometry,proof)
            routes[entry['ref']]=result;set_table(items[entry['ref']],entry,result,api)
            parsed=BeautifulSoup(result,'html.parser')
            count=len(parsed.thead.find_all('tr')) if parsed.thead else 0
            if count and len(geometry['y_grid'])>count:
                entry['source_header_bbox']=[xs[0],geometry['y_grid'][0],xs[-1],geometry['y_grid'][count]]
            entry['empty_rule_row_repair']=proof
            record=dict(ref=entry['ref'],page=entry['page'],**proof);proofs.append(record)
            api.diagnostic_event('TABLE_EMPTY_RULE_BAND_REMOVED',**record)
    manifest['empty_source_rule_rows']=proofs


def join_double_rule_headers(doc,routes,manifest,reader,regions,api):
    """Rejoin a header-only fragment and its source-adjacent body on one page."""
    if reader is None:return
    from docling_core.types.doc import TableData
    items={t.self_ref:t for t in doc.tables};previous=None
    joined=manifest.setdefault('joined_tables',[])
    for current in sorted(manifest.get('tables',[]),key=lambda e:(e['page'],e['bbox'][1],e['bbox'][0])):
        if current.get('joined_into') or current['ref'] not in items:continue
        geo=current.get('join_geometry');text=routes.get(current['ref'],'')
        parsed=BeautifulSoup(text,'html.parser')
        if not geo or parsed.table is None:previous=None;continue
        if previous is not None:
            old=previous;old_geo=old['join_geometry']
            left=BeautifulSoup(routes.get(old['ref'],''),'html.parser')
            a,b=left.table,parsed.table
            header_only=(a is not None and a.thead is not None and all(r.find_parent('thead') is a.thead for r in a.find_all('tr')))
            same_grid=(len(old_geo['x_grid'])==len(geo['x_grid']) and
                all(abs(x-y)<=2 for x,y in zip(old_geo['x_grid'],geo['x_grid'])))
            gap=geo['bbox'][1]-old_geo['bbox'][3]
            lp,rp=api.table_profile(str(a)),api.table_profile(str(b))
            data_rows=any(not c['header'] and c['text'].strip() for c in rp['cells'])
            if (current['page']==old['page'] and header_only and same_grid and gap>=0
                    and not lp['issues'] and not rp['issues'] and lp['columns']==rp['columns'] and data_rows):
                # A new independent caption prevents an accidental merge of
                # two nearby tables, even if their columns happen to agree.
                caption=' '.join(r.resolve(doc).text for r in items[current['ref']].captions)
                if caption.strip():previous=current;continue
                bounds=[old_geo['x_grid'][0],old_geo['bbox'][1],old_geo['x_grid'][-1],geo['bbox'][3]]
                image=regions.crop(current['page'],bounds)
                try:
                    source=grid_ink(image,bounds,old_geo['x_grid'])
                    if source is None or gap>=source['glyph_height']:previous=current;continue
                    ends=(old_geo['bbox'][3],geo['bbox'][1])
                    if any(not any(abs(y-rule)<=max(1/source['sy'],source['stroke_radius']/source['sy'])
                                   for rule in source['full_width_rules']) for y in ends):previous=current;continue
                    reader.load_page(current['page'])
                    if any(c.get('text','').strip() and ends[0]<(c['top']+c['bottom'])/2<ends[1]
                        and bounds[0]<=(c['x0']+c['x1'])/2<=bounds[2] for c in reader.chars):previous=current;continue
                    top=max(0,round((ends[0]-bounds[1])*source['sy']))
                    bottom=min(image.height,round((ends[1]-bounds[1])*source['sy'])+1)
                    if np.any(source['nongrid'][top:bottom]):previous=current;continue
                finally:image.close()
                for group in b.find_all(['thead','tbody','tfoot'],recursive=False):
                    group.name='tbody'
                    for th in group.find_all('th'):th.name='td'
                    a.append(group.extract())
                result=str(a);profile=api.table_profile(result,lp['columns'])
                if profile['issues']:previous=current;continue
                routes[old['ref']]=result;routes[current['ref']]=''
                set_table(items[old['ref']],old,result,api)
                items[current['ref']].data=TableData(num_rows=0,num_cols=0,table_cells=[])
                items[old['ref']].prov.extend(p.model_copy(deep=True) for p in items[current['ref']].prov)
                combined=dict(geo,bbox=[old_geo['bbox'][0],old_geo['bbox'][1],geo['bbox'][2],geo['bbox'][3]])
                old.update(join_geometry=combined,last_join_geometry=combined,source_pages=[old['page']])
                current['joined_into']=old['ref']
                proof=dict(into=old['ref'],from_ref=current['ref'],page=current['page'],
                    basis='header_and_body_separated_only_by_source_double_rules',gap=gap,
                    nongrid_ink_pixels=0,payloads_unchanged=True)
                joined.append(dict(proof,**{'from':current['ref']}))
                api.diagnostic_event('TABLE_DOUBLE_RULE_HEADER_JOINED',**proof)
                continue
        previous=current


def _trim(mask):
    yy,xx=np.where(mask>0)
    if not len(xx):return None
    return mask[yy.min():yy.max()+1,xx.min():xx.max()+1]


def compare_header_ink(left,right,left_region,right_region,left_xs,right_xs,diagnostics=None):
    """Compare each source-column strip and every meaningful ink component."""
    if len(left_xs)!=len(right_xs):return None
    a=grid_ink(left,left_region,left_xs);b=grid_ink(right,right_region,right_xs)
    if a is None or b is None:return None
    for obj in (a,b):
        # Comparison-only antialiasing fringes of confirmed rules. The exact
        # nongrid mask used to prove empty rows above is deliberately unchanged.
        fringe=cv2.dilate(obj['rules'],np.ones((3,3),np.uint8))
        obj['comparison']=cv2.bitwise_and(obj['ink'],cv2.bitwise_not(fringe))
        edge=obj['stroke_radius']+1
        obj['comparison'][:edge,:]=0;obj['comparison'][-edge:,:]=0
        obj['comparison'][:,:edge]=0;obj['comparison'][:,-edge:]=0
    columns=[]
    windows=[(col,col,col+1) for col in range(len(left_xs)-1)]+[('whole_header',0,len(left_xs)-1)]
    for col,start,stop in windows:
        strips=[]
        for obj,region,xs in ((a,left_region,left_xs),(b,right_region,right_xs)):
            lo=max(0,round((xs[start]-region[0])*obj['sx']));hi=min(obj['nongrid'].shape[1],round((xs[stop]-region[0])*obj['sx']))
            strips.append(_trim(obj['comparison'][:,lo:hi]))
        first,second=strips
        if first is None or second is None:
            if first is not None or second is not None:return None
            columns.append(dict(column=col,empty=True));continue
        height,width=max(first.shape[0],second.shape[0]),max(first.shape[1],second.shape[1])
        first=cv2.resize(first,(width,height),interpolation=cv2.INTER_NEAREST)
        second=cv2.resize(second,(width,height),interpolation=cv2.INTER_NEAREST)
        tolerance=max(a['stroke_radius'],b['stroke_radius'])+1
        details=[]
        for source,target in ((first,second),(second,first)):
            distance=cv2.distanceTransform((target==0).astype('uint8'),cv2.DIST_L2,3)
            bad=(source>0)&(distance>tolerance)
            fraction=float(np.count_nonzero(bad))/max(1,np.count_nonzero(source))
            count,labels,stats,_=cv2.connectedComponentsWithStats(source,8)
            component_bad=[float(np.count_nonzero(bad[labels==i]))/area
                for i,(x,y,w,h,area) in enumerate(stats) if i and area>2
                and (col=='whole_header' or (x>tolerance and x+w<width-tolerance))]
            worst=max(component_bad,default=0.)
            details.append(dict(unmatched_fraction=fraction,worst_component_unmatched=worst))
            if fraction>.035 or worst>.20:
                if diagnostics is not None:diagnostics.append(dict(column=col,unmatched_fraction=fraction,worst_component_unmatched=worst,
                    components=[dict(box=list(map(int,stats[i][:4])),area=int(stats[i][4]),bad=int(np.count_nonzero(bad[labels==i])))
                        for i in range(1,count) if stats[i][4]>2 and np.count_nonzero(bad[labels==i])/stats[i][4]>.2]))
                return None
        columns.append(dict(column=col,comparisons=details))
    return dict(method='source_header_column_ink_and_component_agreement',columns=columns)


def _header_key(text):return re.sub(r'[\s\u00ad-]+','',text).casefold()


def _header_literal(image,language='ru'):
    """Read a natural heading at both scales, without algebra OCR."""
    from docling_source_text import read_text
    # Smoothing a small scanned glyph can close its opening differently at
    # each scale. Retry by replicating the original pixels, retaining the
    # same two-scale agreement requirement for either recognition view.
    for resampling in (Image.Resampling.LANCZOS,Image.Resampling.NEAREST):
        readings=[]
        for scale in (3,4):
            resized=image.resize((image.width*scale,image.height*scale),resampling)
            padded=ImageOps.expand(resized,border=24,fill='white');resized.close()
            stream=BytesIO();padded.save(stream,'PNG');padded.close()
            readings.append(read_text(stream.getvalue(),language)['text'].strip())
        if readings[0] and _header_key(readings[0])==_header_key(readings[1]):
            return dict(text=readings[0],readings=readings,engine='Windows.Media.Ocr',language=language,
                        scales=[3,4],resampling=resampling.name.lower())
    return None


def _heading_cells(table,api):
    profile=api.table_profile(str(table));nodes=table.find_all(['td','th'])
    return {(c['row'],c['column'],c['row_span'],c['column_span']):(c,n)
            for c,n in zip(profile['cells'],nodes) if n.find_parent('thead')}


def _footnote_reference(node):
    """A superscript reference after a word is distinct from a unit power."""
    if node.find('math'):return False
    scripts=node.find_all(['sub','sup'])
    meaningful=[s for s in scripts if s.get_text(strip=True)]
    if not meaningful or any(s.name!='sup' for s in meaningful):return False
    for script in meaningful:
        value=script.get_text(strip=True)
        if not re.fullmatch(r'\d+\)?',value):return False
        previous=script.previous_sibling
        if previous is None or getattr(previous,'name',None):return False
        if not re.search(r'[A-Za-zА-Яа-яЁё]{3,}\s*$',str(previous)):return False
        tail=str(script.next_sibling or '').lstrip()
        if not value.endswith(')') and not tail.startswith(')'):return False
    return True


def confirmed_header_copy(left_table,right_table,left_entry,left_box,right_box,regions,api):
    """Keep one header; replace differing literal fields only with source proof."""
    from docling_pdf_native_scripts import script_terms
    a=_heading_cells(left_table,api);b=_heading_cells(right_table,api)
    if a.keys()!=b.keys():return None,None
    chosen=BeautifulSoup(str(left_table.thead),'html.parser').thead
    targets=chosen.find_all(['td','th']);left_nodes=left_table.thead.find_all(['td','th'])
    repairs=[]
    geo=left_entry.get('pdf_geometry') or left_entry['join_geometry']
    xs=geo['x_grid'];ys=geo.get('y_grid',[])
    source_cells=geo.get('cells',[])
    for key,(cell,node) in a.items():
        other=b[key][1]
        texts=[node.get_text(' ',strip=True),other.get_text(' ',strip=True)]
        fragmented=any(sum(len(w)==1 for w in t.split())>len(t.split())/2 for t in texts if t.split())
        untrusted_styles=(fragmented and left_entry.get('method')=='raster_geometry'
                          and not geo.get('source_script_recoveries'))
        if script_terms(str(node))!=script_terms(str(other)):
            reference=next((n for n in (node,other) if _footnote_reference(n)),None)
            if reference is not None and _header_key(texts[0])==_header_key(texts[1]):
                target=targets[next(i for i,n in enumerate(left_nodes) if n is node)]
                target.clear()
                for child in list(BeautifulSoup(str(reference),'html.parser').find(['td','th']).contents):target.append(child.extract())
                repairs.append(dict(row=key[0],column=key[1],previous=texts,selected=reference.get_text(' ',strip=True),
                    method='identical_source_header_and_word_footnote_reference_markup'))
                continue
            if not untrusted_styles:return None,None
        if _header_key(node.get_text(' ',strip=True))==_header_key(other.get_text(' ',strip=True)):
            continue
        # Existing independently checked mathematical content is never
        # rewritten by a natural-language OCR engine.
        if node.find(['math','img','svg']) or other.find(['math','img','svg']):return None,None
        meaningful_scripts=any(s.get_text(strip=True) for n in (node,other) for s in n.find_all(['sub','sup']))
        if meaningful_scripts and not untrusted_styles:return None,None
        language=('ru' if any(re.search('[А-Яа-яЁё]',t) for t in texts) else
                  'en-US' if any(re.search('[A-Za-z]',t) for t in texts) else None)
        if language is None:return None,None
        source=next((c for c in source_cells if c['row']==key[0] and c['column']==key[1]),None)
        if source and source.get('bbox'):
            box=source['bbox']
        elif len(ys)>key[0]+key[2]:
            box=[xs[key[1]],ys[key[0]],xs[key[1]+key[3]],ys[key[0]+key[2]]]
        else:return None,None
        normalized=[(box[0]-left_box[0])/(left_box[2]-left_box[0]),
                    (box[1]-left_box[1])/(left_box[3]-left_box[1]),
                    (box[2]-left_box[0])/(left_box[2]-left_box[0]),
                    (box[3]-left_box[1])/(left_box[3]-left_box[1])]
        pair_boxes=[box,[right_box[0]+normalized[0]*(right_box[2]-right_box[0]),
            right_box[1]+normalized[1]*(right_box[3]-right_box[1]),
            right_box[0]+normalized[2]*(right_box[2]-right_box[0]),
            right_box[1]+normalized[3]*(right_box[3]-right_box[1])]]
        accepted=None
        for angle in (0,90,270):
            results=[]
            for page,bounds in zip((left_entry['page'],right_box[4]),pair_boxes):
                crop=regions.crop(page,bounds)
                # Boundary strokes belong to the table, not to a word crop.
                inset=max(1,round(min(crop.size)*.015))
                inner=crop.crop((inset,inset,crop.width-inset,crop.height-inset));crop.close()
                view=inner.rotate(angle,expand=True,fillcolor='white');inner.close()
                try:result=_header_literal(view,language)
                finally:view.close()
                if not result:break
                results.append(result)
            if len(results)!=2 or _header_key(results[0]['text'])!=_header_key(results[1]['text']):continue
            value=results[0]['text']
            if any(_header_key(t)==_header_key(value) for t in texts):
                if not meaningful_scripts or re.fullmatch(r'[A-Za-zА-Яа-яЁё\s,-]+',value):
                    value=next(t for t in texts if _header_key(t)==_header_key(value))
                    accepted=(value,results,angle);break
            # Spaced/reversed source-layer letters are a layout failure, not
            # a competing complete transcription. Both source images must
            # independently confirm their natural-word reconstruction.
            source_letters=max(sum(c.isalpha() for c in t) for t in texts)
            if (fragmented and re.fullmatch(r'[A-Za-zА-Яа-яЁё\s,-]+',value) and len(value.split())>=2
                    and sum(c.isalpha() for c in value)>=source_letters):
                accepted=(value,results,angle);break
        if accepted is None:return None,None
        value,readings,angle=accepted
        replacement=targets[next(i for i,n in enumerate(left_nodes) if n is node)]
        replacement.clear();replacement.append(value)
        repairs.append(dict(row=key[0],column=key[1],previous=texts,selected=value,source_bboxes=pair_boxes,
                            recognition_quarter_turn=angle,source_readings=readings,
                            discarded_unverified_ocr_styles=bool(meaningful_scripts)))
    return chosen,dict(method='source_identical_header_with_confirmed_literal_fields',fields=repairs)


def repeated_source_header(old,current,left_table,right_table,regions,api):
    """Resolve OCR differences only when the actual printed heads agree."""
    if regions is None or not left_table.thead or not right_table.thead:return None,None
    left_geo=old.get('pdf_geometry') or old.get('join_geometry') or {}
    right_geo=current.get('join_geometry') or {}
    ax,bx=left_geo.get('x_grid',[]),right_geo.get('x_grid',[])
    if len(ax)<3 or len(ax)!=len(bx):return None,None
    count=len(left_table.thead.find_all('tr'));box=old.get('source_header_bbox')
    if box is None:
        ys=left_geo.get('y_grid',[])
        if len(ys)<=count:return None,None
        box=[ax[0],ys[0],ax[-1],ys[count]]
    left=regions.crop(old['page'],box)
    bounds=right_geo['bbox'];full=regions.crop(current['page'],bounds)
    try:
        ink=grid_ink(full,bounds,bx)
        if ink is None:return None,None
        explicit=current.get('source_header_bbox')
        expected=(box[3]-box[1])*(bx[-1]-bx[0])/(ax[-1]-ax[0])
        ends=([explicit[3]] if explicit else [y for y in ink['full_width_rules']
            if .5*expected<y-bounds[1]<2*expected])
        for end in ends:
            candidate=[bx[0],bounds[1],bx[-1],end]
            right=regions.crop(current['page'],candidate)
            try:proof=compare_header_ink(left,right,box,candidate,ax,bx)
            finally:right.close()
            if proof is None:continue
            header,literal=confirmed_header_copy(left_table,right_table,old,box,[*candidate,current['page']],regions,api)
            if header is not None:
                return header,dict(**proof,left_page=old['page'],right_page=current['page'],
                    left_bbox=box,right_bbox=candidate,literal_fields=literal)
        return None,None
    finally:left.close();full.close()
