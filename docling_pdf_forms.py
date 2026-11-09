"""Explicit form headings own the following source illustration, above its crop."""
import hashlib
import re


FORM_HEADING = re.compile(r'^\s*(?:(?:Продолжение|Окончание)\s+формы|Форма)\s+'
                          r'(?P<number>(?:[A-ZА-ЯЁ]\s*\.\s*)?\d+(?:[.\-]\d+)*)'
                          r'(?:\s*[—–−-]\s*(?P<title>\S[\s\S]*))?\s*$', re.I)


def form_owns_region(manifest, page, box):
    """A printed form remains an illustration in every recovery pass."""
    if not box:
        return False
    return any(entry['page'] == page and entry['bbox'][0]-2 <= box[0]
        and entry['bbox'][1]-2 <= box[1] and box[2] <= entry['bbox'][2]+2
        and box[3] <= entry['bbox'][3]+2 for entry in manifest.get('source_form_figures', []))


def capture_form_figures(doc, records, manifest, api):
    """Bind a form number/title to source boxes before semantic table repairs.

    Only standalone headings qualify. Nearby citations in prose do not. The
    original layout envelope, including nested sketches, remains authoritative;
    following instructions and the next form remain outside the image.
    """
    objects = [(item, api.item_region(item, doc)) for item in [*doc.tables, *doc.pictures]]
    objects = [(item, pos) for item, pos in objects if pos]
    texts = [(item, api.item_region(item, doc)) for item in doc.texts]
    table_refs = {item.self_ref for item in doc.tables}
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
            and item.self_ref not in registry]
        if not candidates:
            # A raw primary table may have been omitted by the assembled layout.
            packets = [r for r in records if r.get('page_no') == page and r.get('bbox')
                and '<table' in r.get('html', '').lower()
                and capbox[3] <= r['bbox'][1] <= capbox[3]+12*em]
            if packets:
                packet = min(packets, key=lambda r: r['bbox'][1])
                item = api.insert_source_item(doc, page, packet['bbox'], 'picture')
                candidates = [(item, list(packet['bbox']))]
                objects.append((item, (page, list(packet['bbox']))))
        if not candidates:
            # A layout failure must not silently drop an explicitly printed
            # form. Preserve the source band below its heading; positioned
            # following prose/next forms and page furniture bound that band.
            if not match.group('title') and following:
                title, title_box = following[0]
                if (str(getattr(title, 'label', '')) in {'section_header', 'caption'}
                        and not FORM_HEADING.fullmatch(title.text)
                        and title_box[1]-capbox[3] <= 4*em):
                    captions.append(title)
            last = max(api.item_region(item, doc)[1][3] for item in captions)
            def prose_boundary(item):
                text = item.text.strip()
                return (FORM_HEADING.fullmatch(text) or
                    re.match(r'^(?:(?:[A-ZА-ЯЁ]\.)?\d+(?:\.\d+)*\s+|Примечани|Указания\b)', text, re.I)
                    or len(text) > 24 and text.endswith(('.', ':', ';')))
            ends = [b[1] for item, b in following if item not in captions
                    and b[1] > last and prose_boundary(item)]
            ends += [pos[1][1] for item, pos in texts if pos and pos[0] == page
                     and str(getattr(item, 'label', '')) in furniture and pos[1][1] > last]
            end = min(ends or [doc.pages[page].size.height])
            if end-last <= em:
                raise ValueError(f'No source area remains after explicit form heading on page {page}')
            fallback_box = [0., last+.1*em, doc.pages[page].size.width, end-.1*em]
            item = api.insert_source_item(doc, page, fallback_box, 'picture')
            candidates = [(item, fallback_box)]
            objects.append((item, (page, fallback_box)))
            api.diagnostic_event('SOURCE_FORM_LAYOUT_FALLBACK', page=page,
                caption_ref=caption.self_ref, bbox=fallback_box, reason='following_source_region_missing')
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
                prose = [(item, b) for item, b in between if FORM_HEADING.fullmatch(item.text)
                    or re.match(r'^\s*(?:[А-ЯA-Z]\.)?\d+(?:\.\d+)*\s+[А-ЯA-Z]', item.text)
                    or len(item.text) > 24 and item.text.rstrip().endswith(('.', ':', ';'))]
                if prose:
                    end = min(b[1] for _, b in prose)
                    if end-capbox[3] <= em:
                        raise ValueError(f'No source area remains after explicit form heading on page {page}')
                    # The later table is beyond the actual form. Keep the
                    # missing illustration's source band and leave that table.
                    box = [0., capbox[3]+.1*em, doc.pages[page].size.width, end-.1*em]
                    owner = api.insert_source_item(doc, page, box, 'picture')
                    objects.append((owner, (page, box)))
                else:
                    # Labels/dimensions above a detected grid belong to the
                    # printed form, even when layout emitted them as plain text.
                    boxes = [box, *[b for _, b in between]]
                    box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                           max(b[2] for b in boxes), max(b[3] for b in boxes)]
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
        pad = .5*em
        size = doc.pages[page].size
        bottom = min([p[1][1] for t, p in texts if p and p[0] == page
            and str(getattr(t, 'label', '')) not in furniture
            and p[1][1] >= envelope[3] and t.self_ref not in {c.self_ref for c in captions}]
            or [size.height])
        # Keep the entire gap below the heading: an unrecognized form title
        # can live above the detected grid (and must remain in the source crop).
        crop = [max(0., envelope[0]-pad), max(0., last_caption+.1*em),
                min(size.width, envelope[2]+pad), min(size.height, bottom, envelope[3]+pad)]
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
