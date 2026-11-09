"""Suppress known whole-table layout rasters independently of restoration.

Source table identity and matching provenance establish what the picture is.
Restoration quality determines OK versus DIAG, never permission to retain a
raster duplicate of a known table in the published document.
"""
import base64
import hashlib
import re


def _coverage(inner,outer):
    area=max(0,inner[2]-inner[0])*max(0,inner[3]-inner[1])
    common=max(0,min(inner[2],outer[2])-max(inner[0],outer[0]))*max(0,min(inner[3],outer[3])-max(inner[1],outer[1]))
    return common/area if area else 0.


def _same_table_footprint(bounds,source):
    return (len(bounds)==len(source)==4 and all(abs(a-b)<=2 for a,b in zip(bounds,source))
        and min(_coverage(bounds,source),_coverage(source,bounds))>=.98)


def _image_digest(picture):
    uri=str(getattr(getattr(picture,'image',None),'uri',''))
    if not uri.startswith('data:image/'):return None
    try:return hashlib.sha256(base64.b64decode(uri.split(',',1)[1],validate=True)).hexdigest()
    except (ValueError,IndexError):return None


def _complete_source_cells(entry, api):
    """Stricter evidence for the caption-extended footprint, never a guess."""
    from bs4 import BeautifulSoup
    fragment = entry.get('html', '')
    # A source-image fallback is a registered table candidate, not a
    # structured table. It may legitimately carry html=None. Such a route
    # cannot prove that deleting its source picture preserves every cell.
    if not isinstance(fragment, str) or not fragment.strip() or '<table' not in fragment.lower():
        return False
    profile = api.table_profile(fragment)
    geometry = entry.get('pdf_geometry') or entry.get('join_geometry') or {}
    position = lambda c: tuple(c[k] for k in ('row','column','row_span','column_span'))
    physical = geometry.get('cells') or []
    try:
        same_cells = {position(c) for c in physical} == {position(c) for c in profile.get('cells', [])}
    except (KeyError, TypeError):
        return False
    if profile.get('issues') or not physical or not profile.get('cells') or not same_cells:
        return False
    soup = BeautifulSoup(fragment, 'html.parser')
    return bool(soup.table and all(n.get_text(strip=True) or n.find(['img','math'])
        for n in soup.table.find_all(['td','th']) if n.find_parent('table') is soup.table))


def _caption_extended_footprint(bounds, source, table, entry, page, doc, excluded, api):
    """Prove a full table raster whose only extra band is its own caption."""
    if (bounds[1] >= source[1]
            or any(abs(bounds[i]-source[i]) > 2 for i in (0,2,3))
            or _coverage(source, bounds) < .98):
        return None
    if not _complete_source_cells(entry, api):
        return None
    captions = []
    for reference in table.captions:
        caption = reference.resolve(doc)
        if not api.extract_table_caption(getattr(caption, 'text', '')):
            continue
        for prov in caption.prov:
            if prov.page_no != page: continue
            b = prov.bbox.to_top_left_origin(doc.pages[page].size.height)
            box = [b.l,b.t,b.r,b.b]
            if (source[1]-64 <= box[1] < source[1] and box[3] <= source[1]+2
                    and box[1]-2 <= bounds[1] <= box[3]+2):
                captions.append((reference.cref, box))
    if not captions: return None
    owned = {ref for ref, _ in captions}
    band = [bounds[0],bounds[1],bounds[2],source[1]]
    for text in doc.texts:
        if text.self_ref in owned or text.self_ref in excluded: continue
        for prov in text.prov:
            if prov.page_no != page: continue
            b = prov.bbox.to_top_left_origin(doc.pages[page].size.height)
            if max(b.l,band[0]) < min(b.r,band[2]) and max(b.t,band[1]) < min(b.b,band[3]):
                return None  # An unrelated source block would be lost.
    return dict(owned_caption_refs=sorted(owned), owned_caption_bboxes=[b for _,b in captions],
                all_source_cells_represented=True)


def published_source_cell_contains(entry, retained_bounds, picture_bounds, api):
    """Prove that a smaller layout crop is already inside a published crop.

    Source-cell crops intentionally contain a white margin. Equal-area tests
    cannot identify a smaller layout crop inside those same source pixels.
    Require the exact physical cell and an actual source-image payload in the
    selected table before using containment instead of equal crop size.
    """
    from bs4 import BeautifulSoup
    import math
    try:
        if (len(retained_bounds) != 4 or len(picture_bounds) != 4
                or not all(math.isfinite(float(v)) for box in (retained_bounds, picture_bounds) for v in box)):
            return False
        retained_bounds = [float(v) for v in retained_bounds]
        picture_bounds = [float(v) for v in picture_bounds]
    except (TypeError, ValueError):
        return False
    geometry = entry.get('pdf_geometry') or entry.get('join_geometry') or {}
    fragment = entry.get('html')
    if (not geometry.get('source_grid_verified') or not isinstance(fragment, str)
            or not retained_bounds or not picture_bounds
            or _coverage(picture_bounds, retained_bounds) < .995):
        return False
    retained = [obj for obj in geometry.get('retained_cell_images', [])
                if obj.get('bbox') and len(obj['bbox']) == 4
                and all(abs(a-b) <= 1e-4 for a,b in zip(obj['bbox'], retained_bounds))]
    if len(retained) != 1:
        return False
    obj = retained[0]
    physical = [cell for cell in geometry.get('cells', [])
                if (cell.get('row'), cell.get('column')) == (obj.get('row'), obj.get('column'))]
    if len(physical) != 1 or not physical[0].get('bbox'):
        return False
    source_cell = physical[0]
    if _coverage(retained_bounds, source_cell['bbox']) < .995:
        return False
    profile = api.table_profile(fragment)
    if profile.get('issues'):
        return False
    positions = ('row', 'column', 'row_span', 'column_span')
    try:
        identity = tuple(source_cell[key] for key in positions)
        selected = [i for i, cell in enumerate(profile.get('cells', []))
                    if tuple(cell[key] for key in positions) == identity]
    except (TypeError, KeyError):
        return False
    if len(selected) != 1:
        return False
    soup = BeautifulSoup(fragment, 'html.parser')
    if soup.table is None:
        return False
    cells = [cell for cell in soup.table.find_all(['td', 'th'])
             if cell.find_parent('table') is soup.table]
    if selected[0] >= len(cells):
        return False
    return any(image.get('data-source-cell') == 'true' and image.get('src')
               for image in cells[selected[0]].find_all('img'))


def suppress_layout_table_rasters(doc,manifest,excluded,api):
    """Remove a picture only when ALL its source parts are known tables.

    A small drawing inside a table has a different footprint. Explicit source
    illustration routes and figure captions remain drawings, even when a
    drawing happens to contain a rectangular frame.
    """
    owners={table.self_ref:table for table in doc.tables}
    protected=set(manifest.get('source_illustration_picture_refs',[]))
    by_page={};entries={}
    for entry in manifest.get('tables',[]):
        if entry.get('ref') not in owners:continue
        ref=entry['ref'];entries[ref]=entry
        boxes=[('registered_table',entry.get('bbox'))]
        for key in ('pdf_geometry','join_geometry'):
            geometry=entry.get(key) or {}
            boxes.append((key,geometry.get('bbox')))
        seen=set()
        for basis,box in boxes:
            if not box or len(box)!=4 or tuple(box) in seen:continue
            seen.add(tuple(box));by_page.setdefault(entry['page'],[]).append((ref,box,basis))
    removed=[];issues=[]
    figure_prefix=re.compile(r'^\s*(?:[а-яёa-z]\)\s*)?(?:(?:Рисунок|Рис\.|Схема|Черт[её]ж|Узел|Разрез|План|Вид)(?:\s|$)'
        r'|(?:Figure|Fig\.?)(?=\s|\d|[.:—–-]|$))',re.I)
    for picture in doc.pictures:
        if picture.self_ref in protected or not picture.prov:continue
        captions=[ref.resolve(doc) for ref in picture.captions]
        if any(figure_prefix.match(getattr(caption,'text','')) for caption in captions):continue
        matches=[]
        for prov in picture.prov:
            if prov.page_no not in doc.pages:break
            box=prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
            bounds=[box.l,box.t,box.r,box.b]
            candidates=[(ref,source,basis) for ref,source,basis in by_page.get(prov.page_no,[])
                if _same_table_footprint(bounds,source)]
            caption_proof = None
            if not candidates:
                for ref, source, basis in by_page.get(prov.page_no, []):
                    caption_proof = _caption_extended_footprint(bounds, source, owners[ref], entries[ref],
                        prov.page_no, doc, excluded, api)
                    if caption_proof:
                        candidates = [(ref,source,'complete_table_and_owned_caption')]
                        break
            if not candidates:break
            ref,source,basis=candidates[0]
            matches.append(dict(page=prov.page_no,table_ref=ref,picture_bbox=bounds,
                source_table_bbox=list(source),source_identity_basis=basis,**(caption_proof or {})))
        if len(matches)!=len(picture.prov):continue
        already_excluded=picture.self_ref in excluded
        excluded.add(picture.self_ref)
        proof=dict(picture=picture.self_ref,source_image_sha256=_image_digest(picture),
            source_parts=matches,already_excluded=already_excluded,
            method='known_table_raster_removed_policy')
        removed.append(proof);api.diagnostic_event('KNOWN_TABLE_RASTER_REMOVED',**proof)
        for ref in {match['table_ref'] for match in matches}:
            entry=entries[ref];selected=entry.get('selected') or {}
            usable=('<table' in str(entry.get('html','')).lower() and not selected.get('issues')
                and selected.get('rows',0)>0 and selected.get('columns',0)>0)
            if not usable:
                issue=dict(ref=ref,page=entry['page'],bbox=entry.get('bbox'),picture=picture.self_ref,
                    reason='known_table_raster_removed_without_usable_structured_publication',
                    method='known_table_raster_removed_policy')
                if issue not in issues:issues.append(issue)
    manifest['known_table_rasters_removed']=removed
    manifest['known_table_raster_policy_issues']=issues
    return removed
