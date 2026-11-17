"""Join atomic OCR subrows only on a unique measured physical source grid."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import math
import re
import unicodedata

from bs4 import BeautifulSoup


POSITION = ('row', 'column', 'row_span', 'column_span')


def _layout_key(value):
    value = unicodedata.normalize('NFC', value)
    # Header matching uses spelling only to locate the original payload.
    # A printed wrap hyphen does not change which source heading owns it.
    value = re.sub(r'(?<=[^\W\d_])[-‐‑]\s*(?=[^\W\d_])', '', value)
    return re.sub(r'\s|\u00ad', '', value)


def _align_primary_header(fragment, geometry, target, api):
    """Place complete primary headings on a unique measured source header."""
    from docling_pdf_table_content import _safe_markup
    height = geometry.get('header_rows', 0)
    if height < 1:
        return None
    parsed = BeautifulSoup(fragment, 'html.parser')
    table = parsed.table
    if table is None or table.thead is None:
        return None
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    band = [index for index, row in enumerate(rows) if row.find_parent('thead') is table.thead]
    if band != list(range(height)):
        return None  # Moving a header never shifts a body row or its payload.
    primary = api.table_profile(fragment)
    raw = [cell for cell in primary['cells'] if cell['row'] < height]
    source = [cell for cell in geometry['cells'] if cell['row'] < height]
    if (len(raw) != len(source) or any(cell['row']+cell['row_span'] > height for cell in raw+source)):
        return None
    choices = []
    for cell in raw:
        literal = _layout_key(BeautifulSoup(_safe_markup(cell['html'], api), 'html.parser').get_text())
        if len(literal) < 3:
            return None
        matches = [index for index, owner in enumerate(source)
                   if (owner['column'] == cell['column'] or primary['issues'])
                   and literal in _layout_key(owner.get('scan_layout_text', ''))]
        if not matches:
            return None
        choices.append(matches)
    solutions = []
    def match(index, used, owners):
        if len(solutions) > 1:
            return
        if index == len(raw):
            if len(used) == len(source):
                solutions.append(list(owners))
            return
        for owner in choices[index]:
            if owner not in used:
                match(index+1, used | {owner}, owners+[owner])
    match(0, set(), [])
    if len(solutions) != 1:
        return None
    keys = lambda cell: tuple(cell[k] for k in POSITION)
    mapping = {keys(source[owner]): cell for cell, owner in zip(raw, solutions[0])}
    if all(keys(cell) == keys(source[owner]) for cell, owner in zip(raw, solutions[0])):
        return None
    before_body = [str(row) for row in rows[height:]]
    header = parsed.new_tag('thead')
    for row in range(height):
        tr = parsed.new_tag('tr')
        header.append(tr)
        for owner in sorted((cell for cell in source if cell['row'] == row), key=lambda cell: cell['column']):
            node = parsed.new_tag('th')
            for attr, field in (('rowspan', 'row_span'), ('colspan', 'column_span')):
                if owner[field] > 1:
                    node[attr] = str(owner[field])
            node.append(BeautifulSoup(mapping[keys(owner)]['html'], 'html.parser'))
            tr.append(node)
    table.thead.replace_with(header)
    if before_body != [str(row) for row in rows[height:]]:
        return None
    if Counter(cell['html'] for cell in raw) != Counter(node.decode_contents() for node in header.find_all('th')):
        return None
    profile = api.table_profile(str(table))
    if (set(profile['issues'])-set(primary['issues'])
            or profile['columns'] != target['columns']):
        return None
    return str(table), dict(method='unique_complete_primary_header_on_measured_source_header',
        header_rows=height, published_hidden_characters=0, body_payloads_unchanged=True,
        cells=[dict(primary=list(keys(cell)), source=list(keys(source[owner])),
                    source_bbox=source[owner]['bbox']) for cell, owner in zip(raw, solutions[0])])


def quarantine_scan_text(fragment, geometry, image, region, page, api):
    """A page scan's hidden OCR supplies geometry, never published text."""
    from docling_pdf_table_layout import grid_ink
    from docling_pdf_table_content import _source_crop
    import cv2

    geometry = deepcopy(geometry)
    geometry['page_scan_transcription_required'] = True
    parsed = BeautifulSoup(fragment, 'html.parser')
    profile = api.table_profile(fragment)
    cells = {tuple(cell[k] for k in POSITION): cell for cell in geometry['cells']}
    nodes = [node for node in parsed.table.find_all(['td', 'th']) if node.find_parent('table') is parsed.table]
    ink = grid_ink(image, region, geometry['x_grid'])
    retained = []
    for node, position in zip(nodes, profile['cells']):
        cell = cells[tuple(position[k] for k in POSITION)]
        # These characters are lookup coordinates only. Every eventual output
        # character must come from primary OCR or a new source-pixel reading.
        cell['scan_layout_text'] = node.get_text('\n', strip=True)
        box = cell['bbox']
        crop = _source_crop(image, region, box)
        try:
            if ink is None:
                visible = True
            else:
                left = max(0, math.ceil((box[0]-region[0]+.7)*ink['sx']))
                top = max(0, math.ceil((box[1]-region[1]+.7)*ink['sy']))
                right = min(image.width, math.floor((box[2]-region[0]-.7)*ink['sx']))
                bottom = min(image.height, math.floor((box[3]-region[1]-.7)*ink['sy']))
                residual = ink['nongrid'][top:bottom, left:right]
                _, _, components, _ = cv2.connectedComponentsWithStats(residual, 8) if residual.size else (None, None, [], None)
                visible = any(component[4] >= 3 for component in components[1:])
            payload = api.image_html(crop, box[2]-box[0], 'cell') if visible else ''
        finally:
            crop.close()
        node.clear()
        if payload:
            picture = BeautifulSoup(payload, 'html.parser').img
            picture['data-source-cell'] = 'true'
            node.append(picture)
            retained.append(dict(row=position['row'], column=position['column'], bbox=list(box),
                                 basis='visible_scan_pixels_without_hidden_ocr_authority'))
        cell.update(html=node.decode_contents(), text='', source_text_unreliable=True)
        cell['scan_layout_has_ink'] = visible
        cell.pop('positioned_source_text', None)
    geometry['retained_cell_images'] = retained
    geometry['text_method'] = 'scan_pixels_pending_primary_transcription'
    api.diagnostic_event('SCAN_HIDDEN_TEXT_QUARANTINED', page=page, cells=len(cells),
                         source_grid_verified=geometry.get('source_grid_verified'), hidden_text_used=False)
    return str(parsed.table), geometry


def verify_scan_radicals(fragment, geometry, image, region, page, args, manifest, api, *, ref=None):
    """Check radicals against two original physical-cell readings.

    A correction may change only a complete, uniquely matched radical equation.
    The two source readings must agree on every returned character, index and
    other mathematical structure. An unchanged primary tail is never replaced.
    """
    if not geometry.get('page_scan_transcription_required') or not geometry.get('source_grid_verified'):
        return fragment
    from docling_pdf_table_content import _safe_markup
    from docling_pdf_primary_cells import _read_cell
    from docling_pdf_vector_content import content_signature

    parsed = BeautifulSoup(fragment, 'html.parser')
    profile = api.table_profile(fragment)
    if profile['issues']:
        return fragment
    physical = {tuple(cell[k] for k in POSITION): cell for cell in geometry['cells']}
    nodes = [node for node in parsed.table.find_all(['td', 'th']) if node.find_parent('table') is parsed.table]
    for node, position in zip(nodes, profile['cells']):
        cell = physical.get(tuple(position[k] for k in POSITION))
        if not cell or not cell.get('primary_ocr') or node.find(['img', 'table', 'svg']):
            continue
        primary = BeautifulSoup(_safe_markup(node.decode_contents(), api), 'html.parser')
        roots = primary.find_all(['msqrt', 'mroot'])
        if not roots:
            continue
        context = dict(page=page, ref=ref, **{k: position[k] for k in POSITION}, bbox=list(cell['bbox']))
        def review(reason):
            warning = dict(context, reason=reason,
                message=f"Строка {position['row']+1}, столбец {position['column']+1}: "
                        'проверьте границы корня по исходному PDF; независимые чтения не подтвердили его структуру.')
            manifest.setdefault('scan_formula_source_reviews', []).append(warning)
            api.diagnostic_event('SCAN_RADICAL_SOURCE_REVIEW', **warning)
        if cell.get('primary_packet_graft'):
            # External positioned packets have a separate immutable ownership
            # ledger. Do not invalidate it with a later structural replacement.
            api.diagnostic_event('SCAN_RADICAL_POSITIONED_PACKET_RETAINED', **context)
            continue
        readings = []
        try:
            for scale in (1, 2):
                source = image if scale == 1 else image.resize((image.width*scale, image.height*scale))
                try:
                    result = _read_cell(args, source, region, cell, page, api)
                finally:
                    if source is not image:
                        source.close()
                if not result:
                    break
                content, proof = result
                clean = BeautifulSoup(_safe_markup(content, api), 'html.parser')
                if clean.find(['img', 'svg', 'table']):
                    break
                readings.append((clean, proof, content_signature(str(clean), api)))
        except Exception as error:
            api.diagnostic_event('SCAN_RADICAL_SOURCE_READ_FAILED', **context,
                                 error_type=type(error).__name__, error=str(error))
        original_signature = content_signature(str(primary), api)
        if len(readings) != 2 or readings[0][2] != readings[1][2]:
            review('composite_radical_source_readings_disagree')
            continue
        source_signature = readings[0][2]
        limit = len(source_signature[0])
        if (not original_signature[0].startswith(source_signature[0])
                or any(start < limit < stop for _, start, stop in original_signature[1])
                or tuple(value for value in original_signature[1] if value[2] <= limit) != source_signature[1]):
            review('radical_source_visible_characters_or_indices_disagree')
            continue
        def radical_inventory(tree):
            inventory = []
            for root in tree.find_all(['msqrt', 'mroot']):
                parts = root.find_all(True, recursive=False)
                if root.name == 'mroot':
                    if len(parts) != 2 or not all(part.get_text(strip=True) for part in parts):
                        return None
                    degree = content_signature(str(parts[1]), api)
                else:
                    if not root.get_text(strip=True):
                        return None
                    degree = None
                inventory.append((root.name, degree))
            return Counter(inventory)
        radicals = radical_inventory(primary)
        if radicals is None or any(radical_inventory(reading[0]) != radicals for reading in readings):
            review('source_radical_count_kind_or_degree_disagrees')
            continue
        # The two source readings may place an equation prefix inside/outside
        # MathML. Compare their complete visible sequence above, then locate
        # only the original radical-bearing equation for a local replacement.
        def stable(signature):
            return signature[:2] + (tuple(value for value in signature[2]
                                          if value[0] not in {'msqrt', 'mroot'}),)
        if (any(value[0] not in {'msqrt', 'mroot'} and value[1] < limit < value[2]
                for value in original_signature[2])
                or stable(source_signature)[2] != tuple(value for value in stable(original_signature)[2]
                                                       if value[2] <= limit)):
            review('nonradical_source_structure_disagrees')
            continue
        changes = []
        for equation in primary.find_all('math'):
            if not equation.find(['msqrt', 'mroot']):
                continue
            expected = content_signature(str(equation), api)
            matches = [[candidate for candidate in reading[0].find_all('math')
                        if (stable(content_signature(str(candidate), api)) == stable(expected)
                            and radical_inventory(candidate) == radical_inventory(equation))]
                       for reading in readings]
            if any(len(found) != 1 for found in matches):
                changes = None
                break
            first, second = (found[0] for found in matches)
            if content_signature(str(first), api) != content_signature(str(second), api):
                changes = None
                break
            if content_signature(str(first), api) != expected:
                changes.append((equation, str(first)))
        if changes is None:
            review('radical_equation_source_owner_not_unique')
            continue
        verification = dict(context,
            method='two_original_cell_readings_with_complete_matched_radical_equations',
            primary_sha256=sha256(str(primary).encode()).hexdigest(),
            unchanged_primary_tail_characters=len(original_signature[0])-limit,
            responses=[reading[1]['response_sha256'] for reading in readings])
        manifest.setdefault('scan_formula_source_verifications', []).append(verification)
        cell['primary_ocr']['radical_source_verified'] = True
        for equation, replacement in changes:
            before = str(equation)
            equation.replace_with(BeautifulSoup(replacement, 'html.parser').math)
            manifest.setdefault('scan_formula_source_repairs', []).append(dict(context,
                method='two_source_reads_only_complete_radical_structure_changed_primary_tail_preserved',
                primary_sha256=sha256(before.encode()).hexdigest(),
                source_sha256=sha256(replacement.encode()).hexdigest(),
                responses=[reading[1]['response_sha256'] for reading in readings]))
            api.diagnostic_event('SCAN_RADICAL_SOURCE_CORRECTED', **context)
        if changes:
            node.clear()
            node.append(primary)
            cell.update(html=node.decode_contents(), text=node.get_text(' ', strip=True))
    return str(parsed.table)


def align_atomic_primary_rows(fragment, geometry, target, api):
    """Preserve all OCR cells while joining logical subrows inside source cells.

    Only a unique monotone assignment is accepted. A primary cell is never
    divided, moved between columns, or carried across a measured source border.
    Exact primary literals may locate a unique hidden-layer cell, but those
    hidden characters never participate in the returned transcription.
    """
    if (not geometry.get('source_grid_verified') or not geometry.get('page_scan_transcription_required')
            or target.get('issues')):
        return None
    header_alignment = _align_primary_header(fragment, geometry, target, api)
    if header_alignment:
        fragment, header_proof = header_alignment
    else:
        header_proof = None
    primary = api.table_profile(fragment)
    if (primary.get('issues') or primary['columns'] != target['columns']
            or primary['rows'] < target['rows']-1):
        return None
    key = lambda cell: tuple(cell[name] for name in POSITION)
    physical = {key(cell): cell for cell in geometry.get('cells', [])}
    if set(physical) != {key(cell) for cell in target['cells']}:
        return None
    n = primary['rows']
    ends = [target['rows']]
    while ends[-1] > 1:
        tail = [cell for cell in target['cells'] if cell['row'] == ends[-1]-1]
        if (len(tail) != 1 or tail[0]['column'] != 0 or tail[0]['column_span'] != target['columns']
                or tail[0]['row_span'] != 1):
            break
        ends.append(ends[-1]-1)
    solutions, states = [], 0
    primary_cells = primary['cells']
    active = [[cell for cell in primary_cells if cell['row'] <= row < cell['row']+cell['row_span']]
              for row in range(n)]
    from docling_pdf_table_content import _safe_markup
    anchor_key = _layout_key
    def has_payload(cell):
        parsed = BeautifulSoup(cell['html'], 'html.parser')
        return bool(parsed.get_text(strip=True) or parsed.find(['math', 'img', 'svg']))
    anchors = []
    for cell in primary_cells:
        literal = anchor_key(BeautifulSoup(_safe_markup(cell['html'], api), 'html.parser').get_text())
        if len(literal) < 3:
            continue
        matches = [owner for owner in physical.values()
                   if owner['column'] == cell['column'] and owner['column_span'] == cell['column_span']
                   and literal in anchor_key(owner.get('scan_layout_text', ''))]
        if len(matches) == 1:
            anchors.append((cell, matches[0]))
    for end in ends:
        if end > n:
            continue
        prefix = [cell for cell in target['cells'] if cell['row'] < end]
        if any(cell['row']+cell['row_span'] > end for cell in prefix):
            continue
        by_slot = {(row, cell['column'], cell['column_span']): cell
                   for cell in prefix for row in range(cell['row'], cell['row']+cell['row_span'])}
        domains = [set(range(end)) for _ in range(n)]
        for cell, owner in anchors:
            rows = set(range(owner['row'], owner['row']+owner['row_span']))
            for row in range(cell['row'], cell['row']+cell['row_span']):
                domains[row] &= rows
        if any(not domain for domain in domains):
            continue
        assignment = []

        def solve(row):
            nonlocal states
            states += 1
            if states > 8192 or len(solutions) > 1:
                return
            if row == n:
                if assignment[-1] != end-1:
                    return
                owners = {}
                for cell in primary_cells:
                    first, last = assignment[cell['row']], assignment[cell['row']+cell['row_span']-1]
                    owner = by_slot[(first, cell['column'], cell['column_span'])]
                    owners.setdefault(key(owner), []).append(cell)
                complete = set(owners) == {key(cell) for cell in prefix}
                # An empty OCR placeholder cannot erase a source cell with ink.
                if complete and any(owner.get('scan_layout_has_ink', False)
                                    and not any(has_payload(piece) for piece in owners[key(owner)])
                                    for owner in prefix):
                    complete = False
                if complete and any(owner.get('scan_layout_has_ink') is False
                                    and any(has_payload(piece) for piece in owners[key(owner)])
                                    for owner in prefix):
                    complete = False
                if complete:
                    solutions.append((list(assignment), owners, end))
                return
            previous = assignment[-1] if assignment else 0
            for source_row in ((previous, previous+1) if assignment else (0,)):
                if source_row not in domains[row] or end-1-source_row > n-1-row:
                    continue
                valid = True
                for cell in active[row]:
                    first = source_row if cell['row'] == row else assignment[cell['row']]
                    owner = by_slot.get((first, cell['column'], cell['column_span']))
                    current = by_slot.get((source_row, cell['column'], cell['column_span']))
                    if owner is None or current is not owner:
                        valid = False
                        break
                if valid:
                    assignment.append(source_row)
                    solve(row+1)
                    assignment.pop()
        solve(0)
    if len(solutions) != 1 or states > 8192:
        return None
    assignment, owners, end = solutions[0]
    out = BeautifulSoup('<table></table>', 'html.parser')
    original = BeautifulSoup(fragment, 'html.parser')
    out.table.attrs = dict(original.table.attrs)
    mapping = []
    for row in range(end):
        tr = out.new_tag('tr')
        out.table.append(tr)
        for cell in (cell for cell in target['cells'] if cell['row'] == row):
            node = out.new_tag('th' if cell.get('header') else 'td')
            for attr, name in (('rowspan', 'row_span'), ('colspan', 'column_span')):
                if cell[name] > 1:
                    node[attr] = str(cell[name])
            pieces = owners[key(cell)]
            payload = '<br/>'.join(piece['html'] for piece in pieces)
            mapping.extend(dict(primary=list(key(piece)), source=list(key(cell))) for piece in pieces)
            node.append(BeautifulSoup(payload, 'html.parser'))
            tr.append(node)
    result = api.table_profile(str(out.table))
    expected_positions = {key(cell) for cell in target['cells'] if cell['row'] < end}
    if result['issues'] or {key(cell) for cell in result['cells']} != expected_positions:
        return None
    # Formula spelling and structure must survive joining, including duplicates.
    expected = Counter(str(node) for node in original.find_all('math'))
    body = BeautifulSoup(''.join(cell['html'] for cell in result['cells'] if cell['row'] < end), 'html.parser')
    if Counter(str(node) for node in body.find_all('math')) != expected:
        return None
    result['source_partition_proof'] = dict(
        method='unique_monotone_atomic_primary_rows_on_measured_source_grid',
        primary_rows=n, source_rows=end, row_mapping=assignment, mapping=mapping,
        primary_cells=len(primary_cells), primary_payload_characters_preserved=True,
        hidden_pdf_text_used_for_content=False,
        source_layout_text_used_for_row_assignment=bool(anchors),
        exact_primary_literal_geometry_anchors=len(anchors))
    if header_proof:
        result['source_partition_proof']['primary_header_alignment'] = header_proof
    return result


def source_primary_subrows(fragment, geometry, records, page, reader, api):
    """Restore primary logical rows on a single measured physical source band.

    Complete positioned descriptions locate every record. Other columns must
    preserve their visible values on those same baselines, and each formula
    must own one native source asset within its record's vertical extent.
    """
    from docling_pdf_table_content import _safe_markup

    def visible(value, *, without_math=False):
        parsed = BeautifulSoup(value, 'html.parser')
        for node in parsed.find_all(['img', 'svg'] + (['math'] if without_math else [])):
            node.decompose()
        return _layout_key(BeautifulSoup(_safe_markup(str(parsed), api), 'html.parser').get_text())

    if (not geometry or geometry.get('hidden_ocr_direction_conflict')
            or not (geometry.get('source_grid_verified') or geometry.get('positioned_source_grid_verified'))):
        return None
    xs, ys = geometry.get('x_grid', []), geometry.get('y_grid', [])
    if len(ys) > 2:
        source = BeautifulSoup(fragment, 'html.parser')
        target = api.table_profile(fragment)
        positions = lambda cell: tuple(cell[k] for k in POSITION)
        physical_all = {positions(c): c for c in geometry.get('cells', [])}
        if (source.table is None or target['issues'] or len(xs) != target['columns']+1
                or len(ys) != target['rows']+1
                or set(physical_all) != {positions(c) for c in target['cells']}):
            return None
        solutions = []
        for band in range(geometry.get('header_rows', 0), target['rows']):
            owners = sorted((c for c in physical_all.values() if c['row'] == band), key=lambda c: c['column'])
            if (len(owners) != target['columns']
                    or any((c['column'], c['row_span'], c['column_span']) != (index, 1, 1)
                           for index, c in enumerate(owners))
                    or any(c['row'] < band < c['row']+c['row_span'] for c in physical_all.values())):
                continue
            for record in records or ():
                if record.get('page_no') != page or record.get('label') not in {'Table', 'TableOfContents'}:
                    continue
                bounds = record.get('bbox')
                if (not bounds or len(bounds) != 4
                        or bounds[0] > xs[0]+4 or bounds[2] < xs[-1]-4
                        or bounds[1] > ys[0]+4 or bounds[3] < ys[-1]-4
                        or abs((bounds[2]-bounds[0])-(xs[-1]-xs[0])) > .05*(xs[-1]-xs[0])):
                    continue
                primary_dom = BeautifulSoup(record.get('html', ''), 'html.parser')
                if len(primary_dom.find_all('table')) != 1 or primary_dom.find(['img', 'svg']):
                    continue
                primary = api.table_profile(str(primary_dom.table))
                added = primary['rows']-target['rows']
                count = added+1
                if primary['issues'] or primary['columns'] != target['columns'] or count < 2:
                    continue
                outside = {(c['row']+(added if c['row'] > band else 0), *positions(c)[1:]): c
                           for c in target['cells'] if c['row'] != band}
                primary_outside = {positions(c): c for c in primary['cells']
                                   if c['row'] < band or c['row'] >= band+count}
                if (set(outside) != set(primary_outside)
                        or any(visible(c['html']) != visible(primary_outside[key]['html'])
                               or c.get('header') != primary_outside[key].get('header')
                               for key, c in outside.items())):
                    continue
                body = [c for c in primary['cells'] if band <= c['row'] < band+count]
                if (len(body) != count*target['columns']
                        or any(c['row_span'] != 1 or c['column_span'] != 1 or c.get('header') for c in body)):
                    continue
                rows = [row for row in primary_dom.table.find_all('tr') if row.find_parent('table') is primary_dom.table]
                mini_html = '<table>'+''.join(str(row) for row in rows[band:band+count])+'</table>'
                physical_rows = [row for row in source.table.find_all('tr') if row.find_parent('table') is source.table]
                native_body = '<table>'+str(physical_rows[band])+'</table>'
                mini = deepcopy(geometry)
                mini.update(header_rows=0, y_grid=ys[band:band+2],
                    bbox=[xs[0], ys[band], xs[-1], ys[band+1]],
                    cells=[dict(c, row=0) for c in owners],
                    retained_cell_images=[dict(a, row=0) for a in geometry.get('retained_cell_images', [])
                                          if a.get('row') == band])
                planned = source_primary_subrows(native_body, mini,
                    [dict(record, html=mini_html, bbox=list(mini['bbox']))], page, None, api)
                if planned is None:
                    continue
                body_html, proof = planned
                result = BeautifulSoup(fragment, 'html.parser')
                old_rows = [row for row in result.table.find_all('tr') if row.find_parent('table') is result.table]
                before = [str(row) for index, row in enumerate(old_rows) if index != band]
                for row in list(BeautifulSoup(body_html, 'html.parser').table.find_all('tr')):
                    old_rows[band].insert_before(row.extract())
                old_rows[band].decompose()
                final = api.clean_table_html(str(result.table))
                checked = api.table_profile(final)
                after_rows = BeautifulSoup(final, 'html.parser').table.find_all('tr')
                after = [str(row) for index, row in enumerate(after_rows)
                         if not band <= index < band+count]
                if checked['issues'] or checked['rows'] != primary['rows'] or before != after:
                    continue
                proof.update(source_physical_band=band, source_band_logical_rows=count,
                    physical_rows=target['rows'], logical_rows=checked['rows'],
                    total_physical_rows=target['rows'], total_logical_rows=checked['rows'],
                    columns=target['columns'],
                    row_mapping=list(range(band))+[band]*count+list(range(band+1, target['rows'])),
                    physical_y_grid=list(ys),
                    logical_y_grid=list(ys[:band+1])+proof['source_row_boundaries'][1:-1]+list(ys[band+1:]),
                    unchanged_outer_cells=len(outside),
                    source_record_sha256=sha256(record['html'].encode()).hexdigest(),
                    source_record_bbox=list(record['bbox']), primary_body_rows=[band, band+count])
                solutions.append((final, proof))
        unique = {html: (html, proof) for html, proof in solutions}
        return next(iter(unique.values())) if len(unique) == 1 else None
    physical = sorted(geometry.get('cells', []), key=lambda c: c['column'])
    if (len(ys) != 2 or len(xs) < 3 or len(physical) != len(xs)-1
            or any((c['row'], c['column'], c['row_span'], c['column_span']) != (0, index, 1, 1)
                   for index, c in enumerate(physical))
            or any(c.get('source_text_unreliable') or c.get('primary_ocr')
                   or c.get('primary_packet_graft') or c.get('pending_primary_html') for c in physical)):
        return None
    lines = [[line for line in c.get('lines', []) if line.get('text', '').strip()] for c in physical]
    assets = geometry.get('retained_cell_images', [])
    solutions = []
    whole = [xs[0], ys[0], xs[-1], ys[-1]]
    for record in records or ():
        bounds = record.get('bbox')
        if (record.get('page_no') != page or record.get('label') not in {'Table', 'TableOfContents'}
                or not bounds or len(bounds) != 4
                or bounds[0] > whole[0]+4 or bounds[2] < whole[2]-4
                or bounds[1] > whole[1]+4 or bounds[3] < whole[3]-4
                or abs((bounds[2]-bounds[0])-(whole[2]-whole[0])) > .05*(whole[2]-whole[0])):
            continue
        parsed = BeautifulSoup(record.get('html', ''), 'html.parser')
        if len(parsed.find_all('table')) != 1 or parsed.find(['img', 'svg']):
            continue
        primary = api.table_profile(str(parsed.table))
        if primary['issues'] or primary['columns'] != len(physical):
            continue
        # A whole OCR block can include notes below the physical table. Keep
        # them with the existing note route only after matching source text.
        notes = [c for c in primary['cells'] if c['column_span'] == len(physical)]
        if notes:
            first = min(c['row'] for c in notes)
            if (reader is None or any(c['row'] < first or c['column'] != 0 or c['row_span'] != 1
                                     or not re.match(r'^\s*(?:примечани[ея]|notes?)\b', c['text'], re.I)
                                     for c in notes)
                    or any(c['row'] >= first for c in primary['cells'] if c not in notes)):
                continue
            reader.load_page(page)
            outside = [g for g in reader.chars if g.get('text', '').strip()
                       and bounds[0]-1 <= (g['x0']+g['x1'])/2 <= bounds[2]+1
                       and ys[-1]+1 < (g['top']+g['bottom'])/2 <= bounds[3]+1]
            if not outside or visible(''.join(l['html'] for l in api.native_cell_lines(outside))) != visible(
                    ''.join(c['html'] for c in notes)):
                continue
            rows = [r for r in parsed.table.find_all('tr') if r.find_parent('table') is parsed.table]
            for row in rows[first:]:
                row.decompose()
            primary = api.table_profile(str(parsed.table))
        count = primary['rows']
        if (primary['issues'] or count < 2
                or any(c['row_span'] != 1 or c['column_span'] != 1 or c.get('header')
                       for c in primary['cells']) or len(primary['cells']) != count*len(physical)):
            continue
        by_slot = {(c['row'], c['column']): c for c in primary['cells']}
        # Exactly one descriptive column must account for every source line
        # in order, including wrapped descriptions that occupy two baselines.
        descriptor_plans = []
        for column, content in enumerate(lines):
            if not content or any(a.get('column') == column for a in assets):
                continue
            groups, cursor = [], 0
            for row in range(count):
                wanted = visible(by_slot[row, column]['html'])
                if not re.search(r'[^\W\d_]', wanted):
                    break
                group, value = [], ''
                while cursor < len(content) and len(value) < len(wanted):
                    line = content[cursor]; cursor += 1
                    group.append(line); value += visible(line.get('html', line['text']))
                if value != wanted:
                    break
                groups.append(group)
            else:
                if cursor == len(content):
                    descriptor_plans.append((column, groups))
        if len(descriptor_plans) != 1:
            continue
        descriptor, groups = descriptor_plans[0]
        boundaries = [ys[0]] + [(left[-1]['y']+right[0]['y'])/2
                               for left, right in zip(groups, groups[1:])] + [ys[-1]]
        if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
            continue
        valid = True
        for column, content in enumerate(lines):
            for row in range(count):
                cell = by_slot[row, column]
                selected = [line for line in content if boundaries[row] <= line['y'] <= boundaries[row+1]]
                row_assets = [a for a in assets if a.get('column') == column
                              and boundaries[row] <= a['bbox'][1] and a['bbox'][3] <= boundaries[row+1]]
                math_nodes = BeautifulSoup(cell['html'], 'html.parser').find_all('math')
                if column != descriptor:
                    source = '<br/>'.join(line.get('html', line['text']) for line in selected)
                    matched = (visible(source) == visible(cell['html'], without_math=bool(row_assets))
                               and len(math_nodes) == len(row_assets))
                    if not matched and len(row_assets) == len(math_nodes) == 1:
                        from docling_pdf_formula_fragments import graft_source_fraction_fragment
                        from docling_pdf_table_content import _cell_content_signature
                        owner = physical[column]
                        selected_source = dict(owner, lines=selected,
                            glyphs=[g for g in owner.get('glyphs', []) if g.get('bbox')
                                    and boundaries[row] <= (g['bbox'][1]+g['bbox'][3])/2 <= boundaries[row+1]])
                        primary_html = _safe_markup(cell['html'], api)
                        grafted = graft_source_fraction_fragment(source, primary_html,
                            selected_source, row_assets, api)
                        matched = bool(grafted and _cell_content_signature(grafted[0], api)
                                       == _cell_content_signature(primary_html, api))
                    if not matched:
                        valid = False; break
            if not valid:
                break
        assigned = [a for a in assets if any(boundaries[row] <= a['bbox'][1]
                                            and a['bbox'][3] <= boundaries[row+1] for row in range(count))]
        if not valid or len(assigned) != len(assets):
            continue
        original = BeautifulSoup(fragment, 'html.parser').table
        if original:
            parsed.table.attrs = dict(original.attrs)
        result = api.clean_table_html(str(parsed.table))
        proof = dict(method='complete_primary_rows_on_positioned_source_descriptions_and_formula_assets',
            page=page, logical_rows=count, physical_rows=1, descriptor_column=descriptor,
            columns=len(physical), row_mapping=[0]*count,
            physical_y_grid=list(ys), logical_y_grid=list(boundaries),
            physical_rails_preserved_separately=True,
            source_record_sha256=sha256(record['html'].encode()).hexdigest(),
            physical_geometry_unchanged=True, source_row_boundaries=boundaries,
            native_formula_assets=len(assets), external_note_rows=len(notes))
        solutions.append((result, proof))
    unique = {html: (html, proof) for html, proof in solutions}
    return next(iter(unique.values())) if len(unique) == 1 else None


def graft_primary_body(native, records, page, manifest, api, *, ref=None, args=None,
                       source_image=None, region=None):
    """Reuse a complete primary tbody independently of its damaged header.

    Exact physical spans and independent primary source-cell readings must
    identify every body row uniquely. Hidden PDF text is never an anchor or
    a transcription. Optional OCR reads only one distinguishing cell per row.
    """
    from docling_pdf_table_content import _typed_payload_key, _typed_payload_digest, _has_source_authority

    if not native:
        return None
    fragment, original = native
    if original.get('primary_logical_rows_proof'):
        return None
    if not original.get('source_grid_verified'):
        return None
    parsed = BeautifulSoup(fragment, 'html.parser')
    if parsed.table is None:
        return None
    target = api.table_profile(fragment)
    header = original.get('header_rows', 0)
    rows, columns = target.get('rows', 0), target.get('columns', 0)
    if target.get('issues') or not isinstance(header, int) or not 0 < header < rows:
        return None
    key = lambda cell: tuple(cell[name] for name in POSITION)
    physical = {key(cell): cell for cell in original.get('cells', [])}
    if len(physical) != len(original.get('cells', [])) or set(physical) != {key(c) for c in target['cells']}:
        return None
    xs, ys = original.get('x_grid', []), original.get('y_grid', [])
    if (len(xs) != columns+1 or len(ys) != rows+1 or any(not math.isfinite(v) for v in xs+ys)
            or any(b <= a for axis in (xs, ys) for a, b in zip(axis, axis[1:]))
            or any(c['row'] < header < c['row']+c['row_span'] for c in physical.values())):
        return None
    whole = [xs[0], ys[0], xs[-1], ys[-1]]
    body = {k: cell for k, cell in physical.items() if k[0] >= header}
    for cell in physical.values():
        r, c, rs, cs = key(cell)
        wanted = [xs[c], ys[r], xs[c+cs], ys[r+rs]]
        if len(cell.get('bbox', [])) != 4 or any(abs(a-b) > 1 for a, b in zip(cell['bbox'], wanted)):
            return None
    nodes = {key(cell): node for cell, node in zip(target['cells'],
        (n for n in parsed.table.find_all(['td', 'th']) if n.find_parent('table') is parsed.table))}
    graphic_positions = {(entry.get('row'), entry.get('column'))
                         for entry in original.get('source_graphic_assets', [])}
    graphic_positions.update((entry.get('row'), entry.get('column'))
        for entry in original.get('retained_cell_images', [])
        if any(entry.get('source_column_context', {}).get(name)
               for name in ('graphic_column', 'dimension_column')))
    if any(k[:2] in graphic_positions or nodes[k].select_one('[data-cell-content="source-diagram"]')
           for k in body):
        return None

    def payload(value):
        return _typed_payload_key(value, equivalent_glyphs=False)

    def same_region(bounds):
        if (not bounds or len(bounds) != 4 or any(not isinstance(v, (int, float))
                or not math.isfinite(v) for v in bounds)):
            return False
        area = lambda b: max(0, b[2]-b[0])*max(0, b[3]-b[1])
        intersection = area([max(bounds[0], whole[0]), max(bounds[1], whole[1]),
                             min(bounds[2], whole[2]), min(bounds[3], whole[3])])
        return (min(area(bounds), area(whole)) > 0
                and all(intersection/area(box) >= .95 for box in (bounds, whole)))

    candidates = []
    expected = {(r-header, c, rs, cs) for r, c, rs, cs in body}
    for record in records or ():
        if (record.get('label') not in {'Table', 'TableOfContents'}
                or record.get('page_no') != page or not same_region(record.get('bbox'))):
            continue
        raw = record.get('html', '')
        source = BeautifulSoup(raw, 'html.parser')
        if len(source.find_all('table')) != 1:
            continue
        groups = source.table.find_all('tbody', recursive=False)
        if len(groups) != 1 or groups[0].find(['img', 'svg', 'script', 'style', 'iframe', 'object']):
            continue
        primary = api.table_profile('<table>'+str(groups[0])+'</table>')
        if (primary.get('issues') or primary['rows'] != rows-header or primary['columns'] != columns
                or {key(c) for c in primary['cells']} != expected):
            continue
        mapped = {(c['row']+header, c['column'], c['row_span'], c['column_span']): c
                  for c in primary['cells']}
        if any((cell.get('scan_layout_has_ink') is True and not payload(mapped[k]['html']))
               or (cell.get('scan_layout_has_ink') is False and payload(mapped[k]['html']))
               for k, cell in body.items()):
            continue
        candidates.append((record, mapped))
    if len(candidates) != 1:
        return None  # Competing packets may not assign the same physical body.
    record, mapped = candidates[0]
    identity = sha256(record['html'].encode()).hexdigest()
    anchors = []

    def unique_rows(k, value):
        return [other for other in body if other[1:] == k[1:] and payload(mapped[other]['html']) == value]

    def row_identifier(content):
        node = BeautifulSoup(content, 'html.parser')
        if node.find(['math', 'sub', 'sup', 'img', 'svg', 'table']):
            return None
        text = node.get_text(' ', strip=True)
        return api.cell_text_key(text).casefold() if re.search(r'[^\W\d_]', text, re.UNICODE) else None

    def independent(cell, node):
        proof = cell.get('primary_ocr') or {}
        if (proof.get('method') == 'primary_surya_on_exact_source_cell'
                and proof.get('source_bbox') == cell.get('bbox')
                and proof.get('response_sha256') and proof.get('image_sha256')
                and payload(node.decode_contents()) == payload(cell.get('html', ''))):
            return proof
        graft = cell.get('primary_packet_graft') or {}
        packets = graft.get('packets') or []
        if (graft.get('method') == 'complete_primary_packets_on_verified_physical_cell'
                and graft.get('rendered_payload_sha256') == _typed_payload_digest(node.decode_contents())
                and packets and all(p.get('source_record_sha256') != identity for p in packets)):
            return graft
        return None

    for k, cell in body.items():
        if (_has_source_authority(nodes[k], cell, original)
                and payload(nodes[k].decode_contents()) != payload(mapped[k]['html'])):
            return None
        proof = independent(cell, nodes[k])
        if proof is None:
            continue
        value = payload(nodes[k].decode_contents())
        if value != payload(mapped[k]['html']):
            return None  # Preserve independently read values on a conflict.
        if value and unique_rows(k, value) == [k]:
            anchors.append(dict(position=list(k), proof=proof))
    anchored = {row for entry in anchors for row in range(entry['position'][0],
                                                         entry['position'][0]+entry['position'][2])}
    for row in range(header, rows):
        if row in anchored:
            continue
        if args is None or source_image is None or region is None:
            return None
        # A distinct literal with letters can identify its row; a repeated
        # dash, number or blank cannot authorize a guessed row assignment.
        choices = [k for k in body if k[0] == row and k[2] == 1
                   and row_identifier(mapped[k]['html'])
                   and [other for other in body if other[1:] == k[1:]
                        and row_identifier(mapped[other]['html']) == row_identifier(mapped[k]['html'])] == [k]]
        if not choices:
            return None
        k = min(choices, key=lambda v: (v[1], len(mapped[v]['text'])))
        from docling_pdf_primary_cells import _read_cell
        try:
            reading = _read_cell(args, source_image, region,
                                 dict(body[k], _source_cell_role='text'), page, api)
        except RuntimeError as error:
            if not (str(error).startswith('Local block request failed after retry:')
                    or str(error) == 'Primary source-cell OCR did not finish'):
                raise
            api.diagnostic_event('PRIMARY_BODY_ROW_ANCHOR_UNAVAILABLE', page=page,
                                 ref=ref, row=row, reason=str(error))
            return None
        if (not reading or row_identifier(reading[0]) != row_identifier(mapped[k]['html'])
                or reading[1].get('method') != 'primary_surya_on_exact_source_cell'
                or reading[1].get('source_bbox') != body[k]['bbox']):
            return None
        # Casing can differ between the whole-table and source-cell OCR
        # (small capitals in row marks). It locates the row only: every
        # published body value still comes unchanged from the primary table.
        anchors.append(dict(position=list(k), proof=reading[1],
                            comparison='unique_case_insensitive_plain_row_identifier'))
        anchored.add(row)
    if anchored != set(range(header, rows)):
        return None
    geometry = deepcopy(original)
    fresh = {key(cell): cell for cell in geometry['cells']}
    before_header = [nodes[key(cell)].decode_contents() for cell in target['cells'] if cell['row'] < header]
    proof = dict(method='complete_primary_body_on_exact_grid_with_independent_row_anchors',
        page=page, ref=ref, source_record_sha256=identity, source_bbox=list(record['bbox']),
        physical_table_bbox=whole, header_rows=header, body_rows=rows-header, body_columns=columns,
        body_cells=len(body), row_anchors=anchors, primary_body_payloads_preserved=True,
        hidden_pdf_text_used_for_content=False, hidden_pdf_text_used_for_row_assignment=False)
    for k in body:
        content = mapped[k]['html']
        node = nodes[k]
        node.clear(); node.append(BeautifulSoup(content, 'html.parser'))
        fresh[k].update(html=content, text=node.get_text(' ', strip=True),
                        primary_ocr=dict(proof, source_cell_bbox=list(fresh[k]['bbox'])),
                        primary_body_graft=dict(source_record_sha256=identity,
                            rendered_payload_sha256=_typed_payload_digest(content)))
        for name in ('pending_primary_html', 'typed_content_recovery_required', 'primary_content_unmapped'):
            fresh[k].pop(name, None)
    result = str(parsed.table)
    checked = api.table_profile(result)
    if (checked.get('issues') or {key(c) for c in checked['cells']} != set(physical)
            or before_header != [nodes[key(cell)].decode_contents()
                                 for cell in target['cells'] if cell['row'] < header]
            or [c['html'] for c in checked['cells'] if c['row'] >= header]
               != [mapped[key(c)]['html'] for c in checked['cells'] if c['row'] >= header]):
        return None
    geometry['retained_cell_images'] = [entry for entry in geometry.get('retained_cell_images', [])
                                       if (entry.get('row'), entry.get('column')) not in {k[:2] for k in body}]
    geometry['primary_body_graft'] = proof
    manifest.setdefault('complete_primary_body_grafts', []).append(proof)
    api.diagnostic_event('PRIMARY_TABLE_COMPLETE_BODY_GRAFTED', **proof)
    return result, geometry
