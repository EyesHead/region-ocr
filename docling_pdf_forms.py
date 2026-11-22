"""Explicit form and figure captions retain their source bodies as images."""
import hashlib
import re
from types import SimpleNamespace


FORM_HEADING = re.compile(r'^\s*(?:(?:Продолжение|Окончание)\s+формы|Ф\s*о\s*р\s*м\s*а)\s*[.:№]?\s*'
                          r'(?P<number>(?:[A-ZА-ЯЁ]\s*\.\s*)?\d+(?:[.\-]\d+)*)'
                          r'(?:(?:\s*[.:—–−-]\s*|\s+)(?P<title>\S[\s\S]*))?[.]?\s*$', re.I)
FIGURE_CAPTION = re.compile(r'^\s*Р\s*и\s*с(?:\s*у\s*н\s*о\s*к|\s*\.)\s*[.:№]?\s*(?P<number>(?:[A-ZА-ЯЁ]\s*\.\s*)?\d+'
                            r'(?:\s*[.\-]\s*\d+)*)'
                            r'(?:\s*[.:—–−-]\s*(?P<title>\S[\s\S]*))?[.]?\s*$', re.I)


def form_owns_region(manifest, page, box):
    """Explicit caption image exceptions own their source rectangles."""
    if not box:
        return False
    entries = [*manifest.get('source_form_figures', []), *manifest.get('captioned_figures', [])]
    return any(entry['page'] == page and entry['bbox'][0]-2 <= box[0]
        and entry['bbox'][1]-2 <= box[1] and box[2] <= entry['bbox'][2]+2
        and box[3] <= entry['bbox'][3]+2 for entry in entries)


def attach_form_dimensions(soup, doc, records, manifest, reader, regions, api):
    """Expose exterior dimensions of a captioned drawing that also has a measured table."""
    from bs4 import BeautifulSoup
    from docling_pdf_logical_rows import _same_region
    from docling_pdf_table_content import _source_text_coverage

    if reader is None or regions is None:
        return
    recovered = manifest.get('recovered_scan_tables', [])
    if not recovered:
        return
    for picture in doc.pictures:
        loc = api.item_region(picture, doc)
        if not loc or len(picture.captions) != 1:
            continue
        page, box = loc
        title = getattr(picture.captions[0].resolve(doc), 'text', '')
        if not FORM_HEADING.fullmatch(re.sub(r'^\s*Ф\s*о\s*р\s*м\s*а\b', 'Форма', title, flags=re.I)):
            continue
        grids = [r for r in recovered if r['page'] == page and api._captioned_figure_contains(box, r['bbox'])]
        packets = [r for r in records if r.get('page_no') == page and r.get('label') == 'Table'
                   and _same_region(r.get('bbox'), box)]
        figures = [cap.parent for cap in soup.find_all('figcaption') if cap.parent.name == 'figure'
                   and api.furniture_key(cap.get_text(' ', strip=True)) == api.furniture_key(title)]
        if len(grids) != 1 or len(packets) != 1 or len(figures) != 1:
            continue
        grid, figure = grids[0], figures[0]
        pictures = figure.find_all('img')
        if len(pictures) != 1 or figure.has_attr('data-form-grid'):
            continue
        primary = BeautifulSoup(packets[0]['html'], 'html.parser')
        rows = primary.find_all('tr')
        if len(primary.find_all('table')) != 1 or len(rows) < 3:
            continue
        words = reader.load_page(page).extract_words(return_chars=True)
        for side, row, top, bottom in (('above', rows[0], box[1], grid['bbox'][1]),
                                       ('below', rows[-1], grid['bbox'][3], box[3])):
            values = row.get_text(' ', strip=True).split()
            if not values or not all(v.isdecimal() for v in values):
                continue
            band = sorted((w for w in words if box[0] <= w['x0'] and w['x1'] <= box[2]
                           and top <= (w['top']+w['bottom'])/2 <= bottom), key=lambda w: w['x0'])
            if ([w['text'] for w in band] != values or max(w['top'] for w in band) >= min(w['bottom'] for w in band)
                    or any(w['top'] < top or w['bottom'] > bottom for w in band)):
                continue
            proofs = []
            for w in band:
                bounds = [w[k] for k in ('x0', 'top', 'x1', 'bottom')]
                with regions.crop(page, bounds) as crop:
                    support = _source_text_coverage(crop, [{'label': 'Text'}], reader, bounds, page, api, w['text'])
                if support is None:
                    break
                proofs.append(support[1])
            if len(proofs) != len(values):
                continue
            label = soup.new_tag('p', attrs={'data-form-dimensions': side})
            label.string = ('Размеры сверху — ' if side == 'above' else 'Размеры снизу — ') + ', '.join(values)
            (pictures[0].insert_before if side == 'above' else pictures[0].insert_after)(label)
            figure['data-form-grid'] = grid['ref']
            manifest.setdefault('source_form_dimensions', []).append(dict(page=page, ref=grid['ref'],
                picture_ref=picture.self_ref, side=side, values=values, source_glyphs=proofs))


def _reparent_form(doc, owner, captions):
    """Keep each form reachable when its old floating container is suppressed."""
    from docling_core.types.doc import ContentLayer, DocItemLabel, FloatingItem

    parent = owner.parent.resolve(doc) if owner.parent else None
    anchor = None
    while parent is not None:
        if isinstance(parent, FloatingItem):
            anchor = parent
        parent = parent.parent.resolve(doc) if parent.parent else None
    if anchor is not None and anchor.parent:
        previous = owner.parent.resolve(doc)
        previous.children = [ref for ref in previous.children if ref.cref != owner.self_ref]
        parent = anchor.parent.resolve(doc)
        index = next(i for i, ref in enumerate(parent.children) if ref.cref == anchor.self_ref)
        parent.children.insert(index, owner.get_ref())
        owner.parent = parent.get_ref()
    for text in captions:
        for floating in [*doc.tables, *doc.pictures]:
            floating.captions = [ref for ref in floating.captions if ref.cref != text.self_ref]
        if text.parent:
            previous = text.parent.resolve(doc)
            previous.children = [ref for ref in previous.children if ref.cref != text.self_ref]
        text.parent = owner.get_ref()
        text.label, text.content_layer = DocItemLabel.CAPTION, ContentLayer.BODY
        if not any(ref.cref == text.self_ref for ref in owner.children):
            owner.children.append(text.get_ref())
        owner.captions.append(text.get_ref())


def _source_form_title(reader, regions, page, caption, body, em, api):
    """Locate a printed title; its pixels stay inside the form image."""
    if reader is None or regions is None:
        return None
    import cv2
    import numpy as np
    from docling_pdf_text_cells import positioned_text_proof

    reader.load_page(page)
    glyphs = [g for g in reader.chars if g.get('text', '').strip()
              and caption[3] <= (g['top']+g['bottom'])/2 < body[1]]
    lines = api.native_cell_lines(glyphs)
    if len(lines) != 1:
        return None
    line = lines[0]
    text = line['text'].strip()
    if (not re.search(r'[^\W\d_]{3}', text) or len(text) > 180
            or re.match(r'^\s*(?:\d+|[А-ЯA-Z]\.\d+)\b', text)
            or text.endswith(('.', ':', ';')) or FORM_HEADING.fullmatch(text)
            or FIGURE_CAPTION.fullmatch(text)):
        return None
    box = [min(g['x0'] for g in glyphs), min(g['top'] for g in glyphs),
           max(g['x1'] for g in glyphs), max(g['bottom'] for g in glyphs)]
    if (box[0] < body[0]-.25*em or box[2] > body[2]+.25*em
            or not body[0] <= (box[0]+box[2])/2 <= body[2]
            or body[1]-box[3] > 2*em):
        return None
    bounds = [box[0]-2, box[1]-2, box[2]+2, box[3]+2]
    image = regions.crop(page, bounds)
    try:
        with image.convert('L') as gray:
            ink = cv2.threshold(np.asarray(gray), 0, 255,
                                cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
        proof = positioned_text_proof(line['html'], glyphs, ink, bounds, bounds,
                                     image.width/(bounds[2]-bounds[0]),
                                     image.height/(bounds[3]-bounds[1]))
    finally:
        image.close()
    if proof is None:
        return None
    return dict(bbox=box, method='positioned_source_title_with_complete_ink_support',
                ink_coverage=proof['ink_coverage'], glyph_ink_support=proof['glyph_ink_support'],
                editable_text_published=False)


def reserve_layout_graphics(doc, proofs, manifest, api):
    """Keep crops authoritative even if Docling dropped an empty picture."""
    registry = {}
    for proof in proofs:
        page, body, line = proof['page'], proof['bbox'], proof['caption_bbox']
        located = [(item, api.item_region(item, doc)) for item in doc.pictures]
        owner = next((item for item, pos in located if pos and pos[0] == page
                      and max(abs(a-b) for a, b in zip(body, pos[1])) <= 1), None)
        if owner is None:
            owner = api.insert_source_item(doc, page, body, 'picture')
        located = [(item, api.item_region(item, doc)) for item in doc.texts]
        nearby = [(item, pos[1]) for item, pos in located if pos and pos[0] == page
                  and api.overlap_ratio(line, pos[1]) > .6 and item.text.strip()]
        # OCR can split the caption across several boxes, or include a whole
        # paragraph in one. Keep mixed paragraphs in their original position.
        refs = []
        if nearby:
            aligned = sorted(((item, box) for item, box in nearby if (
                min(box[3], line[3])-max(box[1], line[1])) >= .5*(box[3]-box[1])),
                key=lambda pair: pair[1][0])
            if (not proof['intervening_lines'] and aligned
                    and api.furniture_key(''.join(item.text for item, _ in aligned))
                    == api.furniture_key(proof['caption_text'])):
                refs = [item for item, _ in aligned]
        else:
            caption = api.insert_source_item(doc, page, line, 'text')
            caption.text = caption.orig = proof['caption_text']
            caption.prov[0].charspan = (0, len(caption.text))
            if not proof['intervening_lines']:
                refs = [caption]
        _reparent_form(doc, owner, refs)
        captions = [item.self_ref for item in refs]
        # Assembly may have assigned neighboring prose to the picture. Its
        # serializer emits only the source image and the verified caption.
        # Restore exterior children at their measured place in reading order.
        if owner.parent:
            from docling_core.types.doc import ContentLayer, DocItemLabel, TextItem
            parent = owner.parent.resolve(doc)
            exterior = []
            for ref in owner.children:
                child = ref.resolve(doc)
                if (ref.cref in captions or not isinstance(child, TextItem)
                        or len(child.prov) != 1 or child.parent != owner.get_ref()):
                    continue
                child_page, box = api.item_region(child, doc)
                if child_page == page and (box[3] <= body[1] or box[1] >= body[3]):
                    exterior.append((box, child))
            if exterior:
                moved = {child.self_ref for _, child in exterior}
                owner.children = [ref for ref in owner.children if ref.cref not in moved]
                owner.captions = [ref for ref in owner.captions if ref.cref not in moved]
                before, after = [], []
                for box, child in sorted(exterior, key=lambda pair: (pair[0][1], pair[0][0])):
                    child.parent = owner.parent
                    if child.label == DocItemLabel.CAPTION:
                        child.label, child.content_layer = DocItemLabel.TEXT, ContentLayer.BODY
                    (before if box[3] <= body[1] else after).append(child.get_ref())
                index = next(i for i, ref in enumerate(parent.children) if ref.cref == owner.self_ref)
                parent.children[index:index+1] = [*before, owner.get_ref(), *after]
        entry = dict(proof, ref=owner.self_ref, original_ref=owner.self_ref, original_bbox=body,
                     caption_ref=next(iter(captions), None), caption_refs=captions, caption_position='before',
                     source_font=line[3]-line[1], source_table_refs=[], excluded_refs=[],
                     method='layout_graphic_before_ocr', primary_records=[])
        registry[owner.self_ref] = entry
        manifest.setdefault('captioned_figures', []).append(entry)
    return registry


def clear_caption_tail(image, entry):
    """Remove a caption descender beside a higher dimension line at a source gutter."""
    import math
    import numpy as np
    if not entry.get('overlaps_caption_tail'):
        return
    l, t, r, b = entry['bbox']
    cl, ct, cr, cb = entry['caption_bbox']
    sx, sy = image.width/(r-l), image.height/(b-t)
    x0 = max(0, math.floor((cl-l)*sx)-1)
    x1 = min(image.width, math.ceil((cr-l)*sx)+1)
    limit = min(image.height, math.ceil((cb-t + .3*(cb-ct))*sy))
    if x1 <= x0 or limit <= 0:
        return
    with image.crop((x0, 0, x1, limit)) as strip, strip.convert('L') as gray:
        blank = np.flatnonzero((np.asarray(gray) >= 245).all(axis=1))
    if blank.size and blank[0] > 0:
        image.paste('white', (x0, 0, x1, int(blank[0])))


def capture_form_figures(doc, records, manifest, api, *, reader=None, regions=None, reserved=None):
    """Bind explicit captions to source boxes before semantic table repairs.

    The explicit heading owns the printed form regardless of whether layout
    recognition classified its body as a table or a picture. A table directly
    above an explicit figure caption has the same source-image exception.
    """
    def same_source_box(a, b):
        area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
        return api.overlap_ratio(a, b)*min(area_a, area_b)/max(area_a, area_b, 1e-9) >= .8

    objects = [(item, api.item_region(item, doc)) for item in [*doc.tables, *doc.pictures]]
    objects = [(item, pos) for item, pos in objects if pos]
    texts = [(item, api.item_region(item, doc)) for item in doc.texts]
    table_refs = {item.self_ref for item in doc.tables}
    from docling_pdf_captions import caption_prefix
    labelled_tables = []
    for table, pos in objects:
        if table.self_ref not in table_refs:
            continue
        for link in getattr(table, 'captions', ()):
            label = link.resolve(doc)
            loc = api.item_region(label, doc)
            if not loc or loc[0] != pos[0] or not caption_prefix(getattr(label, 'text', '')):
                continue
            gap = pos[1][1] - loc[1][3]
            height = max(1., loc[1][3] - loc[1][1])
            if -.35 * height <= gap <= max(64., 3 * height) and min(pos[1][2], loc[1][2]) > max(pos[1][0], loc[1][0]):
                labelled_tables.append(pos)
                break

    def owns_table(page, bounds):
        # Layout and primary OCR can report the same labelled grid twice.
        return any(number == page and (same_source_box(bounds, grid)
                   or api._captioned_figure_contains(bounds, grid)) for number, grid in labelled_tables)

    packet_items = {}
    packet_boxes = set()
    for index, record in enumerate(records):
        box, page = record.get('bbox'), record.get('page_no')
        if (not box or page not in doc.pages or box[2] <= box[0] or box[3] <= box[1]
                or not (str(record.get('label', '')).casefold() == 'table'
                        or '<table' in record.get('html', '').lower())):
            continue
        # Matching primary/layout detections are one candidate. A packet that
        # encloses only a small recovered fragment still supplies the full body.
        if any(pos[0] == page and item.self_ref in table_refs
               and same_source_box(box, pos[1]) for item, pos in objects):
            continue
        key = (page, tuple(box))
        if key in packet_boxes:
            continue
        packet_boxes.add(key)
        item = SimpleNamespace(self_ref=f'primary-table:{index}', captions=[])
        packet_items[item.self_ref] = record
        objects.append((item, (page, list(box))))
    registry = dict(reserved or {})
    furniture = {'page_header', 'page_footer'}

    def x_overlap(a, b):
        return max(0., min(a[2], b[2])-max(a[0], b[0]))/max(1e-9, min(a[2]-a[0], b[2]-b[0]))

    # Assembly can attach a form heading to the preceding grid, or merge
    # several forms into one picture. Retained heading geometry bounds each
    # body independently of those proposed links.
    form_headings = [(item, pos) for item, pos in texts if pos
                     and FORM_HEADING.fullmatch(getattr(item, 'text', ''))]
    for caption, position in sorted(texts, key=lambda pair:
            (pair[1][0], pair[1][1][1], pair[1][1][0]) if pair[1] else (float('inf'), 0, 0)):
        match = FORM_HEADING.fullmatch(getattr(caption, 'text', ''))
        is_form = bool(match)
        caption_before = is_form
        if not match:
            match = FIGURE_CAPTION.fullmatch(getattr(caption, 'text', ''))
        if not match or not position:
            continue
        if any(e['page'] == position[0] and api.overlap_ratio(e['caption_bbox'], position[1]) > .6
               for e in (reserved or {}).values()):
            continue
        if any(caption.self_ref in entry.get('caption_refs', [entry['caption_ref']])
               for entry in manifest.get('captioned_figures', [])):
            continue
        page, capbox = position
        em = max(1., capbox[3]-capbox[1])
        def form_bottom_limit(bounds):
            return min((pos[1][1] for item, pos in form_headings
                if item.self_ref != caption.self_ref and pos[0] == page
                and pos[1][1] >= capbox[3] and x_overlap(bounds, pos[1]) > 0),
                default=doc.pages[page].size.height)
        # Figure captions can occur on either side of their objects. Form
        # headings precede their bodies; stale assembly links cannot reverse it.
        sides = []
        for item, pos in objects:
            if pos[0] != page or not is_form and owns_table(page, pos[1]):
                continue
            bounds = pos[1]
            aligned = (x_overlap(bounds, capbox) >= .8 and
                       bounds[0]-.25*em <= (capbox[0]+capbox[2])/2 <= bounds[2]+.25*em)
            linked = any(getattr(r, 'cref', None) == caption.self_ref
                         for r in getattr(item, 'captions', []))
            for before, gap in ((True, bounds[1]-capbox[3]), (False, capbox[1]-bounds[3])):
                if not -.25*em <= gap <= 12*em:
                    continue
                anchored = aligned
                if before and not anchored:
                    anchored = bool(_source_form_title(reader, regions, page, capbox, bounds, em, api))
                if anchored:
                    sides.append((not linked, max(0., gap), before))
        if sides and not is_form:
            sides.sort()
            if any(s[0] == sides[0][0] and s[2] != sides[0][2]
                   and abs(s[1]-sides[0][1]) <= .5*em for s in sides):
                continue
            caption_before = sides[0][2]
        captions = [caption]
        following = sorted([(item, box[1]) for item, box in texts if box and box[0] == page
            and box[1][1] >= capbox[3]-.1*em and str(getattr(item, 'label', '')) not in furniture],
            key=lambda pair: (pair[1][1], pair[1][0]))
        candidates = []
        title_anchors = {}
        for item, pos in objects:
            bounds = pos[1]
            if (pos[0] != page or item.self_ref in registry or not is_form and owns_table(page, bounds)
                    or any(entry['page'] == page and api._captioned_figure_contains(
                        entry['original_bbox'], bounds) for entry in registry.values())):
                continue
            if is_form and bounds[3] > form_bottom_limit(bounds):
                continue
            near = (bounds[1] >= capbox[3]-.25*em and bounds[1]-capbox[3] <= 12*em
                    if caption_before else bounds[3] <= capbox[1]+.25*em
                    and capbox[1]-bounds[3] <= 12*em)
            if not near:
                continue
            aligned = (x_overlap(bounds, capbox) >= .8 and
                       bounds[0]-.25*em <= (capbox[0]+capbox[2])/2 <= bounds[2]+.25*em)
            if not aligned and caption_before:
                # A page-aligned form number can precede a centred drawing.
                # The intervening title supplies its independent source anchor.
                titles = [(t, b) for t, b in following if b[3] <= bounds[1]+.25*em
                          and bounds[1]-b[3] <= 2*em and x_overlap(bounds, b) >= .8
                          and str(getattr(t, 'label', '')) in {'section_header', 'caption'}
                          and not FORM_HEADING.fullmatch(t.text) and not FIGURE_CAPTION.fullmatch(t.text)]
                if len(titles) == 1:
                    aligned = True
                elif not titles:
                    anchor = _source_form_title(reader, regions, page, capbox, bounds, em, api)
                    if anchor:
                        title_anchors[tuple(bounds)] = anchor
                        aligned = True
            if aligned:
                candidates.append((item, bounds))
        if not candidates:
            api.diagnostic_event('SOURCE_FORM_BINDING_SKIPPED' if caption_before
                else 'SOURCE_FIGURE_TABLE_BINDING_SKIPPED', page=page,
                caption_ref=caption.self_ref, reason='no_positioned_source_object')
            continue
        linked = [(item, box) for item, box in candidates if not is_form and any(
            getattr(ref, 'cref', None) == caption.self_ref for ref in getattr(item, 'captions', []))]
        if linked:
            candidates = linked
        elif caption_before:
            candidates = [(item, box) for item, box in candidates if item.self_ref not in packet_items
                or not any(other.self_ref not in packet_items and same_source_box(box, bounds)
                           for other, bounds in candidates)]
        # Layout can report one printed form twice, as a table and a picture
        # with the exact same source rectangle. These are aliases of one body,
        # not competing caption owners. Keep both in objects so reconciliation
        # can exclude the alias after the whole source image is published.
        unique = []
        seen_boxes = set()
        for item, box in candidates:
            key = tuple(box)
            if key not in seen_boxes:
                seen_boxes.add(key)
                unique.append((item, box))
        candidates = unique
        candidates = [(item, box) for item, box in candidates if not any(
            other.self_ref != item.self_ref and outer != box
            and api._captioned_figure_contains(outer, box) for other, outer in candidates)]
        candidates.sort(key=lambda pair: (pair[1][1] if caption_before else -pair[1][3],
            max(0., pair[1][0]-capbox[2], capbox[0]-pair[1][2]),
            -(pair[1][2]-pair[1][0])*(pair[1][3]-pair[1][1])))
        if len(candidates) > 1 and (linked or abs(
                (candidates[1][1][1]-candidates[0][1][1]) if caption_before
                else (candidates[1][1][3]-candidates[0][1][3])) <= .5*em):
            api.diagnostic_event('SOURCE_CAPTION_BINDING_SKIPPED', page=page,
                caption_ref=caption.self_ref, reason='ambiguous_source_objects',
                candidates=[item.self_ref for item, _ in candidates])
            continue
        owner, box = candidates[0]
        next_form_top = form_bottom_limit(box)
        if not caption_before:
            existing = next((entry for entry in manifest.get('captioned_figures', [])
                if caption.self_ref in entry.get('caption_refs', [entry['caption_ref']])
                and api._captioned_figure_contains(entry.get('bbox'), box)), None)
            if existing:
                # Keep the already bound outer figure and its registry key
                # when layout also reports an inner table under its caption.
                existing['method'] = 'explicit_figure_table_heading'
                existing['caption_position'] = 'after'
                if owner.self_ref in table_refs and owner.self_ref not in existing['source_table_refs']:
                    existing['source_table_refs'].append(owner.self_ref)
                registry[existing['ref']] = existing
                continue
            intervening = [item for item, pos in objects if pos[0] == page
                and item.self_ref != owner.self_ref and not api._captioned_figure_contains(box, pos[1])
                and pos[1][1] >= box[3] and pos[1][3] <= capbox[1] and x_overlap(box, pos[1]) > 0]
            if intervening:
                continue
        between = ([(item, b) for item, b in following if item not in captions
                       and b[3] <= box[1]+.25*em and x_overlap(box, b) > 0]
                   if caption_before else [(item, pos[1]) for item, pos in texts if pos and pos[0] == page
                       and item.self_ref != caption.self_ref and str(getattr(item, 'label', '')) not in furniture
                       and pos[1][1] >= box[3]-.25*em and pos[1][3] <= capbox[1]+.25*em
                       and x_overlap(box, pos[1]) > 0])
        if between:
            # A number and a separately recognized title are one printed caption.
            if caption_before and not match.group('title') and len(between) <= 3 and all(
                    (str(getattr(item, 'label', '')) in {'section_header', 'caption'}
                     or len(item.text) <= 180 and not re.match(r'^\s*(?:\d+|[А-ЯA-Z]\.\d+)\b', item.text)
                     and not item.text.rstrip().endswith(('.', ':', ';')))
                    and not FORM_HEADING.fullmatch(item.text)
                    and not FIGURE_CAPTION.fullmatch(item.text) for item, _ in between):
                captions.extend(item for item, _ in between)
            else:
                # Independent prose is not evidence for a missing picture.
                # Keep it and the actual picture in their normal routes.
                continue
        last_caption = max(api.item_region(item, doc)[1][3] for item in captions)
        # Expand across overlapping/adjacent fragments only until actual prose.
        title_anchor = title_anchors.get(tuple(box))
        envelope = list(box)
        if title_anchor:
            title_box = title_anchor['bbox']
            envelope = [min(box[0], title_box[0]), min(box[1], title_box[1]),
                        max(box[2], title_box[2]), max(box[3], title_box[3])]
        owned = [(owner, box)]
        for item, pos in sorted(objects, key=lambda pair: (pair[1][1][1], pair[1][1][0]),
                                reverse=not caption_before):
            if pos[0] != page or item.self_ref == owner.self_ref or item.self_ref in registry:
                continue
            b = pos[1]
            if (is_form and b[3] > next_form_top) or (not is_form and owns_table(page, b)):
                continue
            if (b[1] < last_caption or b[1] > envelope[3]+em if caption_before
                    else b[3] > capbox[1] or b[3] < envelope[1]-em):
                continue
            gap_start, gap_end = (envelope[3], b[1]) if caption_before else (b[3], envelope[1])
            boundary = any(t.self_ref not in {c.self_ref for c in captions}
                and p and p[0] == page and gap_start < gap_end
                and p[1][1] < gap_end and p[1][3] > gap_start
                and x_overlap(envelope, p[1]) > 0
                and str(getattr(t, 'label', '')) not in furniture for t, p in texts)
            if boundary:
                continue
            if x_overlap(envelope, b) >= .8:
                expanded = [min(envelope[0], b[0]), min(envelope[1], b[1]),
                            max(envelope[2], b[2]), max(envelope[3], b[3])]
                if not is_form and owns_table(page, expanded):
                    continue
                owned.append((item, b))
                envelope = expanded
        # OCR packet boxes can retain thin rails or dimension strokes just
        # outside the layout object's bounds. Keep them before adding padding.
        padding = em*.5
        expanded = [envelope[0]-padding, envelope[1]-padding,
                    envelope[2]+padding, envelope[3]+padding]
        packets = [r for r in records if r.get('page_no') == page
                   and api._captioned_figure_contains(expanded, r.get('bbox'))]
        bounds = [envelope, *[r['bbox'] for r in packets]]
        size = doc.pages[page].size
        crop = [max(0., min(b[0] for b in bounds)-padding),
                max(last_caption if caption_before else 0., min(b[1] for b in bounds)-padding),
                min(size.width, max(b[2] for b in bounds)+padding),
                min(size.height, size.height if caption_before else capbox[1],
                    max(b[3] for b in bounds)+padding)]
        if is_form:
            crop[3] = min(crop[3], next_form_top)
        # Leave surrounding prose and page furniture outside the image. Text
        # inside the original body belongs to the form and is excluded later.
        caption_refs = {item.self_ref for item in captions}
        for text, pos in texts:
            if (not pos or pos[0] != page or text.self_ref in caption_refs
                    or api._captioned_figure_contains(envelope, pos[1])
                    or not api.overlap_ratio(crop, pos[1])):
                continue
            b = pos[1]
            if b[3] <= envelope[1]: crop[1] = max(crop[1], b[3])
            elif b[1] >= envelope[3]: crop[3] = min(crop[3], b[1])
            elif b[2] <= envelope[0]: crop[0] = max(crop[0], b[2])
            elif b[0] >= envelope[2]: crop[2] = min(crop[2], b[0])
        if not api._captioned_figure_contains(crop, envelope):
            api.diagnostic_event('SOURCE_CAPTION_BINDING_SKIPPED', page=page,
                caption_ref=caption.self_ref, reason='caption_overlaps_source_object',
                source_ref=owner.self_ref)
            continue
        if owner.self_ref in packet_items:
            # Materialize only the selected, validated packet. Unrelated OCR
            # packets remain untouched for the ordinary recovery route.
            packet_ref = owner.self_ref
            owner = api.insert_source_item(doc, page, box, 'table')
            table_refs.add(owner.self_ref)
            objects = [(owner if item.self_ref == packet_ref else item, pos) for item, pos in objects]
            owned = [(owner if item.self_ref == packet_ref else item, bounds) for item, bounds in owned]
            del packet_items[packet_ref]
        if is_form:
            _reparent_form(doc, owner, captions)
        entry = dict(ref=owner.self_ref, original_ref=owner.self_ref, page=page,
            original_bbox=list(envelope), bbox=crop, caption_ref=caption.self_ref,
            caption_refs=[item.self_ref for item in captions],
            caption_text=' — '.join(item.text for item in captions), caption_bbox=list(capbox),
            caption_position='before' if caption_before else 'after',
            caption_match='explicit_form_heading' if is_form else 'explicit_figure_table_heading', source_font=em,
            font_basis='positioned_primary_caption_height',
            source_table_refs=[item.self_ref for item, _ in owned if item.self_ref in table_refs],
            excluded_refs=[], method='explicit_source_form_heading' if is_form else 'explicit_figure_table_heading',
            primary_records=[dict(record_sha256=hashlib.sha256(r.get('html', '').encode()).hexdigest(),
                                  bbox=list(r['bbox'])) for r in packets])
        if title_anchor:
            entry['source_title_anchor'] = title_anchor
        registry[owner.self_ref] = entry
        entries = manifest.setdefault('captioned_figures', [])
        entries[:] = [existing for existing in entries if existing['ref'] != owner.self_ref]
        entries.append(entry)
        if is_form:
            forms = manifest.setdefault('source_form_figures', [])
            forms[:] = [existing for existing in forms if existing['ref'] != owner.self_ref]
            forms.append(entry)
        api.diagnostic_event('SOURCE_FORM_CROP_REGISTERED' if caption_before
                             else 'SOURCE_FIGURE_TABLE_CROP_REGISTERED', **entry)
    from docling_pdf_captions import bind_figure_pair_after_legend
    bind_figure_pair_after_legend(doc, records, registry, manifest, api)
    _capture_split_containers(doc, registry, manifest, regions, api)
    from docling_pdf_captions import (locate_prose_wrapping_a_figure, recover_detached_raster_captions,
                                     split_lists_crossing_illustrations,
                                     split_stacked_source_figures)
    recover_detached_raster_captions(doc, registry, manifest, reader, regions, api)
    split_lists_crossing_illustrations(doc, manifest, api)
    split_stacked_source_figures(doc, registry, manifest, reader, regions, api)
    locate_prose_wrapping_a_figure(doc, records, manifest, reader, regions, api)
    return registry


def _capture_split_containers(doc, registry, manifest, regions, api):
    """Replace a multi-form container only when every source ink pixel survives."""
    if regions is None:
        return
    import cv2
    import numpy as np
    import math
    from docling_core.types.doc import RefItem

    forms = [entry for entry in registry.values()
             if entry['method'] == 'explicit_source_form_heading']
    for item in [*doc.pictures, *doc.tables]:
        pos = api.item_region(item, doc)
        if item.self_ref in registry or not pos:
            continue
        page, box = pos
        children = [entry for entry in forms if entry['page'] == page
                    and api._captioned_figure_contains(
                        [box[0]-.25*entry['source_font'], box[1]-.25*entry['source_font'],
                         box[2]+.25*entry['source_font'], box[3]+.25*entry['source_font']],
                        entry['original_bbox'])]
        if len(children) < 2:
            continue
        image = regions.crop(page, box)
        try:
            with image.convert('L') as gray:
                ink = cv2.threshold(np.asarray(gray), 0, 255,
                                    cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
            sx, sy = image.width/(box[2]-box[0]), image.height/(box[3]-box[1])
            retained = [entry['bbox'] for entry in children]
            retained.extend(api.item_region(RefItem(cref=ref).resolve(doc), doc)[1]
                            for entry in children for ref in entry['caption_refs'])
            for b in retained:
                left, top = max(0, math.floor((b[0]-box[0])*sx)), max(0, math.floor((b[1]-box[1])*sy))
                right, bottom = min(image.width, math.ceil((b[2]-box[0])*sx)), min(image.height, math.ceil((b[3]-box[1])*sy))
                if left < right and top < bottom:
                    ink[top:bottom, left:right] = 0
            if np.any(ink):
                continue
        finally:
            image.close()
        entry = dict(ref=item.self_ref, page=page, bbox=box,
                     form_refs=[entry['ref'] for entry in children],
                     method='all_source_ink_retained_in_individual_forms_and_captions')
        manifest.setdefault('source_form_container_replacements', []).append(entry)
        api.diagnostic_event('SOURCE_FORM_CONTAINER_SPLIT', **entry)
