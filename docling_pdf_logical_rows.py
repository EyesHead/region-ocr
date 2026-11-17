"""Keep primary logical rows when several records share one ruled source band.

Printed rails and logical records are separate evidence. Every primary word
must match complete positioned source lines, and those lines must account for
the source pixels. Positioned PDF text is never used as the transcription.
"""
from copy import deepcopy
from hashlib import sha256
import math
import re

from bs4 import BeautifulSoup


POSITION = ('row', 'column', 'row_span', 'column_span')


def _key(cell):
    return tuple(cell[name] for name in POSITION)


def _same_region(a, b):
    if (not a or len(a) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in a)):
        return False
    area = lambda box: max(0., box[2]-box[0])*max(0., box[3]-box[1])
    intersection = area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])
    return min(area(a), area(b)) > 0 and all(intersection/area(box) >= .95 for box in (a, b))


def restore_primary_logical_rows(native, records, page, manifest, api, *, reader,
                                 source_image, region, ref=None):
    """Return an unchanged primary table on a source-proved logical row grid.

    This path handles plain text only. Spanning source columns are retained,
    but ambiguous rowspans, illustrations, conflicting packets, partial source
    lines and shifted descriptor/value pairs decline the recovery.
    """
    if not native or reader is None or source_image is None:
        return None
    fragment, original = native
    if (not original.get('source_grid_verified') or original.get('hidden_ocr_direction_conflict')
            or original.get('primary_logical_rows_proof')):
        return None
    target = api.table_profile(fragment)
    physical = original.get('cells', [])
    xs, ys = original.get('x_grid', []), original.get('y_grid', [])
    if (target.get('issues') or target.get('columns', 0) < 2
            or len(xs) != target['columns']+1 or len(ys) != target['rows']+1
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in xs+ys)
            or any(b <= a for axis in (xs, ys) for a, b in zip(axis, axis[1:]))
            or any(cell.get('row_span') != 1 for cell in physical)
            or len({_key(cell) for cell in physical}) != len(physical)
            or {_key(cell) for cell in physical} != {_key(cell) for cell in target['cells']}):
        return None
    for cell in physical:
        row, col, _, span = _key(cell)
        expected = [xs[col], ys[row], xs[col+span], ys[row+1]]
        if len(cell.get('bbox', [])) != 4 or any(abs(a-b) > 1 for a, b in zip(cell['bbox'], expected)):
            return None

    reader.load_page(page)
    sources = {}
    for cell in physical:
        box = cell['bbox']
        glyphs = [g for g in reader.chars if g.get('text', '').strip()
                  and box[0] <= (g['x0']+g['x1'])/2 < box[2]
                  and box[1] <= (g['top']+g['bottom'])/2 < box[3]]
        lines = api.native_cell_lines(glyphs)
        if any(not math.isfinite(line['y']) or line['size'] <= 0 for line in lines):
            return None
        envelopes = []
        for line in lines:
            group = [g for g in glyphs if abs(g['bottom']-line['y']) <= .5*max(g['size'], line['size'])]
            if not group:
                return None
            envelopes.append([min(g['top'] for g in group), max(g['bottom'] for g in group)])
        sources[_key(cell)] = dict(cell=cell, glyphs=glyphs, lines=lines, envelopes=envelopes,
            text=''.join(api.cell_text_key(line['text']) for line in lines))
    whole = [xs[0], ys[0], xs[-1], ys[-1]]
    candidates = []
    for record in records or ():
        if (record.get('label') != 'Table' or record.get('page_no') != page
                or not _same_region(record.get('bbox'), whole)):
            continue
        parsed = BeautifulSoup(record.get('html', ''), 'html.parser')
        if (len(parsed.find_all('table')) != 1
                or parsed.find(['img', 'svg', 'math', 'sub', 'sup', 'script', 'style', 'iframe', 'object'])):
            continue
        primary = api.table_profile(str(parsed.table))
        if (primary.get('issues') or primary['columns'] != target['columns']
                or primary['rows'] <= target['rows'] or any(c['row_span'] != 1 for c in primary['cells'])):
            continue
        assignment = []
        for row in range(primary['rows']):
            possible = None
            for cell in (c for c in primary['cells'] if c['row'] == row and c['text'].strip()):
                text = api.cell_text_key(cell['text'])
                matches = {key[0] for key, source in sources.items()
                           if key[1:] == (cell['column'], 1, cell['column_span']) and text in source['text']}
                possible = matches if possible is None else possible & matches
            if not possible or len(possible) != 1:
                break
            assignment.append(next(iter(possible)))
        else:
            if (assignment != sorted(assignment) or set(assignment) != set(range(target['rows']))):
                continue
            # Several wrapped lines in one paragraph are not independently
            # established records. A split band needs repeated row pairs in
            # different columns; group labels and notes may accompany them.
            split_bands = [row for row in range(target['rows']) if assignment.count(row) > 1]
            if any(sum(sum(bool(c['text'].strip()) for c in primary['cells'] if c['row'] == logical) >= 2
                       for logical, owner in enumerate(assignment) if owner == row) < 2
                   for row in split_bands):
                continue
            matched = {}
            for key, source in sources.items():
                cells = [c for c in primary['cells'] if assignment[c['row']] == key[0]
                         and (c['column'], c['column_span']) == (key[1], key[3])]
                if ''.join(api.cell_text_key(c['text']) for c in cells) != source['text']:
                    break
                cursor = 0
                for cell in cells:
                    wanted = api.cell_text_key(cell['text'])
                    text, indices = '', []
                    while len(text) < len(wanted) and cursor < len(source['lines']):
                        text += api.cell_text_key(source['lines'][cursor]['text'])
                        indices.append(cursor)
                        cursor += 1
                    if text != wanted:
                        break
                    matched[_key(cell)] = dict(physical=key, lines=indices,
                        envelopes=[source['envelopes'][i] for i in indices])
                else:
                    if cursor == len(source['lines']):
                        continue
                break
            else:
                if len(matched) != len(primary['cells']):
                    continue
                row_bounds = []
                for row in range(primary['rows']):
                    entries = [matched[_key(c)] for c in primary['cells'] if c['row'] == row]
                    envelopes = [box for entry in entries for box in entry['envelopes']]
                    if not envelopes or not any(len(entry['lines']) == 1 for entry in entries):
                        # A whole physical note can legitimately wrap; a
                        # many-line body block is not an atomic logical row.
                        count = assignment.count(assignment[row])
                        if not envelopes or count != 1:
                            break
                    row_bounds.append([min(box[0] for box in envelopes), max(box[1] for box in envelopes)])
                else:
                    logical_y = [ys[0]]
                    for source_row in range(target['rows']):
                        rows = [row for row, owner in enumerate(assignment) if owner == source_row]
                        if (min(row_bounds[row][0] for row in rows) < ys[source_row]
                                or max(row_bounds[row][1] for row in rows) > ys[source_row+1]):
                            break
                        for left, right in zip(rows, rows[1:]):
                            if row_bounds[left][1] >= row_bounds[right][0]:
                                break
                            logical_y.append((row_bounds[left][1]+row_bounds[right][0])/2)
                        else:
                            logical_y.append(ys[source_row+1])
                            continue
                        break
                    else:
                        if len(logical_y) == primary['rows']+1:
                            candidates.append((record, str(parsed.table), primary, assignment, matched, logical_y))
    if len(candidates) != 1:
        return None
    record, html, primary, assignment, matched, logical_y = candidates[0]
    import cv2
    import numpy as np
    from docling_pdf_text_cells import positioned_text_proof

    with source_image.convert('L') as gray:
        mask = cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    sx, sy = source_image.width/(region[2]-region[0]), source_image.height/(region[3]-region[1])
    coverage = []
    for key, source in sources.items():
        payload = ''.join(c['html'] for c in primary['cells'] if matched[_key(c)]['physical'] == key)
        proof = positioned_text_proof(payload, source['glyphs'], mask, region, source['cell']['bbox'], sx, sy)
        if proof is None:
            return None
        coverage.append(dict(position=list(key), **proof))
    identity = sha256(record['html'].encode()).hexdigest()
    text_positions = []
    for cell in primary['cells']:
        match = matched[_key(cell)]
        lines = [sources[match['physical']]['lines'][i] for i in match['lines']]
        if lines:
            text_positions.append(dict(row=cell['row'], column=cell['column'],
                bbox=[min(line['x0'] for line in lines), min(b[0] for b in match['envelopes']),
                      max(line['x1'] for line in lines), max(b[1] for b in match['envelopes'])],
                font=min(line['size'] for line in lines)))
    proof = dict(method='complete_primary_logical_rows_on_positioned_source_lines', page=page, ref=ref,
        source_record_sha256=identity, source_bbox=list(record['bbox']),
        physical_rows=target['rows'], logical_rows=primary['rows'], columns=primary['columns'],
        row_mapping=assignment, physical_y_grid=list(ys), logical_y_grid=logical_y,
        physical_cell_coverage=coverage, cell_text_positions=text_positions, primary_payloads_preserved=True,
        hidden_pdf_text_used_for_content=False, physical_rails_preserved_separately=True)
    geometry = deepcopy(original)
    geometry.update(basis='primary_rows_on_verified_source_baselines', y_grid=logical_y,
        physical_y_grid=list(ys), physical_cells=[{k: c[k] for k in (*POSITION, 'bbox')} for c in physical],
        primary_logical_rows_proof=proof, retained_cell_images=[], pending_cell_ocr=[], logical_subrows=[])
    geometry.pop('source_physical_html', None)
    geometry['header_rows'] = max((c['row']+1 for c in primary['cells'] if c['header']), default=0)
    geometry['cells'] = [dict(c, bbox=[xs[c['column']], logical_y[c['row']],
        xs[c['column']+c['column_span']], logical_y[c['row']+1]], source_text_unreliable=True,
        scan_layout_has_ink=bool(c['text'].strip()), primary_ocr=dict(method=proof['method'],
        source_record_sha256=identity, primary_payload_preserved=True)) for c in primary['cells']]
    manifest.setdefault('primary_logical_row_recoveries', []).append(proof)
    api.diagnostic_event('PRIMARY_LOGICAL_ROWS_RESTORED', **proof)
    return html, geometry


def style_primary_logical_rows(soup, manifest, api):
    """Keep source rails while displaying independently proved logical rows.

    Run after HTML serialization and sanitization. Markdown must retain this
    table as HTML, since a pipe table cannot express absent interior rules.
    """
    styled = []
    for entry in manifest.get('tables', []):
        proof = (entry.get('pdf_geometry') or {}).get('primary_logical_rows_proof')
        if not proof or entry.get('joined_into'):
            continue
        table = soup.find('table', attrs={'data-pdf-table-ref': entry['ref']})
        if table is None:
            continue
        profile = api.table_profile(str(table))
        mapping = proof['row_mapping']
        if profile['issues'] or profile['rows'] != len(mapping) or profile['columns'] != proof['columns']:
            continue
        nodes = [n for n in table.find_all(['td', 'th']) if n.find_parent('table') is table]
        positions = {(p['row'], p['column']): p for p in proof.get('cell_text_positions', [])}
        geometry = entry.get('pdf_geometry') or {}
        xs = geometry.get('x_grid', [])
        for node, cell in zip(nodes, profile['cells']):
            first, stop = cell['row'], cell['row']+cell['row_span']
            hidden = []
            if first and mapping[first] == mapping[first-1]:
                hidden.append('top')
            if stop < len(mapping) and mapping[stop-1] == mapping[stop]:
                hidden.append('bottom')
            style = [part for part in node.get('style', '').split(';') if part.strip()
                     and not any(re.match(r'\s*border-'+side+r'\s*:', part, re.I) for side in hidden)]
            style.extend('border-'+side+':0' for side in hidden)
            position = positions.get((cell['row'], cell['column']))
            row_positions = [p['bbox'] for p in positions.values() if p['row'] == cell['row']]
            if position and row_positions and xs:
                bounds = position['bbox']; font = position['font']
                row_center = (min(p[1] for p in row_positions)+max(p[3] for p in row_positions))/2
                if abs((bounds[1]+bounds[3])/2-row_center) <= font*.25:
                    style.append('vertical-align:middle')
                left_gap = bounds[0]-xs[cell['column']]
                right_gap = xs[cell['column']+cell['column_span']]-bounds[2]
                if min(left_gap, right_gap) >= font*.5 and abs(left_gap-right_gap) <= font*.5:
                    style.append('text-align:center')
            if style:
                node['style'] = ';'.join(style)
        table['data-source-logical-rows'] = 'true'
        styled.append(dict(ref=entry['ref'], physical_rows=proof['physical_rows'],
                           logical_rows=proof['logical_rows'], source_horizontal_rules_preserved=True))
    manifest['primary_logical_row_styles'] = styled
    return styled
