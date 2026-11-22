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


def _partition_text_lines(box, fragments, image, reader, api, *, allow_wrap=False):
    """Use a concordant hidden text layer solely to locate existing OCR output."""
    import numpy as np
    from docling_pdf_text_cells import positioned_text_proof

    left, top, right, bottom = box
    glyphs = [g for g in reader.chars if left < (g['x0'] + g['x1']) / 2 < right
              and top < (g['top'] + g['bottom']) / 2 < bottom]
    lines = [line for line in api.native_cell_lines(glyphs) if line['text'].strip()]
    text = BeautifulSoup('<br/>'.join(fragments), 'html.parser')
    if text.find(['img', 'math', 'svg', 'table']):
        return None
    keys = [api._borderless_key(BeautifulSoup(part, 'html.parser').get_text(' ', strip=True))
            for part in fragments]
    source_keys = [api._borderless_key(line['text']) for line in lines]
    aligned = ''.join(source_keys) == ''.join(keys) if allow_wrap else source_keys == keys
    if not aligned:
        return None
    with image.convert('L') as gray:
        mask = (np.asarray(gray) < 180).astype('uint8') * 255
    proof = positioned_text_proof(str(text), glyphs, mask, box, box,
                                  image.width / (right-left), image.height / (bottom-top))
    return lines if proof else None


def _partition_graphic_bands(image, interior, threshold):
    import numpy as np

    left, top, right, bottom = interior
    with image.convert('L') as gray:
        ink = np.asarray(gray)[top:bottom, left:right] < threshold
    occupied = np.count_nonzero(ink, axis=1) >= 2
    edges = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
    bands = []
    for start, end in zip(edges[::2], edges[1::2]):
        columns = np.flatnonzero(ink[start:end].any(axis=0))
        bands.append((int(columns[0]) + left, int(start) + top,
                      int(columns[-1]) + 1 + left, int(end) + top))
    return bands


def locate_partitioned_text_rows(cells, fragments, partitions, originals, page, reader, regions, api):
    """Locate complete primary records within a ruled band with wrapped descriptions."""
    if reader is None or regions is None:
        return None
    digests = {cell.get('primary_ocr', {}).get('source_record_sha256') for cell in cells}
    if len(digests) != 1:
        return None
    digest = digests.pop()
    primary = originals.get((page, digest))
    if primary is None:
        return None
    mappings = {tuple(p['row_mapping']) for p in partitions
                if p['page'] == page and p['source_record_sha256'] == digest}
    if len(mappings) != 1:
        return None
    mapping = mappings.pop()
    table = BeautifulSoup(primary, 'html.parser').table
    if table is None or table.find('table'):
        return None
    rows = table.find_all('tr')
    if len(rows) != len(mapping):
        return None
    owned = [row.find_all('td', recursive=False) for row, owner in zip(rows, mapping)
             if owner == cells[0]['row']]
    if len(owned) < 2 or any(len(row) != len(cells) for row in owned):
        return None
    text_key = lambda part: api._borderless_key(BeautifulSoup(part, 'html.parser').get_text(' ', strip=True))
    anchors, payloads, verified = [], [], []
    for col, (cell, current) in enumerate(zip(cells, fragments)):
        nodes = [row[col] for row in owned]
        if any(str(node.get('rowspan', 1)) != '1' or str(node.get('colspan', 1)) != '1'
               or node.find(['img', 'math', 'svg', 'sub', 'sup', 'table']) for node in nodes):
            return None
        groups = [api._borderless_html_lines(node.decode_contents()) for node in nodes]
        if not all(groups) or [text_key(part) for group in groups for part in group] != list(map(text_key, current)):
            return None
        box = cell['bbox']
        with regions.crop(page, box) as image:
            lines = _partition_text_lines(box, current, image, reader, api)
        if lines:
            verified.append(col)
        else:
            # The other columns must independently confirm every row. A small
            # transcription difference in a positioned line cannot change its owner.
            glyphs = [g for g in reader.chars if box[0] < (g['x0']+g['x1'])/2 < box[2]
                      and box[1] < (g['top']+g['bottom'])/2 < box[3]]
            lines = [line for line in api.native_cell_lines(glyphs) if line['text'].strip()]
            if len(lines) != len(current):
                return None
            for line, part in zip(lines, current):
                source, value = api._borderless_key(line['text']), text_key(part)
                if source != value and (len(value) < 5 or len(value) != len(source)
                        or sum(a != b for a, b in zip(source, value)) != 1):
                    return None
        column, content, start = [], [], 0
        for group in groups:
            end = start + len(group)
            markup = '<br/>'.join(current[start:end])
            column.append(dict(lines[start], last_y=lines[end-1]['y'],
                               text=BeautifulSoup(markup, 'html.parser').get_text(' ', strip=True)))
            content.append(markup)
            start = end
        anchors.append(column)
        payloads.append(content)
    if len(verified) < 2:
        return None
    for row, starts in enumerate(zip(*anchors)):
        size = min(line['size'] for line in starts)
        if max(line['y'] for line in starts) - min(line['y'] for line in starts) > .4 * size:
            return None
        if row and max(column[row-1]['last_y'] for column in anchors) + .4 * size >= min(line['y'] for line in starts):
            return None
    proof = dict(method='primary_row_groups_on_positioned_lines', source_record_sha256=digest,
                 ink_verified_columns=verified)
    return anchors, [proof], payloads


def _reconstruct_partitioned_band(primary, nodes, source, graphic_col, page, reader, regions, api):
    """Align every textual fragment and each independently visible graphic."""
    import base64
    from hashlib import sha256
    from docling_pdf_table_content import TableContentError, _require_ink_coverage
    from docling_pdf_typed_lines import measured_cell_interior

    rows, anchor_columns, assets = {}, [], []
    for col, node in enumerate(nodes):
        if col == graphic_col:
            continue
        groups = [api._borderless_html_lines(row[col].decode_contents()) for row in primary]
        if not all(groups):
            return None
        current = api._borderless_html_lines(node.decode_contents())
        text_key = lambda part: api._borderless_key(
            BeautifulSoup(part, 'html.parser').get_text(' ', strip=True))
        if [text_key(part) for part in current] != [text_key(part) for group in groups for part in group]:
            return None
        with regions.crop(page, source[col]['bbox']) as image:
            lines = _partition_text_lines(source[col]['bbox'], current, image, reader, api)
        if lines is None:
            return None
        anchors, payloads = [], []
        start = 0
        for group in groups:
            end = start + len(group)
            anchors.append(lines[end-1])
            payloads.append('<br/>'.join(current[start:end]))
            start = end
        anchor_columns.append(anchors)
        rows[col] = payloads

    aligned = list(zip(*anchor_columns))
    if any(max(line['y'] for line in group) - min(line['y'] for line in group)
           > min(line['size'] for line in group) * .40 for group in aligned):
        return None
    centers = [median(line['y'] - line['size']/2 for line in group) for group in aligned]
    box = source[graphic_col]['bbox']
    rows[graphic_col] = []
    with regions.crop(page, box) as image:
        measured = measured_cell_interior(image)
        if measured is None:
            return None
        interior = measured[0]
        bands = _partition_graphic_bands(image, interior, 180)
        if len(bands) != len(primary):
            return None
        for threshold in (160, 200):
            alternative = _partition_graphic_bands(image, interior, threshold)
            if len(alternative) != len(bands) or any(
                    max(abs(a-b) for a, b in zip(first, second)) > 4
                    for first, second in zip(bands, alternative)):
                return None
        normalized = [[1000 * left / image.width, 1000 * top / image.height,
                       1000 * right / image.width, 1000 * bottom / image.height]
                      for left, top, right, bottom in bands]
        try:
            _require_ink_coverage(image, normalized, physical_cell=True)
        except TableContentError:
            return None
        sx = image.width / (box[2]-box[0])
        sy = image.height / (box[3]-box[1])
        for index, (row, band) in enumerate(zip(primary, bands)):
            left, top, right, bottom = band
            if abs((top+bottom)/(2*sy) + box[1] - centers[index]) > min(line['size'] for line in aligned[index]):
                return None
            cut = (max(interior[0], left-2), max(interior[1], top-2),
                   min(interior[2], right+2), min(interior[3], bottom+2))
            crop_box = [box[0]+cut[0]/sx, box[1]+cut[1]/sy, box[0]+cut[2]/sx, box[1]+cut[3]/sy]
            payload = row[graphic_col].decode_contents()
            with image.crop(cut) as crop:
                # A citation can remain editable when its own cropped pixels
                # verify it; an OCR dash or rail code does not replace a diagram.
                if re.search(r'[A-Za-zА-Яа-яЁё]{2,}\s+[A-Za-zА-Яа-яЁё]{2,}',
                             row[graphic_col].get_text(' ', strip=True)) and _partition_text_lines(
                                 crop_box, api._borderless_html_lines(payload), crop, reader, api):
                    rows[graphic_col].append(payload)
                    continue
                if right-left < .4 * (interior[2]-interior[0]):
                    return None
                img = BeautifulSoup(api.image_html(crop, crop_box[2]-crop_box[0], 'cell'), 'html.parser').img
            img['data-table-content'] = 'picture'
            img['data-source-cell'] = 'true'
            rows[graphic_col].append(str(img))
            assets.append(dict(page=page, cell=dict(row=source[graphic_col]['row']+index, column=graphic_col,
                row_span=1, column_span=1), label='Diagram', source_bbox=crop_box,
                source_pixel_bbox=list(cut), source_sha256=sha256(img['src'].encode()).hexdigest(),
                source_image_sha256=sha256(base64.b64decode(img['src'].split(',', 1)[1])).hexdigest()))
    return [list(parts) for parts in zip(*(rows[col] for col in range(len(nodes))))], assets, centers


def _complete_primary_partition(table, profile, geometry, row, graphic_col, digest, page, reader, regions, api):
    """Verify every cell before bypassing cell OCR."""
    keys = ('row', 'column', 'row_span', 'column_span')
    physical = {tuple(cell[key] for key in keys): cell for cell in geometry['cells']}
    if set(physical) != {tuple(c[key] for key in keys) for c in profile['cells']}:
        return False
    nodes = [node for node in table.find_all(['td', 'th']) if node.find_parent('table') is table]
    for cell, node in zip(profile['cells'], nodes, strict=True):
        source = physical[tuple(cell[key] for key in keys)]
        if (cell['row'], cell['column']) == (row, graphic_col):
            if any(image.get('data-source-cell') != 'true' for image in node.find_all('img')):
                return False
            continue
        if (source.get('primary_ocr', {}).get('source_record_sha256') != digest
                or source.get('pending_primary_html') or not cell['text'].strip()
                or node.find(['img', 'svg', 'math', 'sub', 'sup', 'table'])):
            return False
        if cell['row'] != row:
            with regions.crop(page, source['bbox']) as image:
                fragments = api._borderless_html_lines(node.decode_contents())
                if not _partition_text_lines(source['bbox'], fragments, image, reader, api, allow_wrap=True):
                    return False
    return True


def expand_partitioned_graphic_rows(doc, routes, manifest, regions, reader, records, api, *, preview=None):
    """Restore one partitioned scan band when its original records and graphics agree."""
    from hashlib import sha256
    from docling_core.types.doc import TableCell, TableData

    partitions = manifest.get('primary_source_grid_partitions', [])
    if not partitions or reader is None or regions is None:
        return
    entries = ({preview['ref']: preview} if preview is not None
               else {entry['ref']: entry for entry in manifest['tables']})
    digests = {proof['source_record_sha256'] for proof in partitions}
    originals = {}
    for record in records:
        if '<table' not in record.get('html', '').lower():
            continue
        digest = sha256(record['html'].encode()).hexdigest()
        if digest in digests:
            originals[(record['page_no'], digest)] = record['html']
    for item in doc.tables:
        entry = entries.get(item.self_ref, {})
        geometry = entry.get('pdf_geometry') or {}
        if (geometry.get('basis') != 'raster_cell_rules' or not geometry.get('source_grid_verified')
                or geometry.get('hidden_ocr_direction_conflict')):
            continue
        page = entry['page']
        fragment = entry['html'] if preview is not None else routes.get(item.self_ref, '')
        table = BeautifulSoup(fragment, 'html.parser').table
        if table is None:
            continue
        profile = api.table_profile(fragment)
        if profile['issues'] or profile['columns'] < 4:
            continue
        columns = {c['column'] for c in profile['cells'] if c['header'] and c['column_span'] == 1
                   and c['text'].casefold() == 'обозначение и изображение'}
        if len(columns) != 1:
            continue
        graphic_col = columns.pop()
        options = {(proof['source_record_sha256'], tuple(proof['row_mapping'])) for proof in partitions
                   if proof['page'] == page and proof['source_rows'] == profile['rows']
                   and api.overlap_ratio(proof['source_bbox'], geometry['bbox']) > .9}
        if len(options) != 1:
            continue
        digest, mapping = options.pop()
        repeated = [row for row, count in Counter(mapping).items() if count > 1]
        if len(repeated) != 1 or (page, digest) not in originals:
            continue
        row_no = repeated[0]
        physical = sorted((cell for cell in geometry['cells'] if cell['row'] == row_no),
                          key=lambda cell: cell['column'])
        if len(physical) != profile['columns'] or any(
                cell['column'] != i or cell['row_span'] != 1 or cell['column_span'] != 1
                or not cell.get('bbox') for i, cell in enumerate(physical)):
            continue
        clean = api.clean_table_html(str(BeautifulSoup(originals[(page, digest)], 'html.parser').table))
        source_table = BeautifulSoup(clean, 'html.parser').table
        if source_table is None:
            continue
        primary_rows = [r for r in source_table.find_all('tr') if r.find_parent('table') is source_table]
        if len(primary_rows) != len(mapping):
            continue
        primary = [row.find_all('td', recursive=False) for i, row in enumerate(primary_rows) if mapping[i] == row_no]
        original = [row for row in table.find_all('tr') if row.find_parent('table') is table][row_no]
        nodes = original.find_all('td', recursive=False)
        if len(nodes) != len(physical):
            continue
        graphic = BeautifulSoup(nodes[graphic_col].decode_contents(), 'html.parser')
        for notice in graphic.select('.table-cell-review'):
            notice.decompose()
        if graphic.get_text(strip=True) or graphic.find('math'):
            continue
        targeted = [*primary, nodes]
        if any(len(row) != len(physical) or any(int(c.get('rowspan', 1)) != 1
                or int(c.get('colspan', 1)) != 1 for c in row) for row in targeted):
            continue
        reader.load_page(page)
        if preview is not None:
            rich_content = 'math, sub, sup, code, pre, table, input, textarea, select, [data-latex], [data-formula], .source-latex'
            if any(row[graphic_col].select_one(rich_content) for row in primary):
                continue
            if not _complete_primary_partition(
                    table, profile, geometry, row_no, graphic_col, digest, page, reader, regions, api):
                continue
        reconstructed = _reconstruct_partitioned_band(primary, nodes, physical,
            graphic_col, page, reader, regions, api)
        if reconstructed is None:
            continue
        payloads, assets, centers = reconstructed
        markup = BeautifulSoup('', 'html.parser')
        for index, parts in enumerate(payloads):
            row = markup.new_tag('tr', attrs=dict(original.attrs))
            if index:
                row.attrs.pop('id', None)
            row['data-pdf-page'] = str(page)
            row['data-source-row'] = str(row_no+1)
            for node, part in zip(nodes, parts, strict=True):
                cell = markup.new_tag('td', attrs=dict(node.attrs))
                if index:
                    cell.attrs.pop('id', None)
                cell.append(BeautifulSoup(part, 'html.parser'))
                row.append(cell)
            original.insert_before(row)
        original.decompose()
        table['data-source-logical-rows'] = 'true'
        rendered = str(table)
        checked = api.table_profile(rendered)
        if checked['issues'] or checked['rows'] != profile['rows'] + len(payloads) - 1:
            continue
        proof = dict(page=page, ref=item.self_ref, source_row=row_no, logical_rows=len(payloads),
            graphic_column=graphic_col, source_record_sha256=digest, anchors=centers,
            method='aligned_primary_table_records_and_independent_source_graphic_bands')
        if preview is not None:
            return rendered, proof, assets
        routes[item.self_ref] = rendered
        item.data = TableData(num_rows=checked['rows'], num_cols=checked['columns'], table_cells=[
            TableCell(text=c['text'], row_span=c['row_span'], col_span=c['column_span'],
                      start_row_offset_idx=c['row'], end_row_offset_idx=c['row']+c['row_span'],
                      start_col_offset_idx=c['column'], end_col_offset_idx=c['column']+c['column_span'],
                      column_header=c['header']) for c in checked['cells']])
        entry.update(html=rendered, selected=checked)
        manifest.setdefault('typed_table_picture_assets', []).extend(assets)
        manifest['table_assembly_reviews'] = [warning for warning in manifest.get('table_assembly_reviews', [])
            if (warning.get('page'), warning.get('ref'), warning.get('row'), warning.get('column'))
            != (page, item.self_ref, row_no, graphic_col)]
        manifest.setdefault('partitioned_graphic_row_repairs', []).append(proof)
        api.diagnostic_event('TABLE_GRAPHIC_ROWS_RECONSTRUCTED', **proof)

