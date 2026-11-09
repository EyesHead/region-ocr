"""Recover structure already present in positioned OCR, without new OCR votes.

The layout assembler can merge distinct OCR paragraphs or turn a complete
table into one text cell.  Keep the retained OCR evidence separate from that
assembler's decisions.  No rule here depends on a document's wording or name.
"""
from collections import Counter
import re
import unicodedata

from bs4 import BeautifulSoup

# Native vector grids may be absent from the assembler's table collection.
# Keep that source-geometry discovery separate from retained OCR restoration.
from docling_pdf_native_discovery import recover_unrouted_native_tables


def _key(text):
    return re.sub(r"[\s\u00ad\u200b]+", "", text)


def _coverage(inner, outer):
    area = max(0, inner[2]-inner[0])*max(0, inner[3]-inner[1])
    common = max(0, min(inner[2], outer[2])-max(inner[0], outer[0])) * max(
        0, min(inner[3], outer[3])-max(inner[1], outer[1]))
    return common/area if area else 0


def _partition_original_text(text, paragraph_keys):
    """Use OCR boundaries while keeping existing characters inside a block."""
    positions = [match.start() for match in re.finditer(r'[^\s\u00ad\u200b]', text)]
    boundaries, consumed = [0], 0
    for key in paragraph_keys[:-1]:
        consumed += len(key)
        boundaries.append(positions[consumed-1]+1)
    boundaries.append(len(text))
    return [text[left:right].strip() for left, right in zip(boundaries, boundaries[1:])]


def numbered_header_width(fragment):
    """Return a source-printed 1..N header width supported by body rows.

    An ordinary numeric data row is not a column-number header. Require its
    header semantics, unmerged number cells and subsequent complete body
    rows with the same physical colspan total. Contradictory headers never
    establish an authoritative width.
    """
    table = fragment if getattr(fragment, 'name', None) == 'table' else BeautifulSoup(
        str(fragment), 'html.parser').find('table')
    if table is None:
        return None
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    widths = set()
    for index, row in enumerate(rows):
        cells = row.find_all(['td', 'th'], recursive=False)
        # Rectangular-occupancy repair may have appended blank padding cells.
        # They cannot erase the source's printed numbering as evidence.
        while cells and not cells[-1].get_text(strip=True) and not cells[-1].find(['math', 'img']):
            cells = cells[:-1]
        values = [cell.get_text(' ', strip=True) for cell in cells]
        size = len(values)
        if size < 2 or values != [str(number) for number in range(1, size+1)]:
            continue
        if row.find_parent('thead') is None and not all(cell.name == 'th' for cell in cells):
            continue
        try:
            if any(int(cell.get('colspan', 1)) != 1 or int(cell.get('rowspan', 1)) != 1 for cell in cells):
                continue
            body = [candidate for candidate in rows[index+1:] if candidate.find_all(['td', 'th'], recursive=False)]
            body_widths = []
            for candidate in body:
                nodes = candidate.find_all(['td', 'th'], recursive=False)
                width = sum(int(cell.get('colspan', 1)) for cell in nodes)
                while width > size and nodes and not nodes[-1].get_text(strip=True) and not nodes[-1].find(['math', 'img']):
                    width -= int(nodes[-1].get('colspan', 1))
                    nodes = nodes[:-1]
                body_widths.append(width)
        except (ValueError, TypeError):
            continue
        if body_widths and any(width == size for width in body_widths) and not any(width > size for width in body_widths):
            widths.add(size)
    return next(iter(widths)) if len(widths) == 1 else None


def restore_ocr_paragraphs(doc, records, excluded, manifest, api):
    """Undo assembly merges only when all characters and boundaries agree.

    The paragraph texts are *not* guessed from a clause-number regex.  Require
    a sequence of distinct, positioned Text OCR blocks whose concatenation is
    exactly the existing item's content (ignoring whitespace/soft hyphens).
    Formula markers are kept verbatim and follow the normal math serializer.
    Each restored paragraph receives the retained block's own provenance.
    """
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel

    by_page = {}
    for record in records:
        if record.get('label') != 'Text' or not record.get('bbox'):
            continue
        parsed = BeautifulSoup(record.get('html', ''), 'html.parser')
        if parsed.find(['table', 'img']) or len(parsed.find_all('p')) != 1:
            continue
        text = api.semantic_ocr_text(record['html'])
        if _key(text):
            by_page.setdefault(record['page_no'], []).append((record, text))
    repairs = []
    protected = set(manifest.get('protected_image_table_text_refs', []))
    for item in list(doc.texts):
        if item.self_ref in excluded or item.label != DocItemLabel.TEXT or len(item.prov) != 1:
            continue
        region = api.item_region(item, doc)
        if not region:
            continue
        page_no, box = region
        # The OCR boxes can share a boundary by a subpixel after scale mapping.
        # Measure that uncertainty using the page's retained image scale.
        tolerance = 1 / max(float(getattr(api, 'IMAGE_SCALE', 1)), 1)
        expanded = [box[0]-tolerance, box[1]-tolerance, box[2]+tolerance, box[3]+tolerance]
        blocks = [(record, text) for record, text in by_page.get(page_no, [])
                  if expanded[0] <= (record['bbox'][0]+record['bbox'][2])/2 <= expanded[2]
                  and expanded[1] <= (record['bbox'][1]+record['bbox'][3])/2 <= expanded[3]
                  and _coverage(record['bbox'], expanded) == 1]
        blocks.sort(key=lambda pair: (pair[0]['bbox'][1], pair[0]['bbox'][0]))
        if len(blocks) < 2 or _key(' '.join(text for _, text in blocks)) != _key(item.text):
            continue
        # Duplicate OCR requests for the same block are not independent
        # paragraphs. Their concatenation normally already fails the equality.
        if len({(tuple(record['bbox']), text) for record, text in blocks}) != len(blocks):
            continue
        keys = [_key(text) for _, text in blocks]
        texts = _partition_original_text(item.text, keys)
        originals = (_partition_original_text(item.orig, keys)
                     if _key(item.orig) == _key(item.text) else texts)
        original = item.self_ref
        previous = None
        restored_refs = []
        for index, ((record, _), text, orig) in enumerate(zip(blocks, texts, originals)):
            prov = item.prov[0].model_copy(deep=True)
            bounds = record['bbox']
            prov.bbox = BoundingBox(l=bounds[0], t=bounds[1], r=bounds[2], b=bounds[3],
                                    origin=CoordOrigin.TOPLEFT)
            prov.charspan = (0, len(text))
            if not index:
                item.text, item.orig, item.prov = text, orig, [prov]
                current = item
            else:
                current = doc.insert_text(sibling=previous, after=True, label=DocItemLabel.TEXT,
                                          text=text, orig=orig, prov=prov,
                                          content_layer=item.content_layer)
            restored_refs.append(current.self_ref)
            previous = current
        entry = dict(original=original, page=page_no, paragraphs=restored_refs,
                     count=len(blocks), method='retained_positioned_ocr_exact_content_partition')
        if original in protected:
            protected.update(restored_refs)
            entry['protected_refs_propagated'] = True
        repairs.append(entry)
        api.diagnostic_event('OCR_PARAGRAPH_BOUNDARIES_RESTORED', **entry)
    manifest['restored_ocr_paragraphs'] = repairs
    if 'protected_image_table_text_refs' in manifest:
        manifest['protected_image_table_text_refs'] = sorted(protected)
    return repairs


def reject_collapsed_table_candidates(candidates, geometry, diagnostic=None, **context):
    """A scalar cell cannot win over a complete table of the same source grid.

    Warning counts are not a useful ranking metric for this comparison: a
    one-cell transcription has fewer opportunities for statistical warnings.
    Require actual source column rails plus a structurally valid 2D reading,
    or exact character equivalence with that 2D reading when rails are absent.
    """
    complete = [c for c in candidates if not c[3].get('issues') and
                c[3].get('rows', 0) > 1 and c[3].get('columns', 0) > 1]
    physical_columns = len((geometry or {}).get('x_grid', []))-1
    physical_rows = len((geometry or {}).get('y_grid', []))-1
    kept = []
    for candidate in candidates:
        profile = candidate[3]
        collapsed = profile.get('rows') == 1 or profile.get('columns') == 1
        equivalent = any(Counter(_key(profile.get('text', '').replace('\\n', ' '))) ==
                         Counter(_key(other[3].get('text', '').replace('\\n', ' ')))
                         for other in complete)
        source_collapse = (profile.get('columns') == 1 and physical_columns > 1 or
                           profile.get('rows') == 1 and physical_rows > 1)
        if collapsed and (source_collapse or complete and equivalent):
            if diagnostic:
                diagnostic('TABLE_COLLAPSED_CANDIDATE_REJECTED', **context,
                           method=candidate[1], rows=profile.get('rows'),
                           columns=profile.get('columns'), source_columns=physical_columns,
                           reason='source_grid_disproves_scalar_transcription' if source_collapse
                                  else 'same_characters_have_complete_structured_read')
            continue
        kept.append(candidate)
    return kept


def image_table_content_proof(profile):
    """Character/MathML anchors with multiplicities for complete publication.

    Empty padding has no content anchor. Line-wrap hyphens, Unicode unit
    variants and whitespace normalize; letters, values and math structure
    remain significant. Header replacements require their own source proof.
    """
    from docling_pdf_repairs import math_signature
    from latex2mathml.converter import convert

    header, body = Counter(), Counter()
    for cell in profile.get('cells', []):
        parsed = BeautifulSoup(cell.get('html', cell.get('text', '')), 'html.parser')
        formulas = []
        for node in parsed.find_all('math'):
            if node.find_parent('math'):
                continue
            math = str(node)
            if not node.find(['mi', 'mn', 'mo', 'mrow', 'mfrac', 'msub', 'msup']):
                try:
                    math = convert(node.get('data-latex') or node.get_text())
                except Exception:
                    # An unrenderable original equation remains an exact
                    # literal anchor and cannot be omitted by publication.
                    math = None
            formulas.append(repr(math_signature(math)) if math else 'literal:'+node.get_text())
            node.decompose()
        text = parsed.get_text('\n', strip=True).replace('\\n', '\n')
        text = re.sub(r'(?<=\w)[-\u00ad]\s*\n\s*(?=\w)', '', text)
        text = re.sub(r'[\s\u00ad\u200b]+', '', unicodedata.normalize('NFKC', text))
        key = text+'\u241f'+'\u241f'.join(formulas)
        if text or formulas:
            (header if cell.get('header') else body)[key] += 1
    return dict(rows=profile.get('rows', 0), columns=profile.get('columns', 0),
                header_anchors=dict(header), body_anchors=dict(body))


def _raster_record_coverage(regions, page_no, bounds, records):
    """Prove that table-adjacent raster notes also have positioned OCR.

    Raster table inserts often include the caption and footnotes. Ownership
    must cover those too, rather than deleting them with a table-only crop.
    Remove the real raster rules, then require every remaining source-ink
    pixel to lie inside a retained content block. The padding is measured from
    the source's typical connected glyph height, not from a page percentage.
    """
    import cv2
    import numpy as np
    from statistics import median

    image = regions.crop(page_no, bounds)
    try:
        gray = np.asarray(image.convert('L'))
    finally:
        image.close()
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    heights = [int(h) for _, _, w, h, area in stats[1:]
               if h > 1 and w > 1 and h < gray.shape[0] and w < gray.shape[1]]
    if not heights:
        return None
    glyph_height = median(heights)
    rail = max(2, round(3*glyph_height))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, rail), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((rail, 1), np.uint8))
    rules = cv2.bitwise_or(horizontal, vertical)
    # Morphological opening shortens a skewed or broken rail at its ends.
    # Expand by the measured stroke radius, not by an arbitrary page margin.
    distances = cv2.distanceTransform(rules, cv2.DIST_L2, 3)
    stroke_radius = max(1, round(float(np.median(distances[distances > 0])))) if np.any(distances) else 1
    rules = cv2.dilate(rules, np.ones((2*stroke_radius+1, 2*stroke_radius+1), np.uint8))
    residual = cv2.bitwise_and(ink, cv2.bitwise_not(rules))
    sx = gray.shape[1]/(bounds[2]-bounds[0])
    sy = gray.shape[0]/(bounds[3]-bounds[1])
    # OCR boxes can end at the final baseline rather than the descenders.
    # One measured source-glyph height covers that coordinate uncertainty;
    # a missing text row still leaves ink beyond the normal line clearance.
    pad = glyph_height
    used = []
    for index, record in enumerate(records):
        if record.get('page_no') != page_no or record.get('label') in {'PageHeader', 'PageFooter', 'Figure', 'Picture'}:
            continue
        box = record.get('bbox')
        if not box or not record.get('html', '').strip() or not _coverage(box, bounds):
            continue
        left = max(0, int((box[0]-bounds[0])*sx-pad))
        top = max(0, int((box[1]-bounds[1])*sy-pad))
        right = min(gray.shape[1], round((box[2]-bounds[0])*sx+pad))
        bottom = min(gray.shape[0], round((box[3]-bounds[1])*sy+pad))
        residual[top:bottom, left:right] = 0
        used.append(index)
    def axes(mask, vertical_axis):
        _, _, parts, _ = cv2.connectedComponentsWithStats(mask, 8)
        positions = sorted((x+w/2 if vertical_axis else y+h/2)
                           for x, y, w, h, _ in parts[1:])
        groups = []
        for position in positions:
            if groups and position-groups[-1][-1] <= 2*stroke_radius:
                groups[-1].append(position)
            else:
                groups.append([position])
        return [(bounds[0]+median(group)/sx if vertical_axis else bounds[1]+median(group)/sy)
                for group in groups]
    x_grid, y_grid = axes(vertical, True), axes(horizontal, False)
    intersection_count = cv2.connectedComponents(cv2.bitwise_and(horizontal, vertical), 8)[0]-1
    remaining = int(np.count_nonzero(residual))
    _, _, uncovered, _ = cv2.connectedComponentsWithStats(residual, 8)
    return dict(covered=not remaining, remaining_ink_pixels=remaining,
                source_glyph_height_pixels=glyph_height, supporting_record_indexes=used,
                x_grid=x_grid, y_grid=y_grid, grid_intersections=intersection_count,
                uncovered_components=[dict(bbox=[bounds[0]+x/sx, bounds[1]+y/sy,
                    bounds[0]+(x+w)/sx, bounds[1]+(y+h)/sy], pixels=int(area))
                    for x, y, w, h, area in uncovered[1:]])


def _outside_record_publication(doc, records, indexes, page_no, table_box, excluded, api, *, restore=False):
    """A retained OCR note is not proof that the assembler published it."""
    from docling_core.types.doc import ContentLayer, DocItemLabel

    results = []
    tolerance = 1/max(float(getattr(api, 'IMAGE_SCALE', 1)), 1)
    for index in indexes:
        record = records[index]
        if _coverage(record['bbox'], table_box) == 1:
            continue
        text = api.semantic_ocr_text(record['html'])
        key = _key(unicodedata.normalize('NFKC', text))
        if not key:
            continue
        matched, furniture = [], []
        for item in doc.texts:
            if item.self_ref in excluded:
                continue
            region = api.item_region(item, doc)
            if not region or region[0] != page_no:
                continue
            box = region[1]
            bounds = [box[0]-tolerance, box[1]-tolerance, box[2]+tolerance, box[3]+tolerance]
            if _coverage(record['bbox'], bounds) == 1:
                item_key = _key(unicodedata.normalize('NFKC', item.text))
                if (item.content_layer == ContentLayer.FURNITURE or
                        item.label in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}):
                    if item_key == key:
                        furniture.append(item)
                elif key in item_key:
                    matched.append(item.self_ref)
        restored = False
        if not matched and restore and record.get('label') in {'Text', 'SectionHeader', 'Caption', 'Formula', 'ListItem'}:
            item = furniture[0] if len(furniture) == 1 else api.insert_source_item(doc, page_no, record['bbox'], 'text')
            item.text, item.orig = text, text
            item.label, item.content_layer = DocItemLabel.TEXT, ContentLayer.BODY
            for prov in item.prov:
                prov.charspan = (0, len(text))
            # Source-proven continuation labels retain their table ownership;
            # they are not ordinary repeated running headers.
            caption = api.extract_table_caption(text)
            if caption:
                tables = [table for table in doc.tables if table.self_ref not in excluded and
                          (region := api.item_region(table, doc)) and region[0] == page_no and
                          api.overlap_ratio(region[1], table_box) > .6]
                if len(tables) == 1:
                    table = tables[0]
                    if item.parent and item.parent.cref != table.self_ref:
                        parent = item.parent.resolve(doc)
                        parent.children = [ref for ref in parent.children if ref.cref != item.self_ref]
                        item.parent = table.get_ref()
                    if not any(ref.cref == item.self_ref for ref in table.children):
                        table.children.append(item.get_ref())
                    if not any(ref.cref == item.self_ref for ref in table.captions):
                        table.captions.append(item.get_ref())
                    item.label = DocItemLabel.CAPTION
            matched, restored = [item.self_ref], True
            api.diagnostic_event('IMAGE_TABLE_OUTSIDE_TEXT_RESTORED', page=page_no,
                bbox=record['bbox'], ref=item.self_ref, source_record=index, text=text,
                method='source_raster_ink_and_retained_positioned_ocr',
                reason='layout_furniture_misclassification' if furniture else 'layout_assembly_omitted_content')
        results.append(dict(record=index, page=page_no, bbox=record['bbox'], text=text,
                            matched_refs=matched, published=bool(matched), restored=restored))
    return results


def recover_image_backed_tables(doc, reader, regions, records, routes, excluded, manifest, api):
    """Identify table-only raster inserts, as well as full-page scan tables.

    A raster table embedded in a native text PDF is not a separate drawing.
    Its retained 2D OCR table and its physical column grid establish ownership
    of the image.  This function records that ownership; publication must only
    suppress the whole source image after a complete table has been selected.
    """
    recovered = {}
    if reader is None:
        manifest['image_backed_tables'] = []
        return recovered
    for record in records:
        if '<table' not in record.get('html', '').lower() or not record.get('bbox'):
            continue
        from docling_pdf_forms import form_owns_region
        if form_owns_region(manifest, record['page_no'], record['bbox']):
            continue
        try:
            fragment, _ = api.repair_table_spans(api.clean_table_html(record['html']))
            profile = api.table_profile(fragment)
        except (ValueError, TypeError, KeyError) as error:
            api.diagnostic_event('IMAGE_BACKED_TABLE_RECORD_REJECTED', page=record['page_no'],
                                 bbox=record['bbox'], reason=str(error), exception=type(error).__name__)
            continue
        numbered_width = numbered_header_width(record['html'])
        repairable_header = bool(numbered_width and profile['issues'] and all(
            issue.startswith('numbered_header_width_') for issue in profile['issues']))
        if (profile['issues'] and not repairable_header) or profile['rows'] < 2 or profile['columns'] < 2:
            continue
        page_no, box = record['page_no'], record['bbox']
        page = reader.load_page(page_no)
        images, image_proofs, outside_records = [], [], []
        for obj in page.images:
            image_box = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
            # A whole-page background containing other content is not owned
            # by one of its tables.  Require both footprints to agree.
            if _coverage(box, image_box) < .9:
                continue
            if hasattr(reader, 'is_page_scan') and reader.is_page_scan(obj, box):
                continue
            proof = _raster_record_coverage(regions, page_no, image_box, records)
            if proof and proof['covered']:
                outside = _outside_record_publication(doc, records, proof['supporting_record_indexes'],
                                                      page_no, box, excluded, api, restore=True)
                if any(not note['published'] for note in outside):
                    api.diagnostic_event('IMAGE_BACKED_TABLE_IMAGE_KEEP', page=page_no, bbox=image_box,
                        reason='source_notes_not_published_by_layout_assembly',
                        missing_records=[note for note in outside if not note['published']])
                    continue
                images.append(obj)
                image_proofs.append(proof)
                outside_records.extend(outside)
            else:
                api.diagnostic_event('IMAGE_BACKED_TABLE_IMAGE_KEEP', page=page_no,
                                     bbox=image_box, reason='source_ink_outside_retained_content_blocks',
                                     proof=proof)
        if not images:
            continue
        geometry = regions.table_geometry(page_no, box)
        if not geometry:
            # Caption/footnote rows can defeat the global preview's area and
            # Hough grouping. The source insert's measured rails still prove
            # that the retained 2D table is not a one-cell paragraph.
            proof = next((entry for entry in image_proofs if len(entry['x_grid']) > 2
                          and len(entry['y_grid']) > 1 and entry['grid_intersections'] >= 4), None)
            if proof:
                geometry = dict(basis='source_insert_raster_rules', bbox=list(box),
                                x_grid=proof['x_grid'], y_grid=proof['y_grid'])
        if not geometry or len(geometry.get('x_grid', [])) < 3:
            continue
        matched = [item for item in doc.tables if item.self_ref not in excluded and
                   (region := api.item_region(item, doc)) and region[0] == page_no and
                   api.overlap_ratio(region[1], box) > .6]
        if not matched:
            matched = [api.insert_source_item(doc, page_no, box, 'table')]
        for item in matched:
            entry = dict(ref=item.self_ref, page=page_no, bbox=list(box),
                         image_bboxes=[[obj['x0'], obj['top'], obj['x1'], obj['bottom']] for obj in images],
                         image_content_proofs=image_proofs,
                         outside_records=outside_records,
                         rows=profile['rows'], columns=numbered_width or profile['columns'], geometry=geometry,
                         recoverable_header=repairable_header,
                         source_complete_profile=image_table_content_proof(profile),
                         method='retained_2d_ocr_and_source_raster_column_grid')
            recovered[item.self_ref] = entry
            api.diagnostic_event('IMAGE_BACKED_TABLE_IDENTIFIED', **entry)
    manifest['image_backed_tables'] = list(recovered.values())
    manifest['protected_image_table_text_refs'] = sorted({ref for entry in recovered.values()
        for record in entry['outside_records'] for ref in record['matched_refs']})
    return recovered


def table_owns_source_image(entry, bounds, profile):
    """Suppress a whole-table raster only after complete 2D publication.

    An in-cell drawing has different bounds and keeps its normal image route.
    A failed, collapsed or fallback-only table never consumes its source image.
    """
    if not entry or profile.get('issues') or profile.get('rows', 0) < 2 or profile.get('columns', 0) < 2:
        return False
    if any(not record.get('published') for record in entry.get('outside_records', [])):
        return False
    if (profile.get('rows', 0) < entry.get('rows', 0) or
            profile.get('columns') != entry.get('columns', profile.get('columns'))):
        return False
    expected = entry.get('source_complete_profile')
    if not expected:
        return False
    published = image_table_content_proof(profile)
    if published['body_anchors'] != expected['body_anchors']:
        return False
    header_proof = entry.get('source_header_repair') or {}
    if header_proof.get('body_unchanged') and header_proof.get('method') == 'source_raster_cells_and_independent_number_bar_ocr':
        accepted = entry.get('verified_header_content')
        if not accepted or published['header_anchors'] != accepted['header_anchors']:
            return False
    elif published['header_anchors'] != expected['header_anchors']:
        return False
    return any(_coverage(bounds, source) >= .99 and _coverage(source, bounds) >= .99
               for source in entry['image_bboxes'])


def numbered_source_header_plan(fragment, source_image, source_region, api):
    """Prove a damaged numbered header's real cells before regional OCR.

    Source rails determine row/column spans. The printed 1..N bar is checked
    against a separate numeric OCR of that exact source strip. Text OCR is
    requested only for the resulting physical header cells; source body HTML
    is retained unchanged. No column count is inferred from a preview alone.
    """
    import math
    import cv2
    import numpy as np
    from statistics import median
    from pdfplumber.table import (merge_edges, edges_to_intersections,
                                 intersections_to_cells, cells_to_tables)
    from docling_pdf_math_grid import raster_grid, _numeric_reader

    width = numbered_header_width(fragment)
    if width is None:
        raise ValueError('No unambiguous source-numbered header supported by body rows')
    parsed = BeautifulSoup(fragment, 'html.parser')
    table = parsed.find('table')
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    numbered_row = None
    for index, row in enumerate(rows):
        nodes = row.find_all(['td', 'th'], recursive=False)
        while nodes and not nodes[-1].get_text(strip=True) and not nodes[-1].find(['math', 'img']):
            nodes = nodes[:-1]
        if [node.get_text(' ', strip=True) for node in nodes] == [str(i) for i in range(1, width+1)]:
            numbered_row = index
            break
    if numbered_row is None:
        raise ValueError('Printed column-number row not located in retained table')
    grid = raster_grid(source_image, source_region)
    edges = [dict(x0=left, x1=right, top=y, bottom=y, width=right-left,
                  height=0, orientation='h') for left, right, y in grid['horizontal']]
    edges += [dict(x0=x, x1=x, top=top, bottom=bottom, width=0,
                   height=bottom-top, orientation='v') for x, top, bottom in grid['connected_vertical']]
    sx = source_image.width/(source_region[2]-source_region[0])
    sy = source_image.height/(source_region[3]-source_region[1])
    gray = np.asarray(source_image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    distances = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    radius = max(1, round(float(np.median(distances[distances > 0])))) if np.any(distances) else 1
    tolerance = 2*radius/min(sx, sy)
    edges = merge_edges(edges, tolerance, tolerance, tolerance, tolerance)
    intersections = edges_to_intersections(edges, 2*tolerance, 2*tolerance)
    candidates = cells_to_tables(intersections_to_cells(intersections))
    if not candidates:
        raise ValueError('Source rails do not establish closed physical header cells')
    boxes = max(candidates, key=lambda group: sum((b[2]-b[0])*(b[3]-b[1]) for b in group))

    def axis(values):
        groups = []
        for value in sorted(values):
            if groups and value-groups[-1][-1] <= tolerance:
                groups[-1].append(value)
            else:
                groups.append([value])
        return [float(median(group)) for group in groups]

    xs = axis([x for box in boxes for x in (box[0], box[2])])
    ys = axis([y for box in boxes for y in (box[1], box[3])])
    if len(xs)-1 != width:
        raise ValueError(f'Source raster has {len(xs)-1} columns but the printed header has {width}')
    cells = []
    for box in boxes:
        left, right = [min(range(len(xs)), key=lambda i: abs(xs[i]-x)) for x in (box[0], box[2])]
        top, bottom = [min(range(len(ys)), key=lambda i: abs(ys[i]-y)) for y in (box[1], box[3])]
        cells.append(dict(row=top, column=left, row_span=bottom-top, column_span=right-left,
                          bbox=list(map(float, box))))
    source_number_row, numeric_proof = None, None
    for row in range(len(ys)-1):
        line = sorted((cell for cell in cells if cell['row'] == row), key=lambda cell: cell['column'])
        if (len(line) != width or any(cell['row_span'] != 1 or cell['column_span'] != 1 for cell in line)
                or [cell['column'] for cell in line] != list(range(width))):
            continue
        bounds = (max(0, round((xs[0]-source_region[0])*sx)),
                  max(0, round((ys[row]-source_region[1])*sy)),
                  min(source_image.width, round((xs[-1]-source_region[0])*sx)),
                  min(source_image.height, round((ys[row+1]-source_region[1])*sy)))
        strip = source_image.crop(bounds)
        try:
            result = _numeric_reader()(np.asarray(strip)[:, :, ::-1].copy(),
                                       use_det=False, use_cls=False, use_rec=True)
        finally:
            strip.close()
        text = ' '.join(getattr(result, 'txts', None) or [])
        if re.sub(r'\s+', '', text) == ''.join(map(str, range(1, width+1))):
            source_number_row = row
            numeric_proof = dict(source_text=text, source_row=row,
                                 bbox=[xs[0], ys[row], xs[-1], ys[row+1]],
                                 scores=list(map(float, getattr(result, 'scores', None) or [])))
            break
    if source_number_row is None or source_number_row == 0:
        raise ValueError('Independent source strip OCR did not confirm the printed 1..N header bar')
    header = sorted((cell for cell in cells if cell['row'] < source_number_row),
                    key=lambda cell: (cell['row'], cell['column']))
    targets, blanks = [], []
    for cell in header:
        box = cell['bbox']
        # Remove only each physical rail's measured stroke width from this
        # OCR copy. The source bbox and original raster stay intact.
        left = max(0, math.ceil((box[0]-source_region[0])*sx)+2*radius)
        top = max(0, math.ceil((box[1]-source_region[1])*sy)+2*radius)
        right = min(source_image.width, math.floor((box[2]-source_region[0])*sx)-2*radius)
        bottom = min(source_image.height, math.floor((box[3]-source_region[1])*sy)-2*radius)
        if right <= left or bottom <= top:
            for target in targets:
                target['image'].close()
            raise ValueError('A measured header cell has no interior after source-rail removal')
        crop = source_image.crop((left, top, right, bottom))
        mask = ink[top:bottom, left:right]
        if not np.any(mask):
            blanks.append((cell['row'], cell['column']))
            crop.close()
        else:
            targets.append(dict(cell, image=crop))
    plan = dict(width=width, source_number_row=source_number_row,
        original_number_row=numbered_row, original_fragment=fragment,
        header_cells=header, x_grid=xs, y_grid=ys, targets=targets, blank_cells=blanks,
        proof=dict(method='source_raster_cells_and_independent_number_bar_ocr',
                   original_header_rows=numbered_row, source_header_rows=source_number_row,
                   columns=width, physical_cells=len(cells), numeric_bar=numeric_proof))
    return plan


def _header_payload_fingerprint(fragment):
    """Match prose formatting while retaining values, operators and scripts."""
    from bs4 import NavigableString
    from docling_pdf_repairs import math_signature
    from latex2mathml.converter import convert

    parsed = BeautifulSoup(fragment, 'html.parser')
    if parsed.find(['table', 'img', 'script']):
        return None
    parts, prose = [], []
    invalid = False
    def flush():
        text = ''.join(prose)
        prose.clear()
        # A numeric range minus is never a word-wrap hyphen.
        text = re.sub(r'(?<=[^\W\d_])[-\u00ad]\s*\n\s*(?=[^\W\d_])', '', text)
        text = unicodedata.normalize('NFKC', text)
        if any(char.isdigit() for char in text):
            # Keep decimal separators, numeric signs, identifiers and all
            # punctuation when a value occurs outside explicit MathML.
            text = re.sub(r'[\s\u00ad\u200b]+', '', text)
        else:
            def prose_case(match):
                word = match.group()
                folded = word.casefold()
                # Short tokens and non-Cyrillic identifiers can be case-
                # sensitive unit symbols (Па/па, MPa/mPa, C/c). Common prose
                # connectives are explicit; this guard only declines reuse.
                connectives = {'и','в','с','к','о','у','на','по','из','от','до','не','за',
                               'со','во','при','для','без','или','над','под','вне'}
                if len(word) <= 3 and text[:match.start()].rstrip().endswith(('°', ',', '/', '(', '[')):
                    return word
                if folded in connectives:
                    return folded
                if len(word) <= 3 or any('CYRILLIC' not in unicodedata.name(char, '') for char in word):
                    return word
                return folded
            text = re.sub(r'[^\W\d_]+', prose_case, text)
            operators = set('%‰°+−–—-*/^_=<>≤≥±≈×·∕∓√∞')
            text = ''.join(char for char in text if char.isalnum() or char in operators)
        if text:
            parts.append(('text', text))
    def visit(node):
        nonlocal invalid
        if isinstance(node, NavigableString):
            for char in str(node):
                decomposition = unicodedata.decomposition(char)
                if decomposition.startswith(('<super>', '<sub>')):
                    flush()
                    parts.append(('unicode_sup' if decomposition.startswith('<super>') else 'unicode_sub',
                                  unicodedata.normalize('NFKC', char)))
                else:
                    prose.append(char)
        elif getattr(node, 'name', None) == 'math':
            flush()
            rendered = str(node)
            if not node.find(['mi', 'mn', 'mo', 'mrow', 'mfrac', 'msub', 'msup', 'mtext']):
                try:
                    rendered = convert(node.get('data-latex') or node.get_text())
                except Exception:
                    invalid = True
                    return
            signature = math_signature(rendered)
            if signature is None:
                invalid = True
            parts.append(('math', signature))
        elif getattr(node, 'name', None) in {'sup', 'sub'}:
            flush()
            signature = _header_payload_fingerprint(node.decode_contents())
            parts.append((node.name, signature))
        elif getattr(node, 'name', None) == 'br':
            prose.append('\n')
        else:
            for child in getattr(node, 'children', []):
                visit(child)
    visit(parsed)
    flush()
    return tuple(parts) if parts and not invalid else None


def _header_prose_words(fragment):
    """Only natural Cyrillic prose is eligible for secondary-word consensus."""
    parsed = BeautifulSoup(fragment, 'html.parser')
    if parsed.find(['math', 'chem', 'sup', 'sub', 'img', 'table', 'script', 'svg', 'code']):
        return None
    text = parsed.get_text('\n', strip=True)
    text = re.sub(r'(?<=[А-Яа-яЁё])[-\u00ad]\s*\n\s*(?=[А-Яа-яЁё])', '', text)
    text = unicodedata.normalize('NFKC', text)
    if re.search(r'[%‰°+−–—*/^_=<>≤≥±≈×·∕∓√∞\d-]', text):
        return None
    if any(char.isalpha() and not re.fullmatch(r'[А-Яа-яЁё]', char) for char in text):
        return None
    words = re.findall(r'[А-Яа-яЁё]+', text)
    connectives = {'и','в','с','к','о','у','на','по','из','от','до','не','за',
                   'со','во','при','для','без','или','над','под','вне'}
    # A short remaining token can be an identifier or a case-sensitive unit.
    if not words or any(len(word) < 4 and word.casefold() not in connectives for word in words):
        return None
    return [word.casefold() for word in words]


def _one_edit_apart(left, right):
    if abs(len(left)-len(right)) > 1 or left == right:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    short, long = (left, right) if len(left) < len(right) else (right, left)
    index = next((i for i, (a, b) in enumerate(zip(short, long)) if a != b), len(short))
    return short[index:] == long[index+1:]


def _retained_header_prose_consensus(fragment, originals, api, context):
    """Prefer a unique actual primary word over an unknown secondary typo.

    The dictionary only validates a candidate already present in this source
    header. It never proposes or creates replacement normative wording.
    """
    words = _header_prose_words(fragment)
    if not words:
        return None
    getter = getattr(api, 'russian_word_dictionary', None)
    dictionary = getter() if getter else None
    if dictionary is None:
        api.diagnostic_event('HEADER_PRIMARY_PROSE_CONSENSUS_WARNING', **context,
                             reason='local_dictionary_unavailable', regional_html=fragment)
        return None
    unknown = [index for index, word in enumerate(words)
               if len(word) >= 4 and not dictionary.word_is_known(word)]
    if not unknown:
        return None
    candidates = {}
    for payload, refs in originals.items():
        primary = _header_prose_words(payload)
        if not primary or len(primary) != len(words):
            continue
        differences = [index for index, pair in enumerate(zip(primary, words)) if pair[0] != pair[1]]
        if len(differences) != 1 or differences[0] not in unknown:
            continue
        index = differences[0]
        if (len(primary[index]) < 4 or not dictionary.word_is_known(primary[index]) or
                not _one_edit_apart(primary[index], words[index])):
            continue
        corrected = list(words)
        corrected[index] = primary[index]
        # All other prose words and their order must already match. Numeric,
        # formula and unit-bearing labels never entered this route.
        if corrected != primary:
            continue
        candidates[payload] = dict(original_header_cells=refs, primary_word=primary[index],
                                   regional_word=words[index])
    if len(candidates) != 1:
        api.diagnostic_event('HEADER_PRIMARY_PROSE_CONSENSUS_WARNING', **context,
            reason='primary_source_payload_not_unique' if candidates else 'no_known_primary_source_word_agrees',
            regional_html=fragment, unknown_words=[words[index] for index in unknown],
            source_candidate_payloads=list(candidates))
        return None
    payload, proof = next(iter(candidates.items()))
    result = dict(source_payload=payload, regional_html=fragment,
                  method='unique_retained_header_word_and_dictionary', **proof)
    api.diagnostic_event('HEADER_PRIMARY_PROSE_RESTORED', **context, **result)
    return result


def apply_numbered_source_header(plan, labels, api):
    """Publish source-cell header spans while preserving every body node."""
    labels = {(int(label['row']), int(label['column'])): label['html'] for label in labels}
    blank = set(map(tuple, plan['blank_cells']))
    expected_keys = {(cell['row'], cell['column']) for cell in plan['header_cells']} - blank
    if set(labels)-expected_keys:
        raise ValueError('Regional header OCR returned labels outside the proven source cells')
    for cell in plan['header_cells']:
        key = cell['row'], cell['column']
        if key not in blank and not labels.get(key, '').strip():
            raise ValueError(f'Source header cell {key} was not read; no rectangular guess published')
    original = BeautifulSoup(plan['original_fragment'], 'html.parser').find('table')
    rows = [row for row in original.find_all('tr') if row.find_parent('table') is original]
    originals, original_payloads = {}, {}
    for row_index, row in enumerate(rows[:plan['original_number_row']]):
        for cell_index, cell in enumerate(row.find_all(['td', 'th'], recursive=False)):
            payload = cell.decode_contents()
            original_payloads.setdefault(payload, []).append(dict(row=row_index, cell=cell_index))
            fingerprint = _header_payload_fingerprint(payload)
            if fingerprint:
                contents = originals.setdefault(fingerprint, {})
                contents.setdefault(payload, []).append(dict(row=row_index, cell=cell_index))
    body_rows = rows[plan['original_number_row']+1:]
    body_before = [str(row) for row in body_rows]
    soup = BeautifulSoup('<table><thead></thead><tbody></tbody></table>', 'html.parser')
    soup.table.attrs = dict(original.attrs)
    caption = original.find('caption', recursive=False)
    if caption:
        soup.table.insert(0, caption.extract())
    reused, ambiguous = [], []
    for row in range(plan['source_number_row']):
        tr = soup.new_tag('tr')
        for cell in (cell for cell in plan['header_cells'] if cell['row'] == row):
            node = soup.new_tag('th')
            if cell['row_span'] > 1:
                node['rowspan'] = str(cell['row_span'])
            if cell['column_span'] > 1:
                node['colspan'] = str(cell['column_span'])
            fragment = labels.get((cell['row'], cell['column']), '')
            parsed = BeautifulSoup(fragment, 'html.parser')
            if parsed.find(['table', 'img', 'script']):
                raise ValueError('Regional header OCR returned nontext content')
            if (cell['row'], cell['column']) not in blank and not parsed.get_text(strip=True):
                raise ValueError('A nonempty source header cell was replaced by empty markup')
            fingerprint = _header_payload_fingerprint(fragment)
            matching = originals.get(fingerprint, {}) if fingerprint else {}
            if len(matching) == 1:
                payload, refs = next(iter(matching.items()))
                if payload != fragment:
                    parsed = BeautifulSoup(payload, 'html.parser')
                    reused.append(dict(row=cell['row'], column=cell['column'],
                        original_header_cells=refs, source_payload=payload,
                        method='unique_primary_and_regional_prose_math_fingerprint'))
            elif len(matching) > 1:
                ambiguous.append(dict(row=cell['row'], column=cell['column'],
                                      distinct_original_payloads=len(matching)))
            else:
                consensus = _retained_header_prose_consensus(fragment, original_payloads, api,
                    dict(row=cell['row'], column=cell['column']))
                if consensus:
                    parsed = BeautifulSoup(consensus['source_payload'], 'html.parser')
                    reused.append(dict(row=cell['row'], column=cell['column'], **consensus))
            node.append(parsed)
            tr.append(node)
        soup.thead.append(tr)
    numbers = soup.new_tag('tr')
    for number in range(1, plan['width']+1):
        cell = soup.new_tag('th')
        cell.string = str(number)
        numbers.append(cell)
    soup.thead.append(numbers)
    for row in body_rows:
        soup.tbody.append(row.extract())
    if body_before != [str(row) for row in soup.tbody.find_all('tr', recursive=False)]:
        raise ValueError('Source header repair changed body content')
    result = str(soup.table)
    profile = api.table_profile(result, plan['width'])
    if profile['issues'] or profile['columns'] != plan['width']:
        raise ValueError('Source header repair did not produce a complete numbered source grid: '+str(profile['issues']))
    return result, dict(**plan['proof'], body_rows_preserved=len(body_rows),
                        header_cells_read=len(labels), body_unchanged=True,
                        header_labels_reused=reused, header_labels_reused_count=len(reused),
                        header_labels_ambiguous=ambiguous)


def align_source_reference_grid(fragment, cells, source_image, source_region, xs, ys, api):
    """Align OCR cells to source rules, retaining empty double-rule bands.

    A double horizontal rail can form a spurious physical row while OCR
    represents the same table with one fewer row and oversized colspans.
    Skip only source bands with zero nongrid ink and zero positioned glyphs.
    Then require an exact, monotone row/cell-count match. Every original cell
    payload is retained verbatim; only source-proven spans are assigned.
    """
    import cv2
    import numpy as np
    from statistics import median
    from docling_pdf_math_grid import raster_grid

    parsed = BeautifulSoup(fragment, 'html.parser')
    table = parsed.find('table')
    if table is None or len(xs) < 2 or len(ys) < 2:
        return fragment, None
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    raw_nodes = [row.find_all(['td', 'th'], recursive=False) for row in rows]
    # An OCR decoder sometimes appends a blank cell beyond the measured
    # source border. It carries no payload and must not force rejection of
    # an otherwise exact source-row alignment. Interior blanks remain cells.
    def payload(node):return bool(node.get_text(strip=True) or node.find(['math','img','svg']))
    trimmed_reference_cells=[]
    nonempty_refs = [i for i, nodes in enumerate(raw_nodes) if any(
        node.get_text(strip=True) or node.find(['math', 'img', 'svg']) for node in nodes)]
    if not nonempty_refs:
        return fragment, None
    gray = np.asarray(source_image.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    sx = source_image.width/(source_region[2]-source_region[0])
    sy = source_image.height/(source_region[3]-source_region[1])
    _, _, parts, _ = cv2.connectedComponentsWithStats(ink, 8)
    heights = [int(h) for _, _, w, h, _ in parts[1:]
               if w > 1 and h > 1 and w < source_image.width and h < source_image.height]
    if not heights:
        return fragment, None
    glyph_height = median(heights)
    length = max(2, round(3*glyph_height))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, length), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((length, 1), np.uint8))
    distances = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    radius = max(1, round(float(np.median(distances[distances > 0])))) if np.any(distances) else 1
    grid = raster_grid(source_image, source_region)
    horizontal_support = np.zeros_like(ink)
    vertical_support = np.zeros_like(ink)
    def point(x, y):
        return round((x-source_region[0])*sx), round((y-source_region[1])*sy)
    # The projection only selects already measured source-rule pixels from
    # the morphological masks. It never erases nearby short text strokes.
    for left, right, y in grid['horizontal']:
        cv2.line(horizontal_support, point(left, y), point(right, y), 255, 2*radius+3)
    for x, top, bottom in grid['connected_vertical']:
        cv2.line(vertical_support, point(x, top), point(x, bottom), 255, 2*radius+3)
    rules = cv2.bitwise_or(cv2.bitwise_and(horizontal, horizontal_support),
                          cv2.bitwise_and(vertical, vertical_support))
    nongrid = cv2.bitwise_and(ink, cv2.bitwise_not(rules))
    measured_grid = None
    empty, source_rows = [], []
    for row in range(len(ys)-1):
        left = max(0, round((xs[0]-source_region[0])*sx))
        right = min(source_image.width, round((xs[-1]-source_region[0])*sx)+1)
        top = max(0, round((ys[row]-source_region[1])*sy))
        bottom = min(source_image.height, round((ys[row+1]-source_region[1])*sy)+1)
        residual = int(np.count_nonzero(nongrid[top:bottom, left:right]))
        glyphs = [glyph for cell in cells for glyph in cell.get('glyphs', [])
                  if glyph.get('text', '').strip() and
                  ys[row] <= (glyph['top']+glyph['bottom'])/2 < ys[row+1]]
        if residual and not glyphs:
            # The measured rail may retain a connected antialiasing fringe.
            # Reuse the stricter double-rule proof: a short band bounded by
            # complete rails, with no independent dot, letter or other ink.
            if measured_grid is None:
                from docling_pdf_table_layout import grid_ink
                measured_grid = grid_ink(source_image, source_region, xs)
            if measured_grid and ys[row+1]-ys[row] < measured_grid['glyph_height']:
                tolerance = max(1/measured_grid['sy'], measured_grid['stroke_radius']/measured_grid['sy'])
                if all(any(abs(y-rail) <= tolerance for rail in measured_grid['full_width_rules'])
                       for y in (ys[row], ys[row+1])):
                    residual = int(np.count_nonzero(measured_grid['nongrid'][top:bottom, left:right]))
        if not residual and not glyphs:
            empty.append(dict(row=row, bbox=[xs[0], ys[row], xs[-1], ys[row+1]],
                              nongrid_ink_pixels=0, positioned_glyphs=0,
                              method='only_connected_source_rule_pixels'))
        else:
            source_rows.append(row)
    if len(source_rows) != len(nonempty_refs):
        api.diagnostic_event('SCAN_REFERENCE_GRID_ALIGNMENT_SKIP',
            reason='nonempty_source_and_reference_row_counts_disagree',
            source_rows=len(source_rows), reference_rows=len(nonempty_refs),
            proven_empty_rows=[entry['row'] for entry in empty])
        return fragment, None
    row_mapping = dict(zip(source_rows, nonempty_refs))
    source_cells = {row: sorted((cell for cell in cells if cell['row'] == row),
                                key=lambda cell: cell['column']) for row in range(len(ys)-1)}
    for source_row, ref_row in row_mapping.items():
        expected=len(source_cells[source_row])
        while len(raw_nodes[ref_row])>expected and not payload(raw_nodes[ref_row][-1]):
            trimmed_reference_cells.append(dict(reference_row=ref_row,reference_cell=len(raw_nodes[ref_row])-1,
                reason='empty_trailing_ocr_cell_outside_measured_source_row'))
            raw_nodes[ref_row].pop()
    aligned_rows = [(raw_nodes[ref_row], source_cells[source_row])
                    for source_row, ref_row in row_mapping.items()
                    if len(raw_nodes[ref_row]) == len(source_cells[source_row])]
    for source_row, ref_row in row_mapping.items():
        if len(raw_nodes[ref_row]) > len(source_cells[source_row]):
            from docling_pdf_cell_alignment import remove_source_proven_duplicate
            reduced, duplicate = remove_source_proven_duplicate(raw_nodes[ref_row], source_cells[source_row],
                aligned_rows, nongrid, source_region, sx, sy, radius)
            if duplicate:
                raw_nodes[ref_row] = reduced
                trimmed_reference_cells.append(dict(reference_row=ref_row, **duplicate))
        if len(source_cells[source_row]) != len(raw_nodes[ref_row]):
            api.diagnostic_event('SCAN_REFERENCE_GRID_ALIGNMENT_SKIP',
                reason='source_and_reference_cell_counts_disagree', source_row=source_row,
                reference_row=ref_row, source_cells=len(source_cells[source_row]),
                reference_cells=len(raw_nodes[ref_row]))
            return fragment, None
    soup = BeautifulSoup('<table></table>', 'html.parser')
    soup.table.attrs = dict(table.attrs)
    caption = table.find('caption', recursive=False)
    if caption:
        soup.table.append(BeautifulSoup(str(caption), 'html.parser').caption)
    source_payloads, result_payloads, mapping, row_groups = [], [], [], {}
    for source_row, ref_row in row_mapping.items():
        parent = rows[ref_row].find_parent(['thead', 'tbody', 'tfoot'])
        row_groups[source_row] = parent.name if parent else 'tbody'
    for row in range(len(ys)-1):
        if row not in row_groups:
            before = [index for index in row_groups if index < row]
            after = [index for index in row_groups if index > row]
            row_groups[row] = (row_groups[max(before)] if before else row_groups[min(after)])
    for row in range(len(ys)-1):
        group_name = row_groups[row]
        group = soup.table.find(group_name, recursive=False)
        if group is None:
            group = soup.new_tag(group_name)
            soup.table.append(group)
        tr = soup.new_tag('tr')
        reference = raw_nodes[row_mapping[row]] if row in row_mapping else None
        for index, cell in enumerate(source_cells[row]):
            if reference is None:
                node = soup.new_tag('th' if group_name == 'thead' else 'td')
            else:
                original = reference[index]
                payload = original.decode_contents()
                node = BeautifulSoup(str(original), 'html.parser').find(['td', 'th'])
                source_payloads.append(payload)
                result_payloads.append(node.decode_contents())
                mapping.append(dict(source_row=row, source_column=cell['column'],
                    source_row_span=cell['row_span'], source_column_span=cell['column_span'],
                    source_bbox=cell['bbox'], reference_row=row_mapping[row],
                    reference_cell=index))
            for attr, key in (('rowspan', 'row_span'), ('colspan', 'column_span')):
                if cell[key] == 1:
                    node.attrs.pop(attr, None)
                else:
                    node[attr] = str(cell[key])
            tr.append(node)
        group.append(tr)
    if source_payloads != result_payloads:
        return fragment, None
    result = str(soup.table)
    profile = api.table_profile(result, len(xs)-1)
    if profile['issues'] or profile['rows'] != len(ys)-1 or profile['columns'] != len(xs)-1:
        api.diagnostic_event('SCAN_REFERENCE_GRID_ALIGNMENT_SKIP',
            reason='reconstructed_source_grid_invalid', issues=profile['issues'])
        return fragment, None
    proof = dict(method='source_rule_bands_and_exact_reference_cell_order',
        source_rows=len(ys)-1, source_columns=len(xs)-1, reference_rows=len(rows),
        empty_rule_bands=empty, mapping=mapping, source_payloads_unchanged=True,
        trimmed_reference_cells=trimmed_reference_cells,
        header_rows=sum(name == 'thead' for name in row_groups.values()),
        profile={key:profile[key] for key in ('rows', 'columns', 'issues')})
    api.diagnostic_event('SCAN_REFERENCE_GRID_ALIGNED', **proof)
    return result, proof
