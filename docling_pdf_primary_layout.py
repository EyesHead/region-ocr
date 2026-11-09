"""Recover primary table payload on source cells and repeated fraction bands.

The PDF layer supplies positions and lookup evidence only. Every editable
formula and text fragment comes from an existing primary OCR response.
Unmapped source content is retained as pixels; this module performs no OCR.
"""
from difflib import SequenceMatcher
from hashlib import sha256
import re

from bs4 import BeautifulSoup


def _norm(text):
    text = text.translate(str.maketrans({'З': '3', 'з': '3', 'О': '0', 'O': '0',
        'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H',
        'Р': 'P', 'Т': 'T', 'Х': 'X', '−': '-', '—': '-', '–': '-', ',': '.'}))
    return re.sub(r'\s|\u00ad', '', text).casefold()


def _append_literal(soup, parent, text):
    if parent.name != 'mrow':
        row = soup.new_tag('mrow')
        parent.replace_with(row)
        row.append(parent)
        parent = row
    math = parent.find_parent('math')
    if math:
        for annotation in math.find_all(['annotation', 'annotation-xml']):
            annotation.decompose()
    for token in re.findall(r'\d+|[A-Za-z]+|[^\s]', text):
        node = soup.new_tag('mn' if token.isdigit() else 'mi' if token.isalpha() else 'mo')
        node.string = token
        parent.append(node)


def primary_fraction_catalog(records, api):
    catalog = {}
    for record in records:
        if '<table' not in record.get('html', '').lower():
            continue
        try:
            parsed = BeautifulSoup(api.clean_table_html(record['html']), 'html.parser')
        except (ValueError, TypeError):
            continue
        for cell in parsed.find_all(['td', 'th']):
            if len(cell.find_all('math')) != 1 or len(cell.find_all('mfrac')) != 1:
                continue
            math = cell.math
            fraction = math.mfrac
            args = fraction.find_all(recursive=False)
            if len(args) != 2 or args[0].find(['mfrac', 'msub', 'msqrt', 'mtable']):
                continue
            outside = BeautifulSoup(str(cell), 'html.parser')
            outside.math.decompose()
            tail = outside.get_text(' ', strip=True)
            if tail and not re.fullmatch(r'\(\s*\d+(?:[,.]\d+)?\s*\)', tail):
                continue
            numerator, denominator = args[0].get_text('', strip=True), args[1].get_text('', strip=True)+tail
            if not re.fullmatch(r'[\d\s,.]+', numerator) or not re.fullmatch(r'[\dA-Za-z\s,.()−—–-]+', denominator):
                continue
            clone = BeautifulSoup(str(math), 'html.parser')
            if tail:
                _append_literal(clone, clone.mfrac.find_all(recursive=False)[1], tail)
                clone.math.attrs.pop('data-latex', None)
            key = (_norm(numerator), _norm(denominator))
            catalog.setdefault(key, dict(html=str(clone.math), page=record.get('page_no'),
                record_sha256=sha256(record['html'].encode()).hexdigest(),
                numerator_has_script=bool(args[0].find(['msup', 'msubsup'])),
                detached_denominator_parentheses=bool(tail)))
    return catalog


def _catalog_choice(catalog, value):
    chosen = catalog.get(value['key'])
    if chosen and '(' in value['key'][1] and not value.get('denominator_under_rule'):
        return None
    if chosen and chosen['numerator_has_script'] != value['source_numerator'].strip().endswith(')'):
        return None
    return chosen


def source_fractions(image, box, glyphs, api):
    """Measure internal fraction rules, then associate positioned source lines."""
    import cv2
    import numpy as np
    from statistics import median

    gray = np.asarray(image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    sx, sy = image.width/(box[2]-box[0]), image.height/(box[3]-box[1])
    px, py = max(2, round(1.5*sx)), max(2, round(1.5*sy))
    ink[:py] = 0; ink[-py:] = 0; ink[:, :px] = 0; ink[:, -px:] = 0
    lines = api.native_cell_lines(glyphs)
    size = median(l['size'] for l in lines) if lines else 0
    if size <= 0:
        return []
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                 np.ones((1, max(8, round(image.width*.18))), np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(horizontal, 8)
    segments = sorted((y+h/2, x, x+w) for x, y, w, h, _ in stats[1:]
                      if h < max(6, w*.12))
    groups = []
    for y, left, right in segments:
        group = next((g for g in groups if abs(g[0]-y) < max(2, .75*sy)
                      and left-max(p[1] for p in g[1]) < size*.75*sx), None)
        if group is None:
            groups.append([y, [(left, right)]])
        else:
            group[1].append((left, right))
    rules = sorted((y, min(p[0] for p in pieces), max(p[1] for p in pieces)) for y, pieces in groups
                   if max(p[1] for p in pieces)-min(p[0] for p in pieces) >= image.width*.45
                   and sum(b-a for a, b in pieces) >= .70*(max(p[1] for p in pieces)-min(p[0] for p in pieces)))
    bars = [r[0] for r in rules]
    if not bars:
        return []
    result = []
    for index, pixel_y in enumerate(bars):
        y = box[1]+pixel_y/sy
        above = [l for l in lines if -.6*l['size'] <= y-l['y'] <= 1.1*l['size']]
        below = [l for l in lines if .5*l['size'] < l['y']-y <= 2*l['size']]
        numerator = min(above, key=lambda l: abs(l['y']-y), default=None)
        denominator = min(below, key=lambda l: abs(l['y']-y-1.2*l['size']), default=None)
        if not numerator or not re.fullmatch(r'[\dЗз\s,.]+\)?', numerator['text']):
            continue
        key = (_norm(numerator['text'].rstrip(')')), _norm(denominator['text'])) if denominator else None
        top = max(py, round(pixel_y-1.5*size*sy))
        bottom = min(image.height-py, round(pixel_y+2*size*sy))
        if index:
            top = max(top, round((bars[index-1]+pixel_y)/2))
        if index+1 < len(bars):
            bottom = min(bottom, round((bars[index+1]+pixel_y)/2))
        patch = ink[top:bottom, px:image.width-px].copy()
        center = round(pixel_y)-top
        patch[max(0, center-3):center+4] = 0
        yy, xx = np.nonzero(patch)
        pattern = None
        if len(xx):
            trimmed = patch[yy.min():yy.max()+1, xx.min():xx.max()+1]
            width = min(180, max(1, round(trimmed.shape[1]/trimmed.shape[0]*40)))
            pattern = np.zeros((48, 192), np.uint8)
            pattern[4:44, 4:4+width] = cv2.resize(trimmed, (width, 40), interpolation=cv2.INTER_AREA) > 96
        result.append(dict(y=y, key=key, pattern=pattern,
            bbox=[box[0]+px/sx, box[1]+top/sy, box[2]-px/sx, box[1]+bottom/sy],
            rule=[box[0]+rules[index][1]/sx, y, box[0]+rules[index][2]/sx, y],
            denominator_under_rule=bool(denominator and denominator['x0'] >= box[0]+rules[index][1]/sx-size*.4
                and denominator['x1'] <= box[0]+rules[index][2]/sx+size*.4),
            source_numerator=numerator['text'], source_denominator=denominator['text'] if denominator else None))
    return result


def _pixel_score(left, right):
    import cv2
    import numpy as np
    if left is None or right is None:
        return 0.0
    kernel = np.ones((3, 3), np.uint8)
    return float(min(np.count_nonzero(left & cv2.dilate(right, kernel))/max(1, np.count_nonzero(left)),
                     np.count_nonzero(right & cv2.dilate(left, kernel))/max(1, np.count_nonzero(right))))


def _primary_text(source, candidates, width):
    if source.get('source_text_unreliable'):
        return None
    text = BeautifulSoup(source.get('html', ''), 'html.parser').get_text('', strip=True)
    key = _norm(text).replace('-', '').replace('i', '1')
    if not key:
        return ''
    ranked = []
    for candidate in candidates:
        if candidate['column'] != source['column'] and candidate['column_span'] != width:
            continue
        if '<math' in candidate['html'] or '<img' in candidate['html']:
            continue
        other = _norm(candidate['text']).replace('-', '').replace('i', '1')
        if not other:
            continue
        score = 1.0 if key == other else SequenceMatcher(None, key, other, autojunk=False).ratio()
        if score >= (.80 if len(key) >= 12 else 1.0):
            ranked.append((score, candidate['html']))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][1] != ranked[1][1] and ranked[0][0]-ranked[1][0] < .10:
        return None
    return ranked[0][1]


def _sync(item, html, entry, api):
    from docling_core.types.doc import TableCell, TableData
    profile = api.table_profile(html)
    item.data = TableData(num_rows=profile['rows'], num_cols=profile['columns'], table_cells=[
        TableCell(text=c['text'], row_span=c['row_span'], col_span=c['column_span'],
                  start_row_offset_idx=c['row'], end_row_offset_idx=c['row']+c['row_span'],
                  start_col_offset_idx=c['column'], end_col_offset_idx=c['column']+c['column_span'],
                  column_header=c['header']) for c in profile['cells']])
    entry.update(html=html, selected=profile)


def repair_primary_fraction_tables(doc, routes, manifest, regions, reader, records, api):
    """Restore physical ownership only for proven collapsed fraction stacks."""
    from statistics import median

    if reader is None:
        return
    catalog = primary_fraction_catalog(records, api)
    items = {t.self_ref: t for t in doc.tables}
    for entry in manifest.get('tables', []):
        ref, page = entry['ref'], entry['page']
        geometry = entry.get('pdf_geometry') or {}
        if not geometry.get('source_grid_verified') or geometry.get('hidden_ocr_direction_conflict'):
            continue
        original = routes.get(ref, '')
        profile = api.table_profile(original)
        if profile.get('issues') or not profile.get('cells'):
            continue
        width = len(geometry.get('x_grid', []))-1
        # Native layer identifies candidate value bands; it never supplies HTML.
        candidates = [c for c in geometry.get('cells', []) if c['column_span'] == c['row_span'] == 1
                      and not c.get('source_text_unreliable')
                      and len(BeautifulSoup(c.get('html', ''), 'html.parser').find_all('br')) >= 3
                      and not re.search(r'[А-Яа-яЁё]{3}', BeautifulSoup(c.get('html', ''), 'html.parser').get_text())]
        if len(candidates) < 2:
            continue
        reader.load_page(page)
        all_glyphs = list(reader.chars)
        measured = {}
        for cell in candidates:
            box = cell['bbox']
            glyphs = [g for g in all_glyphs if box[0] < (g['x0']+g['x1'])/2 < box[2]
                      and box[1] < (g['top']+g['bottom'])/2 < box[3]]
            image = regions.crop(page, box)
            try:
                fractions = source_fractions(image, box, glyphs, api)
            finally:
                image.close()
            if len(fractions) >= 2:
                measured[(cell['row'], cell['column'])] = fractions
        plans = {}
        for row in {key[0] for key in measured}:
            columns = {col: values for (r, col), values in measured.items() if r == row}
            if len(columns) < 2 or len({len(v) for v in columns.values()}) != 1:
                continue
            count = len(next(iter(columns.values())))
            anchors = [median(v[i]['y'] for v in columns.values()) for i in range(count)]
            if all(abs(v[i]['y']-anchors[i]) < 3 for v in columns.values() for i in range(count)):
                plans[row] = columns
        if not plans:
            continue
        complete = True
        for source_row, columns in plans.items():
            anchors = sorted((c for c in geometry['cells'] if c['row'] == source_row
                              and c['column'] < min(columns)), key=lambda c: c['column'])
            anchor = anchors[0] if anchors else None
            label = _norm(BeautifulSoup(anchor.get('html', ''), 'html.parser').get_text('', strip=True)).replace('i', '1') if anchor else ''
            owners = [c for c in profile['cells'] if anchor and c['column'] == anchor['column']
                      and _norm(c['text']).replace('i', '1') == label]
            if len(owners) != 1:
                complete = False
                break
            owner = owners[0]
            for column, values in columns.items():
                count = sum(len(BeautifulSoup(c['html'], 'html.parser').find_all('mfrac')) for c in profile['cells']
                            if c['column'] == column and owner['row'] <= c['row'] < owner['row']+owner['row_span'])
                if count != len(values):
                    complete = False
                    break
        if complete:
            continue
        rebuilt = BeautifulSoup('<table><thead></thead><tbody></tbody></table>', 'html.parser')
        old_table = BeautifulSoup(original, 'html.parser').table
        rebuilt.table.attrs = dict(old_table.attrs)
        mapped = []
        for row in range(len(geometry['y_grid'])-1):
            tr = rebuilt.new_tag('tr')
            (rebuilt.thead if row < geometry.get('header_rows', 0) else rebuilt.tbody).append(tr)
            for cell in sorted((c for c in geometry['cells'] if c['row'] == row), key=lambda c: c['column']):
                node = rebuilt.new_tag('th' if row < geometry.get('header_rows', 0) else 'td')
                for attr, key in (('rowspan', 'row_span'), ('colspan', 'column_span')):
                    if cell[key] != 1:
                        node[attr] = str(cell[key])
                values = plans.get(row, {}).get(cell['column'])
                if values:
                    stack = rebuilt.new_tag('div', attrs={'data-primary-fraction-stack': str(len(values))})
                    for index, value in enumerate(values):
                        chosen = _catalog_choice(catalog, value)
                        basis = 'matching_primary_fraction_and_positioned_source_glyphs'
                        if chosen is None:
                            matches = []
                            for other in values:
                                known = _catalog_choice(catalog, other)
                                if known and other is not value:
                                    matches.append((_pixel_score(value['pattern'], other['pattern']), known))
                            matches.sort(key=lambda pair: pair[0], reverse=True)
                            if (len(matches) >= 2 and matches[0][0] >= .95 and matches[1][0] >= .60
                                    and matches[0][1]['html'] == matches[1][1]['html']):
                                chosen = matches[0][1]
                                basis = 'two_identical_source_fraction_patterns_and_primary_payload'
                        part = rebuilt.new_tag('div', attrs={'data-primary-value-index': str(index)})
                        if chosen:
                            part.append(BeautifulSoup(chosen['html'], 'html.parser'))
                        else:
                            image = regions.crop(page, value['bbox'])
                            try:
                                part.append(BeautifulSoup(api.image_html(image, value['bbox'][2]-value['bbox'][0], 'cell'), 'html.parser'))
                            finally:
                                image.close()
                            basis = 'unmapped_fraction_source_pixels'
                        stack.append(part)
                        mapped.append(dict(row=row, column=cell['column'], ordinal=index, source_bbox=value['bbox'],
                            basis=basis, primary_record_sha256=chosen['record_sha256'] if chosen else None))
                    node.append(stack)
                else:
                    payload = None
                    plain = BeautifulSoup(cell.get('html', ''), 'html.parser').get_text(' ', strip=True)
                    if (not cell.get('source_text_unreliable') and cell['column_span'] == 1
                            and re.search(r'\d', plain) and not re.search(r'[А-Яа-яЁё]{3}', plain)):
                        box = cell['bbox']
                        glyphs = [g for g in all_glyphs if box[0] < (g['x0']+g['x1'])/2 < box[2]
                                  and box[1] < (g['top']+g['bottom'])/2 < box[3]]
                        image = regions.crop(page, box)
                        try:
                            singles = source_fractions(image, box, glyphs, api)
                        finally:
                            image.close()
                        chosen = _catalog_choice(catalog, singles[0]) if len(singles) == 1 else None
                        if chosen:
                            payload = chosen['html']
                            mapped.append(dict(row=row, column=cell['column'], ordinal=0,
                                source_bbox=singles[0]['bbox'], basis='source_fraction_and_complete_primary_payload',
                                primary_record_sha256=chosen['record_sha256'],
                                denominator_parentheses_restored=chosen['detached_denominator_parentheses']))
                    if payload is None:
                        payload = _primary_text(cell, profile['cells'], width)
                    if payload is None and row < geometry.get('header_rows', 0):
                        positional = [c for c in profile['cells'] if all(c[k] == cell[k]
                            for k in ('row', 'column', 'row_span', 'column_span')) and c.get('header')]
                        if len(positional) == 1:
                            payload = positional[0]['html']
                    if payload is not None:
                        node.append(BeautifulSoup(payload, 'html.parser'))
                    else:
                        box = cell['bbox']
                        image = regions.crop(page, box)
                        try:
                            node.append(BeautifulSoup(api.image_html(image, box[2]-box[0], 'cell'), 'html.parser'))
                        finally:
                            image.close()
                tr.append(node)
        html = str(rebuilt.table)
        if api.table_profile(html)['issues']:
            continue
        retained_text = _norm(rebuilt.get_text('', strip=True))
        missing_text = [c for c in profile['cells'] if c['text'].strip() and '<math' not in c['html']
                        and '<img' not in c['html'] and _norm(c['text']) not in retained_text]
        old_keys = primary_fraction_catalog([dict(html=original, page_no=page)], api).keys()
        new_keys = {(_norm(parts[0].get_text('', strip=True)), _norm(parts[1].get_text('', strip=True)))
                    for fraction in rebuilt.find_all('mfrac') if len(parts := fraction.find_all(recursive=False)) == 2}
        if missing_text or not set(old_keys) <= new_keys:
            api.diagnostic_event('PRIMARY_FRACTION_LAYOUT_UNCHANGED', page=page, ref=ref,
                reason='unassigned_primary_payload', text_cells=len(missing_text), fractions=len(set(old_keys)-new_keys))
            continue
        routes[ref] = html
        _sync(items[ref], html, entry, api)
        entry['primary_source_layout_repaired'] = True
        proof = dict(page=page, ref=ref, rows=sorted(plans), fractions=mapped,
                     method='physical_cells_and_positioned_primary_fraction_payloads')
        manifest.setdefault('primary_fraction_layout_repairs', []).append(proof)
        for name in ('source_cell_grid_audits', 'source_grid_reviews', 'source_grid_mismatches'):
            manifest[name] = [r for r in manifest.get(name, []) if r.get('ref') != ref]
        from docling_pdf_table_audit import source_grid
        source_grid(html, geometry, page, ref, manifest, api)
        api.diagnostic_event('PRIMARY_FRACTION_LAYOUT_RESTORED', **proof)


def _descriptor_groups(node):
    content = node.decode_contents()
    return [p.strip() for p in re.split(r'(?:<br\s*/?>\s*){2,}', content) if p.strip()]


def expand_primary_fraction_rows(doc, routes, manifest, api):
    """Represent source-proven value bands as rows with a spanning group cell."""
    entries = {t['ref']: t for t in manifest.get('tables', [])}
    for item in doc.tables:
        original = routes.get(item.self_ref, '')
        if 'data-primary-fraction-stack' not in original:
            continue
        soup = BeautifulSoup(original, 'html.parser')
        changed = []
        for tr in list(soup.table.find_all('tr')):
            cells = tr.find_all(['td', 'th'], recursive=False)
            stacks = [(i, c.find(attrs={'data-primary-fraction-stack': True})) for i, c in enumerate(cells)]
            stacks = [(i, s) for i, s in stacks if s is not None]
            counts = {len(s.find_all(attrs={'data-primary-value-index': True}, recursive=False)) for _, s in stacks}
            if len(stacks) < 2 or len(counts) != 1 or any(int(c.get('rowspan', 1)) != 1 for c in cells):
                continue
            count = next(iter(counts))
            descriptor = next(((i, _descriptor_groups(c)) for i, c in enumerate(cells)
                               if i < min(j for j, _ in stacks) and len(_descriptor_groups(c)) == count), None)
            if descriptor is None:
                continue
            parts = {i: [v.decode_contents() for v in stack.find_all(attrs={'data-primary-value-index': True}, recursive=False)]
                     for i, stack in stacks}
            descriptor_index, groups = descriptor
            new_rows = []
            for ordinal in range(count):
                new = soup.new_tag('tr')
                for i, cell in enumerate(cells):
                    if i in parts or i == descriptor_index:
                        child = soup.new_tag(cell.name)
                        child.append(BeautifulSoup(parts[i][ordinal] if i in parts else groups[ordinal], 'html.parser'))
                        new.append(child)
                    elif ordinal == 0:
                        child = BeautifulSoup(str(cell), 'html.parser').find(['td', 'th'])
                        child['rowspan'] = str(count)
                        new.append(child)
                new_rows.append(new)
            for new in new_rows:
                tr.insert_before(new)
            tr.decompose()
            changed.append(dict(value_rows=count, value_columns=sorted(parts), descriptor_column=descriptor_index))
        if changed and not api.table_profile(str(soup.table))['issues']:
            html = str(soup.table)
            routes[item.self_ref] = html
            _sync(item, html, entries[item.self_ref], api)
            proof = dict(ref=item.self_ref, page=entries[item.self_ref]['page'], rows=changed,
                         method='source_fraction_baselines_and_primary_descriptor_groups')
            manifest.setdefault('primary_fraction_subrows', []).append(proof)
            api.diagnostic_event('PRIMARY_FRACTION_SUBROWS_EXPANDED', **proof)


def merge_primary_denominator_tails(soup, manifest, regions, reader, api):
    """Move existing parenthesized text only when source glyphs lie under the rule."""
    if reader is None:
        return []
    entries = {t['ref']: t for t in manifest.get('tables', [])}
    sources = {}
    for ref, entry in entries.items():
        owner, seen = ref, set()
        while entries.get(owner, {}).get('joined_into') and owner not in seen:
            seen.add(owner); owner = entries[owner]['joined_into']
        geometry = entry.get('pdf_geometry') or {}
        if not geometry.get('source_grid_verified') or geometry.get('hidden_ocr_direction_conflict'):
            continue
        for cell in geometry.get('cells', []):
            if cell.get('source_text_unreliable') or cell['column_span'] != 1:
                continue
            sources.setdefault((owner, cell['column']), []).append((entry['page'], cell))
    proofs = manifest.setdefault('primary_denominator_tail_repairs', [])
    for table in soup.select('table[data-pdf-table-ref]'):
        profile = api.table_profile(str(table))
        nodes = [n for n in table.find_all(['td', 'th']) if n.find_parent('table') is table]
        for position, cell in zip(profile.get('cells', []), nodes):
            if len(cell.find_all('math')) != 1 or len(cell.find_all('mfrac')) != 1:
                continue
            parsed = BeautifulSoup(str(cell), 'html.parser')
            parsed.math.decompose()
            tail = parsed.get_text(' ', strip=True)
            if not re.fullmatch(r'\(\s*\d+(?:[,.]\d+)?\s*\)', tail):
                continue
            fraction = cell.mfrac
            args = fraction.find_all(recursive=False)
            if len(args) != 2:
                continue
            wanted = (_norm(args[0].get_text('', strip=True).rstrip(')')),
                      _norm(args[1].get_text('', strip=True)+tail))
            evidence = []
            for page, source in sources.get((table['data-pdf-table-ref'], position['column']), []):
                plain = BeautifulSoup(source.get('html', ''), 'html.parser').get_text('', strip=True)
                if _norm(plain).replace(')', '') != _norm(cell.get_text('', strip=True)).replace(')', ''):
                    continue
                reader.load_page(page)
                box = source['bbox']
                glyphs = [g for g in reader.chars if box[0] < (g['x0']+g['x1'])/2 < box[2]
                          and box[1] < (g['top']+g['bottom'])/2 < box[3]]
                image = regions.crop(page, box)
                try:
                    values = source_fractions(image, box, glyphs, api)
                finally:
                    image.close()
                if len(values) == 1 and values[0]['key'] == wanted:
                    evidence.append((page, source, values[0]))
            if not evidence or not all(value['denominator_under_rule'] for _, _, value in evidence):
                continue
            _append_literal(soup, args[1], tail)
            math = cell.math.extract()
            math.attrs.pop('data-latex', None)
            cell.clear(); cell.append(math)
            proof = dict(ref=table['data-pdf-table-ref'], row=position['row'], column=position['column'], tail=tail,
                         source_page=evidence[0][0], source_bbox=evidence[0][1]['bbox'], rule=evidence[0][2]['rule'],
                         method='existing_primary_parentheses_below_source_fraction_rule')
            proofs.append(proof)
            api.diagnostic_event('PRIMARY_DENOMINATOR_PARENTHESES_RESTORED', **proof)
    return proofs
