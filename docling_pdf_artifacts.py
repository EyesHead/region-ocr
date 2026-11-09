"""Suppress source-proven recurring page furniture without trimming pages.

The evidence is object ownership and repetition, never a fixed footer strip.
Only picture routes are removed; native/OCR text, footnotes and source pages
are not changed. Unproven pictures remain available to the normal exporter.
"""
from collections import Counter, defaultdict
import hashlib
import re
import unicodedata


def _key(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and c != '\u00ad')


def _contains_point(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _glyph_box(char):
    return (float(char['x0']), float(char['top']),
            float(char['x1']), float(char['bottom']))


def _center(box):
    return (box[0] + box[2])/2, (box[1] + box[3])/2


def outside_body(box, body_boxes):
    """Return the actual free edge beyond every retained content object."""
    if not body_boxes:
        return None
    # No page coordinates, page fractions, font-size or crop-size threshold.
    if box[1] >= max(b[3] for b in body_boxes):
        return 'bottom'
    if box[3] <= min(b[1] for b in body_boxes):
        return 'top'
    if box[2] <= min(b[0] for b in body_boxes):
        return 'left'
    if box[0] >= max(b[2] for b in body_boxes):
        return 'right'
    return None


def _anchor(text):
    """A publisher URL or an independently labelled running page number."""
    return bool(re.search(r'(?:https?://|www\.)\S+|\b[\w-]+\.(?:[a-z]{2,})(?:/\S*)?\b', text, re.I)
                or re.fullmatch(r'\s*(?:(?:страница|стр\.?|лист|page)\s*)?'
                                r'(?:\d+|[IVXLCDM]+)(?:\s*(?:из|of|/)\s*\d+)?\s*', text, re.I))


def _same_band(box, anchor, edge):
    axis = 1 if edge in {'top', 'bottom'} else 0
    return min(box[axis + 2], anchor[axis + 2]) > max(box[axis], anchor[axis])


def decide_margin_artifacts(candidates, furniture, body_boxes):
    """Pure decision stage, with source evidence supplied by the collector.

    Native text requires ownership by a separately recognised furniture block.
    Image-only candidates require a source image stream repeated verbatim.
    Both require an independent running anchor and space beyond the body.
    """
    prepared, reasons = [], Counter()
    for candidate in candidates:
        page, box = candidate['page'], candidate['bbox']
        if candidate.get('source_analysis_error'):
            reasons['source_analysis_unavailable'] += 1
            continue
        if candidate.get('has_caption') or candidate.get('has_children'):
            reasons['caption_or_children'] += 1
            continue
        edge = outside_body(box, body_boxes.get(page, []))
        if edge is None:
            reasons['intersects_or_extends_into_body'] += 1
            continue
        own = set(candidate.get('furniture_owners', []))
        anchors = [seed for seed in furniture.get(page, []) if seed['ref'] not in own
                   and _anchor(seed.get('text', ''))
                   and outside_body(seed['bbox'], body_boxes.get(page, [])) == edge
                   and _same_band(box, seed['bbox'], edge)]
        if not anchors:
            reasons['no_independent_running_anchor'] += 1
            continue
        native = candidate.get('native_signature')
        raster = candidate.get('source_image_signature')
        if native and own:
            fingerprint = ('native_furniture_glyphs', native)
        elif not candidate.get('native_glyphs') and raster and candidate.get('layout_furniture'):
            fingerprint = ('repeated_source_image_stream', raster)
        else:
            reasons['no_source_furniture_ownership'] += 1
            continue
        prepared.append(dict(candidate, edge=edge, fingerprint=fingerprint,
                             anchor_refs=[seed['ref'] for seed in anchors]))
    groups = defaultdict(list)
    for item in prepared:
        groups[item['edge'], item['fingerprint']].append(item)
    removed = []
    for group in groups.values():
        pages = sorted({item['page'] for item in group})
        # Repetition needs two independent physical pages, not duplicated items
        # on one page. This is a logical condition, not a processing budget.
        if len(pages) < 2:
            reasons['not_repeated_on_independent_pages'] += len(group)
            continue
        for item in group:
            removed.append(dict(ref=item['ref'], page=item['page'], bbox=item['bbox'],
                edge=item['edge'], reason=item['fingerprint'][0],
                repeated_pages=pages, native_glyphs=item.get('native_glyphs', 0),
                furniture_owners=item.get('furniture_owners', []),
                anchor_refs=item['anchor_refs']))
    return removed, dict(reasons)


def suppress_repeated_margin_artifacts(doc, reader, picture_routes, excluded, manifest, api):
    """Call after text furniture classification and before picture recognition.

    The reader is the existing NativeTableReader. It supplies source glyphs
    and individual PDF image streams, avoiding another OCR/model pass.
    """
    if reader is None:
        api.diagnostic_event('MARGIN_ARTIFACT_ANALYSIS_SKIPPED', reason='native_source_reader_unavailable')
        return []
    from docling_core.types.doc import ContentLayer, DocItemLabel

    furniture = defaultdict(list)
    known = {item['ref']: item for item in manifest.get('removed_page_furniture', [])}
    for item in doc.texts:
        # A source footnote may have been mistaken for a footer upstream.
        # Never use that mistake as proof for deleting its last visible crop.
        if item.label == DocItemLabel.FOOTNOTE:
            continue
        region = api.item_region(item, doc)
        if region and (item.self_ref in known or item.label in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
                       or getattr(item, 'content_layer', None) == ContentLayer.FURNITURE):
            furniture[region[0]].append(dict(ref=item.self_ref, bbox=region[1], text=item.text))
    candidates, by_page = [], defaultdict(list)
    for picture in doc.pictures:
        if picture.self_ref in excluded or picture_routes.get(picture.self_ref) == '':
            continue
        region = api.item_region(picture, doc)
        # A multipage composite must retain every region; never delete it based
        # on one footer-like fragment.
        if not region or len(picture.prov) != 1:
            continue
        meaningful_children = [ref for ref in picture.children
            if getattr(ref.resolve(doc), 'label', None) == DocItemLabel.FOOTNOTE
            or (ref.cref not in excluded
                and getattr(ref.resolve(doc), 'content_layer', None) != ContentLayer.FURNITURE)]
        layout_furniture = (getattr(picture, 'content_layer', None) == ContentLayer.FURNITURE
            or any(record.get('page_no') == region[0] and record.get('label') in {'PageHeader', 'PageFooter'}
                   and all(_contains_point(record['bbox'], *point) for point in
                       ((region[1][0], region[1][1]), (region[1][2], region[1][3])))
                   for record in manifest.get('ocr_blocks', [])))
        entry = dict(ref=picture.self_ref, page=region[0], bbox=region[1],
                     has_caption=bool(picture.captions), has_children=bool(meaningful_children),
                     layout_furniture=layout_furniture)
        candidates.append(entry)
        by_page[region[0]].append(entry)
    body = defaultdict(list)
    candidate_refs = {entry['ref'] for entry in candidates}
    for item in [*doc.texts, *doc.tables, *doc.pictures]:
        if item.self_ref in excluded or getattr(item, 'content_layer', None) == ContentLayer.FURNITURE:
            continue
        region = api.item_region(item, doc)
        if region and item.self_ref not in candidate_refs:
            body[region[0]].append(region[1])
    # Treat other pictures as body later, after grouping; ordinary body pictures
    # must not be ignored merely because they are candidates for analysis.
    for page_no, entries in by_page.items():
        try:
            page = reader.load_page(page_no)
        except Exception as error:
            api.diagnostic_event('MARGIN_ARTIFACT_SOURCE_UNAVAILABLE', page=page_no,
                                 error_type=type(error).__name__, error=str(error))
            for entry in entries:
                entry['source_analysis_error'] = True
            continue
        chars = [char for char in reader.chars if char.get('text', '').strip()]
        for entry in entries:
            selected = [char for char in chars if _contains_point(entry['bbox'], *_center(_glyph_box(char)))]
            entry['native_glyphs'] = len(selected)
            owners = []
            if selected:
                for seed in furniture[page_no]:
                    if all(_contains_point(seed['bbox'], *_center(_glyph_box(char))) for char in selected):
                        owners.append(seed['ref'])
                native = ''.join(char['text'] for char in selected)
                if not re.search(r'\(cid:\d+\)|\ufffd|\x00', native):
                    entry['native_signature'] = _key(native)
            entry['furniture_owners'] = owners
            streams = []
            for source_image in page.images:
                source_box = _glyph_box(source_image)
                if (all(_contains_point(entry['bbox'], *point) for point in
                        ((source_box[0], source_box[1]), (source_box[2], source_box[3])))
                        and source_image.get('stream') is not None):
                    try:
                        streams.append(hashlib.sha256(source_image['stream'].get_data()).hexdigest())
                    except Exception as error:
                        entry['source_analysis_error'] = True
                        api.diagnostic_event('MARGIN_ARTIFACT_SOURCE_IMAGE_UNREADABLE', page=page_no,
                            ref=entry['ref'], error_type=type(error).__name__, error=str(error))
            if streams:
                entry['source_image_signature'] = tuple(sorted(streams))
    # Only source-owned objects with a running anchor are plausible furniture.
    # Every other picture participates in body geometry, including last-line
    # diagrams and pictures on a text-free page.
    for entry in candidates:
        plausible = (entry.get('furniture_owners') or
                     (not entry.get('native_glyphs') and entry.get('source_image_signature')
                      and entry.get('layout_furniture')))
        if not plausible or entry['has_caption'] or entry['has_children'] or entry.get('source_analysis_error'):
            body[entry['page']].append(entry['bbox'])
    removed, reasons = decide_margin_artifacts(candidates, furniture, body)
    pictures = {item.self_ref: item for item in doc.pictures}
    for proof in removed:
        picture_routes[proof['ref']] = ''
        excluded.add(proof['ref'])
        pictures[proof['ref']].content_layer = ContentLayer.FURNITURE
        api.diagnostic_event('REPEATED_MARGIN_ARTIFACT_REMOVED', **proof)
    manifest['removed_margin_artifacts'] = removed
    manifest['margin_artifact_analysis'] = dict(candidates=len(candidates), removed=len(removed), kept_reasons=reasons)
    api.diagnostic_event('MARGIN_ARTIFACT_ANALYSIS', **manifest['margin_artifact_analysis'])
    return removed
