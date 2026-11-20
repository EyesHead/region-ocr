"""Bind table captions to the following source grid, using page coordinates.

Layout ownership is a proposal. A caption below one table and above another
belongs to the latter; cross-references in prose are never caption candidates.
"""
import re


def caption_prefix(text):
    return bool(re.match(r'^\s*(?:№\s*)?(?:(?:Продолжение|Окончание)\s+)?'
        r'(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|табл\.?)\s*'
        r'(?:[А-ЯЁA-Z]\s*\.?\s*)?\d',text,re.I))


def attach_source_captions(doc,excluded,manifest,api):
    from docling_core.types.doc import ContentLayer,DocItemLabel
    tables=[]
    for table in doc.tables:
        region=api.item_region(table,doc)
        if region and table.self_ref not in excluded:tables.append((table,*region))
    repairs=[]
    for item in doc.texts:
        if item.self_ref in excluded or not caption_prefix(item.text):continue
        caption=api.extract_table_caption(item.text);region=api.item_region(item,doc)
        if not caption or not region:continue
        page,box=region;choices=[]
        height=max(1,box[3]-box[1])
        for table,table_page,bounds in tables:
            if table_page!=page:continue
            gap=bounds[1]-box[3]
            overlap=max(0,min(box[2],bounds[2])-max(box[0],bounds[0]))
            width=min(max(1,box[2]-box[0]),max(1,bounds[2]-bounds[0]))
            if -.35*height<=gap<=max(64,3*height) and overlap/width>=.6:
                choices.append((max(0,gap),table,bounds))
        choices.sort(key=lambda choice:choice[0])
        if not choices:continue
        # Overlapping layout fragments are not enough to select an owner.
        if len(choices)>1 and abs(choices[0][0]-choices[1][0])<height*.5:continue
        _,target,bounds=choices[0]
        previous=[t.self_ref for t,_,_ in tables if any(c.cref==item.self_ref for c in t.captions)]
        if previous==[target.self_ref] and item.parent and item.parent.cref==target.self_ref:continue
        for table,_,_ in tables:
            table.captions=[c for c in table.captions if c.cref!=item.self_ref]
        if item.parent and item.parent.cref!=target.self_ref:
            parent=item.parent.resolve(doc)
            parent.children=[c for c in parent.children if c.cref!=item.self_ref]
        item.parent=target.get_ref()
        if not any(c.cref==item.self_ref for c in target.children):target.children.append(item.get_ref())
        target.captions.append(item.get_ref())
        item.label=DocItemLabel.CAPTION;item.content_layer=ContentLayer.BODY
        proof=dict(page=page,caption_ref=item.self_ref,caption=caption,caption_bbox=box,
            previous_tables=previous,table=target.self_ref,table_bbox=bounds,
            method='nearest_following_same_page_grid_without_intervening_grid')
        repairs.append(proof);api.diagnostic_event('TABLE_SOURCE_CAPTION_OWNER_REPAIRED',**proof)
    manifest['source_caption_ownership_repairs']=repairs
    return repairs


def restore_prose_beside_figures(doc, figures, records, excluded, manifest, api):
    """Return misassigned caption text to its reading position outside a source figure."""
    from collections import Counter
    from docling_core.types.doc import ContentLayer, DocItemLabel

    objects = (*doc.pictures, *doc.tables)
    uses = Counter(ref.cref for owner in objects for ref in owner.captions)
    records_by_page = {}
    for record in records:
        if record.get('label') == 'Text' and record.get('bbox'):
            records_by_page.setdefault(record['page_no'], []).append(record)
    for owner in objects:
        entry = figures.get(owner.self_ref)
        if not entry or not owner.parent:
            continue
        wrap = entry.get('wrapped_prose')
        if wrap and wrap['head'] not in excluded and wrap['tail'] not in excluded:
            from docling_core.types.doc import RefItem
            head = RefItem(cref=wrap['head']).resolve(doc)
            tail = RefItem(cref=wrap['tail']).resolve(doc)
            joiner = api.paragraph_separator(head.text)
            text = head.text.rstrip('\u00ad')
            head.prov[0].charspan = (0, len(text))
            head.text = text + joiner + tail.text
            head.orig = head.orig.rstrip('\u00ad') + joiner + tail.orig
            source = tail.prov[0].model_copy(deep=True)
            source.charspan = (len(text) + len(joiner), len(head.text))
            head.prov.append(source)
            excluded.add(tail.self_ref)
            entry['bbox'] = [*entry['bbox'][:3], wrap['cut']]
            manifest.setdefault('joined_prose_around_figures', []).append(dict(page=entry['page'], **wrap))
        selected = set(entry.get('caption_refs', [entry['caption_ref']]))
        parent = owner.parent.resolve(doc)
        siblings = parent.children
        if not any(ref.cref == owner.self_ref for ref in siblings):
            continue
        low = min(entry['bbox'][1], entry['caption_bbox'][1])
        high = max(entry['bbox'][3], entry['caption_bbox'][3])
        outside = []
        for ref in owner.captions:
            if ref.cref in selected or ref.cref in excluded or uses[ref.cref] != 1:
                continue
            item = ref.resolve(doc)
            loc = api.item_region(item, doc)
            if (not loc or loc[0] != entry['page'] or item.children
                    or getattr(item.parent, 'cref', None) != owner.self_ref):
                continue
            page, box = loc
            if box[3] > low and box[1] < high:
                continue
            matches = [r for r in records_by_page.get(page, ())
                       if max(abs(a-b) for a, b in zip(box, r['bbox'])) <= 1
                       and api.furniture_key(api.semantic_ocr_text(r['html'])) == api.furniture_key(item.text)]
            if len(matches) == 1:
                outside.append((box[1], box[0], box[3] <= low, item))
        if not outside:
            continue
        before, after = [], []
        for _, _, precedes, item in sorted(outside, key=lambda x: x[:2]):
            item.parent = parent.get_ref()
            item.label, item.content_layer = DocItemLabel.TEXT, ContentLayer.BODY
            (before if precedes else after).append(item.get_ref())
            manifest.setdefault('restored_prose_beside_figures', []).append(
                dict(page=entry['page'], figure_ref=owner.self_ref, text_ref=item.self_ref))
        restored = {ref.cref for ref in (*before, *after)}
        owner.captions = [ref for ref in owner.captions if ref.cref not in restored]
        owner.children = [ref for ref in owner.children if ref.cref not in restored]
        index = next(i for i, ref in enumerate(siblings) if ref.cref == owner.self_ref)
        siblings[index:index + 1] = [*before, siblings[index], *after]


def locate_prose_wrapping_a_figure(doc, records, manifest, reader, regions, api):
    """Find a displaced last line below an embedded drawing and above its caption."""
    import math
    import numpy as np
    from docling_core.types.doc import DocItemLabel
    from docling_pdf_forms import FIGURE_CAPTION

    if reader is None or regions is None:
        return
    positions = {item.self_ref: api.item_region(item, doc) for item in doc.texts}
    primary = {(r['page_no'], tuple(round(v, 2) for v in r['bbox']), api.furniture_key(api.semantic_ocr_text(r['html'])))
               for r in records if r.get('label') == 'Text' and r.get('bbox')}
    owners = {item.self_ref: item for item in doc.pictures}
    for entry in manifest.get('captioned_figures', []):
        owner = owners.get(entry['ref'])
        caption = FIGURE_CAPTION.fullmatch(entry['caption_text'])
        if not owner or not owner.parent or not caption or entry.get('caption_position') == 'before':
            continue
        siblings = owner.parent.resolve(doc).children
        index = next((i for i, ref in enumerate(siblings) if ref.cref == owner.self_ref), 0)
        if not index:
            continue
        head = siblings[index-1].resolve(doc)
        page, box = entry['page'], entry['bbox']
        start = positions.get(head.self_ref)
        if (not start or start[0] != page or head.label != DocItemLabel.TEXT or len(head.prov) != 1
                or head.children or start[1][3] > box[1] or start[1][0] > box[0]):
            continue
        # Only a later, misplaced body sibling referencing this same figure qualifies.
        reference = re.compile(r'\bрис(?:\.|унок)\s*' + re.escape(caption['number']) + r'\s*[.)]*\s*$', re.I)
        tails = []
        for link in siblings[index+1:]:
            loc = positions.get(link.cref)
            if not loc or loc[0] != page or abs(start[1][0] - loc[1][0]) > 2 or not box[1] < loc[1][1] < box[3]:
                continue
            tail = link.resolve(doc)
            if (tail.label == DocItemLabel.TEXT and len(tail.prov) == 1 and not tail.children
                    and reference.search(tail.text) and api.paragraph_continuation(head.text, tail.text)):
                tails.append((tail,loc[1]))
        if len(tails) != 1:
            continue
        tail, end = tails[0]
        tail_index = next(i for i, ref in enumerate(siblings) if ref.cref == tail.self_ref)
        # The figure's own caption is emitted with its crop, before any following sibling.
        caption_follows_line = entry['caption_bbox'][1] >= end[3]
        if not caption_follows_line and not any(loc and loc[0] == page and loc[1][1] > end[3]
                for ref in siblings[index+1:tail_index] if (loc := positions.get(ref.cref))):
            continue
        if any((page, tuple(round(v, 2) for v in b), api.furniture_key(t.text)) not in primary for t, b in ((head,start[1]),(tail,end))):
            continue
        source = reader.load_page(page)
        images = [[p['x0'], p['top'], p['x1'], p['bottom']] for p in source.images]
        images = [b for b in images if api._captioned_figure_contains(box, b)]
        if len(images) != 1:
            continue
        parts = []
        for item, bounds in ((head,start[1]),(tail,end)):
            glyphs = [g for g in reader.chars if g['text'].strip()
                      and bounds[0] <= (g['x0']+g['x1'])/2 <= bounds[2]
                      and bounds[1] <= (g['top']+g['bottom'])/2 <= bounds[3]]
            lines = api.native_cell_lines(glyphs)
            if api.furniture_key(' '.join(l['text'] for l in lines)) != api.furniture_key(item.text):
                break
            parts.append((glyphs,lines))
        if (len(parts) != 2 or len(parts[1][1]) != 1 or not parts[0][1]
                or abs(parts[0][1][-1]['x0'] - parts[1][1][0]['x0']) > 1):
            continue
        fonts = {(g['fontname'], round(g['size'], 1)) for chars, _ in parts for g in chars}
        first_y = min(g['top'] for g in parts[1][0])
        if len(fonts) != 1 or images[0][3] >= first_y:
            continue
        # No printed text from another column or paragraph may occupy the flow gap.
        if any(g['text'].strip() and start[1][3]+1 < (g['bottom']+g['top'])/2 < first_y
               and not api._captioned_figure_contains(images[0],[g['x0'],g['top'],g['x1'],g['bottom']])
               for g in reader.chars):
            continue
        cut = (images[0][3] + first_y) / 2
        with regions.crop(page, box) as image, image.convert('L') as gray:
            sx, sy = image.width/(box[2]-box[0]), image.height/(box[3]-box[1])
            ink = np.asarray(gray) < 250
            cut_px = max(0, math.floor((cut-box[1])*sy))
            if ink[cut_px:cut_px+1].any():
                continue
            remainder = ink.copy()
            for g in parts[1][0]:
                left = max(0,math.floor((g['x0']-box[0]-1)*sx))
                right = max(0,math.ceil((g['x1']-box[0]+1)*sx))
                top = max(0,math.floor((g['top']-box[1]-1)*sy))
                bottom = max(0,math.ceil((g['bottom']-box[1]+1)*sy))
                remainder[top:bottom,left:right] = False
            if remainder[cut_px:].any():
                continue
        entry['wrapped_prose'] = dict(head=head.self_ref, tail=tail.self_ref, cut=cut)


def split_stacked_source_figures(doc, registry, manifest, reader, regions, api):
    """Separate two stacked PDF images only when their entire gap is a printed caption."""
    import numpy as np
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel
    from docling_pdf_native_system import _painted_text_objects, _strict_ink_coverage

    if reader is None or regions is None:
        return
    owners = {item.self_ref: item for item in doc.pictures}
    entries = manifest.get('captioned_figures', [])
    for entry in tuple(entries):
        owner = owners.get(entry['ref'])
        outer_title = api._captioned_figure_title(entry['caption_text'])
        if (not owner or not owner.parent or entry['method'] != 'explicit_source_figure_caption'
                or len(owner.prov) != 1 or not outer_title or len(owner.children) != 2 or len(owner.captions) != 1
                or owner.captions[0].cref != entry['caption_ref']):
            continue
        remaining = [ref for ref in owner.children if ref.cref != entry['caption_ref']]
        if len(remaining) != 1:
            continue
        caption = remaining[0].resolve(doc)
        inner_title = api._captioned_figure_title(getattr(caption, 'text', ''))
        if (not inner_title or caption.parent != owner.get_ref() or len(caption.prov) != 1 or caption.children
                or not inner_title['number'].isdecimal() or not outer_title['number'].isdecimal()
                or int(inner_title['number']) + 1 != int(outer_title['number'])):
            continue
        if any(c.cref == caption.self_ref for item in (*doc.tables, *doc.pictures) for c in item.captions):
            continue
        page, box = entry['page'], entry['bbox']
        location = api.item_region(caption, doc)
        if (not location or location[0] != page or not api._captioned_figure_contains(box, location[1])
                or entry['caption_bbox'][1] < box[3]):
            continue
        bounds = location[1]
        source = reader.load_page(page)
        images = sorted(([im['x0'], im['top'], im['x1'], im['bottom']] for im in source.images
                         if im['x0'] < box[2] and im['x1'] > box[0] and im['top'] < box[3] and im['bottom'] > box[1]),
                        key=lambda b: b[1])
        if (len(images) != 2 or not all(api._captioned_figure_contains(box, b) for b in images)
                or images[0][3] >= bounds[1] or images[1][1] <= bounds[3]):
            continue
        band = [box[0], (images[0][3] + bounds[1])/2, box[2], (bounds[3] + images[1][1])/2]
        if not _painted_text_objects(regions, page, band):
            continue
        glyphs = [g for g in reader.chars if g['text'].strip()
                  and band[0] <= (g['x0']+g['x1'])/2 <= band[2] and band[1] <= (g['top']+g['bottom'])/2 <= band[3]]
        if (not glyphs or not all(g.get('native_script_eligible', True) and g.get('upright', True) for g in glyphs)
                or api.furniture_key(''.join(g['text'] for g in sorted(glyphs, key=lambda g: g['x0'])))
                   != api.furniture_key(caption.text)):
            continue
        with regions.crop(page, band) as image, image.convert('L') as gray:
            mask = (np.asarray(gray) < 250).astype('uint8') * 255
            if (mask[:2].any() or mask[-2:].any() or not _strict_ink_coverage(
                    mask, glyphs, band, image.width/(band[2]-band[0]), image.height/(band[3]-band[1]))):
                continue

        top = [box[0], box[1], box[2], band[1]]
        bottom = [box[0], band[3], box[2], box[3]]
        source_prov = owner.prov[0].model_copy(update={'bbox': BoundingBox(
            l=top[0], t=top[1], r=top[2], b=top[3], origin=CoordOrigin.TOPLEFT)})
        first = doc.insert_picture(sibling=owner, after=False, prov=source_prov)
        first.captions = [caption.get_ref()]
        first.children = [caption.get_ref()]
        owner.children = [ref for ref in owner.children if ref.cref != caption.self_ref]
        caption.parent, caption.label = first.get_ref(), DocItemLabel.CAPTION
        new_entry = dict(ref=first.self_ref, original_ref=owner.self_ref, page=page,
            original_bbox=images[0], bbox=top, caption_ref=caption.self_ref, caption_text=caption.text,
            caption_bbox=list(bounds), caption_match='printed_caption_between_source_images',
            source_font=entry['source_font'], font_basis=entry['font_basis'],
            source_table_refs=[], excluded_refs=[], method=entry['method'],
            primary_records=[r for r in entry.get('primary_records', []) if r['bbox'][3] <= band[3]])
        entry.update(bbox=bottom, original_bbox=images[1],
                     primary_records=[r for r in entry.get('primary_records', []) if r['bbox'][1] >= band[3]])
        entries.append(new_entry)
        registry[first.self_ref] = new_entry
        manifest.setdefault('split_stacked_figures', []).append(dict(page=page,
            references=[first.self_ref, owner.self_ref], source_bbox=box, caption_band=band))


def split_lists_crossing_illustrations(doc, manifest, api):
    """Keep numbered items on their own sides of full-width, captioned source objects."""
    from docling_core.types.doc import ListGroup, ListItem, PictureItem, TableItem

    body = doc.body
    for group in tuple(doc.groups):
        if not isinstance(group, ListGroup) or group.parent != body.get_ref() or len(group.children) < 2:
            continue
        items = [r.resolve(doc) for r in group.children]
        if any(not isinstance(item, ListItem) or item.children or len(item.prov) != 1 for item in items):
            continue
        positions = [api.item_region(item, doc) for item in items]
        if not all(positions) or len({loc[0] for loc in positions}) != 1:
            continue
        page = positions[0][0]
        boxes = [loc[1] for loc in positions]
        if (max(b[0] for b in boxes) - min(b[0] for b in boxes) > 3
                or any(right[1] < left[3]-2 for left, right in zip(boxes, boxes[1:]))):
            continue
        width = doc.pages[page].size.width
        index = next((i for i,r in enumerate(body.children) if r.cref == group.self_ref), None)
        if index is None:
            continue
        between = []
        split, previous_bottom = None, None
        for link in body.children[index+1:]:
            obj = link.resolve(doc)
            if not isinstance(obj, (TableItem, PictureItem)) or len(obj.prov) != 1 or len(obj.captions) != 1:
                break
            cap = obj.captions[0].resolve(doc)
            if (len(cap.prov) != 1 or cap.parent != obj.get_ref() or not
                    (caption_prefix(cap.text) if isinstance(obj, TableItem) else api._captioned_figure_title(cap.text))):
                break
            obj_loc, cap_loc = api.item_region(obj, doc), api.item_region(cap, doc)
            if not obj_loc or not cap_loc or obj_loc[0] != page or cap_loc[0] != page:
                break
            bounds, label = obj_loc[1], cap_loc[1]
            top, bottom = min(label[1], bounds[1]), max(label[3], bounds[3])
            cuts = [i for i in range(1, len(boxes)) if boxes[i-1][3]-1 <= top and bottom <= boxes[i][1]+1]
            if (len(cuts) != 1 or (split is not None and (cuts[0] != split or top < previous_bottom-1))
                    or bounds[2]-bounds[0] < .5*width or boxes[cuts[0]][2]-boxes[cuts[0]][0] < .5*width
                    or min(bounds[2], boxes[cuts[0]][2])-max(bounds[0], boxes[cuts[0]][0]) < .4*width):
                break
            split, previous_bottom = cuts[0], bottom
            between.append(link)
        if not between:
            continue
        tail = doc.add_group(label=group.label, name=group.name, parent=body, content_layer=group.content_layer)
        body.children.remove(tail.get_ref())
        body.children.insert(index+1+len(between), tail.get_ref())
        tail.children, group.children = group.children[split:], group.children[:split]
        for item in items[split:]:
            item.parent = tail.get_ref()
        markers = {item.self_ref:int(match[1]) for item in items if item.enumerated
                   and (match := re.fullmatch(r'(\d+)[.)]?', item.marker.strip()))}
        manifest.setdefault('split_illustrated_lists', []).append(dict(page=page, before=group.self_ref,
            after=tail.self_ref, objects=[r.cref for r in between], numeric_markers=markers,
            clauses={item.self_ref: item.marker.strip() for item in items if item.enumerated
                     and re.fullmatch(r'\d+(?:\.\d+)+[.)]?', item.marker.strip())}))


def recover_detached_raster_captions(doc, registry, manifest, reader, regions, api):
    """Reattach a misplaced root caption when the printed band has just one possible owner."""
    from docling_core.types.doc import DocItemLabel
    from docling_pdf_artifacts import picture_caption_band

    if reader is None or regions is None:
        return
    body = doc.body
    pictures = [(p, api.item_region(p, doc)) for p in doc.pictures
                if p.parent == body.get_ref() and len(p.prov) == 1]
    linked = {c.cref for obj in (*doc.pictures, *doc.tables) for c in obj.captions}
    reserved = {e['ref'] for e in manifest.get('captioned_figures', ())}
    for caption in doc.texts:
        if (caption.label != DocItemLabel.CAPTION or caption.parent != body.get_ref() or caption.self_ref in linked
                or len(caption.prov) != 1 or caption.children or not api._captioned_figure_title(caption.text)):
            continue
        loc = api.item_region(caption, doc)
        if not loc:
            continue
        page, cap = loc
        near = [(p, b) for p, pos in pictures if pos and pos[0] == page and (b := pos[1])
                and b[0] <= cap[0] and cap[2] <= b[2]
                and abs(b[3] - cap[1]) <= cap[3]-cap[1]
                and abs((b[0]+b[2]) - (cap[0]+cap[2])) <= .2*(b[2]-b[0])]
        if len(near) != 1:
            continue
        owner, box = near[0]
        if owner.children or owner.captions or owner.self_ref in reserved:
            continue
        siblings = [r.cref for r in body.children]
        if owner.self_ref not in siblings or caption.self_ref not in siblings:
            continue
        i, j = siblings.index(owner.self_ref), siblings.index(caption.self_ref)
        if i+1 >= j or not any(pos and pos[0] == page and pos[1][1] >= cap[3]-1
                for link in body.children[i+1:j] if (item := link.resolve(doc)) and getattr(item, 'prov', ())
                and (pos := api.item_region(item, doc))):
            continue
        band = picture_caption_band(owner, doc, reader, regions, page, box, api, detached_caption=caption)
        if not band:
            continue
        bounds = band[0]
        body.children.remove(caption.get_ref())
        caption.parent = owner.get_ref()
        owner.children.append(caption.get_ref())
        owner.captions.append(caption.get_ref())
        entry = dict(ref=owner.self_ref, original_ref=owner.self_ref, page=page, original_bbox=box, bbox=bounds,
            caption_ref=caption.self_ref, caption_text=caption.text, caption_bbox=cap,
            caption_match='detached_printed_caption_at_image_edge', source_font=cap[3]-cap[1],
            font_basis='positioned_primary_caption_height', source_table_refs=[], excluded_refs=[],
            primary_records=[], method='explicit_source_figure_caption')
        manifest.setdefault('captioned_figures', []).append(entry)
        registry[owner.self_ref] = entry
        reserved.add(owner.self_ref)
