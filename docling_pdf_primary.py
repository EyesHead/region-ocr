"""Preserve positioned primary OCR payload while repairing the source grid."""
import hashlib
import re

from bs4 import BeautifulSoup


POSITION = ('row', 'column', 'row_span', 'column_span')


def _key(cell):
    return tuple(cell[k] for k in POSITION)


def _same_region(bounds, region):
    overlap = (max(0, min(bounds[2], region[2])-max(bounds[0], region[0])) *
               max(0, min(bounds[3], region[3])-max(bounds[1], region[1])))
    areas = [(b[2]-b[0])*(b[3]-b[1]) for b in (bounds, region)]
    return all(area > 0 and overlap/area > .6 for area in areas)


def _prefix_matches(primary, target):
    if primary['issues'] or primary['columns'] != target['columns'] or not primary['cells']:
        return False
    end = primary['rows']
    if end > target['rows']:
        return False
    prefix = [c for c in target['cells'] if c['row'] < end]
    if any(c['row'] + c['row_span'] > end for c in prefix):
        return False
    if {_key(c) for c in primary['cells']} != {_key(c) for c in prefix}:
        return False
    # A primary table may omit the source's trailing full-width notes. They
    # retain their own payload and fallback; no interior row may be skipped.
    return all(c['column'] == 0 and c['column_span'] == target['columns'] and c['row_span'] == 1
               for c in target['cells'] if c['row'] >= end)


def _external_note_prefix(fragment, geometry, target, bounds, page, reader, api):
    """Separate a terminal OCR note only when source glyphs prove it is outside the grid."""
    if (reader is None or not bounds or geometry.get('hidden_ocr_direction_conflict')
            or target.get('issues') or not geometry.get('y_grid')):
        return None
    parsed = BeautifulSoup(fragment, 'html.parser')
    primary = api.table_profile(fragment)
    end = target['rows']
    if primary['issues'] or primary['columns'] != target['columns'] or primary['rows'] <= end:
        return None
    prefix = [c for c in primary['cells'] if c['row'] < end]
    tails = [c for c in primary['cells'] if c['row'] >= end]
    if ({_key(c) for c in prefix} != {_key(c) for c in target['cells']}
            or not tails or any(c['column'] != 0 or c['column_span'] != target['columns']
                                or c['row_span'] != 1 for c in tails)):
        return None
    bottom = geometry['y_grid'][-1]
    if bounds[3] <= bottom + 2:
        return None
    note_start = re.compile(r'^\s*(?:примечани[ея]|обозначения|условные\s+обозначения|notes?)\b', re.I)
    if any(not note_start.match(cell['text']) for cell in tails):
        return None
    reader.load_page(page)
    # Any extra source data in the same OCR block prevents this exact match.
    # No source words are discarded and no inferred per-cell bbox is invented.
    glyphs = [g for g in reader.chars if g.get('text', '').strip()
              and bounds[0] - 1 <= (g['x0'] + g['x1']) / 2 <= bounds[2] + 1
              and bottom + 1 < (g['top'] + g['bottom']) / 2 <= bounds[3] + 1]
    if not glyphs or any(g['top'] < bottom - .5 for g in glyphs):
        return None
    def key(value):
        value = BeautifulSoup(value, 'html.parser').get_text()
        return re.sub(r'\s+', '', value).replace('\u00ad', '').translate(str.maketrans('−–—', '---'))
    lines = api.native_cell_lines(glyphs)
    source = ''.join(line['html'] for line in lines)
    tail_html = ''.join(c['html'] for c in tails)
    note_evidence = 'literal_positioned_glyphs'
    if key(source) != key(tail_html):
        from docling_pdf_table_content import _typed_payload_key
        if _typed_payload_key(source) == _typed_payload_key(tail_html):
            # This comparison establishes ownership only. The native note keeps
            # its original alphabet in the independently published paragraph.
            note_evidence = 'equivalent_printed_glyphs'
        else:
            from docling_pdf_repairs import _primary_note_footer
            note_box = [bounds[0], bottom, bounds[2], bounds[3]]
            record = dict(page_no=page, bbox=bounds, html=fragment)
            proof = _primary_note_footer([record], page, note_box,
                ' '.join(line['text'] for line in lines), glyphs, lines,
                reader.page.images, api)
            if not proof or not proof.get('source_math_images'):
                return None
            note_evidence = 'positioned_inline_math_images'
    nodes = [n for n in parsed.table.find_all(['td', 'th']) if n.find_parent('table') is parsed.table]
    for cell, node in zip(primary['cells'], nodes):
        if cell['row'] >= end:
            node.decompose()
    for row in list(parsed.table.find_all('tr')):
        if not row.find(['td', 'th']):
            row.decompose()
    profile = api.table_profile(str(parsed.table))
    if not _prefix_matches(profile, target):
        return None
    profile['source_external_note_prefix'] = dict(
        method='exact_primary_grid_prefix_and_complete_positioned_external_note',
        page=page, source_bbox=list(bounds), physical_table_bottom=bottom, note_evidence=note_evidence,
        source_note_bbox=[min(g['x0'] for g in glyphs), min(g['top'] for g in glyphs),
                          max(g['x1'] for g in glyphs), max(g['bottom'] for g in glyphs)],
        source_note_glyphs=len(glyphs), primary_tail_rows=len(tails),
        note_html_sha256=hashlib.sha256(''.join(c['html'] for c in tails).encode()).hexdigest())
    return profile


def _fill_positioned_math_images(source_html, primary_html):
    """Replace only source image slots corroborated by the complete surrounding text."""
    from html import escape
    from docling_pdf_table_content import _typed_payload_key

    def text_key(text):
        return ''.join(value for _, value in _typed_payload_key(escape(text)))

    if '<img' not in source_html:
        return None
    source = BeautifulSoup(source_html, 'html.parser')
    primary = BeautifulSoup(primary_html, 'html.parser')
    images = source.find_all('img')
    separator = '\ue000'
    if (not images or any(im.get('data-source-cell') != 'true' for im in images)
            or primary.find(['img', 'svg', 'table']) or separator in source_html + primary_html):
        return None
    atoms = [n for n in primary.find_all(['math', 'sup', 'sub'])
             if n.find_parent(['math', 'sup', 'sub']) is None]
    if len(atoms) < len(images):
        return None
    # Use a display key solely to locate the printed gap. A degree mark can
    # be represented in MathML as a superscript circle. Retain the PDF unit.
    literals = []
    for atom in atoms:
        display = BeautifulSoup(str(atom), 'html.parser')
        for circle in display.select('msup > mo:last-child'):
            if circle.get_text() == '∘':
                circle.string = '°'
        literals.append(text_key(display.get_text()))
        atom.replace_with(separator)
    segments = [text_key(s) for s in primary.get_text().split(separator)]
    if len(segments) != len(atoms) + 1 or any(not v for v in literals):
        return None
    positions = {}
    chunks = [segments[0]]
    offset = len(segments[0])
    for atom, literal, following in zip(atoms, literals, segments[1:]):
        positions[offset] = (atom, len(literal))
        chunks.extend([literal, following])
        offset += len(literal) + len(following)
    rendered = ''.join(chunks)
    for image in images:
        image.replace_with(separator)
    anchors = [text_key(s) for s in source.get_text().split(separator)]
    if not any(anchors) or len(anchors) != len(images) + 1:
        return None
    offset, replacements = 0, []
    for anchor in anchors[:-1]:
        if not rendered.startswith(anchor, offset):
            return None
        offset += len(anchor)
        if offset not in positions:
            return None
        atom, length = positions[offset]
        replacements.append(atom)
        offset += length
    if rendered[offset:] != anchors[-1]:
        return None
    result = BeautifulSoup(source_html, 'html.parser')
    for image, atom in zip(result.find_all('img'), replacements):
        image.replace_with(atom)
    return str(result)


def _trim_empty_right_columns(fragment, primary, width):
    """Project a padded OCR table onto the measured width without changing any payload."""
    parsed = BeautifulSoup(fragment, 'html.parser')
    if len(parsed.find_all('table')) != 1 or parsed.table.find('colgroup'):
        return None
    nodes = parsed.table.find_all(['td', 'th'])
    if len(nodes) != len(primary['cells']):
        return None
    empty_columns = set()
    for node, cell in zip(nodes, primary['cells']):
        column, span = cell['column'], cell['column_span']
        if column >= width:
            if node.decode_contents().strip() or set(node.attrs) - {'rowspan', 'colspan'}:
                return None
            empty_columns.update(range(column, column + span))
            node.decompose()
        elif column + span > width:
            if width - column == 1:
                node.attrs.pop('colspan', None)
            else:
                node['colspan'] = str(width - column)
    if empty_columns == set(range(width, primary['columns'])):
        return str(parsed.table)
    return None


def _aligned_profile(fragment, geometry, target, source_image, region, api, *, bounds=None, page=None, reader=None):
    primary = api.table_profile(fragment)
    normalization = None
    if primary['issues'] and geometry.get('source_grid_verified'):
        from collections import Counter
        original = BeautifulSoup(fragment, 'html.parser')
        repaired, operations = api.repair_table_spans(fragment, target['columns'])
        candidate = BeautifulSoup(repaired, 'html.parser')
        def payloads(parsed):
            return [node.decode_contents() for node in parsed.table.find_all(['td', 'th'])
                    if node.find_parent('table') is parsed.table]
        before, after = payloads(original), payloads(candidate)
        extra = Counter(after)-Counter(before)
        if (not (Counter(before)-Counter(after)) and not any(value for value in extra)
                and [value for value in before if value] == [value for value in after if value]
                and Counter(str(node) for node in original.find_all('math'))
                    == Counter(str(node) for node in candidate.find_all('math'))):
            fresh = api.table_profile(repaired)
            if not fresh['issues']:
                normalization = dict(method='existing_span_normalizer_with_all_atomic_payloads_preserved',
                    expected_columns=target['columns'], operations=operations,
                    original_sha256=hashlib.sha256(fragment.encode()).hexdigest(),
                    normalized_sha256=hashlib.sha256(repaired.encode()).hexdigest())
                fragment, primary = repaired, fresh
    def proven(profile, basis):
        if normalization:
            profile['source_primary_span_normalization'] = normalization
        return profile, basis
    if _prefix_matches(primary, target):
        return proven(primary, 'exact_source_cell_positions')
    from docling_pdf_primary_rows import align_atomic_primary_rows
    atomic = align_atomic_primary_rows(fragment, geometry, target, api)
    if (atomic is None and not primary['issues'] and primary['columns'] > target['columns']
            and geometry.get('source_grid_verified') and geometry.get('page_scan_transcription_required')):
        trimmed = _trim_empty_right_columns(fragment, primary, target['columns'])
        if trimmed is not None:
            candidate = align_atomic_primary_rows(trimmed, geometry, target, api)
            if candidate and candidate['source_partition_proof']['exact_primary_literal_geometry_anchors']:
                candidate['source_partition_proof']['empty_trailing_ocr_columns'] = primary['columns'] - target['columns']
                atomic = candidate
    if atomic is not None:
        return proven(atomic, 'unique_monotone_atomic_primary_rows_on_measured_source_grid')
    external = _external_note_prefix(fragment, geometry, target, bounds, page, reader, api)
    if external is not None:
        return proven(external, 'exact_primary_grid_prefix_and_complete_positioned_external_note')
    xs, ys = geometry.get('x_grid', []), geometry.get('y_grid', [])
    cells = geometry.get('cells', [])
    if (source_image is None or len(ys) != target['rows'] + 1 or len(xs) != target['columns'] + 1
            or {_key(c) for c in cells} != {_key(c) for c in target['cells']}):
        return None, None
    from docling_pdf_structure import align_source_reference_grid
    end = target['rows']
    while end >= primary['rows']:
        subset = [c for c in cells if c['row'] < end]
        if not any(c['row'] + c['row_span'] > end for c in subset):
            aligned, proof = align_source_reference_grid(fragment, subset, source_image, region,
                                                        xs, ys[:end+1], api)
            if proof:
                candidate = api.table_profile(aligned)
                if _prefix_matches(candidate, target):
                    return proven(candidate, 'source_rule_bands_and_exact_reference_cell_order')
        tail = [c for c in cells if c['row'] == end-1]
        if (len(tail) != 1 or tail[0]['column'] != 0 or tail[0]['column_span'] != target['columns']
                or tail[0]['row_span'] != 1):
            break
        end -= 1
    from docling_pdf_primary_partitions import align_flattened_primary
    partitioned = align_flattened_primary(fragment, geometry, target, source_image, region, api)
    if partitioned is not None:
        return proven(partitioned, 'unique_complete_primary_partition_on_visible_positioned_source_glyphs')
    return None, None


def _usable(cell):
    """Accept textual primary payload without judging its vocabulary."""
    parsed = BeautifulSoup(cell['html'], 'html.parser')
    text = parsed.get_text(' ', strip=True)
    return bool(text) and not parsed.find(['img', 'table', 'svg'])


def _compound_footer(raw, primary, target, geometry, bounds, identity, api):
    """Keep note paragraphs emitted inside the same primary table block.

    Some primary blocks contain a table followed by its notes, with one bbox
    for the entire block. Only a unique physical terminal full-width cell can
    own this ordered tail; no coordinates for individual notes are invented.
    """
    tails = [c for c in target['cells'] if c['row'] >= primary['rows']]
    if (len(tails) != 1 or tails[0]['column'] != 0 or tails[0]['row_span'] != 1 or
            tails[0]['column_span'] != target['columns']):
        return None
    cell = tails[0]
    physical = next((c for c in geometry.get('cells', []) if _key(c) == _key(cell)), None)
    box = physical.get('bbox') if physical else None
    if not box or abs(bounds[3]-box[3]) > max(3, .1*(box[3]-box[1])):
        return None
    siblings = [n for n in raw.table.next_siblings if str(n).strip()]
    if not siblings or any(getattr(n, 'name', None) not in {'p', 'div'} for n in siblings):
        return None
    fragment = ''.join(str(n) for n in siblings)
    if not _usable({'html': fragment}):
        return None
    parsed = BeautifulSoup(fragment, 'html.parser')
    if parsed.find(['script', 'style', 'iframe', 'object', 'link']):
        return None
    clean = api.clean_table_html('<table><tr><td>'+fragment+'</td></tr></table>')
    html = BeautifulSoup(clean, 'html.parser').td.decode_contents()
    proof = dict(source_record_sha256=identity, source_bbox=list(bounds), header=False,
                 basis='primary_table_tail_on_unique_terminal_source_cell',
                 source_cell_bbox=list(box), note_blocks=len(siblings))
    return _key(cell), html, proof


def _numeric(value):
    value = re.sub(r'(?i)\b(?:св|до|от|и|менее|более|вкл)\.?', '', value)
    return bool(re.search(r'\d', value)) and not re.search(r'[^\d\s.,+−–—\-/%°()<>≤≥]', value)


def mask_nonprimary_content(fragment, geometry, source_image, region, page, manifest, api):
    """Keep editable payload and request recovery when ownership is unproved.

    A missing grid-to-OCR mapping is not evidence that text or mathematics
    is an illustration. Do not replace the cell, or discard prose beside an
    existing image, while the exporter's typed recovery resolves its source.
    """
    if not geometry:
        return fragment, []
    soup = BeautifulSoup(fragment, 'html.parser')
    if soup.table is None:
        return fragment, []
    before = api.table_profile(str(soup.table))
    physical = {_key(c): c for c in geometry.get('cells', [])}
    nodes = [n for n in soup.table.find_all(['td', 'th']) if n.find_parent('table') is soup.table]
    proofs = []
    for node, position in zip(nodes, before['cells']):
        key = _key(position)
        source = physical.get(key)
        if source is None or source.get('primary_ocr'):
            continue
        if not node.get_text(strip=True) and not node.find('math'):
            continue
        source.update(html=node.decode_contents(), text=node.get_text(' ', strip=True),
                      primary_content_unmapped=True, typed_content_recovery_required=True)
        proof = dict(page=page, row=key[0], column=key[1], row_span=key[2], column_span=key[3],
                     reason='primary_ocr_not_mapped_preserve_editable_content', bbox=source.get('bbox'))
        proofs.append(proof)
        api.diagnostic_event('TABLE_PRIMARY_OCR_UNMAPPED', **proof)
    if not proofs:
        return fragment, []
    result = str(soup.table)
    after = api.table_profile(result)
    if {_key(c) for c in before['cells']} != {_key(c) for c in after['cells']} or after['issues']:
        raise RuntimeError('Editable payload preservation changed physical cell positions')
    manifest.setdefault('primary_ocr_unmapped_cells', []).extend(proofs)
    manifest.setdefault('typed_table_recovery_cells', []).extend(proofs)
    return result, proofs


def _numeric_signature(value):
    value = value.casefold().translate(str.maketrans('−–—', '---'))
    value = re.sub(r'\b(св|вкл)\.', r'\1', value)
    value = re.sub(r'(?<=\d)\s*[,\.]\s*(?=\d)', '.', value)
    return re.sub(r'\s+', '', value)


def _restore_repeated_cell_symbols(nodes, positions, geometry, records, region, page, ref, reader, regions, manifest, api):
    """Reuse a locally transcribed, identical PDF symbol on a separate source line."""
    if (not reader or not regions or not geometry.get('positioned_source_grid_verified')
            or not geometry.get('retained_cell_images') or geometry.get('hidden_ocr_direction_conflict')):
        return
    donors = {(c['row'], c['column']) for c in geometry.get('restored_cell_math', ())
              if c.get('method') == 'page_ocr_math_with_matching_source_anchors_or_cas'}
    if not donors:
        return
    physical = {_key(c): c for c in geometry['cells']}
    eligible, examples = {}, {}
    for position, node in zip(positions, nodes):
        c = physical.get(_key(position))
        if not c or c.get('source_text_unreliable'):
            continue
        at = (c['row'], c['column'])
        if at in donors and c.get('restored_math') == 1 and len(c.get('lines', ())) == 1:
            examples[at] = (c, node)
        lines = c.get('lines', ())
        if c.get('source_images') != 1 or c.get('restored_math') or len(lines) != 2:
            continue
        image = node.find('img', attrs={'data-source-cell': 'true'})
        glyphs = [g for g in c.get('glyphs', ()) if g['text'].strip()]
        if (image is None or image.has_attr('data-cell-content') or len(glyphs) != 1
                or len(glyphs[0]['text']) != 1 or not glyphs[0]['text'].isalpha() or node.get_text(strip=True) != glyphs[0]['text']
                or [tag.name for tag in node.find_all()] not in (['br', 'img'], ['img', 'br'])):
            continue
        blank = [line for line in lines if not line['text'].strip()]
        if len(blank) == 1 and image.get('src') and image['src'] in blank[0]['html']:
            eligible[at] = (c, image, glyphs[0])
    if not eligible or not examples:
        return

    reader.load_page(page)
    owners = {}
    for at, (c, *_) in {**examples, **eligible}.items():
        borders = c['bbox']
        crossed = []
        for obj in reader.page.images:
            box = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
            if min(borders[2], box[2]) > max(borders[0], box[0]) and min(borders[3], box[3]) > max(borders[1], box[1]):
                crossed.append((box, obj['stream']))
        if len(crossed) != 1:
            continue
        box, stream = crossed[0]
        if (api._source_unit_contains(borders, box)
                and not {'Mask', 'SMask', 'SMaskInData', 'ImageMask'}.intersection(stream.attrs)):
            owners[at] = box
    if not (examples.keys() & owners.keys() and eligible.keys() & owners.keys()):
        return
    try:
        assets = api._source_unit_xobjects(regions, page, list(owners.values()))
    except Exception as error:
        api.diagnostic_event('REPEATED_SOURCE_CELL_SYMBOL_SKIPPED', page=page, ref=ref, reason=str(error))
        return
    def identity(at):
        bounds = owners.get(at)
        if not bounds:
            return None
        hits = [v for v in assets if all(abs(a - b) <= api.SOURCE_UNIT_COMPOSITION['bbox_epsilon_pt']
                for a, b in zip(v['bbox'], bounds))]
        if len(hits) == 1:
            v = hits[0]
            return v['source_png_sha256'], tuple(v['source_dimensions']), tuple(v['matrix'][:4])
        return None

    def compact(value):
        return ''.join(value.split())

    def atoms(node, left, right):
        found = set()
        for m in node.find_all('math'):
            if (not m.get('data-latex') or len(m.get_text()) != 1 or not m.get_text().isalpha()
                    or [t.name for t in m.find_all()] not in (['mi'], ['mrow', 'mi'])):
                continue
            chunks = []
            for text in node.find_all(string=True):
                chunks.append('\u0000' if text.find_parent('math') is m else str(text))
            if compact(''.join(chunks)) == left + '\u0000' + right:
                found.add(str(m))
        return found

    packets = [r for r in records or () if r.get('page_no') == page and r.get('bbox')
               and _same_region(r['bbox'], region) and '<table' in r.get('html', '').lower()]
    if len(packets) != 1:
        return
    try:
        packet = BeautifulSoup(api.clean_table_html(packets[0]['html']), 'html.parser')
    except (ValueError, TypeError):
        return
    if len(packet.find_all('table')) != 1:
        return
    labels, veto = {}, set()
    for at, (cell, donor) in examples.items():
        key = identity(at)
        if not key:
            continue
        box = owners[at]
        glyphs = sorted((g for g in cell.get('glyphs', ()) if g['text'].strip()), key=lambda g: g['x0'])
        left = ''.join(g['text'] for g in glyphs if (g['x0'] + g['x1']) / 2 < box[0])
        right = ''.join(g['text'] for g in glyphs if (g['x0'] + g['x1']) / 2 > box[2])
        if (not left or not right or left + right != ''.join(g['text'] for g in glyphs)
                or not any(g['bottom'] > box[1] and g['top'] < box[3] for g in glyphs)):
            veto.add(key)
            continue
        words = atoms(donor, left, right)
        candidates = [atoms(n, left, right) for n in packet.find_all(['th', 'td'])
                      if compact(n.get_text()).startswith(left) and compact(n.get_text()).endswith(right)]
        if len(words) != 1 or len(candidates) != 1 or candidates[0] != words:
            veto.add(key)
            continue
        word = next(iter(words))
        if key in labels and labels[key][0] != word:
            veto.add(key)
        labels[key] = (word, at)
    changed = False
    for at, (cell, image, glyph) in eligible.items():
        key = identity(at)
        if key not in labels or key in veto:
            continue
        box = owners[at]
        before_text = image.find_previous_sibling('br') is None
        if not (box[3] <= glyph['top'] if before_text else box[1] >= glyph['bottom']):
            continue
        word, donor = labels[key]
        owner = image.parent
        image.replace_with(BeautifulSoup(word, 'html.parser').math)
        line = next(line for line in cell['lines'] if not line['text'].strip())
        line.update(html=word, text=BeautifulSoup(word, 'html.parser').get_text())
        cell.pop('positioned_source_text', None)
        cell.update(html=owner.decode_contents(), text=owner.get_text(' ', strip=True), source_images=0, restored_math=1)
        evidence = dict(page=page, ref=ref, row=at[0], column=at[1], donor_row=donor[0], donor_column=donor[1],
                        source_bbox=box, donor_bbox=owners[donor], source_png_sha256=key[0], dimensions=list(key[1]),
                        linear_transform=list(key[2]), latex=BeautifulSoup(word, 'html.parser').math['data-latex'])
        geometry['retained_cell_images'] = [v for v in geometry['retained_cell_images'] if (v['row'], v['column']) != at]
        geometry['restored_cell_math'].append(dict(method='identical_source_symbol_on_separate_line', bbox=box, **evidence))
        manifest.setdefault('repeated_source_cell_symbols', []).append(evidence)
        api.diagnostic_event('REPEATED_SOURCE_CELL_SYMBOL_RESTORED', **evidence)
        changed = True
    return changed


def preserve_primary_cells(fragment, geometry, primary_records, region, page, manifest, api,
                           source_image=None, reader=None, args=None, source_regions=None, table_ref=None):
    """Transfer unambiguous primary cells, retaining physical spans and drawings.

    The source grid remains authoritative. A mismatching OCR grid is usable
    only after existing pixel/glyph-backed alignment proves cell ownership.
    Conflicting math/symbols on proven vector text require regional pixel
    confirmation before either reading can replace the other.
    """
    if not geometry:
        return fragment, []
    soup = BeautifulSoup(fragment, 'html.parser')
    if soup.table is None:
        return fragment, []
    target = api.table_profile(str(soup.table))
    if target['issues']:
        return fragment, []
    proposals = {}
    primary_ends = []
    external_notes = []
    for record in primary_records or ():
        if record.get('page_no') != page or '<table' not in record.get('html', '').lower():
            continue
        bounds = record.get('bbox')
        if not bounds or len(bounds) != 4 or not _same_region(bounds, region):
            continue
        raw = BeautifulSoup(record['html'], 'html.parser')
        if len(raw.find_all('table')) != 1:
            continue
        identity = hashlib.sha256(record['html'].encode('utf-8')).hexdigest()
        graphic_marker = '\ue000'+identity+'\ue001'
        # Mark graphic positions before cleaning. The OCR's image URL is not
        # a source asset; a mixed cell can use only its own retained source
        # images, substituted at these positions after cell ownership aligns.
        for image in list(raw.table.find_all(['img', 'svg'])):
            image.replace_with(graphic_marker)
        try:
            primary, basis = _aligned_profile(api.clean_table_html(str(raw.table)), geometry, target,
                                               source_image, region, api, bounds=bounds, page=page, reader=reader)
        except (ValueError, TypeError) as error:
            api.diagnostic_event('TABLE_PRIMARY_OCR_SKIPPED', page=page, reason=str(error))
            continue
        if primary is None:
            api.diagnostic_event('TABLE_PRIMARY_OCR_SKIPPED', page=page,
                                 reason='source_cell_mapping_not_proven', bbox=bounds)
            continue
        if primary.get('source_primary_span_normalization'):
            proof = dict(page=page, source_bbox=list(bounds), source_record_sha256=identity,
                         **primary['source_primary_span_normalization'])
            manifest.setdefault('primary_source_span_normalizations', []).append(proof)
            api.diagnostic_event('PRIMARY_SOURCE_GRID_SPANS_NORMALIZED', **proof)
        if primary.get('source_external_note_prefix'):
            proof = dict(source_record_sha256=identity, **primary['source_external_note_prefix'])
            manifest.setdefault('primary_source_external_note_prefixes', []).append(proof)
            api.diagnostic_event('TABLE_PRIMARY_EXTERNAL_NOTE_PREFIX_ALIGNED', **proof)
        if primary.get('source_partition_proof'):
            proof = dict(page=page, source_bbox=list(bounds),
                         source_record_sha256=identity, **primary['source_partition_proof'])
            manifest.setdefault('primary_source_grid_partitions', []).append(proof)
            api.diagnostic_event('TABLE_PRIMARY_SOURCE_GRID_PARTITIONED', **proof)
            for payload in primary.get('source_partition_external_notes', []):
                external_notes.append(dict(html=payload, source_bbox=list(bounds),
                    source_record_sha256=identity, physical_table_bbox=list(geometry['bbox']),
                    basis='terminal_primary_note_after_unique_complete_source_grid_partition'))
        primary_ends.append(primary['rows'])
        for cell in primary['cells']:
            if _usable(cell):
                proposals.setdefault(_key(cell), {}).setdefault(cell['html'], []).append(
                    dict(source_record_sha256=identity, source_bbox=list(bounds), basis=basis,
                         header=cell.get('header', False), graphic_marker=graphic_marker,
                         positioned_math_only=(primary.get('source_external_note_prefix', {}).get('note_evidence')
                             in {'equivalent_printed_glyphs', 'positioned_inline_math_images'})))
        compound = _compound_footer(raw, primary, target, geometry, bounds, identity, api)
        if compound:
            key, html, proof = compound
            proposals.setdefault(key, {}).setdefault(html, []).append(proof)
    if primary_ends:
        for key, choices in footer_proposals(geometry, target, primary_records, region, page,
                                             source_image, api, max(primary_ends)).items():
            for html, readings in choices.items():
                proposals.setdefault(key, {}).setdefault(html, []).extend(readings)
    physical = {_key(c): c for c in geometry.get('cells', [])}
    nodes = [n for n in soup.table.find_all(['td', 'th']) if n.find_parent('table') is soup.table]
    if not proposals and _restore_repeated_cell_symbols(nodes, target['cells'], geometry, primary_records,
            region, page, table_ref, reader, source_regions, manifest, api):
        return str(soup.table), []
    graphic_columns = set()
    for node, cell in zip(nodes, target['cells']):
        choices = proposals.get(_key(cell), {})
        headings = [node.get_text(' ', strip=True)] if cell.get('header') else []
        headings += [BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
                     for html, readings in choices.items() if any(r['header'] for r in readings)]
        if ((target['columns'] == 1 or cell['column_span'] < target['columns']) and any(re.search(
                r'изображен|эскиз|схема|черт[её]ж|drawing|sketch|diagram', h, re.I) for h in headings)):
            graphic_columns.update(range(cell['column'], cell['column'] + cell['column_span']))
    local_graphics = geometry.get('source_graphic_assets', [])
    protected = manifest.get('protected_source_graphic_assets', []) + manifest.get('source_cell_diagrams', [])
    preserved = []
    positions = set()
    image_positions = set()
    for node, cell in zip(nodes, target['cells']):
        key = _key(cell)
        choices = proposals.get(key, {})
        if not choices:
            continue
        source = physical.get(key)
        if source is None:
            continue
        columns = set(range(cell['column'], cell['column'] + cell['column_span']))
        entries = [e for e in geometry.get('retained_cell_images', [])
                   if (e.get('row'), e.get('column')) == key[:2]]
        def owns_graphic(proof):
            if proof.get('page', page) != page or (proof.get('row'), proof.get('column')) != key[:2]:
                return False
            bounds = proof.get('cell_bbox') or proof.get('bbox')
            cell_box = source.get('bbox')
            return bool(bounds and cell_box and cell_box[0] <= (bounds[0]+bounds[2])/2 <= cell_box[2]
                        and cell_box[1] <= (bounds[1]+bounds[3])/2 <= cell_box[3])
        graphic = (node.select_one('[data-cell-content="source-diagram"]') is not None or
                   any((p.get('row'), p.get('column')) == key[:2] for p in local_graphics) or
                   any(owns_graphic(p) for p in protected) or
                   (not cell.get('header') and bool(columns & graphic_columns)) or
                   any(e.get('source_column_context', {}).get('graphic_column') or
                       e.get('source_column_context', {}).get('dimension_column') for e in entries))
        if len(choices) != 1:
            conflict = dict(page=page, row=key[0], column=key[1], reason='primary_readings_disagree',
                            readings=list(choices))
            manifest.setdefault('primary_ocr_cell_conflicts', []).append(conflict)
            api.diagnostic_event('TABLE_PRIMARY_OCR_CONFLICT', **conflict)
            continue
        html, readings = next(iter(choices.items()))
        if all(r.get('positioned_math_only') for r in readings):
            if graphic:
                continue
            html = _fill_positioned_math_images(node.decode_contents(), html)
            if html is None:
                continue
        vector_confirmation = None
        if source_image is not None:
            from docling_pdf_vector_content import confirm_vector_cell
            vector_confirmation = confirm_vector_cell(node.decode_contents(), html, source, geometry,
                reader, source_regions, page, args, manifest, api, ref=table_ref)
            if vector_confirmation and vector_confirmation[0] == 'source':
                html = vector_confirmation[1].get('source_literal_html', node.decode_contents())
                if vector_confirmation[1].get('source_literal_html') is not None:
                    from docling_pdf_text_cells import content_fingerprint
                    source['positioned_source_text'] = dict(source['positioned_source_text'],
                        html_sha256=content_fingerprint(html), literal_scripts_preserved=True)
                if vector_confirmation[1].get('review_required'):
                    node['data-source-review'] = vector_confirmation[1]['reason']
                    node['data-source-authority'] = 'vector-pdf-literal'
            elif vector_confirmation:
                source['source_text_unreliable'] = True
                source['source_text_conflict_reason'] = 'regional_pixels_confirm_primary_over_pdf_text'
        marker = readings[0].get('graphic_marker', '')
        marker_count = html.count(marker) if marker else 0
        images = [image for image in node.find_all('img')
                  if image.get('data-source-cell') == 'true'
                  or image.get('data-cell-content') == 'source-diagram']
        mixed_unresolved = None
        if marker_count:
            if marker_count == len(images):
                parts = html.split(marker)
                html = ''.join(part + (str(images[index]) if index < len(images) else '')
                               for index, part in enumerate(parts))
            else:
                mixed_unresolved = 'primary_graphic_placeholders_do_not_match_source_assets'
        elif graphic and images:
            # With no OCR placeholder, only exact editable content agreement
            # proves that retaining the existing interleaving is lossless.
            def editable_payload(value):
                parsed = BeautifulSoup(value, 'html.parser')
                for image in parsed.find_all('img'):
                    image.decompose()
                for math_node in parsed.find_all('math'):
                    math_node.replace_with('\x00math:' + str(math_node) + '\x00')
                return re.sub(r'\s+', '', parsed.get_text())
            if editable_payload(html) == editable_payload(node.decode_contents()):
                html = node.decode_contents()
            else:
                mixed_unresolved = 'primary_graphic_order_not_proven'
        if mixed_unresolved:
            pending = html.replace(marker, '') if marker else html
            source.update(typed_content_recovery_required=True, pending_primary_html=pending)
            proof = dict(page=page, row=key[0], column=key[1], row_span=key[2], column_span=key[3],
                         bbox=source.get('bbox'), reason=mixed_unresolved,
                         pending_primary_html=pending, source_images=len(images),
                         primary_graphic_placeholders=marker_count)
            manifest.setdefault('typed_table_recovery_cells', []).append(proof)
            api.diagnostic_event('TABLE_PRIMARY_MIXED_CONTENT_RECOVERY_REQUIRED', **proof)
            continue
        current = node.get_text(' ', strip=True)
        primary_text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
        if (_numeric(current) and _numeric(primary_text) and
                _numeric_signature(current) != _numeric_signature(primary_text)):
            conflict = dict(page=page, row=key[0], column=key[1], reason='numeric_source_and_primary_disagree',
                            source_text=current, primary_text=primary_text)
            manifest.setdefault('primary_ocr_cell_conflicts', []).append(conflict)
            api.diagnostic_event('TABLE_PRIMARY_OCR_CONFLICT', **conflict)
            if not manifest.get('primary_ocr_only'):
                continue
        if '\ufffd' in primary_text:
            warning = dict(page=page, row=key[0], column=key[1],
                           reason='replacement_character_in_primary_ocr', primary_text=primary_text)
            manifest.setdefault('primary_ocr_content_warnings', []).append(warning)
            api.diagnostic_event('TABLE_PRIMARY_OCR_CONTENT_WARNING', **warning)
        node.clear()
        node.append(BeautifulSoup(html, 'html.parser'))
        if any(r['header'] for r in readings):
            node.name = 'th'
        reading = {name: value for name, value in readings[0].items() if name not in {'graphic_marker', 'positioned_math_only'}}
        proof = dict(page=page, row=key[0], column=key[1], row_span=key[2], column_span=key[3],
                     method='primary_ocr_payload_on_source_grid', **reading)
        if vector_confirmation:
            proof['method'] = (vector_confirmation[1]['method']
                               if 'readings' not in vector_confirmation[1]
                               else 'vector_source_counterread_' + vector_confirmation[0])
            proof['vector_confirmation'] = vector_confirmation[1]
        source.update(html=node.decode_contents(), text=node.get_text(' ', strip=True), primary_ocr=proof)
        source.pop('raster_cell_ocr', None)
        source.pop('raster_cell_ocr_source', None)
        positions.add(key[:2])
        if images and BeautifulSoup(html, 'html.parser').find('img'):
            image_positions.add(key[:2])
        preserved.append(proof)
        api.diagnostic_event('TABLE_PRIMARY_OCR_PRESERVED', **proof)
    if not preserved:
        return fragment, []
    if external_notes:
        unique_notes = {entry['html']: entry for entry in external_notes}
        geometry['primary_external_notes'] = list(unique_notes.values())
    # Keep asset order for remaining fallback cells; removed crops no longer
    # participate in the one-to-one image/bbox mapping used by secondary OCR.
    for name in ('retained_cell_images', 'pending_cell_ocr'):
        if name in geometry:
            geometry[name] = [e for e in geometry[name] if (e.get('row'), e.get('column')) not in positions
                              or name == 'retained_cell_images' and (e.get('row'), e.get('column')) in image_positions]
    result = str(soup.table)
    after = api.table_profile(result)
    if {_key(c) for c in after['cells']} != {_key(c) for c in target['cells']} or after['issues']:
        raise RuntimeError('Primary OCR preservation changed the physical source grid')
    manifest.setdefault('primary_ocr_cells_preserved', []).extend(preserved)
    return result, preserved


def footer_proposals(geometry, target, primary_records, region, page, source_image, api, primary_end):
    """Retain complete positioned primary notes inside trailing source cells.

    Ownership requires the entire block inside one full-width physical cell;
    completeness requires the union of those blocks to cover its nongrid ink.
    This is only a proposal builder: it never changes geometry or calls OCR.
    """
    if (source_image is None or primary_end is None or not geometry or
            not target or target.get('issues') or primary_end >= target.get('rows', 0)):
        return {}
    import math
    import numpy as np
    from docling_pdf_table_layout import grid_ink

    physical = {_key(c): c for c in geometry.get('cells', [])}
    tails = [c for c in target.get('cells', []) if c['row'] >= primary_end]
    if not tails or any(c['column'] != 0 or c['column_span'] != target['columns'] or
                        c['row_span'] != 1 for c in tails):
        return {}
    if len(geometry.get('x_grid', [])) < 2:
        return {}
    source = grid_ink(source_image, region, geometry['x_grid'])
    if source is None:
        return {}

    def intersection(a, b):
        return (max(0, min(a[2], b[2])-max(a[0], b[0])) *
                max(0, min(a[3], b[3])-max(a[1], b[1])))

    def area(box):
        return max(0, box[2]-box[0])*max(0, box[3]-box[1])

    def pixels(box):
        return (max(0, math.floor((box[0]-region[0])*source['sx'])),
                max(0, math.floor((box[1]-region[1])*source['sy'])),
                min(source_image.width, math.ceil((box[2]-region[0])*source['sx'])),
                min(source_image.height, math.ceil((box[3]-region[1])*source['sy'])))

    result = {}
    for cell in tails:
        key = _key(cell)
        original = physical.get(key)
        box = original.get('bbox') if original else None
        if not box or len(box) != 4 or area(box) <= 0:
            continue
        envelope = [box[0]-1, box[1]-1, box[2]+1, box[3]+1]
        notes = []
        rejected = False
        for record in primary_records or ():
            if record.get('page_no') != page:
                continue
            label = re.sub(r'[^a-z]', '', str(record.get('label', '')).lower())
            if label not in {'text', 'footnote', 'listitem'}:
                continue
            bounds = record.get('bbox')
            if not bounds or len(bounds) != 4 or area(bounds) <= 0:
                continue
            if intersection(bounds, envelope)/area(bounds) < .94:
                continue
            raw = record.get('html', '')
            parsed = BeautifulSoup(raw, 'html.parser')
            if (not _usable({'html': raw}) or
                    parsed.find(['script', 'style', 'iframe', 'object', 'link'])):
                rejected = True
                break
            identity = hashlib.sha256(raw.encode('utf-8')).hexdigest()
            if any(n['hash'] == identity and n['bbox'] == list(bounds) for n in notes):
                continue
            notes.append({'html': raw, 'bbox': list(bounds), 'hash': identity})
        if rejected or not notes:
            api.diagnostic_event('TABLE_PRIMARY_NOTES_SKIPPED', page=page, row=key[0], column=key[1],
                                 reason='unusable_positioned_note' if rejected else 'no_complete_positioned_notes')
            continue
        # Alternative reads of the same physical lines cannot be concatenated
        # as though they were separate notes. Small OCR bbox fringes may overlap.
        conflict = any(intersection(a['bbox'], b['bbox'])/min(area(a['bbox']), area(b['bbox'])) > .5
                       for index, a in enumerate(notes) for b in notes[index+1:])
        if conflict:
            api.diagnostic_event('TABLE_PRIMARY_NOTES_SKIPPED', page=page, row=key[0], column=key[1],
                                 reason='overlapping_primary_note_readings')
            continue
        left, top, right, bottom = pixels(box)
        nongrid = source['nongrid'][top:bottom, left:right] > 0
        ink = int(np.count_nonzero(nongrid))
        if ink == 0:
            api.diagnostic_event('TABLE_PRIMARY_NOTES_SKIPPED', page=page, row=key[0], column=key[1],
                                 reason='source_note_cell_has_no_nongrid_ink')
            continue
        covered = np.zeros(nongrid.shape, dtype=bool)
        for note in notes:
            b = note['bbox']
            nl, nt, nr, nb = pixels([b[0]-1, b[1]-1, b[2]+1, b[3]+1])
            cl, ct = max(0, nl-left), max(0, nt-top)
            cr, cb = min(right-left, nr-left), min(bottom-top, nb-top)
            if cl < cr and ct < cb:
                covered[ct:cb, cl:cr] = True
        covered_ink = int(np.count_nonzero(nongrid & covered))
        coverage = covered_ink/ink
        if coverage < .995:
            api.diagnostic_event('TABLE_PRIMARY_NOTES_SKIPPED', page=page, row=key[0], column=key[1],
                                 reason='positioned_notes_do_not_cover_source_cell_ink', coverage=coverage,
                                 source_ink_pixels=ink, uncovered_ink_pixels=ink-covered_ink)
            continue
        notes.sort(key=lambda n: (n['bbox'][1], n['bbox'][0]))
        joined = ''.join(n['html'] for n in notes)
        try:
            clean = api.clean_table_html('<table><tr><td>'+joined+'</td></tr></table>')
            html = BeautifulSoup(clean, 'html.parser').td.decode_contents()
        except (ValueError, TypeError) as error:
            api.diagnostic_event('TABLE_PRIMARY_NOTES_SKIPPED', page=page, row=key[0], column=key[1],
                                 reason='primary_notes_cannot_be_sanitized', detail=str(error))
            continue
        if not _usable({'html': html}):
            continue
        bounds = [min(n['bbox'][0] for n in notes), min(n['bbox'][1] for n in notes),
                  max(n['bbox'][2] for n in notes), max(n['bbox'][3] for n in notes)]
        proof = dict(basis='complete_positioned_primary_notes', header=False,
            source_record_sha256=hashlib.sha256(joined.encode('utf-8')).hexdigest(),
            source_record_sha256s=[n['hash'] for n in notes], source_bbox=bounds,
            source_bboxes=[n['bbox'] for n in notes], note_blocks=len(notes),
            coverage=coverage, source_ink_pixels=ink, uncovered_ink_pixels=ink-covered_ink)
        result[key] = {html: [proof]}
    return result
