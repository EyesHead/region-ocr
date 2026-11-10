"""Explicit form headings may caption an independently retained illustration."""
import hashlib
import re


FORM_HEADING = re.compile(r'^\s*(?:(?:Продолжение|Окончание)\s+формы|Форма)\s+'
                          r'(?P<number>(?:[A-ZА-ЯЁ]\s*\.\s*)?\d+(?:[.\-]\d+)*)'
                          r'(?:\s*[—–−-]\s*(?P<title>\S[\s\S]*))?\s*$', re.I)


def form_owns_region(manifest, page, box):
    """An independently retained form illustration owns its source rectangle."""
    if not box:
        return False
    return any(entry['page'] == page and entry['bbox'][0]-2 <= box[0]
        and entry['bbox'][1]-2 <= box[1] and box[2] <= entry['bbox'][2]+2
        and box[3] <= entry['bbox'][3]+2 for entry in manifest.get('source_form_figures', []))


def capture_form_figures(doc, records, manifest, api):
    """Bind a form number/title to source boxes before semantic table repairs.

    A heading cannot change a table or an unclassified source band into an
    illustration. Only existing picture objects qualify; table/form fields
    continue through the ordinary editable table route.
    """
    objects = [(item, api.item_region(item, doc)) for item in doc.pictures]
    objects = [(item, pos) for item, pos in objects if pos]
    texts = [(item, api.item_region(item, doc)) for item in doc.texts]
    table_refs = {item.self_ref for item in doc.tables}
    table_regions = [api.item_region(item, doc) for item in doc.tables]
    table_regions += [(r['page_no'], r['bbox']) for r in records if r.get('bbox')
                     and '<table' in r.get('html', '').lower()]
    table_regions = [position for position in table_regions if position]
    registry = {}
    furniture = {'page_header', 'page_footer'}
    for caption, position in texts:
        match = FORM_HEADING.fullmatch(getattr(caption, 'text', ''))
        if not match or not position:
            continue
        page, capbox = position
        em = max(1., capbox[3]-capbox[1])
        captions = [caption]
        following = sorted([(item, box[1]) for item, box in texts if box and box[0] == page
            and box[1][1] >= capbox[3]-.1*em and str(getattr(item, 'label', '')) not in furniture],
            key=lambda pair: (pair[1][1], pair[1][0]))
        candidates = [(item, pos[1]) for item, pos in objects if pos[0] == page
            and pos[1][1] >= capbox[3]-.25*em and pos[1][1]-capbox[3] <= 12*em
            and item.self_ref not in registry
            and not any(p == page and api.overlap_ratio(pos[1], box) > 0 for p, box in table_regions)]
        if not candidates:
            api.diagnostic_event('SOURCE_FORM_KEPT_EDITABLE', page=page,
                caption_ref=caption.self_ref, reason='no_independent_source_picture')
            continue
        candidates.sort(key=lambda pair: (pair[1][1],
            max(0., pair[1][0]-capbox[2], capbox[0]-pair[1][2]),
            -(pair[1][2]-pair[1][0])*(pair[1][3]-pair[1][1])))
        owner, box = candidates[0]
        between = [(item, b) for item, b in following if item not in captions and b[3] <= box[1]+.25*em]
        if between:
            # A number and a separately recognized title are one printed caption.
            if not match.group('title') and len(between) <= 3 and all(
                    (str(getattr(item, 'label', '')) in {'section_header', 'caption'}
                     or len(item.text) <= 180 and not re.match(r'^\s*(?:\d+|[А-ЯA-Z]\.\d+)\b', item.text)
                     and not item.text.rstrip().endswith(('.', ':', ';')))
                    and not FORM_HEADING.fullmatch(item.text) for item, _ in between):
                captions.extend(item for item, _ in between)
            else:
                # Independent prose is not evidence for a missing picture.
                # Keep it and the actual picture in their normal routes.
                continue
        last_caption = max(api.item_region(item, doc)[1][3] for item in captions)
        # Expand across overlapping/adjacent fragments only until actual prose.
        envelope = list(box)
        owned = [(owner, box)]
        for item, pos in objects:
            if pos[0] != page or item.self_ref == owner.self_ref:
                continue
            b = pos[1]
            if b[1] < last_caption or b[1] > envelope[3]+em:
                continue
            boundary = any(t.self_ref not in {c.self_ref for c in captions}
                and p and p[0] == page and envelope[3] <= p[1][1] < b[1]
                and str(getattr(t, 'label', '')) not in furniture for t, p in texts)
            if boundary:
                continue
            if b[0] <= envelope[2] and b[2] >= envelope[0]:
                owned.append((item, b))
                envelope = [min(envelope[0], b[0]), min(envelope[1], b[1]),
                            max(envelope[2], b[2]), max(envelope[3], b[3])]
        if any(p == page and api.overlap_ratio(envelope, bounds) > 0 for p, bounds in table_regions):
            continue
        size = doc.pages[page].size
        crop = [max(0., envelope[0]), max(0., envelope[1]),
                min(size.width, envelope[2]), min(size.height, envelope[3])]
        packets = [r for r in records if r.get('page_no') == page
                   and api._captioned_figure_contains(crop, r.get('bbox'))]
        entry = dict(ref=owner.self_ref, original_ref=owner.self_ref, page=page,
            original_bbox=list(envelope), bbox=crop, caption_ref=caption.self_ref,
            caption_refs=[item.self_ref for item in captions],
            caption_text=' — '.join(item.text for item in captions), caption_bbox=list(capbox),
            caption_position='before', caption_match='explicit_form_heading', source_font=em,
            font_basis='positioned_primary_caption_height',
            source_table_refs=[item.self_ref for item, _ in owned if item.self_ref in table_refs],
            excluded_refs=[], method='explicit_source_form_heading',
            primary_records=[dict(record_sha256=hashlib.sha256(r.get('html', '').encode()).hexdigest(),
                                  bbox=list(r['bbox'])) for r in packets])
        registry[owner.self_ref] = entry
        manifest.setdefault('captioned_figures', []).append(entry)
        manifest.setdefault('source_form_figures', []).append(entry)
        api.diagnostic_event('SOURCE_FORM_CROP_REGISTERED', **entry)
    return registry
