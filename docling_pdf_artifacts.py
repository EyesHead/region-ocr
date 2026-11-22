"""Suppress source-proven recurring page furniture without trimming pages.

The evidence is object ownership and repetition, never a fixed footer strip.
Only picture routes are removed; native/OCR text, footnotes and source pages
are not changed. Unproven pictures remain available to the normal exporter.
"""
from collections import Counter, defaultdict
import hashlib
import re
import unicodedata


def primary_body_text(item, region, records, repeated_pages, api):
    """Keep a unique primary Text block mislabelled as running furniture."""
    if len(repeated_pages) > 1 or re.fullmatch(
            r'\s*(?:(?:Лист|Стр(?:аница)?\.?|Page)\s*(?:№\s*)?)?'
            r'(?:\d{1,5}|[IVXLCDMivxlcdm]{1,8})\s*', item.text):
        return None
    page, box = region
    key = api.furniture_key(item.text)
    if not key:
        return None
    matches = []
    for record in records:
        bounds = record.get('bbox')
        if (record.get('page_no') != page or not bounds or len(bounds) != 4
                or any(abs(a-b) > 1 for a, b in zip(box, bounds))):
            continue
        text = record.get('text')
        if not isinstance(text, str):
            continue
        if api.furniture_key(text) != key:
            continue
        label = str(record.get('label', '')).casefold().replace('-', '').replace('_', '')
        if label in {'pageheader', 'pagefooter'}:
            return None  # An explicit primary furniture classification still owns it.
        if label == 'text':
            matches.append(record)
    if len(matches) != 1:
        return None
    return dict(ref=item.self_ref, page=page, bbox=list(box),
                source_record_sha256=hashlib.sha256(matches[0].get('html', '').encode()).hexdigest(),
                reason='unique_primary_text_preserved_over_secondary_layout_furniture')


def _key(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and c != '\u00ad')


def restore_picture_text_alternative(fragment, picture, doc, page_no, box, reader, records, excluded, api):
    """Expose a text-bearing image's verified literal when Docling omits its child."""
    from bs4 import BeautifulSoup

    if reader is None or picture.captions or len(picture.children) != 1:
        return fragment
    child = picture.children[0].resolve(doc)
    if (str(child.label) != 'text' or not child.parent or child.parent.cref != picture.self_ref
            or child.children or child.self_ref in excluded or len(child.prov) != 1):
        return fragment
    page, bounds = api.item_region(child, doc)
    text = ' '.join(child.text.split())
    if (page != page_no or not text or '<' in text or api.overlap_ratio(box, bounds) < .9
            or (bounds[2]-bounds[0])*(bounds[3]-bounds[1]) < .5*(box[2]-box[0])*(box[3]-box[1])):
        return fragment
    if not any(r.get('page_no') == page and r.get('label') == 'Text' and ' '.join((r.get('text') or '').split()) == text
               and r.get('bbox') and max(abs(a-b) for a, b in zip(bounds, r['bbox'])) <= 1 for r in records):
        return fragment
    key = _key(text)
    if any(t.self_ref != child.self_ref and any(p.page_no == page for p in t.prov)
           and _key(t.text) == key for t in doc.texts):
        return fragment
    parsed = BeautifulSoup(fragment, 'html.parser')
    images = parsed.find_all('img')
    if len(images) != 1 or images[0].get('alt') or parsed.get_text(strip=True):
        return fragment
    try:
        literal = reader.load_page(page).crop(tuple(box), strict=False).extract_text()
    except (OSError, RuntimeError, ValueError):
        return fragment
    if ' '.join((literal or '').split()) != text:
        return fragment
    images[0]['alt'] = text
    return str(parsed)


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


def complete_picture_bounds(reader, regions, page_no, box, neighbours):
    """Expand a layout crop only over missing ink belonging to native PDF images."""
    from math import ceil, floor
    import numpy as np

    def intersection(a, b):
        return max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))

    if reader is None:
        return None
    page = reader.load_page(page_no)
    source_boxes = []
    for image in page.images:
        rect = [image[k] for k in ('x0', 'top', 'x1', 'bottom')]
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area <= 0 or reader.is_page_scan(image, box) or min(rect) < 0 or rect[2] > page.width or rect[3] > page.height:
            continue
        overlap = intersection(box, rect)
        # A clipped, standalone figure can cover less of its source bitmap.
        # Accept it only when the bitmap also covers most of the layout box.
        if overlap >= .95 * area or (overlap >= .8 * area and overlap >= .8 * intersection(box, box)):
            source_boxes.append(rect)
    if not source_boxes:
        return None
    rects = [box, *source_boxes]
    full = [op(r[i] for r in rects) for i, op in enumerate((min, min, max, max))]
    # Docling rounds bitmap crops to its render grid. A nominal margin of
    # less than one point can still drop the final row or column of pixels.
    touches_edge = any(min(abs(rect[i] - box[i]) for i in range(4)) < 1.0 for rect in source_boxes)
    if full == list(box) and not touches_edge:
        return None

    for glyph in reader.chars:
        if not glyph['text'].strip():
            continue
        bounds = [glyph[k] for k in ('x0', 'top', 'x1', 'bottom')]
        if intersection(full, bounds) > intersection(box, bounds) + .01:
            return None
    with regions.crop(page_no, full) as bitmap, bitmap.convert('L') as gray:
        ink = np.asarray(gray) < 210
        # PdfRegions also renders very large pages via a bounded window.
        sx, sy = bitmap.width / (full[2] - full[0]), bitmap.height / (full[3] - full[1])
        def window(rect, start, end):
            x0 = max(0, start((rect[0] - full[0]) * sx))
            y0 = max(0, start((rect[1] - full[1]) * sy))
            x1 = min(bitmap.width, end((rect[2] - full[0]) * sx))
            y1 = min(bitmap.height, end((rect[3] - full[1]) * sy))
            return np.s_[y0:y1, x0:x1]
        ink[window(box, ceil, floor)] = False
        # A predicted neighbouring paragraph may overlap blank padding.
        # Preserve the boundary wherever the added strip actually contains its ink.
        for other in neighbours:
            if intersection(full, other) > intersection(box, other) + .01 and ink[window(other, floor, ceil)].any():
                return None
        for rect in source_boxes:
            ink[window(rect, floor, ceil)] = False
        if ink.any():
            return None
    return full


def picture_caption_band(item, doc, reader, regions, page_no, box, api, *, detached_caption=None):
    """Recover printed captions owned as children when Docling omits their caption links."""
    from docling_pdf_native_system import _painted_text_objects, _strict_ink_coverage
    import numpy as np
    import re

    if reader is None:
        return None
    if detached_caption is not None:
        if item.children or item.captions or item.parent != doc.body.get_ref() or detached_caption.parent != item.parent:
            return None
        children = [detached_caption]
        expected_parent = item.parent
    else:
        if not item.children:
            return None
        children = [link.resolve(doc) for link in item.children]
        expected_parent = item.get_ref()
    # Layout sometimes links the following numbered paragraph as a caption.
    # Require a separate source region below the entire crop before detaching it.
    following_paragraph = bool(item.captions)
    if following_paragraph:
        if len(item.captions) != 1:
            return None
        linked = item.captions[0].resolve(doc)
        position = api.item_region(linked, doc)
        if (linked not in children or len(linked.prov) != 1 or not position or position[0] != page_no
                or position[1][1] <= box[3] or not re.match(r'^\s*\d+[.)]\s+\S', linked.text)
                or api._captioned_figure_title(linked.text)):
            return None
        children = [child for child in children if child.self_ref != linked.self_ref]
        if not children:
            return None
    if any(str(child.label) not in {'caption', 'text'} or len(child.prov) != 1
           or child.parent != expected_parent for child in children):
        return None
    page = reader.load_page(page_no)
    rasters = [im for im in page.images if im['x1'] > box[0] and im['x0'] < box[2]
               and im['bottom'] > box[1] and im['top'] < box[3]]
    if not rasters or any(reader.is_page_scan(im, box) for im in rasters):
        return None
    labels, glyphs = [], []
    for child in children:
        pos = api.item_region(child, doc)
        if not pos or pos[0] != page_no or not api._captioned_figure_title(child.text):
            return None
        x0, top, x1, bottom = pos[1]
        letters = [g for g in reader.chars if g['text'].strip() and g.get('native_script_eligible', True)
                   and x0 <= (g['x0'] + g['x1']) / 2 <= x1 and top <= (g['top'] + g['bottom']) / 2 <= bottom]
        if (not letters or any(not g.get('upright', True) for g in letters)
                or ''.join(''.join(g['text'] for g in sorted(letters, key=lambda c: c['x0'])).split())
                != ''.join(child.text.split())):
            return None
        labels.append((top, x0, child.text))
        glyphs.extend(letters)

    end_of_image = max(im['bottom'] for im in rasters)
    start_of_text = min(g['top'] for g in glyphs)
    if start_of_text <= end_of_image or min(g['bottom'] for g in glyphs) <= box[1]:
        return None
    cut = (end_of_image + start_of_text) / 2
    band = [max(0., min(box[0], min(g['x0'] for g in glyphs)) - .4), cut,
            min(page.width, max(box[2], max(g['x1'] for g in glyphs)) + .4),
            min(page.height, max(box[3], max(g['bottom'] for g in glyphs)) + .4)]
    if not box[1] < cut < box[3] or not _painted_text_objects(regions, page_no, band):
        return None
    with regions.crop(page_no, band) as crop, crop.convert('L') as gray:
        ink = (np.asarray(gray) < 225).astype('uint8') * 255
        if (ink[0].any() or not _strict_ink_coverage(ink, glyphs, band,
                crop.width / (band[2] - band[0]), crop.height / (band[3] - band[1]))):
            return None
    # Separate labels on the same baseline in physical left-to-right order.
    if max(top for top, _, _ in labels) - min(top for top, _, _ in labels) > min(g['size'] for g in glyphs) / 2:
        return None
    return [box[0], box[1], box[2], cut], [label for _, _, label in sorted(labels, key=lambda row: row[1])], following_paragraph


def unassigned_table_fragment_group(boxes, table_box):
    """Keep adjacent pieces of a text-bearing table in their source positions."""
    from statistics import median

    left, top, right, bottom = table_box
    inside = sorted({tuple(b) for b in boxes if left <= b[0] < b[2] <= right
                     and top <= b[1] < b[3] <= bottom})
    if len(inside) < 3:
        return []
    # Three neighbouring objects on a common baseline provide evidence of
    # fragmented printing. An isolated diagram still uses its own picture.
    typical_height = median(b[3] - b[1] for b in inside)
    runs = {}
    for i, box in enumerate(inside):
        height = box[3] - box[1]
        if height > 2 * typical_height:
            continue
        for j in range(i + 1, len(inside)):
            other = inside[j]
            if other[0] - box[2] > height:
                break
            overlap = min(box[3], other[3]) - max(box[1], other[1])
            if (other[0] >= (box[0] + box[2]) / 2 and
                    overlap >= .65 * max(height, other[3] - other[1])):
                runs[j] = max(runs.get(j, 1), runs.get(i, 1) + 1)
                if runs[j] >= 3:
                    return inside
    return []


def table_fragment_picture(regions, page_no, boxes):
    """Copy selected source pixels at their original spacing on a white canvas."""
    from math import ceil, floor
    from PIL import Image

    bounds = [op(b[i] for b in boxes) for i, op in enumerate((min, min, max, max))]
    scale = 3.0
    x0, y0 = floor(bounds[0] * scale), floor(bounds[1] * scale)
    width, height = ceil(bounds[2] * scale) - x0, ceil(bounds[3] * scale) - y0
    if width * height > regions.pixel_budget:
        return None
    with regions.crop(page_no, bounds, scale=scale) as source:
        if source.size != (width, height):
            return None
        result = Image.new('RGB', source.size, 'white')
        for left, top, right, bottom in boxes:
            window = (floor(left * scale) - x0, floor(top * scale) - y0,
                      ceil(right * scale) - x0, ceil(bottom * scale) - y0)
            with source.crop(window) as patch:
                result.paste(patch, window)
    return bounds, result
