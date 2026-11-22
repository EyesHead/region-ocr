"""Local recovery seams for failed inference and unassigned TableFormer text."""
from dataclasses import replace
from html import escape
import json
import math


class InvalidLayoutOutput(RuntimeError):
    """The backend responded but the layout could not be decoded reliably."""


def _layout_error(item, output):
    if output is None:
        return 'inference_failed_or_empty'
    if output.raw:
        from surya.recognition import _detect_repeat_loop
        if _detect_repeat_loop(output.raw):
            return 'repeating_output'
    if getattr(output, 'finish_reason', None) == 'length' or (
            item.max_tokens and output.token_count >= item.max_tokens):
        return 'output_token_limit'
    if output.error:
        return 'inference_failed_or_empty'
    if not output.raw:
        from surya.common.blank import is_blank_region
        return None if is_blank_region(item.image) else 'inference_failed_or_empty'
    if item.prompt_type == 'layout':
        from surya.inference.parsers import parse_layout
        try:
            blocks = parse_layout(output.raw)
            if item.metadata.get('purpose') == 'physical_cell_layout':
                # Surya logs and drops bad boxes. A partial layout can turn a
                # mixed text/picture cell into an apparently picture-only cell.
                raw_array = output.raw[output.raw.index('['):output.raw.rindex(']') + 1]
                if len(blocks) != len(json.loads(raw_array)):
                    return 'invalid_layout: parser dropped one or more cell regions'
                for block in blocks:
                    left, top, right, bottom = block.bbox
                    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                        return 'invalid_layout: cell region outside normalized crop bounds'
        except (ValueError, TypeError, KeyError, OverflowError) as error:
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


def _complete_region_output(item, output, request, diagnostic):
    """Retry a truncated region once within the installed output budget."""
    reason = _layout_error(item, output)
    if (reason != 'output_token_limit' or item.prompt_type == 'high_accuracy_bbox' or not item.max_tokens
            or item.metadata.get('purpose') == 'physical_cell_layout'):
        return item, output, reason
    from surya.settings import settings
    previous = item.max_tokens
    ceiling = settings.SURYA_MAX_TOKENS_BLOCK_CEILING
    budget = min(ceiling, previous * 2)
    if budget <= previous:
        return item, output, reason
    item = replace(item, max_tokens=budget, temperature=0.0)
    diagnostic('OCR_REQUEST_RETRY', prompt_type=item.prompt_type, reason=reason,
               image_size=list(item.image.size), metadata=item.metadata,
               previous_max_tokens=previous, max_tokens=budget,
               method='increased_region_output_budget')
    repeated = request([item])
    if len(repeated) != 1:
        raise RuntimeError('Local OCR budget retry result count mismatch')
    result = repeated[0]
    if result is None:
        return item, result, 'inference_failed_or_empty'
    reason = _layout_error(item, result)
    if reason is None:
        result = replace(result, metadata=dict(result.metadata, region_ocr_completed_budget=budget))
    return item, result, reason


def output_within_request_budget(output, initial_limit):
    """Validate against the exact expanded budget recorded on successful retry."""
    initial_limit = initial_limit or 2048
    final_limit = output.metadata.get('region_ocr_completed_budget', initial_limit)
    return (not output.error and type(final_limit) is int
            and initial_limit <= final_limit <= 2 * initial_limit
            and 0 <= output.token_count < final_limit)


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
    """Complete truncated blocks; retry errors and split malformed layout.

    No document/page/element count budget is imposed. A persistent error is
    raised with context instead of returning an apparently successful blank OCR.
    """
    from surya.inference.prompts import LAYOUT_JSON_SCHEMA

    outputs = request(batch)
    if len(outputs) != len(batch):
        raise RuntimeError('Local inference result count mismatch')
    recovered = []
    for item, output in zip(batch, outputs, strict=True):
        retry, output, reason = _complete_region_output(item, output, request, diagnostic)
        if not reason:
            if retry.max_tokens != item.max_tokens:
                diagnostic('OCR_REQUEST_RECOVERED', prompt_type=item.prompt_type,
                           image_size=list(item.image.size), method='completed_region_output_budget',
                           max_tokens=retry.max_tokens)
            recovered.append(output)
            continue
        # Repeating the same greedy loop or a known exhausted budget cannot
        # complete its transcription. Keep those failures explicit.
        # Local layout inference defaults to greedy decoding. Repeating the
        # same prompt and schema on the same crop produces the same bad JSON.
        same_layout = (item.prompt_type == 'layout' and reason.startswith('invalid_layout:')
                       and item.guided_json == LAYOUT_JSON_SCHEMA and item.temperature in (None, 0.0))
        if (retry.max_tokens == item.max_tokens and not same_layout
                and reason not in {'output_token_limit', 'repeating_output'}):
            diagnostic('OCR_REQUEST_RETRY', prompt_type=item.prompt_type, reason=reason,
                       image_size=list(item.image.size), metadata=item.metadata)
            if item.prompt_type == 'layout':
                retry = replace(retry, guided_json=LAYOUT_JSON_SCHEMA, temperature=0.0)
            repeated = request([retry])
            output = repeated[0] if len(repeated) == 1 else None
            retry, output, reason = _complete_region_output(retry, output, request, diagnostic)
        if not reason:
            diagnostic('OCR_REQUEST_RECOVERED', prompt_type=item.prompt_type,
                       image_size=list(item.image.size), method='single_request_retry')
            recovered.append(output)
            continue
        # Only the type of content is unknown for an already located cell.
        # Re-segmenting that same cell cannot establish which source tokens
        # it owns. Its caller checks positioned readings and retains its pixels.
        split_page = (item.prompt_type in {'layout', 'high_accuracy_bbox'}
                      and item.metadata.get('purpose') != 'physical_cell_layout')
        parts = _layout_parts(item.image) if split_page else []
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
            from surya.inference.schema import BatchOutputItem
            output = BatchOutputItem(
                raw=json.dumps(blocks) if item.prompt_type == 'layout' else ''.join(blocks),
                token_count=tokens, error=False, metadata=item.metadata)
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
        invalid_layout = (item.prompt_type == 'layout' and output is not None and bool(output.raw)
                          and (reason.startswith('invalid_layout:') or reason in {
                              'output_token_limit', 'repeating_output'}))
        if invalid_layout:
            error = InvalidLayoutOutput(f'Local layout output could not be recovered: {reason}')
        else:
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
