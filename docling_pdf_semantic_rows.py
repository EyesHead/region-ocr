"""Display source-proven records inside a physically merged table band.

Physical bboxes and cell ownership remain unchanged. Independent positioned
value baselines, rather than HTML line breaks, prove the logical records.
"""
from collections import Counter
from statistics import median
import re

from bs4 import BeautifulSoup

POSITION = ('row', 'column', 'row_span', 'column_span')


def _source_lines(cell, api):
    glyphs = []
    for glyph in cell.get('glyphs', []):
        box = glyph.get('bbox')
        if not box or len(box) != 4:
            return []
        glyphs.append(dict(glyph, x0=box[0], top=box[1], x1=box[2], bottom=box[3]))
    return [line for line in api.native_cell_lines(glyphs) if line['text'].strip()] if glyphs else []


def _value(line):
    text = re.sub(r'\s*(?:<\*+>|\*+|<\d+>|\d+\))\s*$', '', line['text']).strip()
    return bool(re.fullmatch(r'[-−–—]+|[IVXLCDM]+(?:\s*,\s*[IVXLCDM]+)*|'
        r'[A-Za-zА-Яа-яЁё]\s*\d+(?:\s*,\s*[A-Za-zА-Яа-яЁё]\s*\d+)*|'
        r'[\d\s.,+−–—/\-%°()<>≤≥]+(?:\s*[A-Za-zА-Яа-яЁё]{1,4})?', text))


def _plan(cells, api):
    lines = {tuple(c[k] for k in POSITION): _source_lines(c, api) for c in cells}
    # A shared categorical stub establishes a grouped band, not wrapped prose.
    stubs = [c for c in cells if c['column'] == 0 and c['column_span'] == 1
             and len(lines[tuple(c[k] for k in POSITION)]) == 1
             and re.search(r'[A-Za-zА-Яа-яЁё]', lines[tuple(c[k] for k in POSITION)][0]['text'])]
    if len(stubs) != 1:
        return None
    candidates = []
    for cell in cells:
        content = lines[tuple(cell[k] for k in POSITION)]
        if (cell['column'] > 0 and cell['column_span'] == 1
                and len(content) >= 2 and all(_value(line) for line in content)
                and not cell.get('source_images') and not cell.get('restored_math')):
            candidates.append((cell, content))
    counts = Counter(len(content) for _, content in candidates)
    viable = [count for count, votes in counts.items() if votes >= 2]
    if not viable:
        return None
    count = max(viable)
    supporting = [(cell, content) for cell, content in candidates if len(content) == count]
    size = median(line['size'] for _, content in supporting for line in content)
    anchors = [median(content[index]['y'] for _, content in supporting) for index in range(count)]
    if (any(b - a < size * .8 for a, b in zip(anchors, anchors[1:]))
            or any(abs(line['y'] - anchors[index]) > size * .4
                   for _, content in supporting for index, line in enumerate(content))):
        return None
    if any(min(abs(line['y'] - anchor) for anchor in anchors) > size * .4
           for _, content in candidates for line in content):
        return None  # Incomplete columns may corroborate blank slots, never a competing grid.
    stub_key = tuple(stubs[0][k] for k in POSITION)
    if abs(lines[stub_key][0]['y'] - median(anchors)) > size * .6:
        return None  # A first-record label is not automatically a shared category.
    groups, covered = {}, {}
    for cell in cells:
        key = tuple(cell[k] for k in POSITION)
        content = lines[key]
        if (key == stub_key
                or cell.get('source_images') or cell.get('restored_math')):
            continue  # A shared or already spanning cell keeps its complete payload.
        slots = [[] for _ in anchors]
        indices = [i for i, anchor in enumerate(anchors)
                   if cell['bbox'][1] - size * .2 <= anchor <= cell['bbox'][3] + size * .2]
        if not indices:
            continue
        for line in content:
            distances = sorted((abs(line['y'] - anchors[index]), index) for index in indices)
            if distances[0][0] > size * .5 or len(distances) > 1 and abs(distances[1][0] - distances[0][0]) < size * .2:
                break
            slots[distances[0][1]].append(line)
        else:
            groups[key] = slots
            covered[key] = indices
    if len(groups) < 2:
        return None
    return dict(count=count, anchors=anchors, groups=groups, covered_slots=covered,
                value_columns=[cell['column'] for cell, _ in supporting])


def display_source_record_rows(fragment, geometry, api):
    """Return a logical display grid only after complete DOM/source matching.

Call after physical ownership and auditing, before building final TableData.
The original physical geometry is evidence and is never rewritten here.
"""
    if not geometry or geometry.get('hidden_ocr_direction_conflict'):
        return fragment, []
    soup = BeautifulSoup(fragment, 'html.parser')
    if soup.table is None:
        return fragment, []
    profile = api.table_profile(str(soup.table))
    physical = {tuple(c[k] for k in POSITION): c for c in geometry.get('cells', [])}
    if profile['issues'] or {tuple(c[k] for k in POSITION) for c in profile['cells']} != set(physical):
        return fragment, []
    from docling_pdf_primary_partitions import _text, _clip

    nodes = [n for n in soup.table.find_all(['td', 'th']) if n.find_parent('table') is soup.table]
    node_map = {tuple(c[k] for k in POSITION): n for c, n in zip(profile['cells'], nodes)}
    plans, payloads = {}, {}
    row_counts = [1] * profile['rows']
    reserved = set()
    for row in range(geometry.get('header_rows', 0), profile['rows']):
        if row in reserved:
            continue
        roots = [c for c in physical.values() if c['row'] == row and c['column'] == 0 and c['column_span'] == 1]
        end = row + roots[0]['row_span'] if len(roots) == 1 else row + 1
        cells = sorted((c for c in physical.values() if row <= c['row'] and c['row'] + c['row_span'] <= end),
                       key=lambda c: (c['row'], c['column']))
        plan = _plan(cells, api)
        if plan is None:
            continue
        ys = geometry.get('y_grid', [])
        if len(ys) != profile['rows'] + 1:
            continue
        counts = [sum(ys[index] <= anchor <= ys[index + 1] for anchor in plan['anchors'])
                  for index in range(row, end)]
        if sum(counts) != plan['count']:
            continue
        anchor_rows = []
        for index in range(row, end):
            anchor_rows.extend((index, local) for local in range(counts[index - row]))
        counts = [max(1, count) for count in counts]  # Retain an untouched physical row with no proven record baselines.
        clipped = {}
        for key, slots in plan['groups'].items():
            node = node_map[key]
            if node.find(['img', 'svg', 'math', 'table', 'code', 'pre']):
                break
            source_keys = [_text(BeautifulSoup(''.join(line['html'] for line in slot), 'html.parser'))[0]
                           for slot in slots]
            value, offsets, spans = _text(node)
            if ''.join(source_keys) != value:
                break  # No source-text replacement or unverified BR splitting.
            pieces, cursor = [], 0
            for text in source_keys:
                if text:
                    part = _clip(node, spans, offsets[cursor], offsets[cursor + len(text) - 1] + 1)
                    pieces.append(part.decode_contents())
                    cursor += len(text)
                else:
                    pieces.append('')  # Missing source entries remain genuinely empty.
            clipped[key] = (anchor_rows, {index: pieces[index] for index in plan['covered_slots'][key]})
        else:
            plans[row] = plan
            payloads.update(clipped)
            row_counts[row:end] = counts
            reserved.update(range(row, end))
    if not plans:
        return fragment, []
    offsets = [0]
    for row in range(profile['rows']):
        offsets.append(offsets[-1] + row_counts[row])
    original_rows = [r for r in soup.table.find_all('tr') if r.find_parent('table') is soup.table]
    display_rows = []
    for row, old in enumerate(original_rows):
        count = row_counts[row]
        replacements = [soup.new_tag('tr', attrs=dict(old.attrs)) for _ in range(count)]
        for replacement in replacements[1:]:
            replacement.attrs.pop('id', None)
        for node in replacements:
            old.insert_before(node)
        display_rows.extend(replacements)
    pending = {index: [] for index in range(offsets[-1])}
    for cell in profile['cells']:
        row = cell['row']
        key = tuple(cell[k] for k in POSITION)
        original = node_map[key]
        if key in payloads:
            anchor_rows, pieces = payloads[key]
            for index, content in pieces.items():
                node = soup.new_tag(original.name, attrs=dict(original.attrs))
                if index:
                    node.attrs.pop('id', None)
                node.attrs.pop('rowspan', None)
                node.append(BeautifulSoup(content, 'html.parser'))
                physical_row, local = anchor_rows[index]
                pending[offsets[physical_row] + local].append((cell['column'], node))
        else:
            span = offsets[row + cell['row_span']] - offsets[row]
            if span > 1:
                original['rowspan'] = str(span)
            pending[offsets[row]].append((cell['column'], original.extract()))
    for index, values in pending.items():
        for column, node in sorted(values, key=lambda value: value[0]):
            display_rows[index].append(node)
    for old in original_rows:
        old.decompose()
    result = str(soup)
    final = api.table_profile(result)
    if final['issues'] or final['columns'] != profile['columns'] or final['rows'] != offsets[-1]:
        return fragment, []
    # A slot boundary adds no characters and consumes none of the original DOM.
    before_text = Counter(_text(BeautifulSoup(c['html'], 'html.parser'))[0] for c in profile['cells'])
    before_chars = Counter(''.join(before_text.elements()))
    after_chars = Counter(''.join(_text(BeautifulSoup(c['html'], 'html.parser'))[0] for c in final['cells']))
    if before_chars != after_chars:
        return fragment, []
    proofs = [dict(source_row=row, logical_start_row=offsets[row], records=plan['count'],
                   anchors=plan['anchors'], value_columns=plan['value_columns'],
                   split_columns=[key[1] for key in plan['groups']],
                   method='shared_source_stub_and_independent_positioned_value_baselines',
                   physical_geometry_unchanged=True) for row, plan in sorted(plans.items())]
    return result, proofs


def finalize_source_record_routes(doc, routes, excluded, manifest, api):
    """Run the display stage after every physical route transform has finished."""
    from docling_core.types.doc import TableCell, TableData

    table_items = {item.self_ref: item for item in doc.tables}
    repaired = []
    for entry in manifest.get('tables', []):
        ref = entry['ref']
        if entry.get('joined_into') or ref in excluded or ref not in table_items:
            continue
        content, logical_rows = display_source_record_rows(routes.get(ref, ''), entry.get('pdf_geometry'), api)
        if not logical_rows:
            continue
        routes[ref] = entry['html'] = content
        profile = entry['selected'] = api.table_profile(content)
        table_items[ref].data = TableData(num_rows=profile['rows'], num_cols=profile['columns'], table_cells=[
            TableCell(text=cell['text'], row_span=cell['row_span'], col_span=cell['column_span'],
                start_row_offset_idx=cell['row'], end_row_offset_idx=cell['row'] + cell['row_span'],
                start_col_offset_idx=cell['column'], end_col_offset_idx=cell['column'] + cell['column_span'],
                column_header=cell['header']) for cell in profile['cells']])
        proofs = [dict(ref=ref, page=entry.get('page'), **proof) for proof in logical_rows]
        repaired.extend(proofs)
        api.diagnostic_event('SOURCE_SEMANTIC_RECORD_ROWS_DISPLAYED', ref=ref, repairs=proofs)
    manifest.setdefault('source_semantic_row_repairs', []).extend(repaired)
    return repaired
