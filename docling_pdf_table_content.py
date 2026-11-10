"""Publish table text/math as markup and only typed pictures as source crops."""
import base64
from hashlib import sha256
import json
import math
from pathlib import Path

from bs4 import BeautifulSoup

SCHEMA = 1
PICTURE_LABELS = {'image', 'figure', 'diagram', 'picture'}


class TableContentError(RuntimeError):
    """A table raster cannot be silently substituted for editable content."""


def _safe_markup(content, api):
    parsed = BeautifulSoup(content, 'html.parser')
    if parsed.find(['img', 'svg', 'script', 'style', 'iframe', 'object']):
        raise TableContentError('Typed table text contains unresolved image or executable markup')
    for node in parsed.find_all(True):
        for name in list(node.attrs):
            if name.lower().startswith('on') or name.lower() in {'src', 'srcset', 'href'}:
                del node[name]
    for node in list(parsed.find_all('math')):
        if not node.get_text(strip=True) and not node.get('data-latex', '').strip():
            raise TableContentError('Empty formula has no editable source notation')
        if node.find_parent('math') or node.find(['mi', 'mn', 'mo', 'mrow', 'mfrac', 'msup', 'msub', 'mtext']):
            continue
        latex = node.get('data-latex') or node.get_text()
        rendered = api.latex_math(latex, node.get('display', 'inline'))
        if rendered:
            node.replace_with(BeautifulSoup(rendered, 'html.parser').math)
        else:
            # Failed MathML conversion is still editable source LaTeX.
            literal = parsed.new_tag('span', attrs={'class': 'source-latex', 'data-latex': latex})
            literal.string = '\\(' + latex + '\\)'
            node.replace_with(literal)
    return str(parsed)


def recognize_typed_crop(image, args, page, cell, api):
    """Read one ambiguous raster, keeping the model's type and source bbox.

    Use the installed Surya backend's trained high-accuracy contract. A
    private, integrity-checked response cache supports snapshot replays.
    """
    from surya.inference.parsers import parse_full_page_html
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_HIGH_ACCURACY_BBOX

    digest = sha256(f'typed-table:{SCHEMA}:{image.size}:'.encode() + image.convert('RGB').tobytes()).hexdigest()
    root = getattr(args, 'ocr_cache_dir', None)
    path = Path(root) / 'typed_table_content' / (digest + '.json') if root else None
    blocks = None
    if path and path.is_file():
        saved = json.loads(path.read_text(encoding='utf8'))
        if (saved.get('schema') != SCHEMA or saved.get('image_sha256') != digest
                or sha256(saved.get('raw', '').encode()).hexdigest() != saved.get('response_sha256')):
            raise TableContentError('Invalid typed table OCR cache')
        raw = saved['raw']
        blocks = parse_full_page_html(raw)
        if blocks:
            api.diagnostic_event('TYPED_TABLE_CONTENT_CACHE_HIT', page=page, cell=cell, key=digest)
    if not blocks:
        model = getattr(args, 'surya_ocr_model', None)
        if model is None:
            raise TableContentError('Table image requires typed OCR; no response cache or OCR model is available')
        api.diagnostic_event('TYPED_TABLE_CONTENT_REQUEST', page=page, cell=cell, key=digest)
        outputs = api.local_surya_backend(model).generate([BatchInputItem(image=image,
            prompt_type=PROMPT_TYPE_HIGH_ACCURACY_BBOX, max_tokens=8192,
            metadata=dict(page=page, element='typed_table_content', cell=cell))])
        if len(outputs) != 1 or outputs[0].error or outputs[0].token_count >= 8192:
            raise TableContentError('Typed table OCR did not finish')
        raw = outputs[0].raw
        blocks = parse_full_page_html(raw)
        if not blocks:
            raise TableContentError('Typed table OCR returned no positioned content')
        if path:
            saved = dict(schema=SCHEMA, image_sha256=digest, raw=raw,
                         response_sha256=sha256(raw.encode()).hexdigest())
            path.parent.mkdir(parents=True, exist_ok=True)
            api.write_atomic(path, json.dumps(saved, ensure_ascii=False, indent=2))
    return [dict(label=b.label, bbox=list(b.bbox), html=b.html) for b in blocks]


def _render_typed(image, blocks, width, page, position, manifest, api):
    if not blocks:
        raise TableContentError('Table raster has no typed recognition result')
    blocks = _refine_picture_bounds(image, blocks)
    parts = []
    boxes = []
    for block in blocks:
        box = block.get('bbox')
        if (not box or len(box) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)
                or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000)):
            raise TableContentError('Invalid typed table block bounds')
        label = str(block.get('label', '')).casefold()
        if label in PICTURE_LABELS:
            payload = BeautifulSoup(block.get('html', ''), 'html.parser')
            if payload.get_text(strip=True) or payload.find('math'):
                raise TableContentError('Picture block carries editable text or formula')
            # The source rectangle belongs to this picture, never the entire
            # enclosing physical cell or its neighbouring text/math blocks.
            pixels = (math.floor(box[0]*image.width/1000), math.floor(box[1]*image.height/1000),
                      math.ceil(box[2]*image.width/1000), math.ceil(box[3]*image.height/1000))
            crop = image.crop(pixels)
            try:
                fragment = api.image_html(crop, width*(box[2]-box[0])/1000, 'cell')
            finally:
                crop.close()
            picture = BeautifulSoup(fragment, 'html.parser').img
            picture['data-table-content'] = 'picture'
            picture['data-source-cell'] = 'true'
            picture['data-cell-content'] = 'source-diagram'
            proof = dict(page=page, cell=position, label=block['label'],
                         normalized_bbox=box, source_pixel_bbox=list(pixels),
                         source_sha256=sha256(picture['src'].encode()).hexdigest(),
                         source_image_sha256=sha256(base64.b64decode(picture['src'].split(',', 1)[1])).hexdigest())
            manifest.setdefault('typed_table_picture_assets', []).append(proof)
            parts.append(str(picture))
            boxes.append(box)
        elif label in {'blank-page', 'blank'}:
            if block.get('html', '').strip():
                raise TableContentError('Blank table block contains unclassified payload')
        else:
            content = _safe_markup(block.get('html', ''), api)
            parsed = BeautifulSoup(content, 'html.parser')
            if not parsed.get_text(strip=True) and not parsed.find('math'):
                raise TableContentError('Typed table text/equation was not recognized')
            parts.append(content)
            boxes.append(box)
    if not parts:
        raise TableContentError('Table raster recognition produced no publishable content')
    _require_ink_coverage(image, boxes)
    return ''.join(parts)


def _refine_picture_bounds(image, blocks):
    """Include a picture's measured stroke fringe, without neighbouring text."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    distances = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    positive = distances[distances > 0]
    halo = max(2, math.ceil(2*float(np.median(positive)))+2) if positive.size else 2
    _, labels, components, _ = cv2.connectedComponentsWithStats(ink, 8)
    result = []
    for block in blocks:
        if block.get('label', '').casefold() not in PICTURE_LABELS:
            result.append(block)
            continue
        box = block.get('bbox')
        if not box or len(box) != 4 or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
            raise TableContentError('Invalid typed picture bounds')
        left, top = math.floor(box[0]*image.width/1000), math.floor(box[1]*image.height/1000)
        right, bottom = math.ceil(box[2]*image.width/1000), math.ceil(box[3]*image.height/1000)
        bounds = [left, top, right, bottom]
        owned, counts = np.unique(labels[top:bottom, left:right], return_counts=True)
        for index, count in zip(owned, counts):
            if not index:
                continue
            x, y, w, h, area = components[index]
            if (count < .9*area or x < left-halo or y < top-halo
                    or x+w > right+halo or y+h > bottom+halo):
                continue
            bounds = [min(bounds[0], int(x)), min(bounds[1], int(y)),
                      max(bounds[2], int(x+w)), max(bounds[3], int(y+h))]
        refined = [1000*bounds[0]/image.width, 1000*bounds[1]/image.height,
                   1000*bounds[2]/image.width, 1000*bounds[3]/image.height]
        for other in blocks:
            if other is block or other.get('label', '').casefold() in PICTURE_LABELS:
                continue
            b = other.get('bbox')
            if b and min(refined[2], b[2]) > max(refined[0], b[0]) and min(refined[3], b[3]) > max(refined[1], b[1]):
                raise TableContentError('Picture bounds overlap editable text or formula')
        result.append(dict(block, bbox=refined))
    return result


def _require_ink_coverage(image, boxes):
    """Reject a partial answer which omits source text beside a picture."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    # Measure enclosing source rails, including thick/antialiased borders.
    # A fixed three-pixel inset leaves their fringes as false missing text.
    from docling_pdf_cell_crop import cell_content_crop
    bounds = [0, 0, image.width, image.height]
    interior = cell_content_crop(image, bounds, bounds)
    if interior:
        (left, top, right, bottom), _, _ = interior
        ink[:top, :] = ink[bottom:, :] = ink[:, :left] = ink[:, right:] = 0
    covered = np.zeros_like(ink)
    for box in boxes:
        left = max(0, math.floor(box[0]*image.width/1000)-3)
        top = max(0, math.floor(box[1]*image.height/1000)-3)
        right = min(image.width, math.ceil(box[2]*image.width/1000)+3)
        bottom = min(image.height, math.ceil(box[3]*image.height/1000)+3)
        covered[top:bottom, left:right] = 255
    missing = cv2.bitwise_and(ink, cv2.bitwise_not(covered))
    _, _, components, _ = cv2.connectedComponentsWithStats(missing, 8)
    if any(area >= 3 for _, _, _, _, area in components[1:]):
        raise TableContentError('Typed table OCR omitted visible source content')


def _source_crop(source_image, region, box):
    if source_image is None or not region or not box:
        raise TableContentError('Typed table recovery has no original source region')
    if len(region) != 4 or len(box) != 4 or region[2] <= region[0] or region[3] <= region[1]:
        raise TableContentError('Invalid complete source region')
    sx = source_image.width / (region[2]-region[0])
    sy = source_image.height / (region[3]-region[1])
    epsilon = min(1/sx, 1/sy)
    if not (region[0]-epsilon <= box[0] < box[2] <= region[2]+epsilon
            and region[1]-epsilon <= box[1] < box[3] <= region[3]+epsilon):
        raise TableContentError('Complete physical cell is not available in source region')
    pixels = (max(0, math.floor((box[0]-region[0])*sx)),
              max(0, math.floor((box[1]-region[1])*sy)),
              min(source_image.width, math.ceil((box[2]-region[0])*sx)),
              min(source_image.height, math.ceil((box[3]-region[1])*sy)))
    if pixels[0] >= pixels[2] or pixels[1] >= pixels[3]:
        raise TableContentError('Typed table recovery region is outside source pixels')
    return source_image.crop(pixels)


def _physical_cell(geometry, position):
    keys = ('row', 'column', 'row_span', 'column_span')
    for cell in (geometry or {}).get('cells', []):
        if all(cell.get(k) == position.get(k) for k in keys):
            return cell
    return None


def _formula_anchors(fragment, api):
    from collections import Counter
    from docling_pdf_repairs import math_signature

    parsed = BeautifulSoup(fragment, 'html.parser')
    for image in parsed.find_all(['img', 'svg']):
        image.decompose()
    parsed = BeautifulSoup(_safe_markup(str(parsed), api), 'html.parser')
    anchors = [repr(math_signature(str(node))) for node in parsed.find_all('math')
               if not node.find_parent('math')]
    anchors += ['latex:'+node.get('data-latex', node.get_text())
                for node in parsed.select('.source-latex')]
    return Counter(anchors)


def _retained_blocks(args, page, box):
    """Use positioned primary text/math without requiring an OCR table grid."""
    blocks = []
    for record in getattr(args, 'surya_records', ()):
        bounds = record.get('bbox')
        if (record.get('page_no') != page or not bounds or len(bounds) != 4
                or record.get('label', '').casefold() not in {'text', 'equation', 'caption', 'section-header'}):
            continue
        if not (box[0] <= bounds[0] and box[1] <= bounds[1]
                and bounds[2] <= box[2] and bounds[3] <= box[3]):
            continue
        blocks.append(dict(label=record['label'], html=record['html'], bbox=[
            1000*(bounds[0]-box[0])/(box[2]-box[0]), 1000*(bounds[1]-box[1])/(box[3]-box[1]),
            1000*(bounds[2]-box[0])/(box[2]-box[0]), 1000*(bounds[3]-box[1])/(box[3]-box[1])],
            source_bbox=list(bounds), source_record_sha256=sha256(record['html'].encode()).hexdigest()))
    return blocks


def _combine_blocks(retained, recognized):
    """Retained primary words own their positions; OCR supplies missing parts."""
    def area(b): return (b[2]-b[0])*(b[3]-b[1])
    def overlap(a, b):
        return max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    result = list(retained)
    for block in recognized:
        box = block.get('bbox')
        if not box or len(box) != 4:
            raise TableContentError('Unpositioned typed table response')
        hits = [r for r in retained if overlap(box, r['bbox']) > .15*area(r['bbox'])]
        if block.get('label', '').casefold() in PICTURE_LABELS:
            if hits:
                raise TableContentError('Picture crop overlaps positioned primary text or math')
            result.append(block)
        elif not hits:
            result.append(block)
    return sorted(result, key=lambda b: (b['bbox'][1], b['bbox'][0]))


def normalize_table_content(fragment, geometry, source_image, region, page, args, manifest, api,
                            *, recognize=None, ref=None):
    """Resolve each raster in place, retaining mixed-cell order and spans.

    Existing editable markup is never deleted because of grid uncertainty.
    OCR-supplied image URLs never become source-image evidence. Unresolved
    crops stop publication rather than silently emitting a picture of text.
    """
    recognize = recognize or recognize_typed_crop
    soup = BeautifulSoup(fragment, 'html.parser')
    before = api.table_profile(fragment) if soup.table else None
    tasks = []
    if soup.table and before and not before['issues']:
        nodes = [n for n in soup.table.find_all(['td', 'th']) if n.find_parent('table') is soup.table]
        for node, position in zip(nodes, before['cells']):
            physical = _physical_cell(geometry, position)
            retained = _retained_blocks(args, page, physical['bbox']) if physical else []
            math_missing = any(_formula_anchors(b['html'], api) - _formula_anchors(node.decode_contents(), api)
                               for b in retained)
            if node.find('img') or (physical or {}).get('pending_primary_html') or math_missing:
                if not physical or not physical.get('bbox'):
                    tasks = [(soup, dict(region=True), dict(bbox=region))]
                    break
                tasks.append((node, position, physical))
    elif soup.find('img'):
        tasks = [(soup, dict(region=True), dict(bbox=region))]
    for node, position, physical in tasks:
        # Never treat OCR HTML/base64 as evidence. Re-read ORIGINAL page
        # pixels for the physical owner, including text beside its picture.
        box = physical['bbox']
        image = _source_crop(source_image, region, box)
        original = node.decode_contents()
        try:
            retained = _retained_blocks(args, page, box)
            rendered = None
            if retained:
                try:
                    rendered = _render_typed(image, retained, box[2]-box[0], page, position, {}, api)
                    blocks = retained
                except TableContentError:
                    pass  # Additional drawing/text must be resolved from source.
            if rendered is None:
                blocks = _combine_blocks(retained, recognize(image, args, page, position, api))
                proof = {}
                rendered = _render_typed(image, blocks, box[2]-box[0], page, position, proof, api)
                manifest.setdefault('typed_table_picture_assets', []).extend(proof.get('typed_table_picture_assets', []))
        finally:
            image.close()
        # Preserve known editable words: a shorter OCR answer is not a
        # successful resolution of the mixed cell.
        expected = BeautifulSoup(original, 'html.parser')
        pending = BeautifulSoup(physical.get('pending_primary_html') or '', 'html.parser')
        actual = BeautifulSoup(rendered, 'html.parser')
        required_math = _formula_anchors(str(expected), api) | _formula_anchors(str(pending), api)
        if required_math - _formula_anchors(str(actual), api):
            raise TableContentError('Typed table OCR omitted or changed an existing formula')
        for parsed in (expected, pending, actual):
            for math_node in parsed.find_all('math'):
                math_node.decompose()
        import re
        tokens = lambda parsed: re.findall(r'\w+|[^\w\s]', parsed.get_text(' ', strip=True).replace('\u00ad', ''))
        actual_tokens = tokens(actual)
        for known in (expected, pending):
            remaining = iter(actual_tokens)
            if not all(any(candidate == token for candidate in remaining) for token in tokens(known)):
                raise TableContentError('Typed table OCR omitted or changed existing editable text')
        node.clear()
        node.append(BeautifulSoup(rendered, 'html.parser'))
        physical.pop('pending_primary_html', None)
        physical.pop('typed_content_recovery_required', None)
        for block in retained:
            manifest.setdefault('typed_table_primary_ownership', []).append(dict(
                page=page, ref=ref, source_bbox=block['source_bbox'],
                source_record_sha256=block['source_record_sha256']))
        manifest.setdefault('typed_table_content_repairs', []).append(dict(page=page,
            **{k: position[k] for k in ('row', 'column', 'row_span', 'column_span') if k in position},
            original_html_sha256=sha256(original.encode()).hexdigest(),
            blocks=[b['label'] for b in blocks]))
        api.diagnostic_event('TYPED_TABLE_CONTENT_RESTORED', page=page, cell=position,
                             blocks=[b['label'] for b in blocks])
    # Preserve existing native text and normalize remaining raw LaTeX too.
    for node in list(soup.find_all('math')):
        normalized = _safe_markup(str(node), api)
        node.replace_with(BeautifulSoup(normalized, 'html.parser'))
    if before and soup.table:
        after = api.table_profile(str(soup.table))
        shape = lambda p: [(c['row'], c['column'], c['row_span'], c['column_span']) for c in p['cells']]
        if shape(before) != shape(after) or after['issues']:
            raise TableContentError('Typed table content changed the enclosing physical grid')
    return str(soup)


def validate_published_table_images(soup, manifest):
    """Catch late serializer/footnote routes which bypass typed recovery."""
    known = {entry['source_sha256'] for entry in manifest.get('typed_table_picture_assets', [])}
    for table in soup.find_all('table'):
        for image in table.find_all('img'):
            digest = sha256(image.get('src', '').encode()).hexdigest()
            if image.get('data-table-content') != 'picture' or digest not in known:
                raise TableContentError('Table image lacks typed picture provenance at publication')


def exclude_typed_primary_copies(doc, routes, excluded, manifest, api):
    """Remove only exact positioned source packets already owned by a table."""
    for item in doc.texts:
        region = api.item_region(item, doc)
        if not region or len(item.prov) != 1 or item.self_ref in excluded:
            continue
        digest = sha256((item.orig or '').encode()).hexdigest()
        for proof in manifest.get('typed_table_primary_ownership', []):
            if (proof['page'] != region[0] or not routes.get(proof['ref'])
                    or digest != proof['source_record_sha256']):
                continue
            if any(abs(a-b) > 1 for a, b in zip(region[1], proof['source_bbox'])):
                continue
            excluded.add(item.self_ref)
            manifest.setdefault('typed_table_duplicate_packets_removed', []).append(
                dict(ref=item.self_ref, owner=proof['ref'], page=region[0],
                     source_record_sha256=digest, bbox=region[1]))
            break
