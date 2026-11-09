"""PDF repairs backed by source rules, glyphs and retained OCR.

No document names, known equations or normative wording are encoded here.
The converter supplies its existing readers and recognition implementation.
"""
import base64
from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
from functools import lru_cache
from html import escape
from io import BytesIO
import re
import time

from bs4 import BeautifulSoup


GROUPS = {'boundaries', 'math', 'subrows', 'empty-columns'}


def enabled(args, group, *, format_only=False):
    if group == 'math' and getattr(args, 'primary_ocr_only', False) and not format_only:
        return False
    requested = set(getattr(args, 'pdf_repairs', ()) or ())
    return group in requested or 'all' in requested


def _coverage(inner, outer):
    area = max(0, inner[2]-inner[0])*max(0, inner[3]-inner[1])
    overlap = max(0, min(inner[2], outer[2])-max(inner[0], outer[0]))*max(
        0, min(inner[3], outer[3])-max(inner[1], outer[1]))
    return overlap/area if area else 0


def _cell_glyphs(reader, box):
    return [g for g in reader.chars if g['text'] and
            box[0] <= (g['x0']+g['x1'])/2 < box[2] and
            box[1] <= (g['top']+g['bottom'])/2 < box[3]]


def close_open_source_table(reader, region, table, api):
    """Close a missing page-edge rule at aligned real vertical-rule starts.

    TableFinder can otherwise retain only a full-width notes rectangle and
    miss the continued multi-column cells above it. Never close interior gaps.
    """
    from statistics import median
    grid = reader.open_edge_grid(region)
    if not grid:
        return table, False
    existing = {round(v, 1) for cell in table.cells for v in (cell[0], cell[2])} if table else set()
    complete_axes = bool(table and all(any(abs(x-y) < 2 for y in existing) for x in grid['x_grid']))
    starts, ends = [], []
    for x in grid['x_grid']:
        bars = [e for e in reader.page.edges if e['orientation'] == 'v' and abs(e['x0']-x) < 2
                and region[1]-4 <= e['top'] <= region[1]+10 and e['bottom']-e['top'] >= 10]
        if not bars:
            return table, False
        starts.append(min(e['top'] for e in bars))
        ends.append(min(e['bottom'] for e in bars))
    if max(starts)-min(starts) > 3:
        return table, False
    page = reader.page
    bounds = [max(page.bbox[0], region[0]-6), max(page.bbox[1], region[1]-6),
              min(page.bbox[2], region[2]+6), min(page.bbox[3], region[3]+6)]
    horizontal, note_cuts = [median(starts)], []
    glyphs = [g for g in reader.chars if g['text'].strip()
              and region[0] <= (g['x0']+g['x1'])/2 <= region[2]]
    interior = grid['x_grid'][1:-1]
    endpoints = sorted({e['bottom'] for e in page.edges if e['orientation'] == 'v'
                        and e['bottom']-e['top'] >= 10 and region[1]+10 < e['bottom'] < region[3]-5
                        and any(abs(e['x0']-x) < 2 for x in interior)})
    groups = []
    for y in endpoints:
        if groups and y-groups[-1][-1] <= 3:
            groups[-1].append(y)
        else:
            groups.append([y])
    for group in groups:
        end = median(group)
        if not all(any(e['orientation'] == 'v' and abs(e['x0']-x) < 2 and abs(e['bottom']-end) <= 3
                       and e['bottom']-e['top'] >= 10 for e in page.edges) for x in interior):
            continue
        after = [g for g in glyphs if end <= g['top'] <= end+50]
        following = ' '.join(line['text'] for line in api.native_cell_lines(after))
        # Font boxes include descenders below actual ink. Place the virtual
        # boundary in a verified glyph-free gap within intersection tolerance
        # of the real rail endpoints, rather than cutting any source glyph.
        crossing = [g for g in glyphs if g['top'] < end and g['bottom'] > end]
        cut = max([end, *(g['bottom']+.2 for g in crossing)])
        if (re.match(r'^\s*(?:\(в ред\.|Примечани[ея]\b|<\*+>)', following, re.I)
                and cut-end <= 2.5
                and any(g['bottom'] <= end+1 for g in glyphs)
                and not any(g['top'] < cut < g['bottom'] for g in glyphs)
                and not any(o['top'] < cut < o['bottom'] for o in page.images)):
            horizontal.append(cut)
            note_cuts.append(dict(source_rail_end=end, reconstructed_y=cut))
    if complete_axes and not note_cuts:
        return table, False
    bottom_ends = []
    for x in grid['x_grid']:
        bars = [e for e in page.edges if e['orientation'] == 'v' and abs(e['x0']-x) < 2
                and abs(e['bottom']-region[3]) <= 6 and e['bottom']-e['top'] >= 10]
        if not bars:
            break
        bottom_ends.append(median(e['bottom'] for e in bars))
    if len(bottom_ends) == len(grid['x_grid']) and max(bottom_ends)-min(bottom_ends) <= 3:
        horizontal.append(median(bottom_ends))
    candidates = page.crop(bounds).find_tables({'explicit_horizontal_lines': horizontal})
    for candidate in sorted(candidates, key=lambda t: (t.bbox[2]-t.bbox[0])*(t.bbox[3]-t.bbox[1]), reverse=True):
        b = candidate.bbox
        axes = {v for c in candidate.cells for v in (c[0], c[2])}
        if (abs(b[0]-grid['x_grid'][0]) <= 3 and abs(b[2]-grid['x_grid'][-1]) <= 3
                and _coverage(region, [b[0]-3, b[1]-3, b[2]+3, b[3]+3]) >= .95
                and all(any(abs(x-y) < 2 for y in axes) for x in grid['x_grid'])
                and (table is None or len(candidate.cells) >= len(table.cells))):
            if table and sorted(candidate.cells) == sorted(table.cells):
                return table, False
            return candidate, dict(source_rail_start=median(starts),
                                   reconstructed_horizontal_rules=horizontal,
                                   note_boundaries=note_cuts,
                                   basis='source_rail_endpoints_and_literal_note_in_glyph_free_gap')
    return table, False


def _note_paragraphs(lines, api):
    groups = []
    starts = re.compile(r'^(?:<\*+>|Примечани[ея]\b|\d+\s|\(в ред\.|'
                        r'[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9]*\s*[-—–])', re.I)
    for line in lines:
        value = line['text'].strip()
        if not value:
            continue
        if not groups or starts.match(value):
            groups.append(value)
        else:
            previous = groups[-1]
            wrap = re.search(r'([А-Яа-яЁё]+)[-\u00ad]$', previous)
            tail = re.match(r'[а-яё]+', value)
            if wrap and tail and api.joined_russian_word(wrap[0]+tail[0]) == wrap[1]+tail[0]:
                groups[-1] = previous[:-1]+value
            else:
                groups[-1] += ' '+value
    return groups


def repair_source_tables(doc, reader, regions, routes, excluded, manifest, api):
    """Coalesce same-page layout pieces; recover omitted full-width note cells.

    A single native ruled table must contain each layout piece and have the
    same side rails. Independent children prevent coalescing. Note recovery
    needs a literal footnote/notes marker in a real full-width source cell.
    """
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, ProvenanceItem

    note_routes = {}
    native_candidates = {}
    manifest['source_table_boundary_repairs'] = []
    manifest['recovered_source_notes'] = []
    if reader is None:
        return note_routes, native_candidates
    pages = sorted({p.page_no for t in doc.tables for p in t.prov})
    for page_no in pages:
        reader.load_page(page_no)
        source_tables = list(reader.tables)
        for source_table in source_tables:
            box = list(source_table.bbox)
            from docling_pdf_forms import form_owns_region
            if form_owns_region(manifest, page_no, box):
                continue
            matched = []
            for item in doc.tables:
                reg = api.item_region(item, doc)
                if not reg or reg[0] != page_no or item.self_ref in excluded:
                    continue
                b = reg[1]
                if (abs(b[0]-box[0]) <= 6 and abs(b[2]-box[2]) <= 6
                        and _coverage(b, [box[0]-6, box[1]-6, box[2]+6, box[3]+6]) >= .95):
                    matched.append((item, b))
            matched.sort(key=lambda pair: pair[1][1])
            if not matched:
                continue
            # An open bottom row can touch the last closed source rectangle
            # without belonging to TableFinder's original result. Require
            # uninterrupted source side rails at that exact junction.
            end = box[3]
            for item in sorted(doc.tables, key=lambda t: (api.item_region(t, doc) or (0, [0, 0, 0, 0]))[1][1]):
                reg = api.item_region(item, doc)
                if (not reg or reg[0] != page_no or item.self_ref in excluded
                        or any(item is old for old, _ in matched)):
                    continue
                b = reg[1]
                if (abs(b[0]-box[0]) > 6 or abs(b[2]-box[2]) > 6 or not end-3 <= b[1] <= end+8):
                    continue
                sides = []
                for x in (box[0], box[2]):
                    segments = sorted((e['top'], e['bottom']) for e in reader.page.edges
                        if e['orientation'] == 'v' and abs(e['x0']-x) < 2 and e['bottom']-e['top'] >= 8)
                    reached = end-3
                    for a, z in segments:
                        if a <= reached+3 and z >= reached:
                            reached = max(reached, z)
                    sides.append(reached >= b[3]-3)
                if all(sides):
                    matched.append((item, b)); end = b[3]
            matched.sort(key=lambda pair: pair[1][1])
            if len(matched) > 1:
                movable_notes = set()
                for item, _ in matched:
                    for ref in item.children:
                        child = ref.resolve(doc)
                        reg = api.item_region(child, doc)
                        if (getattr(child, 'label', None) != DocItemLabel.FOOTNOTE or not reg
                                or reg[0] != page_no or not re.match(r'^\s*<\*+>', child.text)
                                or reg[1][1] < box[3]-3):
                            continue
                        b = reg[1]
                        native = ' '.join(line['text'] for line in api.native_cell_lines(
                            _cell_glyphs(reader, [b[0]-4, b[1]-4, b[2]+4, b[3]+4])))
                        key = api.cell_text_key(child.text)
                        if len(key) >= 24 and key[:24] in api.cell_text_key(native):
                            movable_notes.add(ref.cref)
                independent = [ref.cref for item, _ in matched for ref in item.children
                               if ref.cref not in {cap.cref for cap in item.captions} | movable_notes]
                # The fragments must tile one table vertically, not overlap as
                # nested or alternative interpretations of a complicated figure.
                ordered = all(a[1][3] <= b[1][1]+4 for a, b in zip(matched, matched[1:]))
                if not independent and ordered:
                    target = matched[0][0]
                    bounds = [min(box[0], *(b[0] for _, b in matched)),
                              min(box[1], *(b[1] for _, b in matched)),
                              max(box[2], *(b[2] for _, b in matched)),
                              max(box[3], *(b[3] for _, b in matched))]
                    image = regions.crop(page_no, bounds)
                    try:
                        verified = reader.read(page_no, bounds, source_image=image)
                    finally:
                        image.close()
                    if not verified or verified[1].get('glyph_coverage') != 1:
                        api.diagnostic_event('SOURCE_TABLE_COALESCE_KEEP', page=page_no, bbox=bounds,
                                             reason='full_source_content_not_verified')
                        continue
                    native_candidates[target.self_ref] = verified
                    target.prov[0].bbox = BoundingBox(l=bounds[0], t=bounds[1], r=bounds[2],
                        b=bounds[3], origin=CoordOrigin.TOPLEFT)
                    for other, _ in matched[1:]:
                        for ref in list(other.children):
                            if ref.cref in movable_notes:
                                other.children.remove(ref)
                                ref.resolve(doc).parent = target.get_ref()
                                target.children.append(ref)
                        for ref in list(other.captions):
                            caption = ref.resolve(doc)
                            if not any(old.cref == ref.cref for old in target.captions):
                                target.captions.append(ref)
                            if caption.parent and caption.parent.cref == other.self_ref:
                                other.children = [child for child in other.children if child.cref != ref.cref]
                                caption.parent = target.get_ref()
                                if not any(child.cref == ref.cref for child in target.children):
                                    target.children.append(ref)
                        other.captions = []
                        routes[other.self_ref] = ''
                        excluded.add(other.self_ref)
                    entry = dict(page=page_no, into=target.self_ref,
                                 fragments=[t.self_ref for t, _ in matched], bbox=bounds,
                                 transferred_footnotes=sorted(movable_notes),
                                 method='same_page_fragments_inside_one_source_ruled_table')
                    manifest['source_table_boundary_repairs'].append(entry)
                    api.diagnostic_event('SOURCE_TABLE_COALESCE', **entry)
                    native_text = api.cell_text_key(api.table_profile(verified[0])['text'])
                    caption_refs = {ref.cref for ref in target.captions}
                    for text in doc.texts:
                        reg = api.item_region(text, doc)
                        key = api.cell_text_key(text.text)
                        if (reg and reg[0] == page_no and text.self_ref not in caption_refs and len(key) >= 8
                                and _coverage(reg[1], bounds) >= .95 and key in native_text):
                            excluded.add(text.self_ref)
                    matched = [(target, bounds)]

            for cell in source_table.cells:
                if cell[2]-cell[0] < .98*(box[2]-box[0]):
                    continue
                above = [(item, b) for item, b in matched
                         if abs(cell[1]-b[3]) <= 8 and cell[3] > b[3]+8]
                if len(above) != 1:
                    continue
                glyphs = _cell_glyphs(reader, cell)
                if any(not g.get('upright', True) or re.search(r'\(cid:|[\ufffd\x00-\x08]', g['text']) for g in glyphs):
                    api.diagnostic_event('SOURCE_TABLE_NOTES_KEEP', page=page_no, bbox=cell,
                                         reason='source_glyph_encoding_or_direction_not_verified')
                    continue
                lines = api.native_cell_lines(glyphs)
                text = '\n'.join(line['text'] for line in lines)
                if not re.match(r'^\s*(?:<\*+>|Примечани[ея]\b)', text, re.I):
                    continue
                # An already positioned text item, even an incomplete one, is
                # not silently duplicated or replaced by this recovery route.
                existing = [item for item in doc.texts if item.self_ref not in excluded
                            and (reg := api.item_region(item, doc)) and reg[0] == page_no
                            and _coverage(reg[1], cell) >= .5]
                if existing:
                    api.diagnostic_event('SOURCE_TABLE_NOTES_KEEP', page=page_no, bbox=cell,
                                         reason='existing_positioned_text', refs=[t.self_ref for t in existing])
                    continue
                paragraphs = _note_paragraphs(lines, api)
                plain = '\n'.join(paragraphs)
                prov = ProvenanceItem(page_no=page_no, charspan=(0, len(plain)),
                    bbox=BoundingBox(l=cell[0], t=cell[1], r=cell[2], b=cell[3], origin=CoordOrigin.TOPLEFT))
                item = doc.insert_text(sibling=above[0][0], after=True, label=DocItemLabel.TEXT,
                                       text=plain, orig=plain, prov=prov)
                note_routes[item.self_ref] = ''.join('<p>'+escape(p)+'</p>' for p in paragraphs)
                entry = dict(ref=item.self_ref, table=above[0][0].self_ref, page=page_no, bbox=list(cell), glyphs=len(glyphs),
                             method='literal_notes_in_source_fullwidth_cell', text=plain)
                manifest['recovered_source_notes'].append(entry)
                api.diagnostic_event('SOURCE_TABLE_NOTES_RESTORED', **entry)
    return note_routes, native_candidates


def attach_recovered_source_notes(routes, excluded, note_routes, manifest, api):
    """Keep recovered notes immediately after their final joined table.

    Page layout order can put the next appendix before a continued table's
    final fragment. Binding the recovered block to its verified table avoids
    leaving those notes behind when the fragments move or join.
    """
    parents = {entry['from']: entry['into'] for entry in manifest.get('joined_tables', [])}
    for note in [*manifest.get('recovered_source_notes', []), *manifest.get('verified_table_footnotes', [])]:
        target, seen = note['table'], set()
        while target in parents and target not in seen:
            seen.add(target); target = parents[target]
        content = note_routes.get(note['ref']) or '<p>'+escape(note['text'])+'</p>'
        if not content or not routes.get(target, ''):
            raise RuntimeError('Recovered source notes have no verified output table')
        note_key = api.cell_text_key(BeautifulSoup(content, 'html.parser').get_text(' ', strip=True))
        table_key = api.cell_text_key(BeautifulSoup(routes[target], 'html.parser').get_text(' ', strip=True))
        if not note_key or note_key not in table_key:
            routes[target] += '<div class="pdf-source-table-notes">'+content+'</div>'
        excluded.add(note['ref'])
        note['output_table'] = target
        api.diagnostic_event('SOURCE_TABLE_NOTES_ATTACHED', ref=note['ref'], table=target, page=note['page'])


def recover_uncovered_ocr_table_text(doc, reader, records, routes, excluded, note_routes, manifest, api):
    """Keep full-width semantic cells omitted by a native table replacement.

    OCR sometimes encloses several physical grids, section headings and notes
    in one table. Replacing its data grids must not delete the intervening
    text. Only literal source-glyph prose in a gap between the selected grids
    is recovered. Existing OCR math can occupy source-image gaps; it is kept
    as recognized math rather than reconstructed from the prose.
    """
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, ProvenanceItem

    recovered = manifest.setdefault('recovered_ocr_table_blocks', [])
    if reader is None:
        return
    # These alphabet pairs are visually identical in source fonts. This is
    # evidence matching only: the retained OCR and source text are not edited.
    similar_letters = str.maketrans('АВСЕНКМОРТХаесорх', 'ABCEHKMOPTXaecopx')

    def key(value):
        return api.cell_text_key(value).translate(similar_letters)

    def anchored(cell, native, images):
        parsed = BeautifulSoup(str(cell), 'html.parser')
        math = parsed.find_all('math')
        literal = BeautifulSoup(str(cell), 'html.parser')
        for node in literal.find_all('math'):
            rendered = api.latex_math(node.get('data-latex') or node.get_text(), 'inline')
            if rendered is None:
                return False
            node.replace_with(BeautifulSoup(rendered, 'html.parser').get_text('', strip=True))
        if len(key(native)) >= 24 and key(literal.get_text(' ', strip=True)) == key(native):
            return True
        if len(math) > len(images):
            return False
        for node in math:
            node.replace_with('\ue000')
        segments = [key(part) for part in parsed.get_text(' ', strip=True).split('\ue000')]
        if sum(map(len, segments)) < 24:
            return False
        source = key(native)
        # A formula wildcard is limited to a small local expression, and all
        # surrounding prose must agree. It cannot swallow an omitted sentence.
        pattern = '.{0,120}?'.join(re.escape(part) for part in segments)
        return (sum(map(len, segments)) >= .8*len(source)
                and re.fullmatch(pattern, source, re.S) is not None)

    for record in records:
        if '<table' not in record.get('html', '').lower():
            continue
        page_no, region = record['page_no'], record['bbox']
        selected = []
        for entry in manifest.get('tables', []):
            if (entry['page'] == page_no and entry['method'] == 'pdf_geometry'
                    and entry['ref'] not in excluded and '<table' in routes.get(entry['ref'], '')
                    and _coverage(entry['bbox'], [region[0]-6, region[1]-6, region[2]+6, region[3]+6]) >= .95):
                selected.append(entry)
        selected.sort(key=lambda entry: entry['bbox'][1])
        if not selected or any(a['bbox'][3] > b['bbox'][1]+2 for a, b in zip(selected, selected[1:])):
            continue
        cells = []
        parsed = BeautifulSoup(record['html'], 'html.parser')
        for row_index, row in enumerate(parsed.table.find_all('tr')):
            occupied = [cell for cell in row.find_all(['td', 'th'], recursive=False)
                        if cell.get_text(' ', strip=True)]
            if len(occupied) == 1 and int(occupied[0].get('colspan', 1)) > 1:
                cells.append((row_index, occupied[0]))
        if not cells:
            continue
        reader.load_page(page_no)
        if any(reader.is_page_scan(obj, [0, 0, reader.page.width, reader.page.height]) for obj in reader.page.images):
            continue
        intervals = [(region[1], selected[0]['bbox'][1], None, selected[0])]
        intervals += [(a['bbox'][3], b['bbox'][1], a, b) for a, b in zip(selected, selected[1:])]
        intervals.append((selected[-1]['bbox'][3], region[3]+3, selected[-1], None))
        for top, bottom, previous, following in intervals:
            if bottom-top < 3:
                continue
            glyphs = _cell_glyphs(reader, [region[0]-3, top, region[2]+3, bottom])
            if (not glyphs or any(not g.get('upright', True)
                    or re.search(r'\(cid:|[\ufffd\x00-\x08]', g['text']) for g in glyphs)):
                continue
            lines = api.native_cell_lines(glyphs)
            native = ' '.join(line['text'] for line in lines)
            if len(key(native)) < 24:
                continue
            images = [obj for obj in reader.page.images if top <= (obj['top']+obj['bottom'])/2 <= bottom
                      and region[0]-3 <= (obj['x0']+obj['x1'])/2 <= region[2]+3]
            matched = [(row, cell) for row, cell in cells if anchored(cell, native, images)]
            if len(matched) != 1:
                continue
            row, cell = matched[0]
            text = api.semantic_ocr_text(str(cell))
            box = [min(g['x0'] for g in glyphs), min(g['top'] for g in glyphs),
                   max(g['x1'] for g in glyphs), max(g['bottom'] for g in glyphs)]
            if any((loc := api.item_region(item, doc)) and loc[0] == page_no
                   and _coverage(loc[1], [box[0]-3, box[1]-3, box[2]+3, box[3]+3]) >= .8
                   and key(item.text) in {key(text), key(native)} for item in doc.texts
                   if item.self_ref not in excluded):
                continue
            output = BeautifulSoup(str(cell), 'html.parser')
            for node in output.find_all('math'):
                rendered = api.latex_math(node.get('data-latex') or node.get_text(), 'inline')
                if rendered is None:
                    raise RuntimeError('Retained OCR table prose contains unrenderable math')
                node.replace_with(BeautifulSoup(rendered, 'html.parser').math)
            for node in list(output.find_all(True)):
                if node.name in {'td', 'th'}:
                    node.unwrap()
                elif node.name not in {'br', 'p', 'b', 'strong', 'i', 'em', 'sub', 'sup', 'math'} and not node.find_parent('math'):
                    node.unwrap()
                elif node.name != 'math' and not node.find_parent('math'):
                    node.attrs = {}
            html = str(output) if output.find('p') else '<p>'+str(output)+'</p>'
            sibling_ref = (following or previous)['ref']
            sibling = next(table for table in doc.tables if table.self_ref == sibling_ref)
            item = doc.insert_text(sibling=sibling, after=following is None, label=DocItemLabel.TEXT,
                text=text, orig=text, prov=ProvenanceItem(page_no=page_no, charspan=(0, len(text)),
                    bbox=BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], origin=CoordOrigin.TOPLEFT)))
            note_routes[item.self_ref] = html
            entry = dict(ref=item.self_ref, page=page_no, bbox=box, text=text, html=html,
                source_record_bbox=list(region), source_row=row,
                before_table=following['ref'] if following else None,
                after_table=previous['ref'] if previous else None, glyphs=len(glyphs),
                method='source_glyph_prose_anchors_and_retained_ocr_math')
            recovered.append(entry)
            if previous and not following:
                manifest.setdefault('recovered_source_notes', []).append(dict(entry, table=previous['ref']))
            api.diagnostic_event('OCR_TABLE_UNCOVERED_TEXT_RESTORED', **entry)


def join_source_footnote_continuations(doc, reader, excluded, manifest, api):
    """Recover an unfinished footnote mislabelled as next-page furniture.

    Both complete fragments must match source glyphs. Adjacent pages, aligned
    margins, matching fonts and a unique unfinished footnote are required;
    recurring furniture and content preceded by a new heading are rejected.
    """
    from collections import Counter
    from statistics import median
    from docling_core.types.doc import ContentLayer, DocItemLabel
    proofs = manifest.setdefault('joined_source_footnotes', [])
    preserved = manifest.setdefault('verified_table_footnotes', [])
    if reader is None:
        return
    furniture = {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
    repeats = Counter(api.furniture_key(t.text) for t in doc.texts if t.label in furniture)

    def source_fragment(item, prov):
        page = reader.load_page(prov.page_no)
        if any(reader.is_page_scan(o, [0, 0, page.width, page.height]) for o in page.images):
            return None
        box = prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
        glyphs = _cell_glyphs(reader, [box.l-4, box.t-4, box.r+4, box.b+4])
        if not glyphs or any(not g.get('upright', True) or re.search(r'\(cid:|\ufffd', g['text']) for g in glyphs):
            return None
        expected = item.text if len(item.prov) == 1 else item.text[slice(*prov.charspan)]
        native = ' '.join(line['text'] for line in api.native_cell_lines(glyphs))
        if api.furniture_key(native) != api.furniture_key(expected):
            return None
        fonts = {g.get('fontname', '').split('+')[-1] for g in glyphs if g['text'].strip()}
        return dict(glyphs=len(glyphs), fonts=fonts, size=median(g['size'] for g in glyphs))

    pairs = []
    for note in doc.texts:
        if (note.self_ref in excluded or note.label != DocItemLabel.FOOTNOTE or not note.prov
                or not re.match(r'^\s*<\*+>', note.text)):
            continue
        last = note.prov[-1]
        old_size = doc.pages[last.page_no].size
        old = last.bbox.to_top_left_origin(old_size.height)
        if old.b < .8*old_size.height or old.r-old.l < .65*old_size.width:
            continue
        for tail in doc.texts:
            if (tail.self_ref in excluded or len(tail.prov) != 1
                    or tail.prov[0].page_no != last.page_no+1
                    or tail.label not in furniture | {DocItemLabel.TEXT}
                    or repeats[api.furniture_key(tail.text)] > 1
                    or not api.paragraph_continuation(note.text, tail.text)):
                continue
            first = tail.prov[0]
            size = doc.pages[first.page_no].size
            box = first.bbox.to_top_left_origin(size.height)
            if (box.b > .15*size.height or box.r-box.l < .6*size.width
                    or abs(old.l/old_size.width-box.l/size.width) > .03):
                continue
            # A preceding body heading/table means a new source context.
            if any((reg := api.item_region(other, doc)) and reg[0] == first.page_no
                    and reg[1][3] < box.t-2 and other.self_ref not in excluded
                    and getattr(other, 'label', None) not in furniture
                    for other in [*doc.texts, *doc.tables, *doc.pictures] if other is not tail):
                continue
            a, b = source_fragment(note, last), source_fragment(tail, first)
            if not a or not b or not a['fonts'] & b['fonts'] or not .8 <= a['size']/b['size'] <= 1.25:
                continue
            pairs.append((note, tail, a['glyphs'], b['glyphs']))
    left_counts = Counter(note.self_ref for note, _, _, _ in pairs)
    right_counts = Counter(tail.self_ref for _, tail, _, _ in pairs)
    for note, tail, left_glyphs, right_glyphs in pairs:
        if left_counts[note.self_ref] != 1 or right_counts[tail.self_ref] != 1:
            continue
        separator = api.paragraph_separator(note.text)
        left_text = note.text.rstrip('\u00ad')
        offset = len(left_text)+len(separator)
        note.prov[-1].charspan = (note.prov[-1].charspan[0], len(left_text))
        original_label = str(tail.label)
        note.text = left_text+separator+tail.text
        note.orig = note.orig.rstrip('\u00ad')+separator+tail.orig
        note.content_layer = ContentLayer.BODY
        for prov in tail.prov:
            copied = prov.model_copy(deep=True)
            copied.charspan = (offset, offset+len(tail.text))
            note.prov.append(copied)
        excluded.add(tail.self_ref)
        proof = dict(into=note.self_ref, source=tail.self_ref,
                     pages=[note.prov[-2].page_no, tail.prov[0].page_no],
                     source_label=original_label, text=tail.text,
                     verified_glyphs=[left_glyphs, right_glyphs],
                     method='exact_source_fragments_and_unique_unfinished_footnote')
        proofs.append(proof)
        api.diagnostic_event('SOURCE_FOOTNOTE_CONTINUATION_JOINED', **proof)
    # Routed table serialization does not emit all its original footnote
    # children. Preserve every source-verified table footnote, including a
    # single-page note whose table has no continued footnote family.
    tables = {item.self_ref for item in doc.tables}
    for note in doc.texts:
        if (note.self_ref in excluded or note.label != DocItemLabel.FOOTNOTE
                or not note.parent or note.parent.cref not in tables):
            continue
        fragments = [source_fragment(note, prov) for prov in note.prov]
        if not fragments or not all(fragments):
            continue
        preserved.append(dict(ref=note.self_ref, table=note.parent.cref,
            page=note.prov[0].page_no, pages=[p.page_no for p in note.prov], text=note.text,
            verified_glyphs=[part['glyphs'] for part in fragments],
            method='all_footnote_fragments_match_source_glyphs'))


def mark_terminal_source_tables(doc, reader, manifest, api):
    """A late table can end well above 85% when the remaining source is blank."""
    from docling_core.types.doc import ContentLayer, DocItemLabel
    if reader is None:
        return
    furniture = {}
    for item in doc.texts:
        if item.label in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER} or item.content_layer == ContentLayer.FURNITURE:
            reg = api.item_region(item, doc)
            if reg:
                furniture.setdefault(reg[0], []).append(reg[1])
    for entry in manifest['tables']:
        geo = entry.get('join_geometry')
        if not geo:
            continue
        page = reader.load_page(entry['page'])
        box = geo['bbox']
        if box[3] < .5*page.height or any(reader.is_page_scan(obj, [0, 0, page.width, page.height]) for obj in page.images):
            continue
        def after(obj, graphic=False):
            b = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
            return ((b[3] if graphic else b[1]) > box[3]+3 and
                    not any(_coverage(b, f) >= .8 for f in furniture.get(entry['page'], ())))
        tail = [g for g in reader.chars if g['text'].strip() and after(g)]
        objects = [o for o in page.images+page.curves+page.lines+page.rects
                   if not api.is_white_pdf_background(o) and after(o, graphic=True)]
        geo['terminal_source_table'] = not tail and not objects
        api.diagnostic_event('TABLE_SOURCE_TERMINAL', ref=entry['ref'], page=entry['page'],
                             terminal=geo['terminal_source_table'], trailing_glyphs=len(tail),
                             trailing_graphics=len(objects))


def merge_source_word_tail(left, right, api):
    """Reconnect a literal hyphenated first-column word across a joined page.

    Extend the existing first-column rowspan; other columns, graphics and
    formulas stay in their original rows. A dictionary-confirmed Russian
    word and both boundary cells' occupancy are required.
    """
    old, new = api.table_profile(str(left)), api.table_profile(str(right))
    a = next((c for c in old['cells'] if c['column'] == 0 and c['column_span'] == 1
              and c['row']+c['row_span'] == old['rows']), None)
    b = next((c for c in new['cells'] if c['row'] == 0 and c['column'] == 0 and c['column_span'] == 1), None)
    if not a or not b:
        return False
    prefix = re.search(r'([А-Яа-яЁё]+)[-\u00ad]\s*$', a['text'])
    suffix = re.match(r'([а-яё]+)', b['text'])
    if (not prefix or not suffix or
            api.joined_russian_word(prefix[0].strip()+suffix[0]) != prefix[1]+suffix[1]):
        return False
    an = left.find_all(['td', 'th'])[old['cells'].index(a)]
    bn = right.find_all(['td', 'th'])[new['cells'].index(b)]
    if an.find(['img', 'math']) or bn.find(['img', 'math']):
        return False
    strings = an.find_all(string=True)
    if not strings or not re.search(r'[-\u00ad]\s*$', str(strings[-1])):
        return False
    strings[-1].replace_with(re.sub(r'[-\u00ad]\s*$', '', str(strings[-1])))
    for child in list(bn.contents):
        an.append(child.extract())
    an['rowspan'] = str(a['row_span']+b['row_span'])
    bn.decompose()
    return True


def split_source_subrows(cells, source_rows, header_rows, api):
    """Align descriptors with at least two independent value-column baselines.

    Literal inequalities and units count as values; arbitrary wrapped prose
    does not. Source rowspans are remapped after every inserted logical row.
    Physical cell bboxes and glyphs remain in the reader's original metadata.
    """
    from statistics import median
    plans, fallback, counts, proofs = {}, {}, [1]*source_rows, []
    width = max((c['column']+c['column_span'] for c in cells), default=0)
    number = re.compile(r'\d+(?:[.,]\d+)?\s*(?:%|мм|mm)?$')
    inequality = re.compile(r'[A-Za-zА-Яа-яЁёΑ-Ωα-ω]\s*(?:<=|>=|≤|≥|<|>|=)\s*'
                            r'[\d.,\s+−–—*/()A-Za-zА-Яа-яЁёΑ-Ωα-ω]+$')
    def value(line):
        return bool(re.search(r'\d', line['text']) and
                    (number.fullmatch(line['text']) or inequality.fullmatch(line['text'])))
    for row in range(header_rows, source_rows):
        originals = sorted((c for c in cells if c['row'] == row), key=lambda c: c['column'])
        candidates = []
        expected_column = width-1
        for cell in reversed(originals):
            lines = [l for l in cell['lines'] if l['text'].strip()]
            if (cell['column'] != expected_column or cell['column_span'] != 1 or cell['row_span'] != 1
                    or cell.get('source_images') or cell.get('mapped_ocr_html')
                    or len(lines) < 2 or not all(value(l) for l in lines)):
                break
            if candidates and len(lines) != len(candidates[0]['lines']):
                break
            candidates.append(dict(cell, lines=lines))
            expected_column -= 1
        if len(candidates) >= 2:
            count = len(candidates[0]['lines'])
            size = median(l['size'] for c in candidates for l in c['lines'])
            anchors = [median(c['lines'][i]['y'] for c in candidates) for i in range(count)]
            aligned = all(abs(c['lines'][i]['y']-anchors[i]) <= size*.4
                          for c in candidates for i in range(count))
            aligned = aligned and all(b-a >= size*.6 for a, b in zip(anchors, anchors[1:]))
            descriptor = next((c for c in reversed(originals) if c['column'] < min(v['column'] for v in candidates)
                               and c['column_span'] == c['row_span'] == 1 and not c.get('source_images')
                               and not c.get('mapped_ocr_html') and len(c['lines']) >= count), None)
            if aligned and descriptor:
                groups = [[] for _ in anchors]
                for line in descriptor['lines']:
                    index = next((i for i, y in enumerate(anchors) if line['y'] <= y+size*.4), count-1)
                    groups[index].append(line)
                matches = sum(any(abs(l['y']-y) <= size*.5 for l in group) for y, group in zip(anchors, groups))
                if all(groups) and matches >= 2:
                    plans[row] = dict(values={c['column']: c['lines'] for c in candidates},
                                      descriptor=descriptor['column'], groups=groups)
                    counts[row] = count
                    proofs.append(dict(source_row=row, records=count, anchors=anchors,
                        value_columns=sorted(c['column'] for c in candidates),
                        descriptor_column=descriptor['column'], method='matching_source_value_baselines'))
                    continue
        old = api.native_subrows(originals) if len(originals) == width else None
        if old:
            fallback[row] = old
            counts[row] = len(old)
    offsets = [0]
    for count in counts:
        offsets.append(offsets[-1]+count)
    result = []
    for cell in cells:
        row = cell['row']
        if row in fallback:
            continue
        plan = plans.get(row)
        if plan and cell['row_span'] == 1 and cell['column'] in plan['values']:
            for i, line in enumerate(plan['values'][cell['column']]):
                result.append(dict(cell, row=offsets[row]+i, row_span=1, lines=[line]))
        elif plan and cell['row_span'] == 1 and cell['column'] == plan['descriptor']:
            for i, group in enumerate(plan['groups']):
                result.append(dict(cell, row=offsets[row]+i, row_span=1, lines=group))
        else:
            result.append(dict(cell, row=offsets[row],
                               row_span=offsets[row+cell['row_span']]-offsets[row]))
    for row, groups in fallback.items():
        for i, group in enumerate(groups):
            result.extend(dict(c, row=offsets[row]+i, row_span=1) for c in group)
    return sorted(result, key=lambda c: (c['row'], c['column'])), offsets[header_rows], proofs


def compact_empty_columns(fragment, api):
    """Remove explicit, wholly empty trailing cells; retain all visible values.

    Full-width body notes can shrink. A populated merged header, image, math
    element, or column with fewer than two explicit empty cells blocks removal.
    """
    profile = api.table_profile(fragment)
    if profile['issues'] or profile['columns'] < 3:
        return fragment, None
    soup = BeautifulSoup(fragment, 'html.parser')
    table = soup.table
    nodes = table.find_all(['td', 'th'])
    width, keep = profile['columns'], profile['columns']
    for column in range(width-1, 0, -1):
        direct = 0
        for cell, node in zip(profile['cells'], nodes):
            if not cell['column'] <= column < cell['column']+cell['column_span']:
                continue
            populated = bool(cell['text'].strip() or node.find(['img', 'math', 'svg', 'table']))
            full_note = cell['column'] == 0 and cell['column_span'] == width and not cell['header']
            if cell['column_span'] == 1:
                if populated:
                    break
                direct += 1
            elif populated and not full_note:
                break
        else:
            if direct >= 2:
                keep = column
                continue
        break
    if keep == width:
        return fragment, None
    for cell, node in zip(profile['cells'], nodes):
        if cell['column'] >= keep:
            node.decompose()
        elif cell['column']+cell['column_span'] > keep:
            span = keep-cell['column']
            if span == 1:
                node.attrs.pop('colspan', None)
            else:
                node['colspan'] = str(span)
    columns = table.find_all('col')
    for col in columns[keep:]:
        col.decompose()
    widths = [re.search(r'width\s*:\s*([\d.]+)%', c.get('style', '')) for c in columns[:keep]]
    if widths and all(widths):
        total = sum(float(m[1]) for m in widths)
        if total:
            for col, match in zip(columns[:keep], widths):
                col['style'] = re.sub(r'width\s*:\s*[\d.]+%', f'width:{100*float(match[1])/total:.3f}%', col['style'])
    if 'style' in table.attrs:
        table['style'] = re.sub(r'min-width\s*:\s*[\d.]+em', f'min-width:{keep*6}em', table['style'])
    result = str(table)
    checked = api.table_profile(result)
    if checked['issues'] or checked['columns'] != keep:
        return fragment, None
    before = [c['text'] for c in profile['cells'] if c['text'].strip()]
    after = [c['text'] for c in checked['cells'] if c['text'].strip()]
    if before != after:
        return fragment, None
    return result, dict(source_columns=width, semantic_columns=keep,
                        method='remove_explicit_empty_trailing_columns')


def compact_table_routes(doc, routes, manifest, api):
    from docling_core.types.doc import TableCell, TableData
    entries = {e['ref']: e for e in manifest['tables']}
    for item in doc.tables:
        fragment = routes.get(item.self_ref, '')
        if '<table' not in fragment:
            continue
        content, repair = compact_empty_columns(fragment, api)
        if repair is None:
            continue
        routes[item.self_ref] = content
        profile = api.table_profile(content)
        item.data = TableData(num_rows=profile['rows'], num_cols=profile['columns'], table_cells=[
            TableCell(text=c['text'], row_span=c['row_span'], col_span=c['column_span'],
                      start_row_offset_idx=c['row'], end_row_offset_idx=c['row']+c['row_span'],
                      start_col_offset_idx=c['column'], end_col_offset_idx=c['column']+c['column_span'],
                      column_header=c['header']) for c in profile['cells']])
        entries[item.self_ref].update(html=content, selected=profile, empty_column_repair=repair)
        api.diagnostic_event('TABLE_EMPTY_COLUMNS_COMPACTED', ref=item.self_ref, **repair)


def _read_equation(data, metadata, recognizer, api):
    """Reuse Surya's existing equation reader; never accept raster fallback."""
    context = dict(stage='formula_reader', page=metadata.get('source_page'),
                   bbox=metadata.get('source_bbox'), element=metadata.get('element'))
    try:
        result = recognizer(data, dict(metadata, formula_item=True))
    except Exception as original:
        api.diagnostic_event('PDF_FORMULA_READER_FAILED', **context, error=str(original))
        error = RuntimeError(f'Equation reader failed: {type(original).__name__}: {original}')
        error.context = context
        raise error from original
    parsed = BeautifulSoup(result or '', 'html.parser')
    if parsed.math is None or parsed.find('img') or not parsed.math.find(True):
        error = RuntimeError(f"Equation has no verified MathML: {metadata.get('media_name')}")
        error.context = context
        raise error
    return str(parsed.math)


def math_signature(fragment):
    """Compare characters and script/fraction structure, ignoring font styling.

    Only transparent MathML wrappers and adjacent text-token boundaries are
    ignored. Latin/Cyrillic/Greek letters, case and numeric values stay distinct.
    """
    import unicodedata
    soup = BeautifulSoup(fragment, 'html.parser')
    # TeX ignores spaces between digit tokens: ``1 0^{...}`` is printed as
    # the same numeral as ``10^{...}``, but latex2mathml may attach the script
    # to a separate final <mn>0</mn>. Canonicalize that numeric atom only in
    # this comparison copy. Operators, mspace, decimals and letters are barriers.
    for script in soup.find_all(['msup','msub','msubsup']):
        base=script.find(True,recursive=False)
        if (base is None or base.name!='mn' or base.find(True)
                or not re.fullmatch(r'[0-9]+',base.get_text().strip())):
            continue
        digits=base.get_text().strip()
        while True:
            previous=script.previous_sibling
            while previous is not None and not getattr(previous,'name',None) and not str(previous).strip():
                previous=previous.previous_sibling
            if (getattr(previous,'name',None)!='mn' or previous.find(True)
                    or not re.fullmatch(r'[0-9]+',previous.get_text().strip())):
                break
            digits=previous.get_text().strip()+digits
            previous.extract()
        base.string=digits
    def sequence(parts):
        result = []
        for part in parts:
            if part is None:
                continue
            values = part[1] if part[0] == 'sequence' else [part]
            for value in values:
                if result and result[-1][0] == value[0] == 'text':
                    result[-1] = ('text', result[-1][1]+value[1])
                else:
                    result.append(value)
        if not result:
            return None
        return result[0] if len(result) == 1 else ('sequence', tuple(result))
    def visit(node):
        if not getattr(node, 'name', None):
            return None
        children = [visit(c) for c in node.children if getattr(c, 'name', None)]
        if node.name in {'math', 'mrow', 'mstyle', 'semantics'}:
            return sequence(children)
        if node.name == 'mtd':
            # A cell keeps its structural boundary, while token boundaries
            # inside it have the same semantics as a transparent mrow. TeX
            # matrix cells may contain adjacent mn tokens directly.
            content = sequence(children)
            return ('mtd', (content,) if content is not None else ())
        if node.name in {'mi', 'mn', 'mo', 'mtext'}:
            value = re.sub(r'\s+', '', node.get_text())
            value = ''.join(unicodedata.normalize('NFKD', c) if
                            unicodedata.name(c, '').startswith('MATHEMATICAL ') else c for c in value)
            return ('text', value) if value else None
        if node.name in {'msub', 'msup', 'msubsup'}:
            if len(children) < 2:
                return ('invalid_script',)
            base, sub, sup = children[0], None, None
            if node.name == 'msub': sub = children[1]
            elif node.name == 'msup': sup = children[1]
            else: sub, sup = children[1:3]
            if base and base[0] == 'script':
                _, base, old_sub, old_sup = base
                sub, sup = sub or old_sub, sup or old_sup
            if base is None and sub is None and sup is None:
                return None
            return ('script', base, sub, sup)
        if node.name == 'mspace':
            return ('linebreak',) if node.get('linebreak') else None
        return (node.name, tuple(c for c in children if c is not None))
    return visit(soup.math) if soup.math else None


@lru_cache(maxsize=1)
def _code_formula_model(device):
    """Legacy private name retained for strict-reader callback compatibility."""
    from docling_p2t import model
    return model(device)


@lru_cache(maxsize=1)
def resolve_formula_device():
    """Choose once, before model allocation changes the available GPU memory.

    Rechecking free memory for every asset can move verification to CPU after
    its own CUDA model has been loaded, evicting the cached model each time.
    """
    from docling_p2t import resolve_device
    return resolve_device()


@lru_cache(maxsize=256)
def _code_formula_readings(data, device):
    """Decode complete formulas within the model's actual context capacity.

    A fixed 512-token/20-second cap used to return a plausible but truncated
    transcription.  Natural EOS now ends decoding; capacity exhaustion is an
    explicit verification failure rather than accepted partial MathML.
    """
    return _code_formula_model(device).readings(data,(1.,.65))


def load_formula_corrections(path, source_sha256):
    if path is None:
        return []
    payload = json.loads(path.read_text('utf-8'))
    if payload.get('schema') != 1 or not isinstance(payload.get('corrections'), list):
        raise ValueError('Unsupported reviewed formula correction schema')
    selected = []
    for entry in payload['corrections']:
        if not all(re.fullmatch(r'[0-9a-f]{64}', entry.get(key, '')) for key in ('source_sha256', 'asset_sha256')):
            raise ValueError('Reviewed formula corrections need source and asset SHA256')
        box = entry.get('bbox', [])
        if (len(box) != 4 or not all(isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 for x in box)
                or box[2] <= box[0] or box[3] <= box[1] or not isinstance(entry.get('page'), int)
                or entry['page'] < 1 or not entry.get('latex') or entry.get('method') != 'visual_source_review'):
            raise ValueError('Invalid reviewed formula correction record')
        if entry['source_sha256'] == source_sha256:
            selected.append(entry)
    return selected


def _reviewed_matches(manifest, page, box):
    return [entry for entry in manifest.get('reviewed_formula_corrections', [])
            if page == entry['page'] and box and max(abs(a-b) for a, b in zip(box, entry['bbox'])) <= .25]


def _code_formula_candidate(raw, api, source_row_proof=None):
    """Preserve independently printed source rows in a bare multiline reading.

    Newlines are otherwise TeX whitespace. They establish row boundaries only
    when complete multiglyph source rows prove exactly the same allocation.
    Explicit TeX environments and incomplete groups keep the original parser.
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    row_count = (source_row_proof or {}).get('row_count')
    if (isinstance(row_count, int) and row_count >= 2 and len(lines) == row_count
            and not re.search(r'\\(?:begin|end)\s*\{', raw) and r'\\' not in raw):
        balanced = True
        for line in lines:
            depth = 0
            for group in re.finditer(r'(?<!\\)[{}]', line):
                depth += 1 if group[0] == '{' else -1
                if depth < 0:
                    balanced = False
                    break
            if depth or not balanced:
                balanced = False
                break
        if balanced:
            rows = [api.latex_math(line, 'inline') for line in lines]
            if all(rows):
                from docling_office_math import parsed_formula
                rendered = parsed_formula(''.join('<div>'+row+'</div>' for row in rows),
                    api.latex_math, source_row_count=row_count)
                if rendered is not None:
                    return rendered, 'source_nonempty_lines_as_mtable'
    return api.latex_math(raw, 'inline'), 'latex_math'


def verified_equation(data, metadata, recognizer, manifest, api):
    if manifest.get('primary_ocr_only'):
        raise RuntimeError('Auxiliary content OCR disabled: preserve the primary Surya formula or source image')
    recognition_digest = hashlib.sha256(data).hexdigest()
    digest = metadata.get('source_asset_sha256', recognition_digest)
    page, box = metadata.get('source_page'), metadata.get('source_bbox')
    matches = _reviewed_matches(manifest, page, box)
    if len(matches) > 1:
        raise RuntimeError('Ambiguous reviewed formula corrections')
    proof = dict(page=page, bbox=box, asset_sha256=digest)
    if recognition_digest != digest:
        proof.update(recognition_asset_sha256=recognition_digest,
                     recognition_preprocessing=metadata.get('recognition_preprocessing'))
    if matches:
        entry = matches[0]
        if entry['asset_sha256'] != digest:
            raise RuntimeError(f'Reviewed formula asset changed on page {page}; no HTML published')
        rendered = api.latex_math(entry['latex'], 'inline')
        if rendered is None:
            raise RuntimeError('Reviewed formula correction does not render as MathML')
        proof.update(method='visual_source_review', latex=entry['latex'], evidence=entry.get('evidence'))
    elif manifest.get('formula_reader')=='p2t':
        from docling_p2t import read,MODEL_ID,REVISION
        device=manifest.get('formula_verification_device','auto')
        if device=='auto':device=resolve_formula_device()
        raw=read(data,device)
        from docling_pdf_quantities import source_multiline_ink
        rendered,rendering=_code_formula_candidate(raw,api,source_multiline_ink(data))
        if rendered is None:
            error=RuntimeError('Pix2Text transcription cannot be rendered as complete MathML')
            error.context=dict(page=page,bbox=box,stage='formula_recognition',element=metadata.get('element'))
            raise error
        proof.update(method='pix2text_mfr_1_5',model=MODEL_ID,model_revision=REVISION,
            verification_device=device,latex=raw,rendering=rendering)
    else:
        rendered = _read_equation(data, metadata, recognizer, api)
        if manifest.get('formula_reader', 'verified') == 'verified':
            device = manifest.get('formula_verification_device', 'auto')
            if device == 'auto':
                device = resolve_formula_device()
            proof['verification_device'] = device
            try:
                readings = _code_formula_readings(data, device)
            except Exception as original:
                context = dict(page=page, bbox=[float(x) for x in box] if box else None,
                               stage='formula_verification', element=metadata.get('element'))
                api.diagnostic_event('PDF_FORMULA_VERIFIER_FAILED', **context,
                                     error_type=type(original).__name__, error=str(original))
                error = RuntimeError('Independent formula verification failed: '+str(original))
                error.context = context
                raise error from original
            from docling_pdf_quantities import source_multiline_ink
            source_rows = source_multiline_ink(data)
            candidate_results = [_code_formula_candidate(raw, api, source_rows) for raw in readings]
            candidates = [result[0] for result in candidate_results]
            if any(result[1] == 'source_nonempty_lines_as_mtable' for result in candidate_results):
                proof.update(codeformula_source_rows=source_rows,
                             codeformula_rendering=[result[1] for result in candidate_results])
            signature = math_signature(rendered)
            signatures = [math_signature(raw) if raw else None for raw in candidates]
            literal_proof=None
            text_index_proof=None
            indexed_atoms_proof=None
            if (metadata.get('source_literal_index') and
                    (not signature or any(other != signature for other in signatures))):
                from docling_source_text import verify_literal
                parsed=BeautifulSoup(rendered,'html.parser').math
                if not parsed.find(['msub','msup','msubsup','mfrac','mtable','mover','munder','msqrt','mroot']):
                    try:
                        literal_proof=verify_literal(data,parsed.get_text('',strip=True),metadata.get('literal_context'))
                    except Exception as original:
                        api.diagnostic_event('PDF_LITERAL_INDEX_VERIFIER_FAILED',page=page,bbox=box,
                            error_type=type(original).__name__,reason=str(original))
                if literal_proof:
                    proof.update(method='surya_and_independent_literal_text_agree',
                                 literal_index=literal_proof,initial_codeformula=list(readings))
            if not literal_proof and (not signature or any(other != signature for other in signatures)):
                from docling_pdf_formula_retry import embedded_image_views
                initial = dict(surya=BeautifulSoup(rendered, 'html.parser').math.get('data-latex',''),
                               codeformula=list(readings))
                for retry_data, retry_proof in embedded_image_views(recognizer, metadata):
                    retry_metadata=dict(metadata, recognition_preprocessing=retry_proof)
                    api.diagnostic_event('PDF_FORMULA_SOURCE_RETRY',page=page,bbox=box,
                        initial_readings=initial,source_asset_sha256=digest,**retry_proof)
                    try:
                        retry_readings=_code_formula_readings(retry_data,device)
                        retry_candidates=[_code_formula_candidate(raw,api,source_multiline_ink(retry_data))[0]
                                          for raw in retry_readings]
                        retry_signatures=[math_signature(raw) if raw else None for raw in retry_candidates]
                        # A changed recognition view cannot make a divided
                        # verifier trustworthy. Only a complete two-read
                        # consensus warrants another expensive Surya request.
                        if len(retry_signatures)!=2 or not retry_signatures[0] or retry_signatures[0]!=retry_signatures[1]:
                            api.diagnostic_event('PDF_FORMULA_SOURCE_RETRY_DISAGREEMENT',page=page,bbox=box,
                                surya=initial['surya'],codeformula=list(retry_readings),**retry_proof)
                            continue
                        retry_rendered=(rendered if retry_signatures[0]==signature else
                                        _read_equation(retry_data,retry_metadata,recognizer,api))
                        retry_signature=math_signature(retry_rendered)
                    except Exception as error:
                        api.diagnostic_event('PDF_FORMULA_SOURCE_RETRY_FAILED',page=page,bbox=box,
                            error_type=type(error).__name__,reason=str(error),**retry_proof)
                        continue
                    if (retry_signature and len(retry_signatures)==2
                            and all(s==retry_signature for s in retry_signatures)):
                        rendered,readings=retry_rendered,retry_readings
                        signature,signatures=retry_signature,retry_signatures
                        proof.update(initial_readings=initial,source_retry=retry_proof,
                            recognition_asset_sha256=hashlib.sha256(retry_data).hexdigest())
                        break
                    api.diagnostic_event('PDF_FORMULA_SOURCE_RETRY_DISAGREEMENT',page=page,bbox=box,
                        surya=BeautifulSoup(retry_rendered,'html.parser').math.get('data-latex',''),
                        codeformula=list(retry_readings),**retry_proof)
            if not literal_proof and (not signature or any(other != signature for other in signatures)):
                from docling_pdf_text_indices import verify_text_index
                try:
                    text_index_proof=verify_text_index(rendered,metadata,recognizer,device,
                        _code_formula_readings,math_signature,api.latex_math)
                except Exception as original:
                    api.diagnostic_event('PDF_FORMULA_TEXT_INDEX_RETRY_FAILED',page=page,bbox=box,
                        error_type=type(original).__name__,reason=str(original))
                if text_index_proof:
                    proof.update(method='math_and_cyrillic_index_text_independently_verified',
                        source_partition=text_index_proof,initial_codeformula=list(readings))
            if not literal_proof and not text_index_proof and (not signature or any(other != signature for other in signatures)):
                from docling_pdf_index_parts import verify_indexed_atoms
                try:
                    indexed_atoms_proof=verify_indexed_atoms(rendered,metadata,recognizer,device,
                        _code_formula_readings,math_signature,api.latex_math)
                except Exception as original:
                    api.diagnostic_event('PDF_FORMULA_INDEX_ATOMS_RETRY_FAILED',page=page,bbox=box,
                        error_type=type(original).__name__,reason=str(original))
                if indexed_atoms_proof:
                    proof.update(method='surya_and_two_codeformula_reads_agree_for_all_source_parts',
                                 source_partition=indexed_atoms_proof,initial_codeformula=list(readings))
            if not literal_proof and not text_index_proof and not indexed_atoms_proof and (not signature or any(other != signature for other in signatures)):
                proof.update(method='readers_disagree', surya=BeautifulSoup(rendered, 'html.parser').math.get('data-latex', ''),
                             codeformula=list(readings))
                manifest.setdefault('formula_verification', []).append(proof)
                api.diagnostic_event('PDF_FORMULA_READERS_DISAGREE', **proof)
                error = RuntimeError(f'Formula readers disagree on page {page}, bbox {box}; reviewed transcription required')
                error.context = dict(page=page, bbox=[float(x) for x in box] if box else None,
                                     stage='formula_verification', element=metadata.get('element'))
                raise error
            if not literal_proof and not text_index_proof and not indexed_atoms_proof:
                proof.update(method='surya_and_two_codeformula_reads_agree', codeformula=list(readings))
        else:
            proof['method'] = 'surya_only'
        proof['latex'] = BeautifulSoup(rendered, 'html.parser').math.get('data-latex', '')
    # Preserve actual printed line breaks: Chromium renders mtable reliably,
    # whereas an mspace linebreak can flatten two printed plus signs together.
    latex = proof.get('latex', '')
    if r'\\' in latex and r'\begin' not in latex:
        lines = [api.latex_math(line, 'inline') for line in re.split(r'\\\\', latex)]
        if any(not line for line in lines):
            raise RuntimeError('Printed formula lines could not be rendered as MathML')
        parsed = BeautifulSoup(rendered, 'html.parser')
        root = parsed.math
        root.clear()
        table = parsed.new_tag('mtable', columnalign='left')
        for line in lines:
            row, cell = parsed.new_tag('mtr'), parsed.new_tag('mtd')
            line_math = BeautifulSoup(line, 'html.parser').math
            for child in list(line_math.contents):
                cell.append(child.extract())
            row.append(cell); table.append(row)
        root.append(table)
        rendered = str(root)
        proof['layout'] = 'source_lines_as_mathml_table'
    manifest.setdefault('formula_verification', []).append(proof)
    api.diagnostic_event('PDF_FORMULA_VERIFIED', **proof)
    return rendered


def defer_math(manifest, api, page_no, box, kind, reason):
    issue = dict(page=page_no, bbox=[float(x) for x in box] if box else None,
                 kind=kind, reason=reason, status='deferred', element=manifest.get('active_math_element'))
    if issue not in manifest.setdefault('deferred_math', []):
        manifest['deferred_math'].append(issue)
    api.diagnostic_event('PDF_MATH_ELEMENT_DEFERRED', **issue)


def retain_source_matched_table_tokens(doc, records, routes, excluded, manifest, api):
    """Use actual source cell ownership, including punctuation and wrapped words.

    Text substring checks cannot prove recovery of a lone dash or a word prefix
    such as one ending at a source line break. A complete native cell with all
    source glyphs retained supplies the stronger coordinate evidence instead.
    """
    from docling_pdf_ocr import recover_unassigned_text
    from docling_pdf_token_ownership import pixel_cell_token_owner
    covered, remaining = [], []
    for record in records:
        if record.get('kind') != 'table_unassigned_token':
            continue
        pixel_owners=[proof for table in manifest.get('tables',[])
                      if table['ref'] not in excluded and routes.get(table['ref'],'')
                      and (proof:=pixel_cell_token_owner(record,table,routes[table['ref']],api))]
        if len(pixel_owners)==1:
            proof=pixel_owners[0];covered.append(proof)
            api.diagnostic_event('TABLE_MATCHING_UNRELIABLE_TOKEN_SUPERSEDED',**proof)
            continue
        owners = []
        for table in manifest.get('tables', []):
            geometry = table.get('pdf_geometry') or {}
            if (table['page'] != record['page_no'] or table.get('method') != 'pdf_geometry'
                    or geometry.get('glyph_coverage') != 1.0
                    or table['ref'] in excluded or '<table' not in routes.get(table['ref'],'')):
                continue
            for cell in geometry.get('cells', []):
                if cell.get('glyphs') and _coverage(record['bbox'],cell['bbox']) >= .95:
                    owners.append((table,cell))
        if len(owners) != 1:
            remaining.append(record)
            continue
        table,cell = owners[0]
        proof = dict(page=record['page_no'],bbox=record['bbox'],text=record['text'],
                     method='complete_source_cell_glyph_ownership',table=table['ref'],
                     row=cell['row'],column=cell['column'])
        covered.append(proof)
        api.diagnostic_event('TABLE_MATCHING_TOKEN_RETAINED',**proof)
    recover_unassigned_text(doc,remaining,routes,excluded,manifest,api)
    manifest['table_unassigned_token_retention'] = covered + manifest['table_unassigned_token_retention']


def source_reference_text(text, native):
    """Correct a terminal OCR 6/б only when a unique native reference proves it."""
    pattern=re.compile(r'\b(?P<label>рисун(?:ок|ке|ка|ку|ки)|таблиц[аеыу])\s*'
        r'(?P<id>[А-ЯЁA-Z]?\.?\d+(?:\.\d+)*[а-яёa-z]?)(?!\w)',re.I)
    def key(value):
        return re.sub(r'\W+','',value.casefold().replace('ё','е'))
    native_matches=list(pattern.finditer(native));changes=[]
    for target in pattern.finditer(text):
        old=target['id']
        if not old.endswith('6'):continue
        matches=[]
        for source in native_matches:
            new=source['id']
            if (source['label'].casefold()!=target['label'].casefold() or not new.endswith('б')
                    or old[:-1]!=new[:-1]):continue
            left,right=key(text[:target.start('id')]),key(text[target.end('id'):])
            native_left,native_right=key(native[:source.start('id')]),key(native[source.end('id'):])
            size=min(24,len(left),len(native_left));tail=min(12,len(right),len(native_right))
            if size<16 or left[-size:]!=native_left[-size:]:continue
            if right[:tail]!=native_right[:tail] or bool(right)!=bool(native_right):continue
            matches.append(new)
        if len(matches)==1:changes.append(dict(start=target.start('id'),end=target.end('id'),old=old,new=matches[0],label=target['label']))
    for change in reversed(changes):text=text[:change['start']]+change['new']+text[change['end']:]
    return text,changes


def repair_source_references(doc, reader, excluded, manifest, api):
    if reader is None:return
    proofs=[]
    for item in doc.texts:
        if '<math' in item.orig:continue  # keep independently rendered math untouched
        if item.self_ref in excluded or not re.search(r'(?:рисун|таблиц)\w*\s*[А-ЯA-Z]?\.?\d*[.]?\d*6\b',item.text,re.I):continue
        region=api.item_region(item,doc)
        if not region:continue
        reader.load_page(region[0]);glyphs=_cell_glyphs(reader,region[1])
        if not glyphs or any(re.search(r'\(cid:|[\ufffd\x00-\x08]',g['text']) or not g.get('upright',True) for g in glyphs):continue
        native=' '.join(line['text'] for line in api.native_cell_lines(glyphs))
        fixed,changes=source_reference_text(item.text,native)
        if not changes:continue
        item.text=fixed
        item.orig=source_reference_text(item.orig,native)[0]
        for change in changes:
            proof=dict(page=region[0],bbox=region[1],ref=item.self_ref,**change,method='unique_native_reference_and_matching_prose_anchors')
            proofs.append(proof);api.diagnostic_event('OCR_SOURCE_REFERENCE_RESTORED',**proof)
    manifest['source_reference_repairs']=proofs


@contextmanager
def _image_budget(manifest, page_no, kind, api, box=None):
    """Independent per-page budgets. Reaching one defers an element, never raises."""
    group = manifest.setdefault('image_budgets', {}).setdefault(kind, dict(pages={}))
    state = group['pages'].setdefault(str(page_no), dict(assets=0, seconds=0.0, deferred=0))
    limit_key = 'math_asset_limits' if kind == 'formula' else 'classification_limits'
    limits = manifest.get(limit_key, {})
    asset_limit, second_limit = limits.get('assets'), limits.get('seconds')
    if ((asset_limit is not None and state['assets'] >= asset_limit)
            or (second_limit is not None and state['seconds'] >= second_limit)):
        state['deferred'] += 1
        defer_math(manifest, api, page_no, box, kind, 'per_page_budget_exhausted')
        api.diagnostic_event('PDF_IMAGE_BUDGET_DEFERRED', kind=kind, page=page_no, **state)
        yield False
        return
    state['assets'] += 1
    started = time.monotonic()
    try:
        yield True
    finally:
        state['seconds'] += time.monotonic()-started
        api.diagnostic_event('PDF_IMAGE_BUDGET', kind=kind, page=page_no, asset_limit=asset_limit, **state)


def _math_asset_budget(manifest, page_no, api, box=None):
    return _image_budget(manifest, page_no, 'formula', api, box)


def repair_table_math(fragment, geometry, recognizer, page_no, manifest, api,
                      source_image=None, source_region=None, source_reader=None):
    """Recognize only source assets explicitly classified as equations.

    The positioned native reader supplies the image order and source bboxes.
    A mixed graphic/equation is retained as a diagram, not reduced to labels.
    """
    if manifest.get('primary_ocr_only'):
        api.diagnostic_event('TABLE_SECONDARY_CONTENT_OCR_DISABLED', page=page_no, stage='table_math',
                             reason='primary_surya_content_only')
        return fragment, []
    from PIL import Image
    def keep_diagram(data,entry,evidence):
        if evidence.get('method')=='source_layout_graphic_without_equation':
            from docling_pdf_formula_fragments import fraction_bar_context
            source=next((c for c in (geometry or {}).get('cells',[]) if
                c['row']==entry.get('row') and c['column']==entry.get('column')),None)
            fraction=fraction_bar_context(data,entry,source)
            if fraction:
                digest=hashlib.sha256(data).hexdigest()
                pending=geometry.setdefault('fragmented_formula_pending',[])
                if not any((p.get('row'),p.get('column'),p['source_image_sha256'])==
                        (entry.get('row'),entry.get('column'),digest) for p in pending):
                    pending.append(dict(page=page_no,row=entry.get('row'),column=entry.get('column'),
                        source_image_sha256=digest,reason='native_fraction_rule_is_not_an_illustration'))
                api.diagnostic_event('TABLE_SOURCE_GRAPHIC_REJECTED_FRACTION_RULE',page=page_no,
                    row=entry.get('row'),column=entry.get('column'),source_image_sha256=digest,**fraction)
                return False
        proof=dict(page=page_no,row=entry.get('row'),column=entry.get('column'),bbox=entry['bbox'],
            source_image_sha256=hashlib.sha256(data).hexdigest(),evidence=evidence)
        manifest.setdefault('source_cell_diagrams',[]).append(proof)
        api.diagnostic_event('TABLE_SOURCE_DIAGRAM_CONFIRMED',**proof)
        return True
    soup = BeautifulSoup(fragment, 'html.parser')
    images = soup.find_all('img')
    entries = (geometry or {}).get('retained_cell_images', [])
    if len(images) != len(entries):
        api.diagnostic_event('TABLE_MATH_ASSETS_KEEP', page=page_no,
                             reason='source_asset_mapping_not_verified', images=len(images), entries=len(entries))
        return fragment, []
    # An image must still have the display width assigned to that source bbox.
    if any(abs(int(img.get('width', 0))-round((entry['bbox'][2]-entry['bbox'][0])*96/72)) > 1
           for img, entry in zip(images, entries)):
        api.diagnostic_event('TABLE_MATH_ASSETS_KEEP', page=page_no,
                             reason='source_image_display_width_not_verified', images=len(images), entries=len(entries))
        return fragment, []
    capacity = manifest.setdefault('classification_capacity', {})
    capacity[str(page_no)] = capacity.get(str(page_no), 0) + len(entries)
    api.diagnostic_event('PDF_CLASSIFICATION_CAPACITY', page=page_no, candidates=capacity[str(page_no)],
                         mode='unlimited' if manifest.get('classification_limits', {}).get('assets') is None else 'manual_cap')
    from docling_pdf_math_grid import raster_grid, crossed_grid, split_numeric_cells, plain_numeric_cell, _numeric_reader, recognition_grid_copy
    from docling_pdf_quantities import native_quantity, raster_quantity_candidate, graphic_outline, source_permille_quantity, single_symbol_ink, source_frame_context, source_boundary_rule_fragment, complete_source_component_ownership
    grid = raster_grid(source_image, source_region) if source_image is not None and source_region else None
    source_outline_cache = {}
    source_prose_templates = {}
    mixed_plans = {}
    asset_pairs = list(zip(images, entries))
    from docling_pdf_roman_labels import roman_row_plans
    roman_plans = roman_row_plans(asset_pairs,(geometry or {}).get('source_roman_references',()))
    priority = {}
    # Establish source prose templates from unambiguous cells first. Every
    # mathematical part of such a seed still passes the strict verifier before
    # its prose bitmap is made available to an ambiguous neighbouring cell.
    for image_node, entry in asset_pairs:
        priority[id(image_node)] = 1
        if not entry.get('mixed_source_html'):
            continue
        from docling_table_content import split_mixed_cell_ink
        try:
            raw = base64.b64decode(image_node['src'].split(',',1)[1],validate=True)
            mixed_plans[id(image_node)] = split_mixed_cell_ink(raw,entry['mixed_source_html'],entry['bbox'])
            priority[id(image_node)] = 0
        except ValueError:
            priority[id(image_node)] = 2
    asset_pairs.sort(key=lambda pair:priority[id(pair[0])])
    restored = []
    for img, entry in asset_pairs:
        src = img.get('src', '')
        if not src.startswith('data:image/png;base64,'):
            continue
        data = base64.b64decode(src.split(',', 1)[1], validate=True)
        source_data = data
        if any((p.get('row'),p.get('column'),p['source_image_sha256'])==
                (entry.get('row'),entry.get('column'),hashlib.sha256(source_data).hexdigest())
                for p in (geometry or {}).get('fragmented_formula_pending',[])):
            img['data-math-status']='complete-source-formula-pending'
            api.diagnostic_event('TABLE_MATH_FRAGMENTED_FORMULA_PENDING',page=page_no,
                bbox=entry['bbox'],reason='individual_asset_cannot_replace_unresolved_complete_formula')
            continue
        roman_plan=roman_plans.get(id(img))
        if roman_plan:
            p=roman_plan['proof']; b=entry['bbox']; pixels=p['subscript_pixel_bbox']
            width,height=p['source_size']
            sub_box=[b[0]+pixels[0]*(b[2]-b[0])/width,b[1]+pixels[1]*(b[3]-b[1])/height,
                     b[0]+pixels[2]*(b[2]-b[0])/width,b[1]+pixels[3]*(b[3]-b[1])/height]
            with Image.open(BytesIO(roman_plan['data'])) as index_image:
                metadata=dict(width=index_image.width,height=index_image.height,inline=True,paragraph_text='',
                    source_page=page_no,source_bbox=sub_box,element=manifest.get('active_math_element'),
                    source_script_kind='subscript',source_literal_index=True,literal_context=roman_plan.get('literal_context'),
                    media_name=f'PDF page {page_no}, Roman designation lower index {sub_box}')
            index=None
            primary_literal=roman_plan.get('primary_literal')
            if primary_literal:
                from docling_source_text import verify_literal
                try:
                    literal_proof=verify_literal(roman_plan['data'],primary_literal['index'],roman_plan.get('literal_context'))
                except Exception as original:
                    literal_proof=None
                    api.diagnostic_event('PDF_LITERAL_INDEX_VERIFIER_FAILED',page=page_no,bbox=sub_box,
                        error_type=type(original).__name__,reason=str(original))
                if literal_proof:
                    index=api.latex_math(primary_literal['index'],'inline')
                    primary_proof=dict(page=page_no,bbox=list(map(float,sub_box)),
                        method='primary_ocr_and_independent_literal_text_agree',
                        primary=primary_literal,literal_index=literal_proof)
                    manifest.setdefault('literal_index_verification',[]).append(primary_proof)
                    api.diagnostic_event('PDF_LITERAL_INDEX_VERIFIED',**primary_proof)
            if index is None:
                with _math_asset_budget(manifest,page_no,api,sub_box) as allowed:
                    if not allowed:
                        img['data-math-status']='formula-deferred'
                        continue
                    index=verified_equation(roman_plan['data'],metadata,recognizer,manifest,api)
            index_math=BeautifulSoup(index,'html.parser').math
            latex=index_math.get('data-latex','')
            if not latex:
                error=RuntimeError('Verified Roman designation index has no complete transcription')
                error.context=dict(stage='roman_label',page=page_no,bbox=list(map(float,b)),element=metadata['element'])
                raise error
            expression=r'\mathrm{'+p['numeral']+'}_{'+latex+'}'
            rendered=api.latex_math(expression,'inline')
            if rendered is None:
                raise RuntimeError('Verified Roman designation cannot be rendered')
            img.replace_with(BeautifulSoup(rendered,'html.parser').math)
            proof=dict(page=page_no,bbox=list(map(float,b)),latex=expression,
                       source_asset_sha256=hashlib.sha256(data).hexdigest(),**p)
            restored.append(proof)
            manifest.setdefault('source_roman_labels',[]).append(proof)
            api.diagnostic_event('TABLE_SOURCE_ROMAN_LABEL_RESTORED',**proof)
            continue
        if entry.get('mixed_source_html'):
            from docling_table_content import split_mixed_cell_ink
            context = dict(stage='mixed_text_math', page=page_no,
                           element=manifest.get('active_math_element'), bbox=list(map(float,entry['bbox'])))
            try:
                if img.parent.get_text(strip=True) or len(img.parent.find_all('img'))!=1:
                    raise ValueError('Mixed-cell proposal does not exclusively own its source raster')
                segmented = mixed_plans.get(id(img)) or split_mixed_cell_ink(data,
                    entry['mixed_source_html'], entry['bbox'],prose_templates=source_prose_templates)
            except ValueError as original:
                api.diagnostic_event('TABLE_MIXED_SOURCE_SEGMENTATION_REJECTED', **context, reason=str(original))
                error = RuntimeError('Mixed source cell cannot be segmented without changing its content: '+str(original))
                error.context = context
                raise error from original
            chunks=[];verified_parts=[];deferred=False
            for part in segmented['parts']:
                if part['kind']=='text':
                    chunks.append(escape(part['text']))
                    continue
                with Image.open(BytesIO(part['data'])) as part_image:
                    metadata=dict(width=part_image.width,height=part_image.height,inline=True,paragraph_text='',
                        source_page=page_no,source_bbox=part['bbox'],element=context['element'],
                        media_name=f'PDF page {page_no}, mixed source math {part["bbox"]}')
                with _math_asset_budget(manifest,page_no,api,part['bbox']) as allowed:
                    if not allowed:
                        deferred=True;break
                    rendered=verified_equation(part['data'],metadata,recognizer,manifest,api)
                proposal=BeautifulSoup(part['proposed_html'],'html.parser').math
                proposed=(str(proposal) if proposal and proposal.find(True) else
                          api.latex_math(proposal.get_text(),'inline') if proposal else None)
                if not proposed or math_signature(rendered)!=math_signature(proposed):
                    api.diagnostic_event('TABLE_MIXED_SOURCE_PROPOSAL_DISAGREES',page=page_no,
                        bbox=part['bbox'],element=context['element'],source_fragment_sha256=part['source_sha256'])
                    error=RuntimeError(f'Verified mixed-cell formula differs from the complete source proposal on page {page_no}')
                    error.context=dict(context,bbox=list(map(float,part['bbox'])),stage='formula_verification')
                    raise error
                chunks.append(rendered)
                verified_parts.append(dict(bbox=part['bbox'],source_fragment_sha256=part['source_sha256']))
            if deferred:
                img['data-math-status']='formula-deferred';continue
            seed_proofs=[]
            for part in segmented['parts']:
                if part['kind']=='text' and part.get('data'):
                    templates=source_prose_templates.setdefault(part['text'],[])
                    if not any(t['source_sha256']==part['source_sha256'] for t in templates):
                        templates.append(part)
                        seed_proofs.append(dict(text=part['text'],source_sha256=part['source_sha256'],bbox=part['bbox']))
            if seed_proofs:
                api.diagnostic_event('TABLE_MIXED_SOURCE_PROSE_TEMPLATES_VERIFIED',page=page_no,
                    bbox=entry['bbox'],templates=seed_proofs)
            replacement=BeautifulSoup(' '.join(chunks),'html.parser')
            for child in list(replacement.contents):img.insert_before(child.extract())
            img.decompose()
            proof=dict(page=page_no,bbox=entry['bbox'],method='source_mixed_cell_gaps_and_independently_verified_math',
                       segmentation=segmented['proof'],verified_fragments=verified_parts)
            restored.append(proof);manifest.setdefault('source_mixed_text_math',[]).append(proof)
            api.diagnostic_event('TABLE_SOURCE_MIXED_TEXT_MATH_RESTORED',**proof)
            continue
        if source_reader is not None:
            quantity = native_quantity(data, entry['bbox'], source_reader.chars, api.native_cell_lines)
            if quantity:
                img.replace_with(BeautifulSoup(quantity.pop('html'), 'html.parser').math)
                proof = dict(page=page_no, bbox=entry['bbox'], **quantity)
                restored.append(proof)
                manifest.setdefault('source_quantities', []).append(proof)
                api.diagnostic_event('TABLE_SOURCE_QUANTITY_RESTORED', **proof)
                continue
        proof = crossed_grid(entry['bbox'], geometry, grid)
        if proof:
            manifest.setdefault('rejected_grid_math', []).append(dict(page=page_no, **proof))
            api.diagnostic_event('TABLE_MATH_SOURCE_GRID_REJECTED', page=page_no, **proof)
            with _image_budget(manifest, page_no, 'classification', api, entry['bbox']) as allowed:
                if allowed:
                    numeric = split_numeric_cells(soup, img, entry, geometry, grid, data, api)
                    if numeric:
                        manifest.setdefault('source_numeric_cells', []).append(dict(page=page_no, **numeric))
                        api.diagnostic_event('TABLE_SOURCE_NUMERIC_CELLS_RESTORED', page=page_no, **numeric)
            continue
        outline = graphic_outline(data)
        if outline:
            proof = dict(page=page_no, bbox=entry['bbox'], **outline)
            keep_diagram(source_data,entry,outline)
            manifest.setdefault('source_graphic_outlines', []).append(proof)
            api.diagnostic_event('TABLE_MATH_SOURCE_GRAPHIC_REJECTED', **proof)
            continue
        source_digest = hashlib.sha256(data).hexdigest()
        data, preprocessing = recognition_grid_copy(data, entry['bbox'], grid)
        if preprocessing:
            proof = dict(page=page_no, bbox=entry['bbox'], source_asset_sha256=source_digest,
                         recognition_asset_sha256=hashlib.sha256(data).hexdigest(), **preprocessing)
            manifest.setdefault('formula_preprocessing', []).append(proof)
            api.diagnostic_event('TABLE_MATH_SOURCE_GRID_PREPROCESSED', **proof)
        key = hashlib.sha256(data).hexdigest()
        if key in recognizer.layout_memory:
            manifest['classification_cache_hits'] = manifest.get('classification_cache_hits', 0)+1
            api.diagnostic_event('TABLE_MATH_CLASSIFICATION_CACHE_HIT', page=page_no, bbox=entry['bbox'])
            classification = nullcontext(True)
        else:
            classification = _image_budget(manifest, page_no, 'classification', api, entry['bbox'])
        with classification as allowed:
            if not allowed:
                img['data-math-status'] = 'classification-deferred'
                continue
            if entry.get('basis') == 'nonempty_scan_cell_without_positioned_text':
                numeric = plain_numeric_cell(data)
                if numeric:
                    img.replace_with(numeric['value'])
                    proof = dict(page=page_no,row=entry['row'],column=entry['column'],
                                 bbox=entry['bbox'],method='source_scalar_cell_ink_and_two_numeric_reads',**numeric)
                    manifest.setdefault('source_scalar_cells',[]).append(proof)
                    api.diagnostic_event('TABLE_SOURCE_SCALAR_CELL_RESTORED',**proof)
                    continue
            with Image.open(BytesIO(data)) as image:
                metadata = dict(width=image.width, height=image.height, inline=True, paragraph_text='',
                                source_bbox=entry['bbox'],source_page=page_no,
                                element=manifest.get('active_math_element'),
                                media_name=f"PDF page {page_no}, source cell asset {entry['bbox']}")
                if preprocessing:
                    metadata.update(source_asset_sha256=source_digest, recognition_preprocessing=preprocessing)
                if key not in recognizer.layout_memory:
                    recognizer.layout_memory[key] = recognizer.layout(image, metadata)
                layout = recognizer.layout_memory[key]
            if layout['labels']==['Text'] and not layout['graphics']:
                outline=source_frame_context(data,source_image,source_region,entry['bbox'],cache=source_outline_cache)
                if outline:
                    proof=dict(page=page_no,bbox=entry['bbox'],**outline)
                    keep_diagram(source_data,entry,outline)
                    manifest.setdefault('source_graphic_outlines',[]).append(proof)
                    api.diagnostic_event('TABLE_MATH_SOURCE_GRAPHIC_REJECTED',**proof)
                    continue
            if not layout['graphics']:
                try:
                    quantity = source_permille_quantity(data, _numeric_reader())
                except Exception as error:
                    quantity = None
                    api.diagnostic_event('TABLE_SOURCE_QUANTITY_PROBE_FAILED', page=page_no,
                        bbox=entry['bbox'], error_type=type(error).__name__, error=str(error))
                if quantity:
                    rendered_quantity = BeautifulSoup(quantity.pop('html'), 'html.parser').math
                    rendered_quantity['data-source-quantity'] = 'true'
                    img.replace_with(rendered_quantity)
                    proof = dict(page=page_no, bbox=entry['bbox'], source_asset_sha256=source_digest, **quantity)
                    restored.append(proof)
                    manifest.setdefault('source_quantities', []).append(proof)
                    api.diagnostic_event('TABLE_SOURCE_QUANTITY_RESTORED', **proof)
                    continue
            # A single printed mathematical letter can be labelled Text by
            # layout. Only propose a tiny isolated glyph, and require the
            # independent equation checks before replacing its source image.
            w, h = entry['bbox'][2]-entry['bbox'][0], entry['bbox'][3]-entry['bbox'][1]
            quantity_candidate = None
            if layout['labels'] == ['Text'] and not layout['graphics']:
                try:
                    quantity_candidate = raster_quantity_candidate(data, _numeric_reader())
                except Exception as error:
                    api.diagnostic_event('TABLE_SOURCE_QUANTITY_PROBE_FAILED', page=page_no,
                        bbox=entry['bbox'], error_type=type(error).__name__, error=str(error))
                if quantity_candidate:
                    api.diagnostic_event('TABLE_SOURCE_QUANTITY_CANDIDATE', page=page_no,
                                         bbox=entry['bbox'], **quantity_candidate)
            role=entry.get('source_column_context',{})
            role_proof=None
            column_anchors=role.get('reference_column_anchors',[])
            anchor_count=len(column_anchors) if isinstance(column_anchors,(list,tuple)) else int(column_anchors or 0)
            blank_reference=(role.get('reference_cell_empty') or
                role.get('reference_column_body_empty') and anchor_count>=2)
            if not quantity_candidate and role.get('graphic_column'):
                role_proof=dict(reason='source_grid_column_explicitly_contains_drawings',
                                method='positioned_source_graphic_header')
            elif (not quantity_candidate and role.get('table_has_graphic_columns')
                  and blank_reference
                  and not any(text.strip() for text in role.get('header_texts',[]))):
                fragment_proof=source_boundary_rule_fragment(data,source_image,source_region,
                    entry['bbox'],cache=source_outline_cache)
                if fragment_proof:
                    role_proof=dict(reason='source_blank_form_auxiliary_rule_rail',**fragment_proof)
            if role_proof:
                proof=dict(page=page_no,bbox=entry['bbox'],source_column_context=role,**role_proof)
                keep_diagram(source_data,entry,role_proof)
                manifest.setdefault('source_graphic_roles',[]).append(proof)
                api.diagnostic_event('TABLE_MATH_SOURCE_ROLE_GRAPHIC_KEEP',**proof)
                continue
            symbol_candidate = (manifest.get('formula_reader', 'verified') == 'verified'
                and layout['labels'] == ['Text'] and not layout['graphics']
                and .4 <= w/max(h, .01) <= 1.8)
            if symbol_candidate:
                symbol_ink=single_symbol_ink(data)
                if not symbol_ink['single']:
                    symbol_candidate=False
                    api.diagnostic_event('TABLE_SOURCE_SYMBOL_REJECTED',page=page_no,bbox=entry['bbox'],
                                         reason='multiple_positioned_source_ink_groups',**symbol_ink)
            if symbol_candidate and not quantity_candidate and not layout['math'] and source_image is not None:
                ownership=complete_source_component_ownership(source_data,source_image,source_region,
                    entry['bbox'],cache=source_outline_cache)
                if not ownership['complete']:
                    symbol_candidate=False
                    api.diagnostic_event('TABLE_SOURCE_SYMBOL_REJECTED',page=page_no,bbox=entry['bbox'],**ownership)
            if (not layout['math'] and not symbol_candidate and not quantity_candidate) or layout['graphics']:
                if layout.get('error'):
                    defer_math(manifest, api, page_no, entry['bbox'], 'classification',
                               'asset_layout_failed: '+str(layout.get('reason','unknown')))
                    img['data-math-status'] = 'classification-failed'
                    continue
                kept = manifest.setdefault('source_graphics_kept', {})
                kept[str(page_no)] = kept.get(str(page_no), 0)+1
                if layout['graphics'] and not layout['math']:
                    keep_diagram(source_data,entry,dict(method='source_layout_graphic_without_equation',labels=layout['labels']))
                api.diagnostic_event('TABLE_MATH_SOURCE_GRAPHIC_KEEP', page=page_no, bbox=entry['bbox'],
                                     labels=layout['labels'], reason='not_an_isolated_equation')
                continue
            metadata.update(source_page=page_no, source_bbox=entry['bbox'])
            if symbol_candidate and not quantity_candidate and not layout['math'] and not _reviewed_matches(manifest, page_no, entry['bbox']):
                try:
                    candidate = math_signature(_read_equation(data, metadata, recognizer, api))
                except RuntimeError as error:
                    api.diagnostic_event('TABLE_SOURCE_SYMBOL_KEEP', page=page_no, bbox=entry['bbox'],
                                         reason='symbol_probe_failed', error=str(error))
                    continue
                if (not candidate or candidate[0] != 'text' or len(candidate[1]) != 1
                        or not candidate[1].isalpha()):
                    api.diagnostic_event('TABLE_SOURCE_SYMBOL_KEEP', page=page_no, bbox=entry['bbox'],
                                         reason='not_a_single_printed_letter')
                    continue
        with _math_asset_budget(manifest, page_no, api, entry['bbox']) as allowed:
            if not allowed:
                img['data-math-status'] = 'formula-deferred'
                continue
            rendered = verified_equation(data, metadata, recognizer, manifest, api)
        img.replace_with(BeautifulSoup(rendered, 'html.parser').math)
        proof = dict(page=page_no, bbox=entry['bbox'], method='source_equation_asset_to_mathml',
                     latex=BeautifulSoup(rendered, 'html.parser').math.get('data-latex', ''))
        restored.append(proof)
        api.diagnostic_event('TABLE_SOURCE_MATH_RESTORED', **proof)
    return str(soup.table), restored


def repair_inline_placeholders(doc, reader, regions, recognizer, picture_routes, excluded, manifest, api):
    """Bind one damaged OCR variable slot to one real PDF image and its text.

    Exact source words to the right must match the slot's following prose.
    Geometry and a black-square marker alone do not justify replacing text.
    """
    if manifest.get('primary_ocr_only'):
        return {}, set()
    from PIL import Image
    targets, pages, proofs = {}, set(), []
    marker = re.compile(r'[█▓■]{2,}')
    if reader is None:
        return targets, pages
    for item in doc.texts:
        matches = list(marker.finditer(item.text))
        region = api.item_region(item, doc)
        if not matches and region and item.self_ref not in excluded:
            restored = _source_asset_definition(item, doc, reader, regions, recognizer,
                                               picture_routes, manifest, api)
            if restored:
                targets[item.self_ref], proof = restored
                pages.add(region[0]); proofs.append(proof)
            continue
        if item.self_ref in excluded or len(matches) != 1 or not region:
            continue
        page_no, box = region
        manifest['active_math_element'] = item.self_ref
        if hasattr(recognizer,'args'):
            recognizer.args.pdf_export_context=dict(stage='inline_formula',page=page_no,element=item.self_ref,bbox=box)
        reader.load_page(page_no)
        from docling_pdf_inline import source_asset_paragraph
        recovered = source_asset_paragraph(item, doc, reader, regions, recognizer,
            picture_routes, manifest, api, verified_equation, _math_asset_budget,excluded=excluded)
        if recovered is not None:
            targets[item.self_ref], proof = recovered
            pages.add(page_no); proofs.append(proof)
            continue
        assets = [obj for obj in reader.page.images if obj['height'] <= 35 and obj['width'] <= 160
                  and _coverage([obj['x0'], obj['top'], obj['x1'], obj['bottom']],
                                [box[0]-4, box[1]-4, box[2]+4, box[3]+4]) >= .9]
        if len(assets) != 1:
            raise RuntimeError(f'Ambiguous damaged formula slot on page {page_no}, {item.self_ref}')
        obj = assets[0]
        words = [g for g in reader.chars if g['text'] and g['x0'] >= obj['x1']-1
                 and obj['top']-4 <= (g['top']+g['bottom'])/2 <= obj['bottom']+4
                 and g['x1'] <= box[2]+4]
        source_right = api.cell_text_key(' '.join(l['text'] for l in api.native_cell_lines(words))).lstrip('-—–')
        suffix = api.cell_text_key(item.text[matches[0].end():]).lstrip('-—–')
        if len(source_right) < 10 or not suffix.startswith(source_right[:24]):
            raise RuntimeError(f'Unverified formula-slot text anchors on page {page_no}, {item.self_ref}')
        bounds = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
        pictures = [p for p in doc.pictures if (reg := api.item_region(p, doc)) and reg[0] == page_no
                    and api.overlap_ratio(reg[1], bounds) > .8 and not p.children and not p.captions]
        if len(pictures) != 1:
            raise RuntimeError(f'Ambiguous formula-slot picture on page {page_no}, {item.self_ref}')
        image = regions.crop(page_no, [bounds[0]-2, bounds[1]-2, bounds[2]+2, bounds[3]+2])
        try:
            buffer = BytesIO()
            image.save(buffer, 'PNG')
            with _math_asset_budget(manifest, page_no, api, bounds) as allowed:
                if not allowed:
                    continue
                rendered = verified_equation(buffer.getvalue(), dict(width=image.width, height=image.height,
                    inline=True, paragraph_text=item.text, source_page=page_no, source_bbox=bounds, element=item.self_ref,
                    media_name=f'PDF page {page_no}, {item.self_ref}, {bounds}'), recognizer, manifest, api)
        finally:
            image.close()
        targets[item.self_ref] = [('placeholder', rendered)]
        picture_routes[pictures[0].self_ref] = ''
        pages.add(page_no)
        proof = dict(ref=item.self_ref, picture=pictures[0].self_ref, page=page_no, bbox=bounds,
                     method='source_image_and_exact_following_text_anchor',
                     latex=BeautifulSoup(rendered, 'html.parser').math.get('data-latex', ''))
        proofs.append(proof)
        api.diagnostic_event('PDF_FORMULA_SLOT_RESTORED', **proof)
    manifest['repaired_inline_formula_slots'] = proofs
    return targets, pages


def _source_asset_definition(item, doc, reader, regions, recognizer, picture_routes, manifest, api):
    """Restore omitted variables on one source line with exact matching prose.

    This route requires multiple small, aligned PDF image objects, an existing
    corresponding PictureItem, and equality of all non-image source words.
    Only the matched first line is replaced; remaining prose is untouched.
    """
    page_no, box = api.item_region(item, doc)
    manifest['active_math_element'] = item.self_ref
    reader.load_page(page_no)
    assets = sorted([o for o in reader.page.images if o['height'] <= 35 and o['width'] <= 160
        and _coverage([o['x0'], o['top'], o['x1'], o['bottom']],
                      [box[0]-4, box[1]-4, box[2]+4, box[3]+4]) >= .95], key=lambda o: o['x0'])
    if len(assets) < 2 or max(o['top'] for o in assets)-min(o['top'] for o in assets) > 7:
        return None
    if any((reg := api.item_region(t, doc)) and reg[0] == page_no and
           any(_coverage([o['x0'], o['top'], o['x1'], o['bottom']], reg[1]) >= .5 for o in assets)
           for t in doc.tables):
        return None
    pictures = [p for p in doc.pictures if not p.children and not p.captions
        and (reg := api.item_region(p, doc)) and reg[0] == page_no
        and any(api.overlap_ratio(reg[1], [o['x0'], o['top'], o['x1'], o['bottom']]) > .8 for o in assets)]
    if not pictures:
        return None
    top, bottom = min(o['top'] for o in assets)-2, max(o['bottom'] for o in assets)+2
    glyphs = _cell_glyphs(reader, [box[0]-4, top, box[2]+4, bottom])
    lines = api.native_cell_lines(glyphs)
    if len(lines) != 1 or any(not g.get('upright', True) or '(cid:' in g['text'] for g in glyphs):
        return None
    marker = getattr(item, 'marker', '')
    def without_marker(text):
        return re.sub(r'^\s*'+re.escape(marker)+r'\s*', '', text) if marker else text
    expected = item.text.splitlines()[0]
    if len(expected) < 24 or api.cell_text_key(without_marker(lines[0]['text'])) != api.cell_text_key(expected):
        return None
    chunks, previous = [], box[0]-4
    boxes = []
    for obj in assets:
        bounds = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
        selected = [g for g in glyphs if previous <= (g['x0']+g['x1'])/2 < bounds[0]]
        prose = ' '.join(l['text'] for l in api.native_cell_lines(selected))
        chunks.append(escape(without_marker(prose) if not chunks else prose))
        image = regions.crop(page_no, [bounds[0]-2, bounds[1]-2, bounds[2]+2, bounds[3]+2])
        try:
            buffer = BytesIO(); image.save(buffer, 'PNG')
            with _math_asset_budget(manifest, page_no, api, bounds) as allowed:
                if not allowed:
                    return None
                rendered = verified_equation(buffer.getvalue(), dict(width=image.width, height=image.height,
                    inline=True, paragraph_text=expected, source_page=page_no, source_bbox=bounds, element=item.self_ref,
                    media_name=f'PDF source definition {page_no}, {item.self_ref}, {bounds}'),
                    recognizer, manifest, api)
        finally:
            image.close()
        chunks.append(rendered); previous = bounds[2]; boxes.append(bounds)
    trailing = [g for g in glyphs if (g['x0']+g['x1'])/2 >= previous]
    chunks.append(escape(' '.join(l['text'] for l in api.native_cell_lines(trailing))))
    for picture in pictures:
        picture_routes[picture.self_ref] = ''
    proof = dict(ref=item.self_ref, page=page_no, bboxes=boxes, pictures=[p.self_ref for p in pictures],
                 method='multiple_source_assets_and_exact_nonimage_first_line')
    api.diagnostic_event('PDF_FORMULA_DEFINITION_RESTORED', **proof)
    return [('source_line', (expected, ' '.join(chunk for chunk in chunks if chunk)))], proof
