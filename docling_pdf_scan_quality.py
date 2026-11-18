"""Re-read source table crops through the already loaded Surya runtime."""
from collections import Counter
from dataclasses import replace
from hashlib import sha256
import math
import re
import unicodedata

from bs4 import BeautifulSoup
from PIL import Image

_MAX_PAGE_PIXELS = 3072 * 2048
_CROP_PADDING = 8


def _key(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or '')).casefold()


def _payloads(table):
    return [(node.name, node.decode_contents()) for node in table.find_all(['td', 'th'])
            if node.find_parent('table') is table
            and (node.get_text(strip=True) or node.find(['math', 'img', 'svg', 'input']))]


def _empty_extra_column(table, expected_columns):
    """Trim the same explicit surplus empty columns in every affected row."""
    if not isinstance(expected_columns, int) or expected_columns < 1:
        return None
    plan = []
    widest = expected_columns
    for ordinal, row in enumerate(table.find_all('tr')):
        if row.find_parent('table') is not table:
            continue
        cells = row.find_all(['td', 'th'], recursive=False)
        try:
            if not cells or any(int(c.get('rowspan', 1)) != 1 for c in cells):
                return None
            widths = [int(c.get('colspan', 1)) for c in cells]
            if any(width < 1 for width in widths):
                return None
        except (TypeError, ValueError):
            return None
        width = sum(widths)
        if width < expected_columns:
            return None
        widest = max(widest, width)
        surplus = width-expected_columns
        if not surplus:
            continue
        empty, offset = [], 0
        for cell, span in zip(cells, widths):
            if (span == 1 and not cell.get_text(strip=True)
                    and not cell.find(['math', 'img', 'svg', 'input', 'table'])):
                empty.append((offset, cell))
            offset += span
        # More empty cells than surplus would require guessing their positions.
        if len(empty) != surplus:
            return None
        plan.append((ordinal, width, empty))
    if not plan:
        return None
    columns = {tuple(column for column, _ in empty) for _, _, empty in plan}
    if len(columns) != 1:
        return None
    before = _payloads(table)
    for _, _, empty in plan:
        for _, cell in empty:
            cell.decompose()
    if _payloads(table) != before:
        raise ValueError('Empty-cell normalization changed visible cell payloads')
    return dict(method='remove_same_explicit_surplus_empty_columns_to_valid_primary_width',
                source_columns=widest, columns=expected_columns,
                rows=[dict(row=ordinal, source_columns=width,
                           removed_columns=[column for column, _ in empty])
                      for ordinal, width, empty in plan])


def _normalize_candidate(content, api, original_profile=None):
    parsed = BeautifulSoup(content or '', 'html.parser')
    if len(parsed.find_all('table')) != 1:
        raise ValueError('retry_requires_exactly_one_table')
    if parsed.find(['img', 'svg', 'script', 'iframe', 'object']):
        raise ValueError('retry_contains_noneditable_or_executable_content')
    repairs = []
    expected = ((original_profile or {}).get('columns')
                if original_profile is not None and not original_profile.get('issues') else None)
    repair = _empty_extra_column(parsed.table, expected)
    if repair:
        repairs.append(repair)
    profile = api.table_profile(str(parsed.table))
    if profile.get('issues') and hasattr(api, 'repair_table_spans'):
        before = _payloads(parsed.table)
        repaired, operations = api.repair_table_spans(str(parsed.table))
        replacement = BeautifulSoup(repaired, 'html.parser').find('table')
        if replacement is not None and _payloads(replacement) == before:
            parsed.table.replace_with(replacement)
            repairs.extend(operations or [])
            profile = api.table_profile(str(parsed.table))
    if profile.get('issues') or not profile.get('rows') or not profile.get('columns'):
        raise ValueError('retry_has_invalid_table_grid')
    if not parsed.table.get_text(strip=True) and not parsed.table.find('math'):
        raise ValueError('retry_has_no_editable_table_content')
    return str(parsed), profile, repairs


def _preserve_primary_header_topology(original, candidate, original_profile, profile, api):
    """Keep a known header partition when the retry has the same ordered labels."""
    if (original_profile.get('issues') or profile.get('issues')
            or original_profile.get('columns') != profile.get('columns')):
        return candidate, None
    width = profile['columns']
    old, parsed = BeautifulSoup(original, 'html.parser'), BeautifulSoup(candidate, 'html.parser')
    def occupied(cell):
        return bool(cell.get_text(strip=True) or cell.find(['math', 'img', 'svg', 'input', 'table']))
    def text_key(cell):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', cell.get_text(' ', strip=True)))
    def headers(table):
        found = []
        for ordinal, row in enumerate(table.find_all('tr')):
            if row.find_parent('table') is not table:
                continue
            cells = row.find_all(['td', 'th'], recursive=False)
            if (not cells or not any(cell.name == 'th' for cell in cells)
                    or any(cell.name != 'th' and occupied(cell) for cell in cells)):
                break
            # A one-cell caption or section heading is outside this repair.
            if len(cells) == 1:
                continue
            try:
                spans = [int(cell.get('colspan', 1)) for cell in cells]
                if (any(int(cell.get('rowspan', 1)) != 1 for cell in cells)
                        or any(span < 1 for span in spans) or sum(spans) != width):
                    continue
            except (TypeError, ValueError):
                continue
            found.append((ordinal, row, cells, spans))
        return found
    source_headers = {}
    for ordinal, row, cells, spans in headers(old.table):
        if all(occupied(cell) for cell in cells):
            source_headers.setdefault(tuple(text_key(cell) for cell in cells), []).append((ordinal, spans))
    candidates = []
    for ordinal, row, cells, _ in headers(parsed.table):
        nonempty = [cell for cell in cells if occupied(cell)]
        if 1 < len(nonempty) < len(cells):
            candidates.append((ordinal, row, nonempty, tuple(text_key(cell) for cell in nonempty)))
    counts = Counter(entry[3] for entry in candidates)
    plan = []
    for ordinal, row, nonempty, signature in candidates:
        matches = source_headers.get(signature, [])
        if counts[signature] == 1 and len(matches) == 1:
            source_row, spans = matches[0]
            plan.append((ordinal, source_row, row, nonempty, spans))
    if not plan:
        return candidate, None
    before = _payloads(parsed.table)
    operations = []
    for ordinal, source_row, row, nonempty, spans in plan:
        for cell in nonempty:
            cell.extract()
        row.clear()
        for cell, span in zip(nonempty, spans):
            if span == 1:
                cell.attrs.pop('colspan', None)
            else:
                cell['colspan'] = str(span)
            row.append(cell)
        operations.append(dict(row=ordinal, primary_row=source_row, colspans=spans))
    if _payloads(parsed.table) != before:
        raise ValueError('Header topology conservation changed a nonempty cell payload')
    checked = api.table_profile(str(parsed.table))
    if checked.get('issues') or (checked['rows'], checked['columns']) != (profile['rows'], width):
        raise ValueError('Header topology conservation changed the table grid')
    return str(parsed), dict(method='retain_primary_header_topology_for_exact_ordered_labels',
        rows=operations, nonempty_cell_payloads_unchanged=True)


def _conserve_leading_caption(original, candidate, original_profile, profile, api):
    """Keep a primary caption when the retry garbles or omits that caption."""
    from difflib import SequenceMatcher
    from local_web.table_continuations import CAPTION

    if original_profile.get('issues') or original_profile.get('columns') != profile['columns']:
        return candidate, None
    before, after = BeautifulSoup(original, 'html.parser'), BeautifulSoup(candidate, 'html.parser')
    def first_row(table):
        return next((r for r in table.find_all('tr') if r.find_parent('table') is table), None)
    def leading(table):
        row = first_row(table)
        cells = row.find_all(['td', 'th'], recursive=False) if row else []
        if len(cells) != 1:
            return None
        cell = cells[0]
        try:
            return cell if int(cell.get('colspan', 1)) == profile['columns'] and int(cell.get('rowspan', 1)) == 1 else None
        except (TypeError, ValueError):
            return None
    old, new = leading(before.table), leading(after.table)
    if old is None:
        return candidate, None
    old_text = old.get_text(' ', strip=True)
    caption = CAPTION.fullmatch(old_text)
    if not caption or caption['title']:
        return candidate, None
    # A recognized candidate caption, including another identifier, must never
    # be overwritten or duplicated using the old reading.
    if any(CAPTION.fullmatch(node.get_text(' ', strip=True))
           for node in after.find_all(True)):
        return candidate, None
    identifier = re.sub(r'\s+', '', caption['number'])
    new_text = new.get_text(' ', strip=True) if new is not None else ''
    similarity = SequenceMatcher(None, _key(old_text), _key(new_text)).ratio()
    all_payloads = _payloads(after.table)
    retry_caption = str(new) if new is not None else ''
    if new is not None and similarity > .65:
        suffix = r'\s*'.join(re.escape(char) for char in identifier)
        if not re.search(r'(?:^|\s)(?:№\s*)?'+suffix+r'\s*$', new_text, re.I):
            return candidate, None
        new.replace_with(BeautifulSoup(str(old), 'html.parser').find(['td', 'th']))
        unchanged = _payloads(after.table)[1:] == all_payloads[1:]
        expected_rows = profile['rows']
        method = 'retain_primary_leading_caption_after_near_match'
    else:
        target = first_row(after.table)
        if target is None:
            return candidate, None
        clone = BeautifulSoup(str(old.find_parent('tr')), 'html.parser').find('tr')
        target.insert_before(clone)
        unchanged = _payloads(after.table)[1:] == all_payloads
        expected_rows = profile['rows']+1
        method = 'retain_primary_caption_missing_from_retry'
    if not unchanged:
        raise ValueError('Caption conservation changed another cell payload')
    checked = api.table_profile(str(after.table))
    if checked.get('issues') or (checked['rows'], checked['columns']) != (expected_rows, profile['columns']):
        raise ValueError('Caption conservation changed the table grid')
    profile.update(rows=checked['rows'])
    return str(after), dict(method=method, original_caption=old_text, retry_caption=new_text,
        identifier=identifier, similarity=similarity, original_caption_html=str(old),
        retry_caption_html=retry_caption, other_cell_payloads_unchanged=True)


def _outside_content_preserved(original, candidate):
    source = BeautifulSoup(original, 'html.parser')
    target = _key(BeautifulSoup(candidate, 'html.parser').get_text(' ', strip=True))
    return all(not _key(str(node)) or _key(str(node)) in target
               for node in source.find_all(string=True)
               if node.find_parent('table') is None)


def _retry_reasons(image, table, profile):
    reasons = []
    if image.width * image.height > _MAX_PAGE_PIXELS:
        reasons.append('full_page_source_was_downscaled')
    if profile.get('issues'):
        reasons.append('primary_table_grid_invalid')
    if any(len(cell.find_all('br')) >= 2 or
           (not cell.find('math') and len(re.findall(r'[^\W\d_]+', cell.get_text(' '))) >= 8)
           for cell in table.find_all(['td', 'th'])):
        reasons.append('primary_cell_may_contain_collapsed_rows')
    formulas = {}
    for cell in profile.get('cells', []):
        parsed = BeautifulSoup(cell.get('html', ''), 'html.parser')
        if parsed.find(['math', 'sup', 'sub']):
            formulas.setdefault(cell['column'], []).append(_key(parsed.get_text()))
    if any(len(values) >= 4 and max(Counter(values).values()) >= max(3, .6*len(values))
           for values in formulas.values()):
        reasons.append('repeated_math_cells')
    return reasons


def refine_primary_table_blocks(image, prediction, model, args, page_no, api, *, reader=None, work_rect=None):
    """Replace table blocks with one source-crop reading; changed text needs REVIEW.

    The returned page and its blocks retain the original pixel coordinates and
    reading order. Original HTML remains in args.primary_table_refinements.
    No agreement between readings is treated as proof of OCR correctness.
    """
    from surya.recognition import _detect_repeat_loop

    source_pdf = getattr(args, 'current_normalized_pdf', None)

    def crop_source(box):
        left, top, right, bottom = box
        scale = model.scale
        bounds = (left/scale+work_rect.l, top/scale+work_rect.t,
                  right/scale+work_rect.l, bottom/scale+work_rect.t)
        regions = api.PdfRegions(source_pdf)
        try:
            return regions.crop(page_no, bounds, scale=scale)
        finally:
            regions.close()

    source_crop = crop_source if source_pdf is not None and work_rect is not None else None

    blocks = list(prediction.blocks)
    attempts = getattr(args, 'primary_table_refinements', None)
    if attempts is None:
        attempts = args.primary_table_refinements = []
    reviews = getattr(args, 'primary_table_refinement_reviews', None)
    if reviews is None:
        reviews = args.primary_table_refinement_reviews = []
    for index, block in enumerate(blocks):
        if getattr(block, 'skipped', False) or getattr(block, 'error', False):
            continue
        original = block.html or ''
        source = BeautifulSoup(original, 'html.parser')
        if len(source.find_all('table')) != 1 or source.find(['img', 'svg']):
            continue
        original_profile = api.table_profile(str(source.table))
        reasons = _retry_reasons(image, source.table, original_profile)
        if not reasons:
            continue
        proof = dict(page=page_no, block=index, bbox=list(block.bbox),
                     coordinate_space='ocr_work_image_pixels',
                     method='same_surya_source_table_block',
                     triggers=reasons, original_html=original,
                     original_html_sha256=sha256(original.encode()).hexdigest(),
                     original_rows=original_profile.get('rows'),
                     original_columns=original_profile.get('columns'))
        crop = padded = None
        try:
            box = list(block.bbox)
            if len(box) != 4 or not all(math.isfinite(v) for v in box):
                raise ValueError('primary_table_bbox_invalid')
            left, top = max(0, math.floor(box[0])), max(0, math.floor(box[1]))
            right, bottom = min(image.width, math.ceil(box[2])), min(image.height, math.ceil(box[3]))
            if right <= left or bottom <= top:
                raise ValueError('primary_table_bbox_has_no_source_pixels')
            crop = image.crop((left, top, right, bottom))
            padded = Image.new('RGB', (crop.width+2*_CROP_PADDING, crop.height+2*_CROP_PADDING), 'white')
            padded.paste(crop, (_CROP_PADDING, _CROP_PADDING))
            proof.update(source_crop_box=[left, top, right, bottom],
                         crop_size=list(padded.size), white_padding_pixels=_CROP_PADDING)
            if reader is None:
                from surya.table_rec import TableRecPredictor
                api.local_surya_backend(model)
                reader = TableRecPredictor(model._get_runtime()._manager)
            # The existing bounded manager handles a truncated output budget.
            estimate = max(1024, min(8092, len(original)*2))
            result = reader.predict_full([padded], counts=[estimate])
            if len(result) != 1 or result[0].error:
                raise ValueError('source_table_retry_failed')
            candidate = result[0].html or ''
            proof['retry_html'] = candidate
            proof['retry_html_sha256'] = sha256(candidate.encode()).hexdigest()
            if _detect_repeat_loop(result[0].raw or candidate):
                raise ValueError('source_table_retry_repeats')
            candidate, profile, repairs = _normalize_candidate(candidate, api, original_profile)
            candidate, header_proof = _preserve_primary_header_topology(original, candidate, original_profile, profile, api)
            if header_proof:
                proof['header_topology_conservation'] = header_proof
            candidate, caption_proof = _conserve_leading_caption(original, candidate, original_profile, profile, api)
            if caption_proof:
                proof['caption_conservation'] = caption_proof
            if profile['columns'] < original_profile.get('columns', 0):
                raise ValueError('source_table_retry_reduced_columns')
            if profile['columns'] > original_profile.get('columns', 0):
                raise ValueError('source_table_retry_increased_columns_without_source_proof')
            candidate, profile = _review_retry_rows(original, candidate, profile, original_profile,
                image, (left, top, right, bottom), reader, estimate, api, proof, source_crop)
            if not _outside_content_preserved(original, candidate):
                raise ValueError('source_table_retry_lost_outside_table_content')
            proof.update(rows=profile['rows'], columns=profile['columns'],
                         lossless_grid_repairs=repairs,
                         accepted_html_sha256=sha256(candidate.encode()).hexdigest())
            changed = bool(header_proof or caption_proof) or str(BeautifulSoup(original, 'html.parser')) != candidate
            if changed:
                blocks[index] = replace(block, html=candidate, text=api.semantic_ocr_text(candidate))
                proof.update(status='changed', reason='same_model_source_table_read_changed',
                             severity='warning',
                             message='Таблица перечитана по исходному изображению тем же Surya. '
                                     'Содержимое изменилось; требуется сверка с исходником.')
                reviews.append(dict(proof))
            else:
                proof['status'] = 'unchanged'
        except Exception as error:
            proof.update(status='retained_primary', reason='source_table_refinement_declined',
                         error_type=type(error).__name__, error=str(error), severity='warning',
                         message='Повторное чтение таблицы не принято. Сохранён первичный OCR; '
                                 'требуется сверка с исходником.')
            reviews.append(dict(proof))
        finally:
            if padded is not None:
                padded.close()
            if crop is not None:
                crop.close()
        attempts.append(proof)
        api.diagnostic_event('PRIMARY_TABLE_SOURCE_REFINEMENT', **proof)
    return replace(prediction, blocks=tuple(blocks))

def _numeric_table_rows(table):
    """Unpack explicit OCR line breaks in a two-column label/value table."""
    def lines(cell):
        contents = cell.decode_contents()
        pieces = re.split(r'<br\s*/?>|</?(?:p|li|ul|ol)(?:\s[^>]*)?>', contents)
        return [(part.strip(), BeautifulSoup(part, 'html.parser').get_text(' ', strip=True))
                for part in pieces if part.strip()]

    headers, columns = [], [[], []]
    for row in table.find_all('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if len(cells) != 2 or any(c.has_attr('rowspan') or c.has_attr('colspan') for c in cells):
            return None
        if all(c.name == 'th' for c in cells) and not headers and not columns[0]:
            headers = cells
            continue
        if any(c.name != 'td' or c.find(['table', 'math', 'img', 'svg']) for c in cells):
            return None
        for values, cell in zip(columns, cells):
            values.extend(lines(cell))
    labels, numbers = columns
    if (not headers or len(numbers) < 5 or not labels
            or not all(re.fullmatch(r'[+\-−]?\d[\d\s.,()+\-−–—%]*', text) for _, text in numbers)
            or len([text for _, text in labels if text.endswith(':')]) != 1):
        return None
    return headers, labels, numbers


def _source_value_line_count(image, api):
    """Count isolated value lines below a source header rule and right of its rail."""
    import cv2
    import numpy as np
    from statistics import median

    rules = api.RasterTableRules(image, 1)
    height, width = rules.preview_shape
    margins = [(x, y, w, h) for x, y, w, h in rules.horizontal if w >= .75*width]
    rails = [(x, y, w, h) for x, y, w, h in rules.vertical
             if .25*width < x < .85*width and w <= .02*width and h >= .55*height]
    if len(margins) != 2 or len(rails) != 1:
        return None
    header = max(y+h for _, y, _, h in margins)
    if header > .4*height:
        return None
    rail, _, thickness, _ = rails[0]
    with image.convert('L') as gray:
        pixels = np.asarray(gray)
        if gray.size != (width, height):
            pixels = cv2.resize(pixels, (width, height), interpolation=cv2.INTER_AREA)
        mask = cv2.threshold(pixels, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    straight = cv2.warpAffine(mask, rules.matrix, (width, height), flags=cv2.INTER_NEAREST)
    projection = np.count_nonzero(straight[header:, rail+thickness+4:], axis=1)
    counts = []
    for strength in (.045, .055):
        active = np.flatnonzero(projection >= max(5, (width-rail)*strength))
        runs = np.split(active, np.flatnonzero(np.diff(active) > 1)+1)
        bands = [run for run in runs if len(run) >= max(4, height*.012)]
        if len(bands) < 5:
            return None
        pitch = median(b[0]-a[0] for a, b in zip(bands, bands[1:]))
        if not all(.35*pitch < len(band) < 1.05*pitch for band in bands):
            return None
        counts.append(len(bands))
    return counts[0] if counts[0] == counts[1] else None


def _recover_value_rows(original, retry, image, box, reader, estimate, api, source_crop):
    """Recover a collapsed descriptor column from three OCR readings and source lines."""
    from difflib import SequenceMatcher

    sources = [_numeric_table_rows(BeautifulSoup(html, 'html.parser').table)
               for html in (original, retry)]
    if any(source is None for source in sources):
        return None
    _, labels, values = sources[0]
    if len(labels) != len(sources[1][1]):
        return None
    left, top, right, bottom = box
    with image.crop((left, top, right, bottom)) as crop:
        count = _source_value_line_count(crop, api)
    if count != len(labels)-1 or count <= min(len(values), len(sources[1][2])):
        return None

    # Preserve real edge pixels, then give the same model another layout view.
    from PIL import ImageOps
    margin = _CROP_PADDING//2
    expanded = (max(0, left-margin), max(0, top-margin),
                min(image.width, right+margin), min(image.height, bottom+margin))
    with (source_crop(expanded) if source_crop else image.crop(expanded)) as crop:
        with ImageOps.expand(crop, border=5*_CROP_PADDING, fill='white') as context:
            results = reader.predict_full([context], counts=[estimate])
    if len(results) != 1 or results[0].error:
        return None
    complete = BeautifulSoup(results[0].html or '', 'html.parser')
    if len(complete.find_all('table')) != 1 or complete.find(['script', 'iframe', 'object', 'img', 'svg']):
        return None
    third = _numeric_table_rows(complete.table)
    if not third or len(third[1]) != len(labels) or len(third[2]) != count:
        return None
    sources.append(third)

    # A longer read may fill omissions, but cannot silently replace existing values.
    numbers = [_key(text) for _, text in third[2]]
    for source in sources[:2]:
        ordered = iter(numbers)
        if not all(any(_key(text) == entry for entry in ordered) for _, text in source[2]):
            return None

    fragments = []
    for options in zip(*(source[1] for source in sources)):
        texts = [_key(text) for _, text in options]
        if any(SequenceMatcher(None, texts[0], text).ratio() < .8 for text in texts[1:]):
            return None
        if len({text.endswith(':') for text in texts}) != 1:
            return None
        fragments.append(max(options, key=lambda item: texts.count(_key(item[1]))))

    target = BeautifulSoup(retry, 'html.parser')
    template = target.table.find_all('tr')[1]
    for row in target.table.find_all('tr')[2:]:
        row.decompose()
    previous = template
    number_cells = iter(third[2])
    for fragment, text in fragments:
        row = target.new_tag('tr')
        for html in (fragment, '' if text.endswith(':') else next(number_cells)[0]):
            cell = target.new_tag('td')
            cell.append(BeautifulSoup(html, 'html.parser'))
            row.append(cell)
        previous.insert_after(row)
        previous = row
    template.decompose()
    return str(target), dict(source_value_lines=count, restored_rows=len(fragments))

def _review_retry_rows(original, candidate, profile, original_profile, image, box, reader, estimate, api, proof, source_crop=None):
    proof.update(rows=profile['rows'], columns=profile['columns'])
    if original_profile.get('issues'):
        return candidate, profile
    if profile['rows'] < original_profile['rows']:
        recovered = (_recover_value_rows(original, candidate, image, box, reader, estimate, api, source_crop)
                     if profile['columns'] == original_profile['columns'] == 2 else None)
        if recovered is not None:
            candidate, row_proof = recovered
            profile = api.table_profile(str(BeautifulSoup(candidate, 'html.parser').table))
            if not profile['issues'] and profile['rows'] >= original_profile['rows']:
                proof['source_row_recovery'] = row_proof
                return candidate, profile
        raise ValueError('source_table_retry_reduced_rows')
    return candidate, profile

_EQUATION_NUMBER = re.compile(r'(?:\\quad\s*)?\((\d+\s*[-–—]\s*\d+)\)\s*\.?$')
_BROKEN_POWER = re.compile(r'/\s*(?:[A-Za-z]\s+)?[A-Za-z]\s*_\s*\{?[A-Za-z0-9]')
_COMPARISON = re.compile(r'\\(?:geq?|leq?)(?![A-Za-z])|[≥≤]')
_MATH_NUMBERS = re.compile(r'\d+(?:[,.]\d+)?')


def _formula_crop(regions, page, bbox, mode):
    import numpy as np

    def spans(occupied):
        edges = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
        return list(zip(edges[::2], edges[1::2]))

    left, top, right, bottom = bbox
    scale = 6
    with regions.crop(page, (left, top, right+(2 if mode == 'comparison' else 0),
                            bottom+(0 if mode == 'leading_comparison' else 2.5)), scale=scale) as probe:
        with probe.convert('L') as gray:
            ink = np.asarray(gray) < 165
        vertical = spans(ink.any(axis=1))
        if mode == 'leading_comparison':
            bands = [band for band in vertical if band[1]-band[0] > 4*scale]
            if len(bands) < 2 or bands[1][0]-bands[0][1] < 1.5*scale:
                return None
            y0, y1 = bands[0]
        elif vertical:
            y0, y1 = vertical[0][0], vertical[-1][1]
        else:
            return None
        horizontal = spans(ink[y0:y1].any(axis=0))
        if not horizontal:
            return None
        x0, x1 = horizontal[0][0], horizontal[-1][1]
        if mode == 'numbered':
            gaps = sorted(((b[0]-a[1], a[1], b[0]) for a, b in zip(horizontal, horizontal[1:])), reverse=True)
            if not gaps or gaps[0][0] < max(25*scale, y1-y0):
                return None
            if gaps[0][1]-x0 < (x1-x0)*.45 or x1-gaps[0][2] > (x1-x0)*.28:
                return None
            x1 = gaps[0][1]
            margins = (5, .8, 2.7, 1.5)
        else:
            margins = (2.4, 1.4, 2.4, 2)
        return (left+x0/scale-margins[0], top+y0/scale-margins[1],
                left+x1/scale+margins[2], top+y1/scale+margins[3])


def _formula_signature(latex):
    # The same shape can be spelled using different LaTeX macros or punctuation.
    latex = re.sub(r'\\(?:varepsilon|epsilon)(?![A-Za-z])|[εϵ]', 'e', latex)
    latex = re.sub(r'\\text\{\s*lg\s*\}|\\lg(?![A-Za-z])', 'lg', latex)
    latex = re.sub(r'(?<=\d),(?=\d)', '.', latex)
    latex = re.sub(r'\\(?:dfrac|tfrac)(?![A-Za-z])', r'\\frac', latex)
    return re.sub(r'\s+|\\(?:left|right)(?![A-Za-z])', '', latex).rstrip('.,')


def _read_formula_crop(regions, page, box, mode, old, backend):
    from PIL import ImageEnhance, ImageOps
    from surya.inference.schema import BatchInputItem

    digits = _MATH_NUMBERS.findall(old)
    comparator = _COMPARISON.findall(old)
    views = [(4.2, 16, 1), (4.5, 16, 1.7), (7.5, 12, 1)]
    if mode != 'numbered':
        views = views[:2]
    reads = []
    for scale, padding, contrast in views:
        with regions.crop(page, box, scale=scale) as raster:
            with ImageEnhance.Contrast(raster).enhance(contrast) as enhanced:
                with ImageOps.expand(enhanced, padding, fill='white') as image:
                    outputs = backend.generate([BatchInputItem(
                        image=image, prompt_type='block', max_tokens=384,
                        metadata={'purpose': 'source_formula_structure_check', 'page': page})])
        if len(outputs) != 1 or outputs[0].error:
            continue
        parsed = BeautifulSoup(outputs[0].raw or '', 'html.parser')
        maths = parsed.find_all('math')
        if len(maths) != 1 or maths[0].find():
            continue
        latex = maths[0].get_text().strip()
        maths[0].decompose()
        if parsed.get_text(strip=True) or parsed.find(['img', 'table', 'svg']):
            continue
        if (r'\sqrt' not in latex or _COMPARISON.findall(latex) != comparator
                or [v.replace(',', '.') for v in _MATH_NUMBERS.findall(latex)]
                   != [v.replace(',', '.') for v in digits]):
            continue
        if mode == 'numbered' and r'\frac' in old and _formula_signature(latex).count(r'\frac') < 2:
            continue
        reads.append(latex)
    if not reads:
        return None
    votes = Counter(_formula_signature(latex) for latex in reads)
    signature, agreement = votes.most_common(1)[0]
    if agreement < 2:
        return None
    eligible = [latex for latex in reads if _formula_signature(latex) == signature]
    return max(eligible, key=lambda latex: (
        _MATH_NUMBERS.findall(latex) == digits,
        bool(re.search(r'\\(?:varepsilon|epsilon)(?![A-Za-z])|[εϵ]', latex))))


def refine_primary_formula_blocks(prediction, model, args, page, rect, api):
    """Re-read broken numbered equations and comparison lines from isolated PDF ink."""
    blocks = list(prediction.blocks)
    source = getattr(args, 'current_normalized_pdf', None)
    if source is None:
        return prediction
    regions = None
    try:
        for index, block in enumerate(blocks):
            html = block.html or ''
            if '<math' not in html or '<table' in html or getattr(block, 'error', False):
                continue
            parsed = BeautifulSoup(html, 'html.parser')
            maths = parsed.find_all('math')
            if not maths:
                continue
            first = maths[0].get_text()
            number = _EQUATION_NUMBER.search(first)
            outside = BeautifulSoup(html, 'html.parser')
            for math_tag in outside.find_all('math'):
                math_tag.decompose()
            prose = outside.get_text(strip=True)
            mode = None
            if (block.label == 'Equation' and number and not prose
                    and (len(maths) == 2 and maths[1].get_text().lstrip().startswith('+')
                         or len(maths) == 1 and _BROKEN_POWER.search(first))):
                mode = 'numbered'
                old = first[:number.start()] + ''.join(m.get_text() for m in maths[1:])
            elif (len(maths) == 1 and _BROKEN_POWER.search(first) and _COMPARISON.search(first)):
                if not prose and block.label == 'Equation':
                    mode = 'comparison'
                elif (prose and maths[0].find_next_sibling()
                      and maths[0].find_next_sibling().name == 'br'
                      and parsed.get_text(strip=True).startswith(first.strip())):
                    mode = 'leading_comparison'
                old = first
            if mode is None:
                continue
            if regions is None:
                regions = api.PdfRegions(source)
            x0, y0, x1, y1 = block.bbox
            bbox = [x0/model.scale+rect.l, y0/model.scale+rect.t,
                    x1/model.scale+rect.l, y1/model.scale+rect.t]
            try:
                box = _formula_crop(regions, page, bbox, mode)
                latex = _read_formula_crop(regions, page, box, mode, old, api.local_surya_backend(model)) if box else None
            except (RuntimeError, ValueError, OSError) as error:
                api.diagnostic_event('PRIMARY_FORMULA_CROP_FAILED', page=page, block=index, error=str(error))
                continue
            if latex is None:
                api.diagnostic_event('PRIMARY_FORMULA_CROP_DECLINED', page=page, block=index, mode=mode)
                continue
            if mode == 'numbered':
                latex += r'\tag{' + re.sub(r'\s+', '', number.group(1)).replace('–', '-').replace('—', '-') + '}'
                for node in maths[1:]:
                    node.decompose()
            maths[0].string = latex
            candidate = str(parsed)
            blocks[index] = replace(block, html=candidate, text=api.semantic_ocr_text(candidate))
            api.diagnostic_event('PRIMARY_FORMULA_CROP_RESTORED', page=page, block=index, bbox=box, mode=mode)
    finally:
        if regions is not None:
            regions.close()
    return replace(prediction, blocks=tuple(blocks))
