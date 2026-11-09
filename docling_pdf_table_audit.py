"""Compare publication cells with the verified physical source-cell grid."""
from collections import Counter
from hashlib import sha256
import base64
import re
from bs4 import BeautifulSoup


def _image_digest(image):
    uri=image.get('src','')
    if not uri.startswith('data:image/'):return None
    try:return sha256(base64.b64decode(uri.split(',',1)[1],validate=True)).hexdigest()
    except (ValueError,IndexError):return None


def record_source_cell_assets(geometry,page,ref,manifest,api):
    """Keep original positioned PDF image identity before table joins.

    A retained raster crop alone is not proof of a drawing: ordinary text
    and formula crops are deliberately excluded from this native-image
    ledger. A final heading or independently measured source drawing
    geometry must additionally establish graphic purpose.
    """
    if not geometry:return []
    records=manifest.setdefault('source_cell_image_assets',[])
    known={(p['ref'],p['row'],p['column'],p['source_image_sha256']) for p in records}
    added=[]
    for cell in geometry.get('cells',[]):
        if not cell.get('source_images'):continue
        fragments=[line.get('html','') for line in cell.get('lines',[])]
        for fragment in fragments:
            for image in BeautifulSoup(fragment,'html.parser').find_all('img'):
                digest=_image_digest(image)
                if digest is None:continue
                key=(ref,cell['row'],cell['column'],digest)
                if key in known:continue
                proof=dict(page=page,ref=ref,row=cell['row'],column=cell['column'],
                    row_span=cell['row_span'],column_span=cell['column_span'],cell_bbox=cell.get('bbox'),
                    source_image_sha256=digest,method='original_positioned_pdf_image_in_source_cell')
                records.append(proof);added.append(proof);known.add(key)
    if added:api.diagnostic_event('TABLE_SOURCE_IMAGE_ASSETS_RECORDED',page=page,ref=ref,assets=len(added))
    return added


def _graphic_column_roles(table,profile):
    nodes=[n for n in table.find_all(['td','th']) if n.find_parent('table') is table]
    roles={}
    role_pattern=re.compile(r'цветографическ\w*\s+изображен\w*|геометрическ\w*\s+форм\w*|эскиз\w*|схем\w*|черт[её]ж\w*',re.I)
    excluded_pattern=re.compile(r'формул|уравнен|математическ|выражени',re.I)
    for node,position in zip(nodes,profile['cells']):
        if not position.get('header'):continue
        text=node.get_text(' ',strip=True)
        if not role_pattern.search(text) or excluded_pattern.search(text):continue
        if profile['columns']>1 and position['column_span']==profile['columns']:continue
        for column in range(position['column'],position['column']+position['column_span']):
            roles.setdefault(column,[]).append(dict(text=text,row=position['row'],column=position['column'],
                row_span=position['row_span'],column_span=position['column_span']))
    return roles


def protect_source_graphic_assets(fragment,geometry,page,ref,manifest,api,caption=''):
    """Preserve original diagram assets until final joined-header audit.

    A continuation without its own heading may borrow the immediately
    preceding page's role only on the same physical column grid. This early
    protection is deliberately NOT final permission to publish a raster:
    register_final_graphic_assets verifies the actual joined heading later.
    Mixed description columns instead need a measured engineering contour
    with section hatching or opposed dimension arrows in the native asset.
    """
    if not geometry:return fragment,[]
    record_source_cell_assets(geometry,page,ref,manifest,api)
    parsed=BeautifulSoup(fragment,'html.parser');table=parsed.table
    if table is None:return fragment,[]
    profile=api.table_profile(str(table));roles=_graphic_column_roles(table,profile)
    columns=geometry.get('x_grid',[]);contexts=manifest.setdefault('source_graphic_column_contexts',[])
    def identity(text):
        match=re.search(r'(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|табл\.?)\s*([А-ЯЁA-Z]?\s*\.?\s*\d+(?:\s*\.\s*\d+)*)',text,re.I)
        return re.sub(r'\s+','',match[1]).lower() if match else ''
    caption_id=identity(caption);inherited=None
    if not roles and contexts and len(columns)>1:
        old=contexts[-1];xs=old['x_grid']
        same_grid=(len(columns)==len(xs) and all(abs(x-y)<3 for x,y in zip(columns,xs)))
        continued=(not caption.strip() or re.match(r'^\s*(?:Продолжение|Окончание)\b',caption,re.I)
                   or (caption_id and caption_id==old.get('caption_id')))
        adjacent=(page==old['page']+1 or (page==old['page'] and caption.strip() and continued))
        if same_grid and adjacent and continued and old.get('roles'):
            roles=old['roles'];inherited=old['ref']
            caption_id=caption_id or old.get('caption_id','')
    contexts.append(dict(page=page,ref=ref,x_grid=list(columns),roles=roles,caption_id=caption_id))
    assets={}
    for asset in manifest.get('source_cell_image_assets',[]):
        if asset['ref']==ref:assets.setdefault((asset['row'],asset['column'],asset['source_image_sha256']),asset)
    proofs=[];protected=manifest.setdefault('protected_source_graphic_assets',[])
    known={(p['ref'],p['row'],p['column'],p['source_image_sha256']) for p in protected}
    nodes=[n for n in table.find_all(['td','th']) if n.find_parent('table') is table]
    for node,position in zip(nodes,profile['cells']):
        if position.get('header'):continue
        occupied=set(range(position['column'],position['column']+position['column_span']))
        if not occupied:continue
        for image in node.find_all('img'):
            digest=_image_digest(image);asset=assets.get((position['row'],position['column'],digest))
            if asset is None:continue
            explicit_role=occupied.issubset(roles)
            geometric_proof=None
            if not explicit_role:
                from docling_pdf_schematic import dimensioned_schematic
                geometric_proof=dimensioned_schematic(base64.b64decode(image['src'].split(',',1)[1]))
                if geometric_proof is None:continue
            proof=dict(page=page,ref=ref,row=position['row'],column=position['column'],
                source_image_sha256=digest,bbox=asset.get('cell_bbox'),
                header_evidence=[h for col in sorted(occupied) for h in roles.get(col,[])],inherited_from=inherited,
                method=('original_pdf_image_with_same_grid_graphic_column_context' if explicit_role else
                    'original_pdf_image_with_source_engineering_schematic_geometry'))
            if geometric_proof:proof['graphic_geometry_evidence']=geometric_proof
            key=(ref,position['row'],position['column'],digest)
            if key not in known:protected.append(proof);known.add(key)
            proofs.append(proof)
            image['data-cell-content']='source-diagram'
            image['data-diagram-evidence']=('original-pdf-image-and-graphic-column-context' if explicit_role else
                'original-pdf-image-and-engineering-schematic-geometry')
            api.diagnostic_event('TABLE_SOURCE_GRAPHIC_ASSET_PROTECTED',**proof)
    geometry['source_graphic_assets']=proofs
    # Entries refer to original positioned images at this stage. Give the
    # legacy math classifier the same column evidence, but only where an
    # original native asset was actually proven in that source cell.
    protected_cells={(p['row'],p['column']) for p in proofs if not p.get('graphic_geometry_evidence')}
    for entry in geometry.get('retained_cell_images',[]):
        if (entry.get('row'),entry.get('column')) in protected_cells:
            context=entry.setdefault('source_column_context',{})
            context.update(graphic_column=True,header_texts=[h['text'] for h in roles[entry['column']]],
                inherited_graphic_header_ref=inherited,original_native_asset=True)
    return str(table),proofs


def register_final_graphic_assets(soup,manifest,api):
    """Require source bytes AND final graphic role or native drawing proof.

    Header ownership is evaluated after continuation joins, so a body image
    on a later page inherits the retained table's actual column heading.
    """
    joined={entry['ref']:entry['joined_into'] for entry in manifest.get('tables',[]) if entry.get('joined_into')}
    joined.update({entry['from']:entry['into'] for entry in manifest.get('joined_tables',[])})
    def owner(ref):
        seen=set()
        while ref in joined and ref not in seen:
            seen.add(ref);ref=joined[ref]
        return ref
    by_identity={}
    for asset in manifest.get('source_cell_image_assets',[]):
        by_identity.setdefault((owner(asset['ref']),asset['source_image_sha256']),[]).append(asset)
    geometric_assets={}
    for asset in manifest.get('protected_source_graphic_assets',[]):
        if asset.get('graphic_geometry_evidence'):
            geometric_assets.setdefault((owner(asset['ref']),asset['source_image_sha256']),[]).append(asset)
    diagrams=manifest.setdefault('source_cell_diagrams',[]);proofs=[]
    final_columns={}
    for table in soup.find_all('table'):
        ref=table.get('data-pdf-table-ref')
        if not ref:continue
        profile=api.table_profile(str(table))
        nodes=[n for n in table.find_all(['td','th']) if n.find_parent('table') is table]
        roles=_graphic_column_roles(table,profile)
        column_images={column:set() for column in range(profile['columns'])}
        for node,position in zip(nodes,profile['cells']):
            if position.get('header'):continue
            occupied=set(range(position['column'],position['column']+position['column_span']))
            if not occupied:continue
            for image in node.find_all('img'):
                if image.find_parent('table') is not table:continue
                digest=_image_digest(image)
                for column in occupied:column_images[column].add(digest)
                sources=by_identity.get((owner(ref),digest),[])
                if not sources:continue
                geometric=geometric_assets.get((owner(ref),digest),[])
                if not occupied.issubset(roles) and not geometric:continue
                # Prefer the same source column; joins retain this column
                # coordinate. SHA identity still pins the complete source
                # asset if later removal of proven empty columns shifts it.
                source=next((p for p in sources if p['column']==position['column']),sources[0])
                proof=dict(page=source['page'],row=position['row'],column=position['column'],
                    bbox=source.get('cell_bbox'),source_image_sha256=digest,table_ref=ref,
                    final_row=position['row'],final_column=position['column'],
                    source_ref=source['ref'],source_row=source['row'],source_column=source['column'],
                    header_evidence=[h for col in sorted(occupied) for h in roles.get(col,[])],
                    method=('original_pdf_image_bytes_and_source_engineering_schematic_geometry' if geometric else
                        'original_pdf_image_bytes_and_final_explicit_graphic_column'))
                if geometric:proof['graphic_geometry_evidence']=geometric[0]['graphic_geometry_evidence']
                diagrams.append(proof);proofs.append(proof)
                image['data-cell-content']='source-diagram'
                image['data-diagram-evidence']=('original-pdf-image-and-engineering-schematic-geometry' if geometric else
                    'original-pdf-image-and-final-graphic-header')
                api.diagnostic_event('TABLE_SOURCE_GRAPHIC_COLUMN_ASSET_CONFIRMED',**proof)
        final_columns.setdefault(owner(ref),[]).append((ref,roles,column_images))
    manifest['final_graphic_asset_registration']=proofs
    losses=[];reported=set()
    def text_key(text):return re.sub(r'\s+','',text).casefold()
    for protected in manifest.get('protected_source_graphic_assets',[]):
        tables=final_columns.get(owner(protected['ref']),[])
        headings={text_key(h['text']) for h in protected.get('header_evidence',[])}
        applicable=[]
        for final_ref,roles,images in tables:
            columns=(set(images) if protected.get('graphic_geometry_evidence') else
                {protected['column']} if protected['column'] in roles else
                {col for col,headers in roles.items() if headings & {text_key(h['text']) for h in headers}})
            if columns:applicable.append((final_ref,columns,images))
        if not applicable:continue
        if any(protected['source_image_sha256'] in images[col]
               for _,columns,images in applicable for col in columns):continue
        # Overlapping OCR layout fragments can refer to one physical asset.
        # Do not count that source image twice or infer a lost repeated head.
        key=(protected['page'],tuple(protected.get('bbox') or ()),protected['source_image_sha256'])
        if key in reported:continue
        reported.add(key)
        loss=dict(page=protected['page'],ref=applicable[0][0],source_ref=protected['ref'],
            source_row=protected['row'],source_column=protected['column'],bbox=protected.get('bbox'),
            source_image_sha256=protected['source_image_sha256'],
            final_graphic_columns=sorted({col for _,columns,_ in applicable for col in columns}),
            reason=('original_schematic_asset_missing_from_final_table' if protected.get('graphic_geometry_evidence') else
                'original_graphic_asset_missing_from_final_graphic_column'))
        losses.append(loss);api.diagnostic_event('TABLE_SOURCE_GRAPHIC_ASSET_LOST',**loss)
    manifest['source_graphic_losses']=losses
    return proofs


def source_grid(fragment,geometry,page,ref,manifest,api):
    if not geometry or not geometry.get('source_grid_verified'):return None
    profile=api.table_profile(fragment)
    keys=('row','column','row_span','column_span')
    expected=[tuple(c[k] for k in keys) for c in geometry.get('cells',[])]
    actual=[tuple(c[k] for k in keys) for c in profile['cells']]
    missing=list((Counter(expected)-Counter(actual)).elements())
    extra=list((Counter(actual)-Counter(expected)).elements())
    record=dict(page=page,ref=ref,element=ref,source_cells=len(expected),published_cells=len(actual),
        source_columns=len(geometry['x_grid'])-1,published_columns=profile['columns'],
        source_rows=len(geometry['y_grid'])-1,published_rows=profile['rows'],
        exact_cell_positions_and_spans=not missing and not extra,
        source_structure_sha256=sha256(repr(sorted(expected)).encode()).hexdigest(),
        published_structure_sha256=sha256(repr(sorted(actual)).encode()).hexdigest(),
        missing_cells=missing,extra_cells=extra)
    manifest.setdefault('source_cell_grid_audits',[]).append(record)
    if missing or extra:
        from docling_pdf_review import matching_primary_review
        record['severity']='warning'
        record['unchanged_registered_primary']=bool(matching_primary_review(profile,page,ref,manifest))
        manifest.setdefault('source_grid_reviews',[]).append(dict(**record,
            reason='published_cells_disagree_with_verified_source_grid'))
    api.diagnostic_event('TABLE_SOURCE_CELL_GRID_AUDIT',**record)
    return record
