"""Suppress table raster duplicates only when another publication owns their content."""
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


def table_crop_has_only_frame_padding(picture, bounds, source):
    """Require four measured frame rules and account for all ink outside the grid."""
    import io
    import math

    margins = (source[0]-bounds[0], source[1]-bounds[1],
               bounds[2]-source[2], bounds[3]-source[3])
    if not all(0 <= value <= 6 for value in margins) or _coverage(bounds, source) < .90:
        return False
    uri = str(getattr(getattr(picture, 'image', None), 'uri', ''))
    if not uri.startswith('data:image/png;base64,'):
        return False
    import cv2
    import numpy as np
    from PIL import Image

    try:
        with Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1], validate=True))) as image:
            if image.mode != 'RGB':
                return False
            darkest = np.asarray(image).min(axis=2)
    except (ValueError, OSError):
        return False
    height, width = darkest.shape
    scale_x = width / (bounds[2]-bounds[0])
    scale_y = height / (bounds[3]-bounds[1])
    left, top = math.floor(margins[0]*scale_x), math.floor(margins[1]*scale_y)
    right, bottom = width-math.floor(margins[2]*scale_x), height-math.floor(margins[3]*scale_y)
    if min(right-left, bottom-top) < 16:
        return False
    # A gray scan margin must not hide an additional dark mark. Test ink
    # against the frame at multiple intensities, including the light fringe.
    for threshold in (128, 224, 255):
        mask = (darkest < threshold).astype('uint8')
        outside = mask.copy()
        outside[top:bottom, left:right] = 0
        # Long runs at the four measured edges can be the scan's thick rules.
        # Restrict the mask to those edges so that interior rulings cannot
        # explain extra text, an arrow or other marks in the outer margin.
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
            np.ones((1, round(.8*(right-left))), np.uint8))
        vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
            np.ones((round(.8*(bottom-top)), 1), np.uint8))
        for lines, start, stop in ((horizontal, top, bottom), (vertical.T, left, right)):
            length, span = lines.shape
            for edge in (start, stop-1):
                band = lines[max(0,edge-3):min(length,edge+4)]
                if not np.any(band.sum(axis=1) > .8*span):
                    return False
            lines[:max(0,start-3)] = 0
            lines[start+4:stop-4] = 0
            lines[min(length,stop+3):] = 0
        frame = cv2.dilate(horizontal | vertical, np.ones((3,3), np.uint8))
        if np.any(outside & (1-frame)):
            return False
    return True


def aligned_table_layout_crop(bounds, source, *, table_caption=False):
    """Recognize the measured table inside a slightly padded layout crop."""
    if min(_coverage(bounds, source), _coverage(source, bounds)) >= .97:
        if all(abs(a-b) <= 6 for a,b in zip(bounds, source)):
            return True
    return (table_caption and all(abs(bounds[i]-source[i]) <= 4 for i in (0,1,2))
            and 0 <= bounds[3]-source[3] <= min(24, (source[3]-source[1])*.06)
            and _coverage(source,bounds) >= .98 and _coverage(bounds,source) >= .95)


def _image_digest(picture):
    uri=str(getattr(getattr(picture,'image',None),'uri',''))
    if not uri.startswith('data:image/'):return None
    try:return hashlib.sha256(base64.b64decode(uri.split(',',1)[1],validate=True)).hexdigest()
    except (ValueError,IndexError):return None


def _complete_source_cells(entry, api, preview_assets=()):
    """Require a published payload for every cell of the verified physical grid."""
    from bs4 import BeautifulSoup
    fragment = entry.get('html', '')
    # A source-image fallback is a registered table candidate, not a
    # structured table. It may legitimately carry html=None. Such a route
    # cannot prove that deleting its source picture preserves every cell.
    if not isinstance(fragment, str) or not fragment.strip() or '<table' not in fragment.lower():
        return False
    profile = api.table_profile(fragment)
    geometry = entry.get('pdf_geometry') or entry.get('join_geometry') or {}
    if (entry.get('fallback_reason') or entry.get('method') in {'editable_table_review', 'unrecognized_table'}
            or not (geometry.get('source_grid_verified') or geometry.get('positioned_source_grid_verified')
                    or geometry.get('glyph_coverage') == 1.0)):
        return False
    position = lambda c: tuple(c[k] for k in ('row','column','row_span','column_span'))
    physical = geometry.get('cells') or []
    if any(cell.get('unresolved_typed_content') for cell in physical):
        return False
    try:
        same_cells = {position(c) for c in physical} == {position(c) for c in profile.get('cells', [])}
    except (KeyError, TypeError):
        return False
    if (profile.get('issues') or not physical or len(physical) != len(profile.get('cells', [])) or not same_cells):
        return False
    soup = BeautifulSoup(fragment, 'html.parser')
    if (not soup.table or soup.table.select_one('[data-unrecognized-table]')
            or '[Фрагмент ячейки не распознан]' in soup.table.get_text()):
        return False
    nodes = [n for n in soup.table.find_all(['td','th']) if n.find_parent('table') is soup.table]
    if len(nodes) != len(physical) or any(not (n.get_text(strip=True) or n.find(['img','math'])) for n in nodes):
        return False
    reviews = soup.table.select('[data-ocr-review]')
    if not reviews:
        return True
    # A disputed transcription is complete visually only when the exact
    # registered source preview is still present inside its own grid cell.
    by_position = {position(cell): cell for cell in physical}
    for warning in reviews:
        owner = warning.find_parent(['td', 'th'])
        index = next((i for i, node in enumerate(nodes) if node is owner), None)
        if index is None:
            return False
        cell = by_position[position(profile['cells'][index])]
        images = owner.select('img[data-ocr-source-preview="true"][data-table-content="picture"][src]')
        if len(images) != 1 or not images[0]['src'].startswith('data:image/png;base64,'):
            return False
        digest = hashlib.sha256(images[0]['src'].encode()).hexdigest()
        if not any(asset.get('method') == 'source_preview_for_unresolved_physical_cell'
                   and asset.get('page') == entry.get('page') and asset.get('source_ref') == entry.get('ref')
                   and asset.get('cell') == {key: cell[key] for key in ('row','column','row_span','column_span')}
                   and asset.get('source_cell_bbox') == cell.get('bbox') and cell.get('bbox')
                   and asset.get('source_sha256') == digest for asset in preview_assets):
            return False
    return True


def _caption_extended_footprint(bounds, source, table, entry, page, doc, excluded, api, preview_assets=()):
    """Prove a full table raster whose only extra band is its own caption."""
    if (bounds[1] >= source[1]
            or any(abs(bounds[i]-source[i]) > 2 for i in (0,2,3))
            or _coverage(source, bounds) < .98):
        return None
    if not _complete_source_cells(entry, api, preview_assets):
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
    removed=[];issues=[];complete={}
    preview_assets = manifest.get('typed_table_picture_assets', [])
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
                        prov.page_no, doc, excluded, api, preview_assets)
                    if caption_proof:
                        candidates = [(ref,source,'complete_table_and_owned_caption')]
                        break
            if not candidates:break
            ref,source,basis=candidates[0]
            matches.append(dict(page=prov.page_no,table_ref=ref,picture_bbox=bounds,
                source_table_bbox=list(source),source_identity_basis=basis,**(caption_proof or {})))
        if len(matches)!=len(picture.prov):continue
        refs = {match['table_ref'] for match in matches}
        for ref in refs - complete.keys():
            complete[ref] = ref not in excluded and _complete_source_cells(entries[ref], api, preview_assets)
        if not all(complete[ref] for ref in refs):
            if picture.self_ref not in excluded:
                for ref in sorted(refs):
                    if not complete[ref]:
                        issue = dict(ref=ref, page=entries[ref]['page'], bbox=entries[ref].get('bbox'),
                                     picture=picture.self_ref,
                                     reason='source_table_crop_retained_without_complete_structured_table')
                        issues.append(issue)
                        api.diagnostic_event('TABLE_SOURCE_RASTER_RETAINED', **issue)
            continue
        # The following ownership pass reparents independent children and captions.
        if picture.children or picture.captions:
            continue
        already_excluded=picture.self_ref in excluded
        excluded.add(picture.self_ref)
        proof=dict(picture=picture.self_ref,source_image_sha256=_image_digest(picture),
            source_parts=matches,already_excluded=already_excluded,
            method='complete_structured_table_owns_source_raster')
        removed.append(proof);api.diagnostic_event('KNOWN_TABLE_RASTER_REMOVED',**proof)
    manifest['known_table_rasters_removed']=removed
    manifest['known_table_raster_policy_issues']=issues
    return removed


def table_review_notice(soup, *, page=None, ref=None, caption=None):
    """A user-visible notice that carries no image and survives all exports."""
    attrs = {'role': 'note', 'data-unrecognized-table': 'true'}
    if page is not None:
        attrs['data-pdf-page'] = str(page)
    if ref:
        attrs['data-pdf-table-ref'] = ref
    node = soup.new_tag('aside', attrs=attrs)
    paragraph = soup.new_tag('p')
    location = f' (страница PDF {page})' if page is not None else ''
    paragraph.string = 'Таблица распознана не полностью' + location + '.'
    if caption:
        node.append(caption)
    node.append(paragraph)
    return node


def remove_published_table_rasters(soup, *, captioned_layout=True):
    """Keep legacy crops unless the same published table still contains their pixels."""
    from docling_pdf_forms import FIGURE_CAPTION, FORM_HEADING

    table_caption = re.compile(r'^\s*(?:(?:Продолжение|Окончание)\s+)?(?:Таблиц[аы]|Табл\.)\s*'
        r'(?:[А-ЯЁA-Z]\s*\.\s*)?\d+(?:[.\-]\d+)*(?:\s*$|\s*[—–-])', re.I)
    changed = 0
    for figure in soup.find_all('figure'):
        if figure.find_parent(['td', 'th']):
            continue
        caption = figure.find('figcaption', recursive=False)
        title = caption.get_text(' ', strip=True) if caption else ''
        marked = 'source-table' in figure.get('class', [])
        if not marked and not (captioned_layout and figure.find('img') and table_caption.match(title)):
            continue
        if FIGURE_CAPTION.fullmatch(title) or FORM_HEADING.fullmatch(title):
            classes = [value for value in figure.get('class', []) if value != 'source-table']
            figure['class'] = list(dict.fromkeys([*classes, 'source-picture']))
            changed += 1
            continue
        ref = figure.get('data-pdf-table-ref')
        if not ref or figure.find(['figure', 'table']):
            continue
        owners = soup.find_all('table', attrs={'data-pdf-table-ref': ref})
        if len(owners) != 1:
            continue
        owner = owners[0]
        page = figure.get('data-pdf-page')
        if not page or owner.get('data-pdf-page') != page:
            continue
        published = {img['src'] for img in owner.find_all('img', src=True)
                     if img.find_parent('table') is owner and img['src']}
        images = figure.find_all('img')
        if not images or any(img.get('src') not in published for img in images):
            continue
        for img in images:
            img.decompose()
        if caption:
            owner_caption = owner.find('caption', recursive=False)
            if owner_caption and caption.decode_contents() == owner_caption.decode_contents():
                caption.decompose()
            else:
                caption.name = 'p'
        figure.unwrap()
        changed += 1
    return changed


def editable_table_partial(fragment, manifest, api):
    """Keep an intact grid on error and leave gaps visible in unread cells."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment, 'html.parser')
    if soup.table is None or api.table_profile(str(soup.table))['issues']:
        return None
    known = {entry['source_sha256'] for entry in manifest.get('typed_table_picture_assets', [])}
    for image in soup.table.find_all('img'):
        digest = hashlib.sha256(image.get('src', '').encode()).hexdigest()
        if image.get('data-table-content') != 'picture' or digest not in known:
            image.replace_with('[Фрагмент ячейки не распознан]')
    return str(soup.table) if soup.table.get_text(strip=True) or soup.table.find(['img', 'math']) else None
