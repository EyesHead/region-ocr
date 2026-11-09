"""Local recovery seams for failed inference and unassigned TableFormer text."""
from dataclasses import replace
from html import escape
import json
import math


def _layout_error(item, output):
    if output is None or output.error:
        return 'inference_failed_or_empty'
    if not output.raw:
        from surya.common.blank import is_blank_region
        return None if is_blank_region(item.image) else 'inference_failed_or_empty'
    if item.prompt_type == 'layout':
        from surya.inference.parsers import parse_layout
        try:
            parse_layout(output.raw)
        except (ValueError, TypeError, KeyError) as error:
            return f'invalid_layout: {error}'
    elif item.prompt_type == 'high_accuracy_bbox':
        from surya.inference.parsers import parse_full_page_html
        from surya.recognition import _detect_repeat_loop
        try:
            if _detect_repeat_loop(output.raw) or not parse_full_page_html(output.raw):
                return 'invalid_or_repeating_full_page_html'
        except (ValueError, TypeError, KeyError) as error:
            return f'invalid_full_page: {error}'
    return None


def _layout_parts(image):
    """Split at a real white gutter; never rotate or cut through source ink."""
    import numpy as np
    ink = np.asarray(image.convert('L')) < 235
    h, w = ink.shape
    for axis in sorted((0, 1), key=lambda axis: (h, w)[axis], reverse=True):
        length = (h, w)[axis]
        if length < 96:
            continue
        occupied = ink.any(axis=1-axis)
        candidates = [n for n in range(max(24, length//4), min(length-24, length*3//4))
                      if not occupied[max(0,n-1):n+2].any()
                      and occupied[:n].any() and occupied[n:].any()]
        if not candidates:
            continue
        cut = min(candidates, key=lambda n: abs(n-length/2))
        boxes = ((0,0,w,cut),(0,cut,w,h)) if axis == 0 else ((0,0,cut,h),(cut,0,w,h))
        return [(image.crop(box), box) for box in boxes]
    return []


def recovering_generate(batch, request, diagnostic):
    """Retry failed requests once; split malformed layout at source white gutters.

    No document/page/element count budget is imposed. A persistent error is
    raised with context instead of returning an apparently successful blank OCR.
    """
    outputs = request(batch)
    if len(outputs) != len(batch):
        raise RuntimeError('Local inference result count mismatch')
    recovered = []
    for item, output in zip(batch, outputs, strict=True):
        reason = _layout_error(item, output)
        if not reason:
            recovered.append(output)
            continue
        diagnostic('OCR_REQUEST_RETRY', prompt_type=item.prompt_type, reason=reason,
                   image_size=list(item.image.size), metadata=item.metadata)
        retry = item
        if item.prompt_type == 'layout':
            from surya.inference.prompts import LAYOUT_JSON_SCHEMA
            retry = replace(item, guided_json=LAYOUT_JSON_SCHEMA, temperature=0.0)
        repeated = request([retry])
        output = repeated[0] if len(repeated) == 1 else None
        reason = _layout_error(retry, output)
        if not reason:
            diagnostic('OCR_REQUEST_RECOVERED', prompt_type=item.prompt_type,
                       image_size=list(item.image.size), method='single_request_retry')
            recovered.append(output)
            continue
        parts = _layout_parts(item.image) if item.prompt_type in {'layout','high_accuracy_bbox'} else []
        if parts:
            from surya.inference.parsers import parse_layout, parse_full_page_html
            blocks = []
            width, height = item.image.size
            tokens = 0
            diagnostic('OCR_LAYOUT_SPLIT_RECOVERY' if item.prompt_type == 'layout' else 'OCR_PAGE_SPLIT_RECOVERY', image_size=list(item.image.size),
                       regions=[list(box) for _,box in parts], reason=reason)
            try:
                for crop, box in parts:
                    part = recovering_generate([replace(retry,image=crop)], request, diagnostic)[0]
                    tokens += part.token_count
                    parsed = parse_layout(part.raw) if item.prompt_type == 'layout' else parse_full_page_html(part.raw)
                    for block in parsed:
                        x0,y0,x1,y1 = block.bbox
                        left,top,right,bottom = box
                        coords = [(left+x0*(right-left)/1000)*1000/width,
                                  (top+y0*(bottom-top)/1000)*1000/height,
                                  (left+x1*(right-left)/1000)*1000/width,
                                  (top+y1*(bottom-top)/1000)*1000/height]
                        if item.prompt_type == 'layout':
                            blocks.append(dict(label=block.label,bbox=' '.join(str(round(v)) for v in coords),count=block.count))
                        else:
                            blocks.append('<div data-label="'+escape(block.label,quote=True)+'" data-bbox="'+
                                ' '.join(str(round(v)) for v in coords)+'">'+block.html+'</div>')
            finally:
                for crop,_ in parts:
                    crop.close()
            from surya.inference.schema import GenerationResult
            output = GenerationResult(raw=json.dumps(blocks) if item.prompt_type == 'layout' else ''.join(blocks),token_count=tokens,error=False)
            diagnostic('OCR_REQUEST_RECOVERED', prompt_type=item.prompt_type,
                       image_size=list(item.image.size), method='source_gutter_layout_parts')
            recovered.append(output)
            continue
        if item.prompt_type == 'high_accuracy_bbox' and output is not None:
            # Surya's full-page recognizer can still recover through its
            # layout + block path when no safe source gutter exists.
            diagnostic('OCR_FULL_PAGE_LAYOUT_FALLBACK', reason=reason, image_size=list(item.image.size))
            recovered.append(output)
            continue
        error = RuntimeError(f'Local {item.prompt_type} request failed after retry: {reason}')
        error.context = dict(stage='ocr/layout', element=item.metadata.get('element'),
                             page=item.metadata.get('page'), reason=reason)
        diagnostic('OCR_REQUEST_FAILED', **error.context, image_size=list(item.image.size))
        raise error
    return recovered


def install_table_retention(model, args, diagnostic):
    """Keep every unassigned token with its actual page/bbox before export.

    TableFormer can lose tokens outside both predicted row and column bands.
    Do not guess a cell from nearest distance: retain the original coordinates
    for the source geometry reader and text recovery during HTML assembly.
    """
    if not getattr(model, 'enabled', False) or not hasattr(model, 'tf_predictor'):
        return
    predict = model.predict_tables
    multi = model.tf_predictor.multi_table_predict
    current_page = None

    def retain(page_input, table_bboxes, *positional, **keywords):
        outputs = multi(page_input, table_bboxes, *positional, **keywords)
        for box, output in zip(table_bboxes, outputs, strict=True):
            details = output.get('predict_details', {})
            matches = details.get('matches', {})
            for cell in details.get('pdf_cells', []):
                if matches.get(str(cell['id'])) or not cell.get('text','').strip():
                    continue
                bbox = cell['bbox']
                if isinstance(bbox, dict):
                    bbox = [bbox[k] for k in ('l','t','r','b')]
                record = dict(kind='table_unassigned_token', page_no=current_page,
                    bbox=[float(v)/model.scale for v in bbox], text=cell['text'], html='',
                    label='TableUnassigned', confidence=1.0,
                    source_table_bbox=[float(v)/model.scale for v in box])
                args.surya_records.append(record)
                diagnostic('TABLE_MATCHING_UNASSIGNED_CAPTURED', **record)
        return outputs

    def predict_pages(conv_res, pages):
        nonlocal current_page
        predictions = []
        for page in pages:
            current_page = page.page_no
            predictions.extend(predict(conv_res, [page]))
        current_page = None
        return predictions

    model.tf_predictor.multi_table_predict = retain
    model.predict_tables = predict_pages


def recover_unassigned_text(doc, records, routes, excluded, manifest, api):
    """Prove source-table recovery or restore the missing positioned text block."""
    from bs4 import BeautifulSoup
    proofs = []
    for record in records:
        if record.get('kind') != 'table_unassigned_token':
            continue
        page, box, text = record['page_no'], record['bbox'], record['text']
        key = api.cell_text_key(api.normalize_metric_unit_text(text))
        proof = dict(page=page,bbox=box,text=text)
        recovered = False
        for entry in manifest['tables']:
            if entry['page'] != page or api.overlap_ratio(box,entry['bbox']) < .85:
                continue
            geometry = entry.get('pdf_geometry') or {}
            content = routes.get(entry['ref'],'')
            if entry['method'] == 'source_image':
                recovered = True
                proof.update(method='source_table_image',table=entry['ref'])
                break
            if geometry.get('glyph_coverage') == 1.0 and key and key in api.cell_text_key(
                    api.normalize_metric_unit_text(BeautifulSoup(content,'html.parser').get_text(' ',strip=True))):
                recovered = True
                proof.update(method='complete_source_glyph_geometry',table=entry['ref'])
                break
        if not recovered:
            for item in doc.texts:
                region = api.item_region(item,doc)
                if (item.self_ref not in excluded and region and region[0] == page
                        and api.overlap_ratio(box,region[1]) > .85
                        and key in api.cell_text_key(api.normalize_metric_unit_text(item.text))):
                    recovered = True
                    proof.update(method='existing_positioned_text',ref=item.self_ref)
                    break
        if not recovered:
            item = api.insert_source_item(doc,page,box,'text')
            item.text = item.orig = text
            for prov in item.prov:
                prov.charspan = (0,len(text))
            proof.update(method='restored_positioned_source_token',ref=item.self_ref)
        proofs.append(proof)
        api.diagnostic_event('TABLE_MATCHING_TOKEN_RETAINED', **proof)
    manifest['table_unassigned_token_retention'] = proofs
