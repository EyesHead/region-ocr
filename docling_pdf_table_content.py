"""Publish table text/math as markup and only typed pictures as source crops."""
import base64
from functools import lru_cache
from hashlib import file_digest, sha256
import json
import math

from bs4 import BeautifulSoup

PICTURE_LABELS = {'image', 'figure', 'diagram', 'picture'}


class TableContentError(RuntimeError):
    """A local assembly needs the primary-reading/source-preserving fallback."""


def primary_table_fallback(records, page, region, api, *, current=None, geometry=None):
    """Return an existing complete primary reading after a local assembly failure."""
    if current and geometry and not geometry.get('page_scan_transcription_required'):
        source = BeautifulSoup(current, 'html.parser')
        if source.table is not None:
            profile = api.table_profile(current)
            nodes = [node for node in source.table.find_all(['td', 'th'])
                     if node.find_parent('table') is source.table]
            if any(_has_source_authority(node, _physical_cell(geometry, cell), geometry)
                   for node, cell in zip(nodes, profile['cells'])):
                return current, dict(source_bbox=list(region), basis='visible_source_authority_retained')
    choices = {}
    for record in records:
        bounds = record.get('bbox')
        if record.get('page_no') != page or not bounds or len(bounds) != 4:
            continue
        overlap = max(0, min(region[2], bounds[2])-max(region[0], bounds[0])) * max(
            0, min(region[3], bounds[3])-max(region[1], bounds[1]))
        areas = [(b[2]-b[0])*(b[3]-b[1]) for b in (region, bounds)]
        if any(area <= 0 or overlap/area < .6 for area in areas):
            continue
        raw = record.get('html', '')
        parsed = BeautifulSoup(raw, 'html.parser')
        if len(parsed.find_all('table')) != 1 or not parsed.get_text(strip=True):
            continue
        try:
            content = _safe_markup(raw, api)
        except TableContentError:
            continue
        score = overlap/(sum(areas)-overlap)
        proof = dict(source_bbox=list(bounds), source_record_sha256=sha256(raw.encode()).hexdigest())
        if geometry and geometry.get('page_scan_transcription_required'):
            from docling_pdf_primary_rows import _align_primary_header
            aligned = _align_primary_header(content, geometry,
                {'columns': len(geometry.get('x_grid', []))-1}, api)
            if aligned:
                parsed = BeautifulSoup(content, 'html.parser')
                parsed.table.replace_with(BeautifulSoup(aligned[0], 'html.parser').table)
                content = str(parsed)
                proof['header_alignment'] = aligned[1]
            if api.table_profile(content)['issues']:
                parsed = BeautifulSoup(content, 'html.parser')
                repaired, operations = api.repair_table_spans(str(parsed.table),
                    len(geometry.get('x_grid', []))-1)
                parsed.table.replace_with(BeautifulSoup(repaired, 'html.parser').table)
                content = str(parsed)
                proof['span_repairs'] = operations
        choices.setdefault(content, (score, proof))
    if not choices:
        return None
    content, (_, proof) = max(choices.items(), key=lambda entry: (entry[1][0], len(entry[0])))
    if geometry:
        content, packets = _retain_primary_footer(content, records, geometry, api)
        if packets:
            proof['preserved_footer_packets'] = packets
    return content, proof


def _retain_primary_footer(content, records, geometry, api):
    """Keep a separately proved footer when the body uses its primary grid."""
    rows = len(geometry.get('y_grid', []))-1
    columns = len(geometry.get('x_grid', []))-1
    footers = [cell for cell in geometry.get('cells', [])
        if cell.get('row', -1)+cell.get('row_span', 0) == rows
        and cell.get('column') == 0 and cell.get('column_span') == columns
        and cell.get('primary_packet_graft')]
    if len(footers) != 1:
        return content, []
    packets = footers[0]['primary_packet_graft'].get('packets', [])
    by_digest = {sha256(record.get('html', '').encode()).hexdigest(): record for record in records}
    if not packets or any(packet['source_record_sha256'] not in by_digest for packet in packets):
        return content, []
    parsed = BeautifulSoup(content, 'html.parser')
    if parsed.table is None:
        return content, []
    from collections import Counter
    wanted = Counter(packet['source_payload_sha256'] for packet in packets)
    footer = parsed.table.find('tfoot', recursive=False)
    known = Counter()
    if footer is not None:
        matches = {}
        for node in footer.find_all(['p', 'li', 'td', 'th', 'div', 'math']):
            digest = _typed_payload_digest(str(node))
            if digest in wanted:
                matches.setdefault(digest, []).append(node)
        for digest, nodes in matches.items():
            known[digest] = sum(not any(other is not node and any(parent is node for parent in other.parents)
                                        for other in nodes) for node in nodes)
    missing = []
    for packet in sorted(packets, key=lambda packet: (packet['source_bbox'][1], packet['source_bbox'][0])):
        digest = packet['source_payload_sha256']
        if known[digest]:
            known[digest] -= 1
        else:
            missing.append(packet)
    if missing:
        if footer is None:
            footer = parsed.new_tag('tfoot')
            parsed.table.append(footer)
        row = parsed.new_tag('tr')
        cell = parsed.new_tag('td', attrs={'colspan': str(max(1, api.table_profile(content)['columns'])),
                                         'data-table-note': 'true'})
        for packet in missing:
            raw = by_digest[packet['source_record_sha256']]['html']
            cell.append(BeautifulSoup(_safe_markup(raw, api), 'html.parser'))
        row.append(cell)
        footer.append(row)
    return str(parsed), packets


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
    """Read one source crop with the installed Surya contract on every call."""
    from surya.inference.parsers import parse_full_page_html
    model = getattr(args, 'surya_ocr_model', None)
    if model is None:
        raise TableContentError('Table image requires an OCR model')
    api.diagnostic_event('TYPED_TABLE_CONTENT_REQUEST', page=page, cell=cell)
    recognized = _infer_typed_blocks(image, model, page, cell, api, args=args)
    raw = ''.join('<div data-label="'+b['label']+'" data-bbox="'+
                  ' '.join(str(v) for v in b['bbox'])+'">'+b['html']+'</div>' for b in recognized)
    blocks = parse_full_page_html(raw)
    if not blocks:
        raise TableContentError('Typed table OCR returned no positioned content')
    return [dict(label=b.label, bbox=list(b.bbox), html=b.html) for b in blocks]


def _infer_typed_blocks(image, model, page, cell, api, *, args=None):
    """Classify source graphics without transcription; recover text through Surya."""
    from surya.layout import LayoutPredictor
    from surya.recognition import RecognitionPredictor

    api.local_surya_backend(model)
    manager = model._get_runtime()._manager
    # A physical grid proves the cell's extent, not that its contents are
    # prose. Use the existing typed layout path before asking for text.
    layouts = LayoutPredictor(manager)([image])
    layout = layouts[0] if len(layouts) == 1 else None
    detected_layout = layout
    has_pictures = False
    if layout is not None and not layout.error and layout.bboxes:
        pictures = [dict(label=b.label, bbox=[1000*b.bbox[0]/image.width,
                    1000*b.bbox[1]/image.height, 1000*b.bbox[2]/image.width,
                    1000*b.bbox[3]/image.height], html='') for b in layout.bboxes]
        has_pictures = any(b['label'].casefold() in PICTURE_LABELS for b in pictures)
        if not cell.get('has_primary_content') and all(b['label'].casefold() in PICTURE_LABELS for b in pictures):
            try:
                pictures = _refine_picture_bounds(image, pictures)
                _require_ink_coverage(image, [b['bbox'] for b in pictures],
                                      physical_cell=bool(cell.get('physical_cell_proven')))
            except TableContentError:
                pass
            else:
                api.diagnostic_event('TYPED_SOURCE_PICTURE_CLASSIFIED', page=page, cell=cell,
                                     blocks=len(pictures))
                return pictures
    if cell.get('physical_cell_proven') and not has_pictures:
        from dataclasses import replace
        from surya.layout.schema import LayoutBox, LayoutResult
        from surya.inference.util import detect_repeat_token
        from docling_pdf_ocr import output_within_request_budget
        import cv2
        import numpy as np

        # A source grid already supplies this block's exact geometry. A page
        # prompt needlessly asks for layout coordinates and can run away on a
        # two-letter unit name. Use the recognizer's trained BLOCK path.
        with image.convert('L') as gray:
            ink = cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
        content = _coverage_without_enclosing_rules(ink, physical_cell=True)
        _, _, components, _ = cv2.connectedComponentsWithStats(content, 8)
        glyphs = sum(area >= 3 for _, _, _, _, area in components[1:])
        estimate = max(256, min(3996, glyphs*8))
        compact_retry = bool(glyphs and glyphs <= 180 and max(image.size) <= 600
                             and detected_layout is not None and not detected_layout.error
                             and detected_layout.bboxes)
        layout = LayoutResult(image_bbox=[0, 0, image.width, image.height], bboxes=[LayoutBox(
            polygon=[[0, 0], [image.width, 0], [image.width, image.height], [0, image.height]],
            label='Text', raw_label='Text', position=0, count=estimate)])
        class CellManager:
            def generate(self, batch):
                outputs = manager.generate(batch)
                return [replace(out, error=bool(not output_within_request_budget(out, item.max_tokens)
                    or detect_repeat_token(out.raw))) for item, out in zip(batch, outputs, strict=True)]
        try:
            predictions = RecognitionPredictor(CellManager())([image], full_page=False, layout_results=[layout])
        except RuntimeError as error:
            if not str(error).startswith('Local block request failed after retry:'):
                raise  # Allocation/backend failures are not an empty OCR reading.
            if not compact_retry:
                failure = TableContentError('Physical cell OCR remained empty or invalid after block recovery')
                failure.context = dict(stage='typed_table_content', page=page, element=cell)
                raise failure from error
            predictions = []
        blocks = predictions[0].blocks if len(predictions) == 1 else []
        if len(blocks) == 1 and not blocks[0].error:
            html = blocks[0].html or ''
            parsed = BeautifulSoup(html, 'html.parser')
            if (parsed.get_text(strip=True) or parsed.find('math')) and not parsed.find(['img', 'svg']):
                api.diagnostic_event('TYPED_PHYSICAL_CELL_BLOCK_RECOGNIZED', page=page, cell=cell,
                                     max_tokens=estimate+100)
                return [dict(label='Text', bbox=[0, 0, 1000, 1000], html=html)]
        api.diagnostic_event('TYPED_PHYSICAL_CELL_BLOCK_REVIEW', page=page, cell=cell,
                             reason='bounded_block_has_no_complete_editable_payload')
        if not compact_retry:
            failure = TableContentError('Physical cell OCR returned no complete editable block')
            failure.context = dict(stage='typed_table_content', page=page, element=cell)
            raise failure
    # RecognitionPredictor owns validation of repeats/HTML and the trained
    # layout + block recovery. Calling backend.generate directly bypasses it.
    predictions = RecognitionPredictor(manager)([image], full_page=True,
        layout_results=layouts if layout is not None and not layout.error and layout.bboxes else None)
    if len(predictions) != 1 or not predictions[0].blocks or any(b.error for b in predictions[0].blocks):
        error = TableContentError('Typed table OCR failed after layout/block recovery')
        error.context = dict(stage='typed_table_content', page=page, element=cell)
        raise error
    return [dict(label=b.label, bbox=[1000*b.bbox[0]/image.width, 1000*b.bbox[1]/image.height,
                 1000*b.bbox[2]/image.width, 1000*b.bbox[3]/image.height], html=b.html or '')
            for b in predictions[0].blocks]


def _unwrap_text_cell_table(parsed, api, *, physical_cell, text_label, source_image=None):
    """Discard OCR-only empty columns around one ordered text stream."""
    wrappers = parsed.find_all('table')
    if len(wrappers) != 1 or wrappers[0].find('caption'):
        return False
    table = wrappers[0]
    cells = table.find_all(['td', 'th'])
    if len(cells) == 1:
        table.replace_with(BeautifulSoup(cells[0].decode_contents(), 'html.parser'))
        return True
    if not physical_cell:
        return False
    profile = api.table_profile(str(table))
    if (profile['issues'] or len(profile['cells']) != len(cells)
            or any(c['row_span'] != 1 or c['column_span'] != 1 for c in profile['cells'])):
        return False
    occupied = [(position, cell) for position, cell in zip(profile['cells'], cells)
                if cell.get_text(strip=True) or cell.find('math')]
    columns = {position['column'] for position, _ in occupied}
    if not occupied or (profile['columns'] != 1 and (not text_label or len(columns) != 1)):
        return False
    if source_image is not None and text_label:
        line_limit = _source_line_upper_bound(source_image)
        if line_limit is not None and len({position['row'] for position, _ in occupied}) > line_limit:
            raise TableContentError('Text-cell OCR proposed more rows than the source ink')
    # A physical source cell and a text block supply the ownership. Empty
    # model columns/rows have no payload; a real multi-column reading stays.
    payload = BeautifulSoup('', 'html.parser')
    for _, cell in occupied:
        part = payload.new_tag('div')
        for child in list(cell.contents):
            part.append(child.extract())
        payload.append(part)
    table.replace_with(payload)
    return True


def _source_line_upper_bound(image):
    # Count distinct ink bands as an upper bound on the printed text lines.
    import numpy as np
    from docling_pdf_typed_lines import measured_cell_interior

    interior = measured_cell_interior(image)
    if interior is None:
        return None
    with image.crop(interior[0]) as crop, crop.convert('L') as gray:
        rows = (np.asarray(gray) < 180).sum(axis=1) >= 2
    edges = np.flatnonzero(np.diff(np.r_[False, rows, False]))
    return sum(b-a >= 2 for a, b in zip(edges[::2], edges[1::2], strict=True))


def _render_typed(image, blocks, width, page, position, manifest, api,
                  *, source_reader=None, source_bbox=None, physical_cell=False, require_coverage=False,
                  allow_nested_table=False):
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
            if block.get('source_graphic_role'):
                proof['source_graphic_role'] = block['source_graphic_role']
                picture['data-diagram-evidence'] = 'verified-physical-grid-and-graphic-header'
            manifest.setdefault('typed_table_picture_assets', []).append(proof)
            parts.append(str(picture))
            boxes.append(box)
        elif label in {'blank-page', 'blank'}:
            if block.get('html', '').strip():
                raise TableContentError('Blank table block contains unclassified payload')
        else:
            content = _safe_markup(block.get('html', ''), api)
            parsed = BeautifulSoup(content, 'html.parser')
            # Block OCR can wrap ordinary cell text in a one-cell table.
            # Keep its complete payload without inventing a nested source grid.
            text_label = label in {'text', 'caption', 'section-header', 'sectionheader', 'title'}
            if (physical_cell or text_label) and _unwrap_text_cell_table(
                    parsed, api, physical_cell=physical_cell, text_label=text_label,
                    source_image=image if physical_cell else None):
                content = str(parsed)
                api.diagnostic_event('TYPED_TEXT_CELL_TABLE_UNWRAPPED', page=page, cell=position)
            if physical_cell and parsed.find('table') and (text_label or not allow_nested_table):
                raise TableContentError('Cell OCR proposed an unverified nested table')
            if not parsed.get_text(strip=True) and not parsed.find('math'):
                raise TableContentError('Typed table text/equation was not recognized')
            parts.append(content)
            boxes.append(box)
    if not parts:
        raise TableContentError('Table raster recognition produced no publishable content')
    try:
        _require_ink_coverage(image, boxes, physical_cell=physical_cell)
    except TableContentError as coverage_error:
        anchored = _source_text_coverage(image, blocks, source_reader, source_bbox, page, api, ''.join(parts))
        if anchored is not None:
            source_boxes, proof = anchored
            try:
                _require_ink_coverage(image, source_boxes, physical_cell=physical_cell)
            except TableContentError as error:
                coverage_error = error
            else:
                manifest.setdefault('typed_table_text_anchors', []).append(
                    dict(page=page, cell=position, **proof))
                coverage_error = None
        if coverage_error is not None:
            if require_coverage:
                raise coverage_error  # Routing proof failed; obtain a source-cell reading instead.
            warning = dict(page=page, cell=dict(position), bbox=list(source_bbox or ()),
                reason='ocr_boxes_do_not_cover_all_source_ink',
                message='Границы OCR не покрывают все пиксели ячейки. Результат сохранён; '
                        'это диагностическое замечание, а не отказ конвертации.')
            manifest.setdefault('typed_table_coverage_reviews', []).append(warning)
            api.diagnostic_event('TYPED_TABLE_COVERAGE_REVIEW', **warning)
    return ''.join(parts)


def _source_text_coverage(image, blocks, reader, box, page, api, content):
    """Anchor unchanged plain OCR text only to matching, visible PDF glyphs."""
    if reader is None or not box:
        return None
    if reader.has_raster_text_direction_conflict(page, box):
        return None
    if any(str(b.get('label', '')).casefold() not in {'text', 'caption', 'section-header', 'sectionheader', 'title'}
           for b in blocks):
        return None
    parsed = BeautifulSoup(content, 'html.parser')
    if parsed.find(['img', 'svg', 'math', 'table', 'sub', 'sup']):
        return None
    reader.load_page(page)
    glyphs = [g for g in reader.chars if g.get('text', '').strip()
              and box[0] <= (g['x0']+g['x1'])/2 < box[2]
              and box[1] <= (g['top']+g['bottom'])/2 < box[3]]
    if not glyphs:
        return None
    lines = api.native_cell_lines(glyphs)
    if any(line.get('source_scripts') for line in lines):
        return None
    # Source spelling never replaces the OCR answer. Every non-whitespace
    # character must already be present, in order, in both complete readings.
    compact = lambda value: ''.join(value.split())
    if compact(parsed.get_text()) != compact(''.join(line['text'] for line in lines)):
        return None
    import numpy as np
    from docling_pdf_text_cells import positioned_text_proof

    sx, sy = image.width/(box[2]-box[0]), image.height/(box[3]-box[1])
    with image.convert('L') as gray:
        mask = (np.asarray(gray) < 180).astype('uint8')*255
    proof = positioned_text_proof(str(parsed), glyphs, mask, box, box, sx, sy)
    if proof is None:
        return None
    boxes = [[max(0, 1000*(g['x0']-box[0])/(box[2]-box[0])),
              max(0, 1000*(g['top']-box[1])/(box[3]-box[1])),
              min(1000, 1000*(g['x1']-box[0])/(box[2]-box[0])),
              min(1000, 1000*(g['bottom']-box[1])/(box[3]-box[1]))] for g in glyphs]
    return boxes, dict(method='matching_typed_text_and_positioned_source_glyphs', **proof)


def _refine_picture_bounds(image, blocks):
    """Complete uniquely anchored source components without reclassifying blocks."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    distances = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    positive = distances[distances > 0]
    halo = max(2, math.ceil(2*float(np.median(positive)))+2) if positive.size else 2
    _, labels, components, centroids = cv2.connectedComponentsWithStats(ink, 8)
    anchors = {}
    owners = {}
    for block_index, block in enumerate(blocks):
        if block.get('label', '').casefold() not in PICTURE_LABELS:
            continue
        box = block.get('bbox')
        if not box or len(box) != 4 or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
            raise TableContentError('Invalid typed picture bounds')
        left, top = math.floor(box[0]*image.width/1000), math.floor(box[1]*image.height/1000)
        right, bottom = math.ceil(box[2]*image.width/1000), math.ceil(box[3]*image.height/1000)
        anchors[block_index] = [left, top, right, bottom]
        # Hollow drawings can have much of their ink just outside a predicted
        # box. A measured-stroke halo finds that ink; the original anchor must
        # still contain its centroid and own a majority of the component.
        owned, counts = np.unique(labels[max(0, top-halo):min(image.height, bottom+halo),
                                         max(0, left-halo):min(image.width, right+halo)], return_counts=True)
        for index, count in zip(owned, counts):
            if not index:
                continue
            cx, cy = centroids[index]
            if left <= cx < right and top <= cy < bottom and count >= .5*components[index, 4]:
                owners.setdefault(int(index), []).append(block_index)
    if any(len(value) != 1 for value in owners.values()):
        raise TableContentError('A source picture component has conflicting typed owners')
    result = []
    for block_index, block in enumerate(blocks):
        if block.get('label', '').casefold() not in PICTURE_LABELS:
            result.append(block)
            continue
        if block.get('source_graphic_role') and block['source_graphic_role'].get('source_rule_proof'):
            # Already bounded by measured cell interior in the original pixels.
            result.append(block)
            continue
        bounds = list(anchors[block_index])
        for index, owner in owners.items():
            if owner != [block_index]:
                continue
            x, y, w, h, area = components[index]
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


def _coverage_without_enclosing_rules(ink, *, physical_cell=False):
    """Exclude only complete thin boundary components from coverage accounting."""
    import cv2
    import numpy as np

    if not physical_cell:
        return ink
    height, width = ink.shape
    if min(height, width) < 12:
        return ink
    distance = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    positive = distance[distance > 0]
    if not positive.size:
        return ink
    band = min(max(3, math.ceil(2*float(np.median(positive)))+3),
               max(3, min(height, width)//20))
    _, labels, components, _ = cv2.connectedComponentsWithStats(ink, 8)
    result = ink.copy()
    for index, (x, y, w, h, _) in enumerate(components[1:], start=1):
        horizontal = w >= .75*width and (y <= 1 or y+h >= height-1)
        vertical = h >= .75*height and (x <= 1 or x+w >= width-1)
        if not (horizontal or vertical):
            continue
        yy, xx = np.where(labels[y:y+h, x:x+w] == index)
        yy, xx = yy+y, xx+x
        component = labels == index
        x_margin, y_margin = max(3, math.ceil(width*.08)), max(3, math.ceil(height*.08))
        horizontal_core = component[:, x_margin:width-x_margin]
        vertical_core = component[y_margin:height-y_margin, :]
        # A lone line near an image boundary is document content, not proof of
        # a table frame. Require at least three independently straight sides
        # of this physically verified cell, not just a long component bbox.
        if not horizontal_core.size or not vertical_core.size:
            continue
        straight_sides = sum((
            bool(y <= 1 and horizontal_core[:band].sum(axis=1).max() >= .75*horizontal_core.shape[1]),
            bool(y+h >= height-1 and horizontal_core[-band:].sum(axis=1).max() >= .75*horizontal_core.shape[1]),
            bool(x <= 1 and vertical_core[:, :band].sum(axis=0).max() >= .75*vertical_core.shape[0]),
            bool(x+w >= width-1 and vertical_core[:, -band:].sum(axis=0).max() >= .75*vertical_core.shape[0]),
        ))
        if straight_sides < 3:
            continue
        # A drawing or character attached to a rule and extending into the
        # interior invalidates this proof; no part of that component is hidden.
        if np.all((xx < band) | (xx >= width-band) | (yy < band) | (yy >= height-band)):
            result[yy, xx] = 0
    return result


def _require_ink_coverage(image, boxes, *, physical_cell=False):
    """Reject a partial answer which omits source text beside a picture."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ink = _coverage_without_enclosing_rules(ink, physical_cell=physical_cell)
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


def _is_source_graphic_header(text):
    """Recognize a graphic's own column, never its name or description field."""
    import re

    text = re.sub(r'\s+', ' ', text).strip()
    if re.search(r'\b(?:наименован\w*|назван\w*|описан\w*|характеристик\w*|номер\w*|шифр\w*|код\w*|'
                 r'формул\w*|уравнен\w*|математич\w*|выражени\w*)\b', text, re.I):
        return False
    if re.fullmatch(r'обозначение\s+и\s+изображение', text, re.I):
        return True
    return bool(re.match(
        r'^(?:(?:условн\w*|графическ\w*|цветографическ\w*|схематическ\w*|принципиальн\w*|электрическ\w*)\s+){0,2}'
        r'(?:изображен\w*|эскиз\w*|схем\w*|черт[её]ж\w*|геометрическ\w*\s+форм\w*|'
        r'условн\w*\s+графическ\w*\s+обозначен\w*)\b', text, re.I))


def _source_graphic_context(fragment, profile, geometry, page, ref, manifest):
    """Bind explicit graphic headers to the same proved physical column grid."""
    import re
    from collections import Counter

    ledger = manifest.setdefault('typed_source_graphic_contexts', [])
    context = dict(page=page, ref=ref, roles={}, text_columns=[], x_grid=[], verified=False)
    previous = next((entry for entry in reversed(ledger) if entry['ref'] != ref), None)
    ledger.append(context)
    if not profile or not geometry or not geometry.get('source_grid_verified'):
        return context
    columns = profile['columns']
    grid = geometry.get('x_grid', [])
    if (len(grid) != columns+1 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in grid)
            or any(b <= a for a, b in zip(grid, grid[1:]))
            or any(_physical_cell(geometry, cell) is None for cell in profile['cells'])):
        return context
    keys = ('row', 'column', 'row_span', 'column_span')
    if Counter(tuple(cell.get(key) for key in keys) for cell in geometry.get('cells', [])) != Counter(
            tuple(cell[key] for key in keys) for cell in profile['cells']):
        return context
    parsed = BeautifulSoup(fragment, 'html.parser')
    roles, text_columns = {}, set()
    nodes = [node for node in parsed.table.find_all(['td', 'th']) if node.find_parent('table') is parsed.table]
    is_line_legend = any(position.get('header')
                         and re.fullmatch(r'наименование\s+линии', node.get_text(' ', strip=True), re.I)
                         for node, position in zip(nodes, profile['cells'], strict=True))
    for node, position in zip(nodes, profile['cells'], strict=True):
        if not position.get('header') or position['column_span'] == columns:
            continue
        text = node.get_text(' ', strip=True)
        if re.fullmatch(r'(?:наименование|название|описание|примечание)(?:\s+(?:объекта|сети|линии))?', text, re.I):
            text_columns.update(range(position['column'], position['column']+position['column_span']))
        is_line_sample = (is_line_legend and position['column_span'] == 1
                          and re.fullmatch(r'обозначение', text, re.I))
        if not (is_line_sample or _is_source_graphic_header(text)):
            continue
        evidence = dict(text=text, source_ref=ref, source_page=page,
                        source_bbox=list(_physical_cell(geometry, position)['bbox']),
                        **{key: position[key] for key in keys})
        for column in range(position['column'], position['column']+position['column_span']):
            roles.setdefault(column, []).append(evidence)
    # The original audit already has the source caption; its older permissive
    # inheritance is not used here without explicit continuation evidence.
    audit = next((entry for entry in reversed(manifest.get('source_graphic_column_contexts', []))
                  if entry['ref'] == ref and entry['page'] == page), {})
    caption = audit.get('caption', '')
    match = re.search(r'(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|табл\.?)\s*'
                      r'([А-ЯЁA-Z]?\s*\.?\s*\d+(?:\s*\.\s*\d+)*)', caption, re.I)
    # The older audit can inherit its caption_id even when the printed
    # caption is empty. Only actual source-caption text proves identity here.
    caption_id = re.sub(r'\s+', '', match[1]).casefold() if match else ''
    if not roles and previous and previous.get('verified'):
        old_grid = previous['x_grid']
        same_grid = len(grid) == len(old_grid) and all(abs(a-b) <= 3 for a, b in zip(grid, old_grid))
        explicit_continuation = bool(re.match(r'^\s*(?:Продолжение|Окончание)\b', caption, re.I))
        same_identity = bool(caption_id and caption_id == previous.get('caption_id'))
        identity_compatible = not (caption_id and previous.get('caption_id') and not same_identity)
        adjacent = page == previous['page']+1 or (page == previous['page'] and explicit_continuation)
        if same_grid and adjacent and identity_compatible and (explicit_continuation or same_identity):
            roles = previous['roles']
            context['inherited_from'] = previous['ref']
            caption_id = caption_id or previous.get('caption_id', '')
    context.update(roles=roles, text_columns=sorted(text_columns - set(roles)),
                   x_grid=list(grid), verified=True, caption=caption, caption_id=caption_id)
    if roles:
        proof_cells = [{key: cell.get(key) for key in ('row', 'column', 'row_span', 'column_span', 'bbox')}
                       for cell in geometry.get('cells', [])]
        context['source_grid_sha256'] = sha256(json.dumps(
            dict(x_grid=grid, cells=proof_cells), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return context


def _has_separate_text_bands(content):
    import cv2
    import numpy as np

    occupied = np.count_nonzero(content, axis=1) >= 2
    edges = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
    groups = []
    for start, end in zip(edges[::2], edges[1::2], strict=True):
        if groups and start-groups[-1][1] <= 8:
            groups[-1][1] = end
        else:
            groups.append([start, end])
    strokes, small = [], []
    for start, end in groups:
        part = content[start:end]
        _, _, items, _ = cv2.connectedComponentsWithStats(part, 8)
        sizes = [max(w, h) for _, _, w, h, area in items[1:] if area >= 5]
        if not sizes:
            continue
        columns = np.flatnonzero(part.any(axis=0))
        band = (start, end, int(columns[0]), int(columns[-1]))
        if max(sizes) >= max(28, .14 * content.shape[1]):
            strokes.append(band)
        elif len(sizes) >= 2:
            small.append(band)
    # Keep only small labels lying alongside a measured diagram stroke.
    # Detached prose and standards references still require editable OCR.
    return any(not any(max(top - b, a - bottom) <= max(8, bottom - top)
                       and l <= left <= right <= r for a, b, l, r in strokes)
               for top, bottom, left, right in small)


def _source_graphic_blocks(image, position, context, physical_cell):
    """Crop complete original graphic content only under an explicit column role."""
    import cv2
    import numpy as np

    if not physical_cell or position.get('header') or not context.get('verified'):
        return None
    occupied = set(range(position['column'], position['column']+position['column_span']))
    roles = context['roles']
    if not occupied or not occupied.issubset(roles):
        return None
    with image.convert('L') as gray:
        values = np.asarray(gray)
        ink = cv2.threshold(values, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    from docling_pdf_typed_lines import measured_cell_interior
    interior = measured_cell_interior(image)
    left, top, right, bottom = interior[0] if interior else (0, 0, image.width, image.height)
    content = ink[top:bottom, left:right] if interior else _coverage_without_enclosing_rules(
        ink, physical_cell=True)
    if _has_separate_text_bands(content):
        return None
    yy, xx = np.where(content)
    if not len(xx):
        return None
    # Unproved boundary ink can be an open frame or a clipped drawing. It
    # cannot be swallowed into a whole-cell picture by the column heading.
    if xx.min() < 3 or yy.min() < 3 or xx.max() >= content.shape[1]-3 or yy.max() >= content.shape[0]-3:
        return None
    distance = cv2.distanceTransform(content, cv2.DIST_L2, 3)
    positive = distance[distance > 0]
    halo = max(2, math.ceil(2*float(np.median(positive))))
    pixels = [max(left, left+int(xx.min())-halo), max(top, top+int(yy.min())-halo),
              min(right, left+int(xx.max())+1+halo), min(bottom, top+int(yy.max())+1+halo)]
    evidence = dict(method='verified_physical_grid_and_explicit_graphic_column',
        source_grid_sha256=context['source_grid_sha256'],
        header_evidence=[header for column in sorted(occupied) for header in roles[column]],
        inherited_from=context.get('inherited_from'), source_content_pixel_bbox=pixels)
    if interior:
        evidence['source_rule_proof'] = interior[1]
    return [dict(label='Image', html='', bbox=[1000*pixels[0]/image.width, 1000*pixels[1]/image.height,
        1000*pixels[2]/image.width, 1000*pixels[3]/image.height], source_graphic_role=evidence)]


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
    seen = set()
    for record in getattr(args, 'surya_records', ()):
        bounds = record.get('bbox')
        if (record.get('page_no') != page or not bounds or len(bounds) != 4
                or record.get('label', '').casefold().replace('-', '').replace('_', '')
                not in {'text', 'equation', 'equationblock', 'formula', 'caption', 'sectionheader'}):
            continue
        if (any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in bounds)
                or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]):
            continue
        inside = (box[0] <= bounds[0] and box[1] <= bounds[1]
                  and bounds[2] <= box[2] and bounds[3] <= box[3])
        clipped = [max(box[0], bounds[0]), max(box[1], bounds[1]),
                   min(box[2], bounds[2]), min(box[3], bounds[3])]
        if not inside:
            # Independently predicted layout boxes can differ by a fraction of
            # a text line. Keep the whole primary payload, never cropped text.
            # Such records require reconciliation with typed content below;
            # their clipped boxes alone cannot certify a complete OCR result.
            margin = min(3.0, (bounds[3]-bounds[1])/4)
            overlap = max(0, clipped[2]-clipped[0])*max(0, clipped[3]-clipped[1])
            area = (bounds[2]-bounds[0])*(bounds[3]-bounds[1])
            if (not (box[0] <= (bounds[0]+bounds[2])/2 <= box[2]
                     and box[1] <= (bounds[1]+bounds[3])/2 <= box[3])
                    or overlap < .75*area
                    or any(overhang > margin for overhang in
                           (box[0]-bounds[0], box[1]-bounds[1], bounds[2]-box[2], bounds[3]-box[3]))):
                continue
        identity = (tuple(bounds), record['html'])
        if identity in seen:
            continue
        seen.add(identity)
        block = dict(label=record['label'], html=record['html'], bbox=[
            1000*(clipped[0]-box[0])/(box[2]-box[0]), 1000*(clipped[1]-box[1])/(box[3]-box[1]),
            1000*(clipped[2]-box[0])/(box[2]-box[0]), 1000*(clipped[3]-box[1])/(box[3]-box[1])],
            source_bbox=list(bounds), source_record_sha256=sha256(record['html'].encode()).hexdigest(),
            source_payload_sha256=_typed_payload_digest(record['html']))
        if not inside:
            block['boundary_reconciled'] = True
        blocks.append(block)
    return blocks


def _typed_payload_key(content, *, equivalent_glyphs=True):
    """Match text for ownership while keeping primary spelling and math intact."""
    import unicodedata
    from docling_pdf_repairs import math_signature

    # OCR can use the Latin or Cyrillic code point of the same printed glyph.
    # This key only locates a unique leaf; publication keeps primary HTML.
    confusables = str.maketrans('АВСЕНКМОРТХУаекорсху', 'ABCEHKMOPTXYaekopcxy')
    def text_key(value):
        value = ''.join(unicodedata.normalize('NFC', value).replace('\u00ad', '').split())
        return value.translate(confusables) if equivalent_glyphs else value
    def visit(node):
        if not getattr(node, 'name', None):
            value = text_key(str(node))
            return [('text', value)] if value else []
        if node.name == 'math':
            signature = math_signature(str(node))
            if signature is None and node.get_text(strip=True):
                from latex2mathml.converter import convert
                try:
                    rendered = convert(node.get('data-latex') or node.get_text(), display=node.get('display', 'inline'))
                    signature = math_signature(rendered)
                except Exception:
                    pass  # Unparseable source notation remains distinct and editable.
            return [('math', signature if signature is not None else str(node))]
        if node.name in {'sup', 'sub'}:
            return [(node.name, tuple(sequence(node.children)))]
        if node.name == 'span' and 'source-latex' in node.get('class', []):
            return [('latex', node.get('data-latex') or node.get_text())]
        if node.name in {'img', 'svg', 'input', 'hr', 'script', 'style', 'iframe', 'object'}:
            return [(node.name, str(node))]
        return sequence(node.children)
    def sequence(nodes):
        result = []
        for node in nodes:
            for part in visit(node):
                if result and result[-1][0] == part[0] == 'text':
                    result[-1] = ('text', result[-1][1]+part[1])
                else:
                    result.append(part)
        return result
    parsed = BeautifulSoup(content, 'html.parser') if isinstance(content, str) else content
    return tuple(sequence(parsed.children))


def _typed_payload_digest(content):
    value = json.dumps(_typed_payload_key(content, equivalent_glyphs=False), ensure_ascii=False, separators=(',', ':'))
    return sha256(value.encode()).hexdigest()


def _cell_content_signature(content, api):
    """Compare complete cell content independently of inline MathML boundaries."""
    import unicodedata
    from docling_pdf_repairs import math_signature

    parsed = BeautifulSoup(content, 'html.parser')
    for image in parsed.find_all(['img', 'svg']):
        image.decompose()
    parsed = BeautifulSoup(_safe_markup(str(parsed), api), 'html.parser')
    def sequence(parts):
        result = []
        for part in parts:
            values = part[1] if part and part[0] == 'sequence' else [part]
            for value in values:
                if value is None:
                    continue
                if result and result[-1][0] == value[0] == 'text':
                    result[-1] = ('text', result[-1][1]+value[1])
                else:
                    result.append(value)
        return ('sequence', tuple(result))
    def visit(node):
        if not getattr(node, 'name', None):
            value = ''.join(unicodedata.normalize('NFC', str(node)).replace('\u00ad', '').split())
            return ('text', value) if value else None
        if node.name == 'math':
            return math_signature(str(node)) or ('source-math', str(node))
        if node.name == 'input':
            return ('input', str(node))
        if node.name in {'sup', 'sub'}:
            return (node.name, sequence(visit(child) for child in node.children))
        return sequence(visit(child) for child in node.children)
    return visit(parsed)


def review_primary_cell_conflict(existing, primary, page, position, box, manifest, api):
    """Keep the complete primary source while exposing an optional read's conflict."""
    parsed = BeautifulSoup(existing or '', 'html.parser')
    if not parsed.get_text(strip=True) and not parsed.find(['math', 'input']):
        return
    if _cell_content_signature(existing, api) == _cell_content_signature(primary, api):
        return
    first_column = position.get('column', 0)+1
    span = position.get('column_span', 1)
    columns = (f'столбцы {first_column}–{first_column+span-1}' if span > 1
               else f'столбец {first_column}')
    record = dict(page=page, row=position.get('row'), column=position.get('column'),
                  row_span=position.get('row_span', 1), column_span=span,
                  bbox=list(box), reason='optional_read_conflicts_with_complete_primary_packets',
                  message=f"Строка {position.get('row', 0)+1}, {columns}: "
                          'дополнительное чтение отличается; сохранён полный первичный OCR.',
                  primary_sha256=sha256(primary.encode()).hexdigest(),
                  secondary_sha256=sha256(existing.encode()).hexdigest())
    reviews = manifest.setdefault('primary_cell_reconciliation_reviews', [])
    if record not in reviews:
        reviews.append(record)
        api.diagnostic_event('PRIMARY_CELL_RECONCILIATION_REVIEW', **record)


def _record_primary_ownership(manifest, packets, page, ref):
    if ref is None:
        return
    ledger = manifest.setdefault('typed_table_primary_ownership', [])
    for packet in packets:
        proof = dict(page=page, ref=ref, **{key: packet[key] for key in
            ('source_bbox', 'source_record_sha256', 'source_payload_sha256')})
        if proof not in ledger:
            ledger.append(proof)


def _has_source_authority(node, cell, geometry):
    from docling_pdf_text_cells import owns_positioned_text
    return bool(node.get('data-source-authority') == 'vector-pdf-literal'
                or node.select_one('[data-source-authority="vector-pdf-literal"]')
                or owns_positioned_text(node, cell or {}, geometry or {}))


def _native_formula_fragments(node, cell):
    """Identify source glyphs whose equation still contains raster fragments.

    Positioned glyph coverage proves the individual characters, not the
    reading order of a fraction or the structure joining those characters.
    Complete primary OCR payload and source diagrams keep their own authority.
    """
    import re

    if (not cell or cell.get('primary_ocr') or cell.get('primary_packet_graft')
            or cell.get('pending_primary_html') or node.find('table')
            or node.select_one('[data-cell-content="source-diagram"]')):
        return False
    images = node.find_all('img')
    if not images or any(image.get('data-source-cell') != 'true' for image in images):
        return False
    return bool(node.find('math') or re.search(r'[=<>≤≥σβπ∑√]', node.get_text()))


def _require_native_formula_atoms(original, recognized, api, *, cell=None):
    """Keep the visible native atoms while allowing a new equation structure."""
    from collections import Counter
    import unicodedata

    confusables = str.maketrans('АВСЕНКМОРТХУаекорсху', 'ABCEHKMOPTXYaekopcxy')
    def atoms(content):
        parsed = BeautifulSoup(content, 'html.parser')
        for image in parsed.find_all(['img', 'svg']):
            image.decompose()
        parsed = BeautifulSoup(_safe_markup(str(parsed), api), 'html.parser')
        text = unicodedata.normalize('NFKC', parsed.get_text()).translate(confusables)
        text = text.translate(str.maketrans({'−': '-', '–': '-', '×': '·', '⋅': '·', '*': '·'}))
        return Counter(char for char in text if char.isalnum() or char in '=<>≤≥±+-·')
    missing = atoms(original) - atoms(recognized)
    if missing:
        raise TableContentError('Complete cell OCR omitted visible native formula atoms')
    from docling_pdf_native_scripts import script_terms
    required_scripts = Counter((proof['base'], role, value.replace('-', '−'))
        for line in (cell or {}).get('lines', []) for proof in line.get('source_scripts', [])
        if proof.get('method') == 'source_glyph_size_text_origin_and_adjacent_base'
        for role, value in proof.get('scripts', {}).items())
    actual_scripts = script_terms(_safe_markup(recognized, api))
    if required_scripts - actual_scripts:
        raise TableContentError('Complete cell OCR changed source-proved formula indices')


def _merge_typed_leaves(block, primary):
    """Keep a recognized container and restore its uniquely matched primary leaves."""
    parsed = BeautifulSoup(block['html'], 'html.parser')
    units = parsed.find_all(['td', 'th', 'p', 'li', 'caption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    units.append(parsed)
    by_key = {}
    for node in units:
        # A text packet cannot replace a row or an enclosing/nested grid.
        if node.find('table'):
            continue
        by_key.setdefault(_typed_payload_key(node), []).append(node)
    replacements = []
    for known in primary:
        key = _typed_payload_key(known['html'])
        matches = by_key.get(key, []) if key else []
        # Nested wrappers of the same payload are one leaf, not alternatives.
        matches = [node for node in matches if not any(
            other is not node and any(parent is node for parent in other.parents) for other in matches)]
        if len(matches) != 1:
            raise TableContentError('Overlapping typed table content has no unique primary-text match')
        target = matches[0]
        if any(target is old or any(p is old for p in target.parents)
               or any(p is target for p in old.parents) for old, _ in replacements):
            raise TableContentError('Overlapping primary table packets claim the same recognized content')
        replacements.append((target, known))
    for target, known in replacements:
        content = BeautifulSoup(known['html'], 'html.parser')
        roots = [node for node in content.contents if getattr(node, 'name', None) or str(node).strip()]
        if len(roots) == 1 and getattr(roots[0], 'name', None) in {'p', 'div'}:
            roots = list(roots[0].contents)
        target.clear()
        for node in roots:
            target.append(node.extract())
    return dict(block, html=str(parsed))


def _combine_blocks(retained, recognized):
    """Retained primary words own their positions; OCR supplies missing parts."""
    def area(b): return (b[2]-b[0])*(b[3]-b[1])
    def overlap(a, b):
        return max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    result = []
    consumed = set()
    for block in recognized:
        box = block.get('bbox')
        if not box or len(box) != 4:
            raise TableContentError('Unpositioned typed table response')
        hits = [i for i, r in enumerate(retained) if overlap(box, r['bbox']) > .15*area(r['bbox'])]
        if block.get('label', '').casefold() in PICTURE_LABELS:
            if hits:
                raise TableContentError('Picture crop overlaps positioned primary text or math')
            result.append(block)
        elif hits:
            if any(i in consumed for i in hits):
                raise TableContentError('Typed table responses duplicate primary content')
            result.append(_merge_typed_leaves(block, [retained[i] for i in hits]))
            consumed.update(hits)
        else:
            result.append(block)
    result.extend(block for i, block in enumerate(retained) if i not in consumed)
    return sorted(result, key=lambda b: (b['bbox'][1], b['bbox'][0]))




def _require_preserved_cell_text(original, pending, rendered, api):
    import re

    expected, proposed, actual = [BeautifulSoup(s, 'html.parser') for s in (original, pending, rendered)]
    required_math = _formula_anchors(original, api) | _formula_anchors(pending, api)
    if required_math - _formula_anchors(rendered, api):
        raise TableContentError('Typed table OCR omitted or changed an existing formula')
    for parsed in (expected, proposed, actual):
        for math_node in parsed.find_all('math'):
            math_node.decompose()
    def tokens(parsed):
        return re.findall(r'\w+|[^\w\s]', parsed.get_text(' ', strip=True).replace('\u00ad', ''))
    actual_tokens = tokens(actual)
    for known in (expected, proposed):
        remaining = iter(actual_tokens)
        if not all(any(candidate == token for candidate in remaining) for token in tokens(known)):
            raise TableContentError('Typed table OCR omitted or changed existing editable text')


def _incomplete_plain_cell(image, rendered):
    # Do not accept a short sample as a tall proved prose cell.
    parsed = BeautifulSoup(rendered, 'html.parser')
    if parsed.select_one('math, sub, sup, img, svg, table, .source-latex'):
        return False
    if not parsed.get_text(strip=True):
        return True
    from docling_pdf_primary_cells import _reading_is_short
    return _reading_is_short(parsed, image)


def _mark_unresolved_cell(node, physical, position, geometry, page, ref, error, manifest, api, *, image=None):
    from docling_pdf_table_audit import register_protected_source_graphic_cell

    if node.find('img') and not register_protected_source_graphic_cell(
            node, physical, geometry, page, ref, manifest):
        known = {entry['source_sha256'] for entry in manifest.get('typed_table_picture_assets', [])
                 if entry.get('page') == page and entry.get('cell', {}).get('row') == position['row']
                 and entry.get('cell', {}).get('column') == position['column']}
        for element in node.find_all('img'):
            if (element.get('data-table-content') != 'picture'
                    or sha256(element.get('src', '').encode()).hexdigest() not in known):
                element.decompose()
    for element in node.find_all(['svg','math']):
        if element.name == 'svg' or (not element.get_text(strip=True) and not element.get('data-latex')):
            element.decompose()
    if image is not None and not node.find('img'):
        from docling_pdf_typed_lines import measured_cell_interior
        interior = measured_cell_interior(image)
        bounds = interior[0] if interior else (0, 0, image.width, image.height)
        with image.crop(bounds) as crop, crop.convert('L') as gray:
            if sum(gray.histogram()[:180]) >= 3:
                width = (physical['bbox'][2] - physical['bbox'][0]) * crop.width / image.width
                preview = BeautifulSoup(api.image_html(crop, width, 'cell-review'), 'html.parser').img
                preview['alt'] = 'Фрагмент исходной ячейки для сверки'
                preview['data-table-content'] = 'picture'
                preview['data-ocr-source-preview'] = 'true'
                node.append(preview)
                manifest.setdefault('typed_table_picture_assets', []).append(dict(
                    page=page, source_ref=ref, label='Picture',
                    cell={key: position[key] for key in ('row', 'column', 'row_span', 'column_span')},
                    source_cell_bbox=list(physical['bbox']), source_pixel_bbox=list(bounds),
                    source_sha256=sha256(preview['src'].encode()).hexdigest(),
                    source_image_sha256=sha256(base64.b64decode(preview['src'].split(',', 1)[1])).hexdigest(),
                    method='source_preview_for_unresolved_physical_cell'))
    note = BeautifulSoup('', 'html.parser').new_tag('span', attrs={
        'class': 'table-cell-review', 'data-ocr-review': 'true'})
    note.string = '[Ячейка распознана не полностью]'
    if node.get_text(strip=True) or node.find(['img','math']):
        node.append(' ')
    node.append(note)
    _record_cell_review(physical, position, page, ref, str(error), manifest, api)


def _record_cell_review(physical, position, page, ref, reason, manifest, api, *, unresolved_lines=0):
    warning = dict(page=page, ref=ref, row=position['row'], column=position['column'],
                   source_bbox=list(physical['bbox']), reason=reason, basis='unresolved_physical_cell',
                   message='Ячейка таблицы распознана не полностью. Сверьте с исходным PDF.')
    if unresolved_lines:
        warning['unconfirmed_line_count'] = unresolved_lines
    manifest.setdefault('table_assembly_reviews', []).append(warning)
    api.diagnostic_event('TYPED_TABLE_CELL_REVIEW', **warning)

@lru_cache(maxsize=1)
def _decimal_line_recognizer():
    """Use the local recognizer shipped in the pinned RapidOCR wheel."""
    from pathlib import Path
    import rapidocr

    model = Path(rapidocr.__file__).parent / 'models' / 'PP-OCRv6_rec_small.onnx'
    with model.open('rb') as stream:
        checksum = file_digest(stream, 'sha256').hexdigest()
    if checksum != '6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884':
        raise ValueError('Unexpected bundled PP-OCRv6 recognizer')
    return rapidocr.RapidOCR(params={
        'Global.use_det': False, 'Global.use_cls': False, 'Global.log_level': 'warning',
        'Rec.model_path': str(model), 'Rec.rec_batch_num': 16,
        'EngineConfig.onnxruntime.intra_op_num_threads': 4,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
    })


def _verified_decimal_cells(tasks, geometry, source_image, region, page, args, ref,
                            source_reader, graphic_context, api):
    """Confirm simple decimals against source pixels and the positioned text layer."""
    if (not tasks or source_reader is None or not geometry or not graphic_context['verified']
            or not geometry.get('page_scan_transcription_required')
            or geometry.get('hidden_ocr_direction_conflict')):
        return {}
    import cv2
    import numpy as np
    import re
    from time import monotonic

    start = monotonic()
    source_reader.load_page(page)
    glyphs = [g for g in source_reader.chars if g.get('text', '').strip()]
    matches, views = [], []
    for node, position, physical in tasks:
        if (position.get('region') or position['header']
                or (position['row_span'], position['column_span']) != (1, 1)
                or position['column'] in graphic_context['roles']
                or physical.get('pending_primary_html') or node.get_text(strip=True)
                or node.select_one('math, sub, sup, svg, input, table, [data-formula], [data-latex], .source-latex')):
            continue
        images = node.find_all('img')
        if len(images) != 1 or images[0].get('data-source-cell') != 'true':
            continue
        box = physical['bbox']
        cell_glyphs = [g for g in glyphs if box[0] <= (g['x0'] + g['x1']) / 2 < box[2]
                       and box[1] <= (g['top'] + g['bottom']) / 2 < box[3]]
        lines = api.native_cell_lines(cell_glyphs)
        if len(lines) != 1:
            continue
        literal = lines[0]['text'].strip()
        if not re.fullmatch(r'(?:[0-9]{1,4}[,.][0-9]{1,4}|\([0-9]{1,4}[,.][0-9]{1,4}\))', literal):
            continue
        if _retained_blocks(args, page, box):
            continue
        try:
            with _source_crop(source_image, region, box) as crop, crop.convert('L') as gray:
                if max(crop.size) > 512:
                    continue
                support = _source_text_coverage(crop, [{'label': 'Text'}], source_reader, box, page, api, literal)
                if support is None:
                    continue
                glyph_boxes, anchor = support
                _require_ink_coverage(crop, glyph_boxes, physical_cell=True)
                pixels = np.asarray(gray)
                ink = cv2.threshold(pixels, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
                interior = _coverage_without_enclosing_rules(ink, physical_cell=True)
                y, x = np.nonzero(interior)
                if not len(x):
                    continue
                part = np.s_[y.min():y.max()+1, x.min():x.max()+1]
                line = pixels[part].copy()
                line[(interior[part] == 0) & (ink[part] != 0)] = 255
        except TableContentError:
            continue
        for padding in (((3, 3), (4, 4)), ((5, 6), (5, 6))):
            padded = np.pad(line, padding, constant_values=255)
            views.append(cv2.cvtColor(padded, cv2.COLOR_GRAY2BGR))
        cell = {name: position[name] for name in ('row', 'column', 'row_span', 'column_span')}
        matches.append((id(node), literal, dict(anchor, page=page, ref=ref, cell=cell,
                       method='rapidocr_and_positioned_source_agree_on_decimal')))
        if len(matches) >= 128:
            break
    if not matches:
        return {}
    try:
        result = _decimal_line_recognizer().recognize_txt(views)
    except Exception as error:
        api.diagnostic_event('TYPED_DECIMAL_BATCH_FALLBACK', page=page, ref=ref, reason=str(error))
        return {}
    if (result.txts is None or len(result.txts) != len(views) or len(result.scores) != len(views)):
        return {}
    confirmed = {}
    for i, (key, literal, anchor) in enumerate(matches):
        values = result.txts[2*i:2*i+2]
        scores = result.scores[2*i:2*i+2]
        if all(value.strip() == literal for value in values) and all(math.isfinite(s) and s >= .95 for s in scores):
            confirmed[key] = (literal, dict(anchor, confidence=float(min(scores))))
    api.diagnostic_event('TYPED_DECIMAL_BATCH', page=page, ref=ref, candidates=len(matches),
                         accepted=len(confirmed), seconds=round(monotonic()-start, 3))
    return confirmed


def normalize_table_content(fragment, geometry, source_image, region, page, args, manifest, api,
                            *, recognize=None, ref=None, source_reader=None, recover_region=False):
    """Resolve each raster in place, retaining mixed-cell order and spans.

    Existing editable markup is never deleted because of grid uncertainty.
    A disputed cell on a verified grid is marked locally for review.
    An unresolved table region has no raster publication path.
    """
    use_decimal_batch = recognize is None
    recognize = recognize or recognize_typed_crop
    soup = BeautifulSoup(fragment, 'html.parser')
    before = api.table_profile(fragment) if soup.table else None
    graphic_context = _source_graphic_context(fragment, before, geometry, page, ref, manifest)
    tasks = []
    if soup.table and before and not before['issues']:
        nodes = [n for n in soup.table.find_all(['td', 'th']) if n.find_parent('table') is soup.table]
        for node, position in zip(nodes, before['cells']):
            physical = _physical_cell(geometry, position)
            graft = (physical or {}).get('primary_packet_graft')
            if graft and _typed_payload_digest(node.decode_contents()) == graft.get('rendered_payload_sha256'):
                _record_primary_ownership(manifest, graft['packets'], page, ref)
            retained = _retained_blocks(args, page, physical['bbox']) if physical else []
            source_authority = physical and _has_source_authority(node, physical, geometry)
            math_missing = not source_authority and any(_formula_anchors(b['html'], api) - _formula_anchors(node.decode_contents(), api)
                               for b in retained)
            if node.find('img') and not (physical or {}).get('pending_primary_html') and not math_missing:
                from docling_pdf_table_audit import register_protected_source_graphic_cell
                if register_protected_source_graphic_cell(node, physical, geometry, page, ref, manifest):
                    api.diagnostic_event('TYPED_ORIGINAL_SOURCE_DIAGRAM_RETAINED',page=page,ref=ref,
                        row=position['row'],column=position['column'],images=len(node.find_all('img')))
                    continue
            if node.find('img') or (physical or {}).get('pending_primary_html') or math_missing:
                if not physical or not physical.get('bbox'):
                    tasks = [(soup, dict(region=True), dict(bbox=region))]
                    break
                tasks.append((node, position, physical))
    elif recover_region or soup.find('img'):
        tasks = [(soup, dict(region=True), dict(bbox=region))]
    decimal_cells = (_verified_decimal_cells(tasks, geometry, source_image, region,
        page, args, ref, source_reader, graphic_context, api) if use_decimal_batch else {})
    for node, position, physical in tasks:
        # Never treat OCR HTML/base64 as evidence. Re-read ORIGINAL page
        # pixels for the physical owner, including text beside its picture.
        box = physical['bbox']
        physical_cell = bool(not position.get('region') and _physical_cell(geometry, position) is physical)
        image = None
        original = node.decode_contents()
        existing = [BeautifulSoup(value, 'html.parser') for value in
                    (original, physical.get('pending_primary_html') or '')]
        editable_existing = any(payload.get_text(strip=True) or payload.find(['math', 'sup', 'sub', 'input', 'table'])
                                for payload in existing)
        verified_cell = physical_cell and bool(geometry.get('source_grid_verified')
                                               or geometry.get('positioned_source_grid_verified'))
        occupied = range(position.get('column', 0), position.get('column', 0) + position.get('column_span', 0))
        text_column = bool(verified_cell and not position.get('header')
                           and occupied and set(occupied).issubset(graphic_context['text_columns']))
        fragment_graft = None
        try:
            image = _source_crop(source_image, region, box)
            retained = _retained_blocks(args, page, box)
            rendered = None
            complete_primary = False
            native_fragments = physical_cell and _native_formula_fragments(node, physical)
            complete_cell_reading = False
            proof = {}
            text_reader = (None if (geometry or {}).get('hidden_ocr_direction_conflict')
                           or (physical.get('source_text_unreliable')
                               and not (geometry or {}).get('page_scan_transcription_required'))
                           else source_reader)
            if (retained and (native_fragments or not _has_source_authority(node, physical, geometry))
                    and (physical_cell or not any(block.get('boundary_reconciled') for block in retained))):
                try:
                    rendered = _render_typed(image, retained, box[2]-box[0], page, position, proof, api,
                                             source_reader=text_reader, source_bbox=box, physical_cell=physical_cell,
                                             require_coverage=True)
                    blocks = retained
                    complete_primary = physical_cell
                except TableContentError:
                    pass  # Additional drawing/text must be resolved from source.
            decimal = decimal_cells.get(id(node))
            if rendered is None and decimal is not None:
                rendered, anchor = decimal
                blocks = [{'label': 'Text'}]
                proof['typed_table_text_anchors'] = [anchor]
            if rendered is None:
                blocks = (None if retained or editable_existing else
                          _source_graphic_blocks(image, position, graphic_context, physical_cell))
                if blocks is None:
                    recognition_cell = dict(position, has_primary_content=bool(retained or editable_existing),
                                            physical_cell_proven=physical_cell)
                    blocks = _combine_blocks(retained, recognize(image, args, page, recognition_cell, api))
                else:
                    manifest.setdefault('typed_source_graphic_cells', []).append(dict(
                        page=page, ref=ref, row=position['row'], column=position['column'],
                        source_bbox=list(box), **blocks[0]['source_graphic_role']))
                if position.get('region') and any(b['label'].casefold() in PICTURE_LABELS for b in blocks):
                    raise TableContentError('Table region OCR could not recover a structured table without raster fallback')
                proof = {}
                rendered = _render_typed(image, blocks, box[2]-box[0], page, position, proof, api,
                                         source_reader=text_reader, source_bbox=box, physical_cell=physical_cell,
                                         require_coverage=bool(native_fragments),
                                         allow_nested_table=bool(node.find('table')))
                if text_column and _incomplete_plain_cell(image, rendered):
                    raise TableContentError('Physical text-cell OCR omitted source lines')
                # A complete exact-cell OCR equation supersedes the native
                # fraction's unassembled glyph/image pieces. Retained primary
                # packets were reconciled by _combine_blocks above.
                complete_cell_reading = bool(native_fragments
                    and BeautifulSoup(rendered, 'html.parser').find('math')
                    and not BeautifulSoup(rendered, 'html.parser').find(['img', 'svg', 'table']))
            complete_native_reading = False
            if native_fragments and (complete_primary or complete_cell_reading):
                try:
                    _require_native_formula_atoms(original, rendered, api, cell=physical)
                except TableContentError:
                    pass  # A prefix conflict may still permit a proved fraction graft.
                else:
                    complete_native_reading = True
            if native_fragments and not complete_native_reading:
                from docling_pdf_formula_fragments import graft_source_fraction_fragment
                entries = [entry for entry in (geometry or {}).get('retained_cell_images', [])
                           if (entry.get('row'), entry.get('column')) ==
                              (physical.get('row'), physical.get('column'))]
                grafted = graft_source_fraction_fragment(original, rendered, physical, entries, api)
                if grafted:
                    rendered, fraction_proof = grafted
                    complete_primary, complete_cell_reading = False, True
                    retained = []  # Only the proved fraction acquired ownership.
                    fragment_graft = dict(page=page, ref=ref, row=position.get('row'),
                                          column=position.get('column'), **fraction_proof)
            if position.get('region') and (not BeautifulSoup(rendered, 'html.parser').table
                                           or api.table_profile(rendered)['issues']):
                raise TableContentError('Table region OCR returned no valid table grid')
            # Preserve the whole known editable payload, including formula atoms.
            expected = original
            pending = physical.get('pending_primary_html') or ''
            if complete_primary or complete_cell_reading:
                primary = ''.join(block['html'] for block in retained) if complete_primary else rendered
                if native_fragments:
                    _require_native_formula_atoms(original, primary, api, cell=physical)
                else:
                    review_primary_cell_conflict(original, primary, page, position, box, manifest, api)
                review_primary_cell_conflict(pending, primary, page, position, box, manifest, api)
                expected, pending = primary, ''
            _require_preserved_cell_text(expected, pending, rendered, api)
        except TableContentError as error:
            error.context = dict(stage='typed_table_content', page=page, element=ref,
                cell={k: position[k] for k in ('row', 'column', 'row_span', 'column_span') if k in position},
                bbox=list(box))
            api.diagnostic_event('TYPED_TABLE_CONTENT_FAILED', **error.context, reason=str(error))
            if not verified_cell:
                raise
            recovered = None
            if text_column and image is not None and not any(
                    payload.select_one('math, sup, sub, table, .source-latex') for payload in existing):
                from docling_pdf_typed_lines import recover_typed_text_lines
                recovered = recover_typed_text_lines(image, args, page, position, api)
                if recovered:
                    try:
                        _require_preserved_cell_text(original, physical.get('pending_primary_html') or '',
                                                     recovered[0], api)
                    except TableContentError:
                        recovered = None
            if recovered is None:
                _mark_unresolved_cell(node, physical, position, geometry, page, ref, error, manifest, api,
                                      image=image)
                continue
            rendered, line_proof = recovered
            proof, retained, fragment_graft = {}, [], None
            blocks = [dict(label='Text')]
            coordinates = {k: position[k] for k in ('row', 'column', 'row_span', 'column_span')}
            manifest.setdefault('typed_table_text_anchors', []).append(
                dict(page=page, cell=coordinates, **line_proof))
            if line_proof.get('unconfirmed_line_count'):
                _record_cell_review(physical, position, page, ref, 'source_lines_remained_unverified',
                                    manifest, api, unresolved_lines=line_proof['unconfirmed_line_count'])
        finally:
            if image is not None:
                image.close()
        for key in ('typed_table_picture_assets', 'typed_table_text_anchors', 'typed_table_coverage_reviews'):
            if proof.get(key):
                manifest.setdefault(key, []).extend(proof[key])
        if fragment_graft:
            manifest.setdefault('source_fraction_fragment_grafts', []).append(fragment_graft)
        node.clear()
        node.append(BeautifulSoup(rendered, 'html.parser'))
        physical.pop('pending_primary_html', None)
        physical.pop('typed_content_recovery_required', None)
        _record_primary_ownership(manifest, retained, page, ref)
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
        from docling_pdf_list_markers import repair_table_list_markers
        repair_table_list_markers(soup, geometry, source_image, region, page, source_reader,
                                  api, manifest, ref=ref)
    return str(soup)


def validate_published_table_images(soup, manifest):
    """Record late image provenance issues without rejecting the document."""
    known = {entry['source_sha256'] for entry in manifest.get('typed_table_picture_assets', [])}
    for table in soup.find_all('table'):
        for image in table.find_all('img'):
            digest = sha256(image.get('src', '').encode()).hexdigest()
            if image.get('data-table-content') != 'picture' or digest not in known:
                manifest.setdefault('table_image_provenance_reviews', []).append(dict(
                    ref=table.get('data-pdf-table-ref'), reason='table_image_without_typed_picture_provenance',
                    message='В таблице сохранено исходное изображение фрагмента без завершённого распознавания.'))


def exclude_typed_primary_copies(doc, routes, excluded, manifest, api):
    """Remove only exact positioned source packets already owned by a table."""
    from collections import Counter

    for item in doc.texts:
        region = api.item_region(item, doc)
        if not region or len(item.prov) != 1 or item.self_ref in excluded:
            continue
        digest = sha256((item.orig or '').encode()).hexdigest()
        payload_digest = _typed_payload_digest(item.orig or '')
        for proof in manifest.get('typed_table_primary_ownership', []):
            if (proof['page'] != region[0] or not routes.get(proof['ref'])
                    or (digest != proof['source_record_sha256']
                        and payload_digest != proof.get('source_payload_sha256'))):
                continue
            if any(abs(a-b) > 1 for a, b in zip(region[1], proof['source_bbox'])):
                continue
            excluded.add(item.self_ref)
            manifest.setdefault('typed_table_duplicate_packets_removed', []).append(
                dict(ref=item.self_ref, owner=proof['ref'], page=region[0],
                     source_record_sha256=digest, bbox=region[1]))
            break
        if item.self_ref in excluded:
            continue
        # Docling may join adjacent source equations into one FormulaItem.
        # Remove that copy only when every complete equation and its full
        # source extent are already owned by one published table.
        parsed = BeautifulSoup(item.orig or '', 'html.parser')
        math_nodes = parsed.find_all('math', recursive=False)
        if len(math_nodes) < 2:
            continue
        wanted = Counter(_typed_payload_digest(str(node)) for node in math_nodes)
        for node in math_nodes:
            node.decompose()
        if parsed.get_text(strip=True) or parsed.find(True):
            continue
        grouped = {}
        seen = set()
        box = region[1]
        for proof in manifest.get('typed_table_primary_ownership', []):
            source = proof['source_bbox']
            if (proof['page'] == region[0] and routes.get(proof['ref'])
                    and proof.get('source_payload_sha256') in wanted
                    and box[0]-1 <= source[0] and box[1]-1 <= source[1]
                    and source[2] <= box[2]+1 and source[3] <= box[3]+1):
                identity = (proof['ref'], proof['page'], tuple(source), proof['source_payload_sha256'])
                if identity in seen:
                    continue
                seen.add(identity)
                grouped.setdefault(proof['ref'], []).append(proof)
        matches = []
        for owner, packets in grouped.items():
            if Counter(packet['source_payload_sha256'] for packet in packets) != wanted:
                continue
            union = [min(p['source_bbox'][0] for p in packets), min(p['source_bbox'][1] for p in packets),
                     max(p['source_bbox'][2] for p in packets), max(p['source_bbox'][3] for p in packets)]
            if any(abs(a-b) > 1 for a, b in zip(box, union)):
                continue
            matches.append((owner, packets))
        if len(matches) == 1:
            owner, packets = matches[0]
            excluded.add(item.self_ref)
            manifest.setdefault('typed_table_duplicate_packets_removed', []).append(dict(
                ref=item.self_ref, owner=owner, page=region[0], bbox=box,
                method='complete_owned_equations_in_one_assembled_source_item', packets=len(packets)))
