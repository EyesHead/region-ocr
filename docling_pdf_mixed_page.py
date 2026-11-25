"""Recover an omitted OCR page without rasterizing its surrounding prose."""
from dataclasses import dataclass, replace
from hashlib import sha256
import math

from bs4 import BeautifulSoup


VISUAL = {'Table', 'Picture', 'Figure', 'Image', 'Diagram'}
FURNITURE = {'PageHeader', 'PageFooter'}


class _RecoveryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _TextRegion:
    bounds: tuple[int, int, int, int]
    label: str
    raw_label: str
    count: int
    position: int
    merged: bool = False


def _pixels(image, box):
    left, top, right, bottom = box
    return (max(0, math.floor(left)), max(0, math.floor(top)),
            min(image.width, math.ceil(right)), min(image.height, math.ceil(bottom)))


def _ink_in(ink, box):
    left, top, right, bottom = box
    return right > left and bottom > top and ink[top:bottom, left:right].any()


def _intersection(a, b):
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


def _gutter(ink, upper, lower):
    """Return a blank cut that does not discard ink outside the other box."""
    if upper[1] == lower[1] or upper[3] >= lower[3]:
        return None
    left, first, right, last = _intersection(upper, lower)
    centre = (first + last) / 2
    for cut in sorted(range(first, last), key=lambda y: abs(y - centre)):
        if ink[cut, min(upper[0], lower[0]):max(upper[2], lower[2])].any():
            continue
        lost = ((upper[0], cut, left, upper[3]), (right, cut, upper[2], upper[3]),
                (lower[0], lower[1], left, cut), (right, lower[1], lower[2], cut))
        if not any(_ink_in(ink, bounds) for bounds in lost):
            return cut
    return None


def _separate_text_regions(image, boxes, visual_boxes=()):
    """Read shared ink once, merging boxes when whitespace cannot split it."""
    import numpy as np

    regions = [_TextRegion(_pixels(image, box.bbox), box.label, box.raw_label, box.count, position)
               for position, box in enumerate(boxes)]
    if any(region.bounds[2] <= region.bounds[0] or region.bounds[3] <= region.bounds[1]
           for region in regions):
        raise _RecoveryUnavailable('Primary text recovery has a degenerate source region')
    with image.convert('L') as gray:
        ink = np.asarray(gray) < 235

    for index, region in enumerate(regions):
        if region is None:
            continue
        index_other = 0
        while index_other < len(regions):
            other = regions[index_other]
            if index_other == index or other is None or not _ink_in(ink, _intersection(region.bounds, other.bounds)):
                index_other += 1
                continue
            upper, lower = sorted((region, other), key=lambda part: part.bounds[1])
            a, b = upper.bounds, lower.bounds
            cut = _gutter(ink, a, b)
            if cut is not None:
                top = replace(upper, bounds=(a[0], a[1], a[2], cut))
                bottom = replace(lower, bounds=(b[0], cut, b[2], b[3]))
                region, regions[index_other] = (top, bottom) if upper is region else (bottom, top)
                index_other += 1
                continue
            bounds = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
            if any(_ink_in(ink, _intersection(bounds, _pixels(image, visual.bbox))) for visual in visual_boxes):
                raise _RecoveryUnavailable('Overlapping text and figure have no verified source boundary')
            label = upper.label if upper.label == lower.label else 'Text'
            region = _TextRegion(bounds, label, label, upper.count + lower.count,
                                 min(upper.position, lower.position), merged=True)
            regions[index_other] = None
            index_other = 0
        regions[index] = region
    return sorted((region for region in regions if region is not None), key=lambda region: region.position)


def _insert(doc, page, box, label, text, api):
    """Use public Docling insertion so headings remain heading items."""
    from docling_core.types.doc import BoundingBox, CoordOrigin, ProvenanceItem, DocItemLabel, TableData

    prov = ProvenanceItem(page_no=page, bbox=BoundingBox(
        l=box[0], t=box[1], r=box[2], b=box[3], origin=CoordOrigin.TOPLEFT), charspan=(0, len(text)))
    items = [(item, api.item_region(item, doc)) for item, _ in doc.iterate_items()
             if hasattr(item, 'prov')]
    items = [(item, pos) for item, pos in items if pos]
    before = [(item, pos) for item, pos in items
              if pos[0] < page or pos[0] == page and pos[1][1] <= box[1]]
    after = [(item, pos) for item, pos in items
             if pos[0] > page or pos[0] == page and pos[1][1] > box[1]]
    sibling = (max(before, key=lambda p: (p[1][0], p[1][1][1]))[0] if before else
               min(after, key=lambda p: (p[1][0], p[1][1][1]))[0] if after else None)
    where = dict(sibling=sibling, after=bool(before)) if sibling is not None else {}
    if label in VISUAL:
        if label == 'Table':
            method = doc.insert_table if sibling is not None else doc.add_table
            return method(data=TableData(num_rows=0, num_cols=0, table_cells=[]), prov=prov, **where)
        method = doc.insert_picture if sibling is not None else doc.add_picture
        return method(prov=prov, **where)
    labels = {'SectionHeader': DocItemLabel.SECTION_HEADER, 'Title': DocItemLabel.TITLE,
              'Caption': DocItemLabel.CAPTION, 'PageHeader': DocItemLabel.PAGE_HEADER,
              'PageFooter': DocItemLabel.PAGE_FOOTER}
    method = doc.insert_text if sibling is not None else doc.add_text
    return method(label=labels.get(label, DocItemLabel.TEXT), text=text, orig=text, prov=prov, **where)


def _predict(predictor, images, **kwargs):
    try:
        return predictor(images, **kwargs)
    except (RuntimeError, ValueError) as error:
        raise _RecoveryUnavailable(str(error)) from error


def _verified_text(blocks, api):
    from docling_pdf_table_content import TableContentError, _safe_markup

    if len(blocks) != 1 or blocks[0].error or not blocks[0].html:
        raise _RecoveryUnavailable('Primary source region was not read completely')
    try:
        html = _safe_markup(blocks[0].html, api)
    except TableContentError as error:
        raise _RecoveryUnavailable(str(error)) from error
    text = api.semantic_ocr_text(html)
    if not text.strip():
        raise _RecoveryUnavailable('Primary source region returned empty text')
    return html, text, blocks[0].confidence


def _recover_page(doc, image, page, manager, regions, api, reader, routes, inserted, records):
    from contextlib import ExitStack

    from surya.layout import LayoutPredictor
    from surya.layout.schema import LayoutBox, LayoutResult
    from surya.recognition import RecognitionPredictor
    from docling_pdf_forms import capture_form_figures

    layouts = _predict(LayoutPredictor(manager), [image])
    if len(layouts) != 1 or layouts[0].error or not layouts[0].bboxes:
        raise _RecoveryUnavailable('Primary page recovery returned no usable layout')
    layout = layouts[0]
    text = [box for box in layout.bboxes if box.label not in VISUAL]
    visual = [box for box in layout.bboxes if box.label in VISUAL]
    text_regions = _separate_text_regions(image, text, visual)
    recognizer = RecognitionPredictor(manager)
    with ExitStack() as stack:
        crops, crop_layouts = [], []
        for region in text_regions:
            crop = stack.enter_context(image.crop(region.bounds))
            crops.append(crop)
            polygon = [[0, 0], [crop.width, 0], [crop.width, crop.height], [0, crop.height]]
            crop_layouts.append(LayoutResult(image_bbox=[0, 0, crop.width, crop.height], bboxes=[LayoutBox(
                polygon=polygon, label=region.label, raw_label=region.raw_label,
                position=0, count=region.count)]))
        readings = _predict(recognizer, crops, full_page=False, layout_results=crop_layouts) if crops else []
    if len(readings) != len(text_regions):
        raise _RecoveryUnavailable('Primary page text recovery result count mismatch')

    size = doc.pages[page].size
    sx, sy = size.width/image.width, size.height/image.height

    def source_bounds(pixels):
        return [pixels[0]*sx, pixels[1]*sy, pixels[2]*sx, pixels[3]*sy]

    recovered, visual_items = [], []
    for region, reading in zip(text_regions, readings):
        html, text, confidence = _verified_text(reading.blocks, api)
        if region.merged and BeautifulSoup(html, 'html.parser').find('table'):
            raise _RecoveryUnavailable('Merged text region requires table boundaries')
        recovered.append(dict(page_no=page, bbox=source_bounds(region.bounds), label=region.label,
                              html=html, text=text, confidence=confidence))
    for record in recovered:
        item = _insert(doc, page, record['bbox'], record['label'], record['text'], api)
        inserted.append(item)
        routes[item.self_ref] = record['html']
    for box in visual:
        bounds = source_bounds(box.bbox)
        item = _insert(doc, page, bounds, box.label, '', api)
        inserted.append(item)
        visual_items.append((item, box, bounds))
    # Caption ownership is resolved before reading independent table bodies.
    protected = {ref: entry for ref, entry in capture_form_figures(
        doc, recovered, {}, api, reader=reader, regions=regions).items() if entry['page'] == page}
    for item, box, bounds in visual_items:
        if box.label != 'Table' or item.self_ref in protected:
            continue
        reading = _predict(recognizer, [image], full_page=False,
                           layout_results=[LayoutResult(image_bbox=layout.image_bbox, bboxes=[box])])
        if len(reading) != 1:
            raise _RecoveryUnavailable('Primary page table recovery result count mismatch')
        html, text, confidence = _verified_text(reading[0].blocks, api)
        if not BeautifulSoup(html, 'html.parser').table:
            raise _RecoveryUnavailable('Primary table recovery returned no table')
        recovered.append(dict(page_no=page, bbox=bounds, label='Table', html=html,
                              text=text, confidence=confidence))

    records[:] = [record for record in records if record.get('page_no') != page] + recovered
    proof = dict(page=page, method='primary_layout_then_separate_text_and_table_blocks',
                 recovered_text_blocks=sum(r['label'] not in VISUAL for r in recovered),
                 merged_text_regions=sum(part.merged for part in text_regions),
                 source_objects=len(visual_items), caption_owned_objects=list(protected),
                 source_rgb_sha256=sha256(image.tobytes()).hexdigest(), hidden_pdf_text_published=False)
    if all(box.label in FURNITURE for box in layout.bboxes):
        # Standalone page numbering is complete only after every block was read.
        return proof, dict(proof, method='primary_layout_and_recognition_verified_furniture_only',
                           layout_labels=[box.label for box in layout.bboxes],
                           text_record_sha256=[sha256(r['html'].encode()).hexdigest() for r in recovered])
    return proof, None


def _retain_source_page(doc, records, image, page, reason, manifest, api, picture_routes, excluded):
    size = doc.pages[page].size
    box = [0, 0, size.width, size.height]
    # An omitted page can still have a recognized running header or footer.
    # Hide those standalone copies once the full image owns their source ink.
    for item in doc.texts:
        if str(getattr(item, 'label', '')) in {'page_header', 'page_footer'} and (
                item.prov and all(prov.page_no == page for prov in item.prov)):
            excluded.add(item.self_ref)
    item = _insert(doc, page, box, 'Picture', '', api)
    if picture_routes is None:
        from docling_core.types.doc import ImageRef
        item.image = ImageRef.from_pil(image, dpi=round(api.IMAGE_SCALE * 72))
    else:
        figure = BeautifulSoup(api.image_html(image, size.width, 'recovered-page'), 'html.parser').figure
        figure['data-source-page'] = page
        figure.img['alt'] = f'Страница PDF {page}'
        picture_routes[item.self_ref] = str(figure)
    records[:] = [record for record in records if record.get('page_no') != page]
    proof = dict(page=page, ref=item.self_ref, bbox=box, reason=str(reason),
                 source_rgb_sha256=sha256(image.tobytes()).hexdigest())
    manifest.setdefault('recovered_source_page_images', []).append(proof)
    manifest.setdefault('source_illustration_picture_refs', []).append(item.self_ref)
    api.diagnostic_event('PRIMARY_MISSING_PAGE_SOURCE_RETAINED', **proof)


def recover_missing_pages(doc, records, args, regions, orientation, manifest, api, *, reader=None,
                          picture_routes=None, excluded=None):
    """Retry nonblank pages omitted by the upstream OCR/layout pipeline.

    Keep the source page available if recovery cannot verify complete OCR.
    """
    populated = {p.page_no for item, _ in doc.iterate_items() if hasattr(item, 'prov')
                 and str(getattr(item, 'label', '')) not in {'page_header', 'page_footer'}
                 for p in item.prov}
    missing = [page for page, info in orientation.items()
               if page not in populated and info.get('ink_fraction', 0) >= .0001]
    if not missing:
        return {}
    model = getattr(args, 'surya_ocr_model', None)
    manager = None
    unavailable = 'Primary page recovery requires the OCR model'
    if model is not None:
        try:
            api.local_surya_backend(model)
            manager = model._get_runtime()._manager
        except (RuntimeError, ValueError) as error:
            unavailable = str(error)
    if excluded is None:
        excluded = set()
    routes = {}
    for page in missing:
        api.diagnostic_event('PRIMARY_MISSING_PAGE_RECOVERY_BEGIN', page=page)
        if page not in doc.pages:
            from docling_core.types.doc import Size
            source_page = regions.pdf[page-1]
            try:
                width, height = source_page.get_size()
            finally:
                source_page.close()
            doc.add_page(page_no=page, size=Size(width=width, height=height))
        with regions.crop(page, scale=api.IMAGE_SCALE) as image:
            inserted = []
            try:
                if manager is None:
                    raise _RecoveryUnavailable(unavailable)
                proof, furniture_proof = _recover_page(doc, image, page, manager, regions, api,
                                                       reader, routes, inserted, records)
            except _RecoveryUnavailable as error:
                for item in inserted:
                    routes.pop(item.self_ref, None)
                if inserted:
                    doc.delete_items(node_items=inserted)
                _retain_source_page(doc, records, image, page, error, manifest, api, picture_routes, excluded)
                continue
            manifest.setdefault('recovered_missing_pages', []).append(proof)
            if furniture_proof:
                manifest.setdefault('recovered_furniture_only_pages', []).append(furniture_proof)
                api.diagnostic_event('PRIMARY_FURNITURE_ONLY_PAGE_VERIFIED', **furniture_proof)
            api.diagnostic_event('PRIMARY_MISSING_PAGE_RECOVERED', **proof)
    return routes
