"""Recover native ruled tables omitted by the OCR/layout table collection.

Only physical, completely bounded cell grids are eligible.  Grid geometry
alone does not establish table purpose: require a table label/OCR Table or
an unobstructed continuation of an already owned table on the next sheet.
"""
from collections import Counter
import re

from bs4 import BeautifulSoup


def _coverage(inner,outer):
    area=max(0,inner[2]-inner[0])*max(0,inner[3]-inner[1])
    overlap=max(0,min(inner[2],outer[2])-max(inner[0],outer[0]))*max(0,min(inner[3],outer[3])-max(inner[1],outer[1]))
    return overlap/area if area else 0.


def _axis(values):
    groups=[]
    for value in sorted(set(values)):
        if groups and value-groups[-1][-1]<=.25:groups[-1].append(value)
        else:groups.append([value])
    return [sum(group)/len(group) for group in groups]


def _rule_coverage(edges,vertical,coordinate,start,end):
    intervals=[]
    for edge in edges:
        if edge.get('orientation')!=('v' if vertical else 'h'):continue
        position=edge['x0'] if vertical else edge['top']
        if abs(position-coordinate)>.75:continue
        a,z=(edge['top'],edge['bottom']) if vertical else (edge['x0'],edge['x1'])
        if z>start and a<end:intervals.append((max(a,start),min(z,end)))
    reached=start;covered=0.
    for a,z in sorted(intervals):
        if a>reached+.75:return False
        if z>reached:covered+=z-max(a,reached);reached=z
    return reached>=end-.75 and covered>=.98*(end-start)


def complete_native_grid(table,edges):
    """Certify all physical cell slots and actual four outer source rules."""
    cells=[list(c) for c in table.cells if c]
    if len(cells)<4:return None
    xs=_axis(v for c in cells for v in (c[0],c[2]));ys=_axis(v for c in cells for v in (c[1],c[3]))
    if len(xs)<3 or len(ys)<3 or min(b-a for a,b in zip(xs,xs[1:]))<=.5 or min(b-a for a,b in zip(ys,ys[1:]))<=.5:return None
    occupancy=Counter();spans=[]
    for cell in cells:
        positions=[]
        for value,axis in zip(cell,(xs,ys,xs,ys)):
            index=min(range(len(axis)),key=lambda i:abs(axis[i]-value))
            if abs(axis[index]-value)>.26:return None
            positions.append(index)
        left,top,right,bottom=positions
        if left>=right or top>=bottom:return None
        for row in range(top,bottom):
            for col in range(left,right):occupancy[row,col]+=1
        spans.append(dict(row=top,column=left,row_span=bottom-top,column_span=right-left))
    if len(occupancy)!=(len(xs)-1)*(len(ys)-1) or any(v!=1 for v in occupancy.values()):return None
    if not all((_rule_coverage(edges,True,xs[0],ys[0],ys[-1]),
                _rule_coverage(edges,True,xs[-1],ys[0],ys[-1]),
                _rule_coverage(edges,False,ys[0],xs[0],xs[-1]),
                _rule_coverage(edges,False,ys[-1],xs[0],xs[-1]))):return None
    return dict(bbox=[xs[0],ys[0],xs[-1],ys[-1]],x_grid=xs,y_grid=ys,cells=spans,
        method='complete_nonoverlapping_native_cells_with_four_actual_source_borders')


_TABLE=re.compile(r'^\s*(?:(?:Продолжение|Окончание)\s+)?(?:т\s*а\s*б\s*л\s*и\s*ц[аы]|табл\.)(?:\s|$)',re.I)
_FIGURE=re.compile(r'^\s*(?:Рисунок|Рис\.|Черт[её]ж|Схема)\s+(?:[А-ЯЁA-Z]\s*\.\s*)?\d',re.I)
_CONTINUED=re.compile(r'^\s*(?:Продолжение|Окончание)\b',re.I)


def _native_lines(reader,api):
    # Keep natural rows local to each candidate, rather than joining opposite
    # page columns into one misleading caption string.
    return api.native_cell_lines(reader.chars)


def _nearby_labels(box,lines):
    table=[];figure=[]
    for line in lines:
        if min(line['x1'],box[2])-max(line['x0'],box[0])<=0:continue
        y=line['y'];text=line['text']
        if box[1]-48<=y<=box[1]+1 and _TABLE.match(text):table.append(text)
        if (box[1]-35<=y<box[1] or box[3]<y<=box[3]+38) and _FIGURE.match(text):figure.append(text)
    return table,figure


def _same_axes(a,b):
    return (a['page']+1==b['page'] and len(a['x_grid'])==len(b['x_grid'])
        and abs(a['page_size'][0]-b['page_size'][0])<3 and abs(a['page_size'][1]-b['page_size'][1])<3
        and all(abs(x-y)<=2.5 for x,y in zip(a['x_grid'],b['x_grid'])))


def _physical_gap_clear(left,right):
    # Only running page furniture may occur between the two source grids.
    # New paragraph/section/table titles are independent source content.
    labelled=any(_CONTINUED.match(text) for text in right.get('table_labels',[]))
    if not labelled and (left['bbox'][3]<left['page_size'][1]*.8 or right['bbox'][1]>right['page_size'][1]*.2):return False
    for item,top in ((left,False),(right,True)):
        box=item['bbox'];height=item['page_size'][1]
        for line in item['native_lines']:
            text=line['text'].strip();y=line['y']
            if not text or re.fullmatch(r'[\d\s—–-]+',text):continue
            key=re.sub(r'\s+','',text).casefold()
            if key in item.get('furniture_text_keys',set()):continue
            if re.fullmatch(r'(?:СП|ГОСТ(?:\s+Р)?|СНиП|ISO|EN)\s*[А-ЯЁA-Z0-9.\-–— ]+',text,re.I):continue
            if (top and y>=box[1]-1) or (not top and y<=box[3]+1):continue
            if _CONTINUED.match(text):continue
            return False
    return True


def _caption_compatible(left,right):
    # A separately named table starts a new family even with identical rails.
    labels=right.get('table_labels',[])
    if not labels:return True
    if any(not _CONTINUED.match(text) for text in labels):return False
    return True


def _has_model_owner(doc,page,box,excluded,api):
    found=[]
    for item in doc.tables:
        reg=api.item_region(item,doc)
        if item.self_ref in excluded or not reg or reg[0]!=page:continue
        other=reg[1]
        # Existing layout tables often omit a source note band or cover more
        # than one closed native rectangle. Those are boundary repairs, not
        # missing tables: one contained footprint with matching side rails
        # already owns this grid and must never be duplicated here.
        if (abs(box[0]-other[0])<=6 and abs(box[2]-other[2])<=6 and max(
                _coverage(box,[other[0]-3,other[1]-3,other[2]+3,other[3]+3]),
                _coverage(other,[box[0]-3,box[1]-3,box[2]+3,box[3]+3]))>=.95):found.append(item.self_ref)
    return found


def _covered_layout_items(doc,item,page,box,geometry,excluded,api):
    """Remove layout replicas only after complete native-cell publication."""
    from docling_core.types.doc import DocItemLabel,ContentLayer
    assets=[e['bbox'] for e in geometry.get('retained_cell_images',[])]
    cells=[c['bbox'] for c in geometry.get('cells',[])]
    removed=[]
    for old,_ in list(doc.iterate_items()):
        if old.self_ref==item.self_ref or old.self_ref in excluded or not hasattr(old,'prov') or len(old.prov)!=1:continue
        if getattr(old,'label',None)==DocItemLabel.TABLE or getattr(old,'content_layer',None)==ContentLayer.FURNITURE:continue
        region=api.item_region(old,doc)
        if not region or region[0]!=page or _coverage(region[1],box)<.98:continue
        bounds=region[1]
        full_table_picture=(old.label==DocItemLabel.PICTURE
            and geometry.get('source_native_discovery') and geometry.get('source_grid_verified')
            and geometry.get('glyph_coverage')==1.
            and _coverage(box,bounds)>=.98
            and all(abs(a-b)<=2 for a,b in zip(bounds,box)))
        if full_table_picture:
            # This layout crop contains the entire already verified source
            # grid, so it necessarily spans several cells. Check ownership
            # before the single-cell guard; otherwise a raster duplicate of
            # the complete table survives beside its structured publication.
            excluded.add(old.self_ref)
            removed.append(dict(ref=old.self_ref,label=str(old.label),bbox=bounds,
                source_grid_bbox=list(box),method='complete_native_source_grid_owns_full_table_picture'))
            continue
        if not any(_coverage(bounds,[c[0]-.5,c[1]-.5,c[2]+.5,c[3]+.5])>=.98 for c in cells):continue
        if old.label==DocItemLabel.PICTURE:
            # A full-table raster is redundant; an in-cell picture needs its
            # own original positioned source asset inside the recovered cell.
            source_owned=any(_coverage(bounds,[a[0]-2,a[1]-2,a[2]+2,a[3]+2])>=.98 for a in assets)
            if not source_owned:continue
        elif old.label not in {DocItemLabel.TEXT,DocItemLabel.FORMULA,DocItemLabel.LIST_ITEM,DocItemLabel.CAPTION}:continue
        excluded.add(old.self_ref)
        removed.append(dict(ref=old.self_ref,label=str(old.label),bbox=bounds,
            method='complete_native_source_cell_owns_layout_replica'))
    return removed


def recover_unrouted_native_tables(doc,reader,regions,routes,excluded,manifest,api,records=()):
    """Return native candidates for omitted, source-owned physical tables.

    Mutations affect only this export's document, never the stored OCR/layout
    snapshot.  Page/table identifiers and document wording are not selectors.
    """
    if reader is None:return {}
    from docling_core.types.doc import ContentLayer,DocItemLabel
    candidates=[];rejected=[]
    # The page map is the requested export scope, including any omitted layout
    # pages. The PDF reader still contains the entire source document.
    known_pages={int(page) for page in (manifest.get('pages') or doc.pages)}
    for page in sorted(known_pages):
        try:
            native_page=reader.load_page(page)
            if not reader.tables:continue
            lines=_native_lines(reader,api)
            furniture={re.sub(r'\s+','',text.text).casefold() for text in doc.texts
                if (getattr(text,'content_layer',None)==ContentLayer.FURNITURE or
                    text.label in {DocItemLabel.PAGE_HEADER,DocItemLabel.PAGE_FOOTER})
                and (reg:=api.item_region(text,doc)) and reg[0]==page}
            for source in reader.tables:
                proof=complete_native_grid(source,native_page.edges)
                if not proof:continue
                box=proof['bbox'];labels,figures=_nearby_labels(box,lines)
                from docling_pdf_forms import form_owns_region
                if form_owns_region(manifest,page,box):
                    rejected.append(dict(page=page,bbox=box,reason='explicit_source_form_illustration'));continue
                owners=_has_model_owner(doc,page,box,excluded,api)
                explicit_ocr=[i for i,r in enumerate(records) if r.get('page_no')==page and r.get('label')=='Table'
                    and r.get('bbox') and _coverage(box,[r['bbox'][0]-3,r['bbox'][1]-3,r['bbox'][2]+3,r['bbox'][3]+3])>=.9]
                candidate=dict(page=page,**proof,table_labels=labels,figure_labels=figures,
                    owner_refs=owners,explicit_ocr_table_records=explicit_ocr,native_lines=lines,
                    page_size=[float(native_page.width),float(native_page.height)],furniture_text_keys=furniture)
                if figures and not owners:
                    rejected.append(dict(page=page,bbox=box,reason='captioned_source_illustration_grid',captions=figures));continue
                local=' '.join(line['text'] for line in lines if box[1]<=line['y']<=box[3]
                    and min(line['x1'],box[2])-max(line['x0'],box[0])>0)
                frame_fields=[word for word in ('Стадия','Листов','Подпись','Дата','Разраб','Пров','Нормоконтр')
                    if re.search(r'(?<![А-Яа-яЁё])'+word+r'[а-яё.]*\b',local,re.I)]
                if len(frame_fields)>=3 and not owners and not labels:
                    rejected.append(dict(page=page,bbox=box,reason='drawing_titleblock_field_grid',fields=frame_fields));continue
                candidate['owned']=bool(owners or labels or explicit_ocr)
                candidates.append(candidate)
        except Exception as error:
            api.diagnostic_event('NATIVE_TABLE_DISCOVERY_PAGE_FAILED',page=page,error_type=type(error).__name__,reason=str(error))
            manifest.setdefault('native_table_discovery_errors',[]).append(dict(page=page,error_type=type(error).__name__,reason=str(error)))
    candidates.sort(key=lambda c:(c['page'],c['bbox'][1],c['bbox'][0]))
    links=[]
    for a in candidates:
        for b in candidates:
            if _same_axes(a,b) and _caption_compatible(a,b) and _physical_gap_clear(a,b):links.append((a,b))
    changed=True
    while changed:
        changed=False
        for a,b in links:
            if a['owned']!=b['owned']:
                a['owned']=b['owned']=True;changed=True
    recovered={};entries=[]
    for candidate in candidates:
        if candidate['owner_refs'] or not candidate['owned']:continue
        page=candidate['page'];box=candidate['bbox']
        image=regions.crop(page,box)
        native=reader.read(page,box,source_image=image)
        if native is None:
            rejected.append(dict(page=page,bbox=box,reason='native_complete_cell_read_unavailable'));continue
        html,geometry=native;profile=api.table_profile(html)
        keys=('row','column','row_span','column_span')
        expected=Counter(tuple(c[k] for k in keys) for c in candidate['cells'])
        actual=Counter(tuple(c[k] for k in keys) for c in profile['cells'])
        if profile['issues'] or expected!=actual or geometry.get('glyph_coverage')!=1.:
            rejected.append(dict(page=page,bbox=box,reason='native_publication_does_not_cover_exact_source_grid',
                source_cells=len(candidate['cells']),published_cells=len(profile['cells'])));continue
        item=api.insert_source_item(doc,page,box,'table');candidate['owner_refs']=[item.self_ref]
        geometry.update(source_grid_verified=True,source_native_discovery=True,
            source_native_grid_proof={k:candidate[k] for k in ('bbox','x_grid','y_grid','cells','method')})
        replicas=_covered_layout_items(doc,item,page,box,geometry,excluded,api)
        recovered[item.self_ref]=(html,geometry)
        entry=dict(ref=item.self_ref,page=page,bbox=box,rows=profile['rows'],columns=profile['columns'],cells=len(profile['cells']),
            source_grid_verified=True,table_labels=candidate['table_labels'],explicit_ocr_table_records=candidate['explicit_ocr_table_records'],
            excluded_layout_replicas=replicas,method='unrouted_complete_native_grid_with_source_table_ownership')
        entries.append(entry);api.diagnostic_event('NATIVE_SOURCE_TABLE_RECOVERED',**entry)
    families=[]
    for a,b in links:
        if not a['owner_refs'] or not b['owner_refs']:continue
        proof=dict(from_ref=a['owner_refs'][0],to_ref=b['owner_refs'][0],from_page=a['page'],to_page=b['page'],
            x_grid=a['x_grid'],next_x_grid=b['x_grid'],method='adjacent_complete_native_grids_same_rails_and_clear_source_gap')
        families.append(proof)
        for candidate,other,side in ((a,b,'next'),(b,a,'previous')):
            for ref in candidate['owner_refs']:
                if ref in recovered:recovered[ref][1]['source_continuation_'+side+'_ref']=other['owner_refs'][0]
    manifest['recovered_unrouted_native_tables']=entries
    manifest['native_source_table_continuations']=families
    manifest['native_table_discovery_rejections']=rejected
    manifest['native_table_discovery_summary']=dict(pages=len(known_pages),complete_source_grids=len(candidates),
        existing_owned_grids=sum(bool(c['owner_refs']) for c in candidates)-len(entries),recovered=len(entries),
        unowned_complete_grids=[dict(page=c['page'],bbox=c['bbox'],reason='no_table_label_or_owned_continuation') for c in candidates if not c['owned']])
    return recovered
