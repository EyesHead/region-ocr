"""Recover an omitted OCR page without rasterizing its surrounding prose."""
from hashlib import sha256
import math

from bs4 import BeautifulSoup


VISUAL = {'Table', 'Picture', 'Figure', 'Image', 'Diagram'}
FURNITURE = {'PageHeader', 'PageFooter'}


def _separate_text_regions(image, boxes):
    """Assign overlapping text boxes at an actual source whitespace gutter."""
    import numpy as np

    bounds = [list(box.bbox) for box in boxes]
    ink = np.asarray(image.convert('L')) < 235
    ordered = sorted(range(len(boxes)), key=lambda i: (bounds[i][1], bounds[i][0]))
    for offset, left in enumerate(ordered):
        for right in ordered[offset+1:]:
            a, b = bounds[left], bounds[right]
            if b[1] >= a[3]:
                break
            overlap = min(a[2], b[2])-max(a[0], b[0])
            if overlap < .5*min(a[2]-a[0], b[2]-b[0]) or b[1] <= a[1]:
                continue
            x0 = max(0, math.floor(min(a[0], b[0])))
            x1 = min(image.width, math.ceil(max(a[2], b[2])))
            y0 = max(0, math.ceil(max((a[1]+a[3])/2, b[1]-12)))
            y1 = min(image.height, math.floor(min((b[1]+b[3])/2, a[3]+12)))
            empty = [y for y in range(y0, y1) if not ink[y, x0:x1].any()]
            if not empty:
                raise RuntimeError('Overlapping OCR text boxes have no safe source gutter')
            cut = min(empty, key=lambda y: abs(y-(a[3]+b[1])/2))
            a[3], b[1] = min(a[3], cut), max(b[1], cut)
    return bounds


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


def recover_missing_pages(doc, records, args, regions, orientation, manifest, api, *, reader=None):
    """Retry nonblank pages omitted by the upstream OCR/layout pipeline.

    Text is recognized separately before table bodies. Explicit form/figure
    captions protect their bodies from table OCR; unrelated tables still use
    the normal primary recognizer. All recovered text comes from Surya.
    """
    populated = {p.page_no for item, _ in doc.iterate_items() if hasattr(item, 'prov')
                 and str(getattr(item, 'label', '')) not in {'page_header', 'page_footer'}
                 for p in item.prov}
    missing = [page for page, info in orientation.items()
               if page not in populated and info.get('ink_fraction', 0) >= .0001]
    if not missing:
        return {}
    model = getattr(args, 'surya_ocr_model', None)
    if model is None:
        raise RuntimeError('A nonblank page has no structured OCR; primary page recovery requires the OCR model')
    from surya.layout import LayoutPredictor
    from surya.layout.schema import LayoutBox, LayoutResult
    from surya.recognition import RecognitionPredictor
    from docling_pdf_forms import capture_form_figures
    from docling_pdf_table_content import _safe_markup

    api.local_surya_backend(model)
    manager = model._get_runtime()._manager
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
        image = regions.crop(page)
        try:
            layout = LayoutPredictor(manager)([image])[0]
            if layout.error or not layout.bboxes:
                raise RuntimeError('Primary page recovery returned no usable layout')
            text_boxes = [box for box in layout.bboxes if box.label not in VISUAL]
            furniture_only = all(box.label in FURNITURE for box in layout.bboxes)
            bounds = _separate_text_regions(image, text_boxes)
            crops, layouts = [], []
            try:
                for box, bounds_ in zip(text_boxes, bounds):
                    pixels = (max(0, math.floor(bounds_[0])), max(0, math.floor(bounds_[1])),
                              min(image.width, math.ceil(bounds_[2])), min(image.height, math.ceil(bounds_[3])))
                    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
                        raise RuntimeError('Primary text recovery has a degenerate source region')
                    crop = image.crop(pixels)
                    crops.append(crop)
                    layouts.append(LayoutResult(image_bbox=[0, 0, crop.width, crop.height], bboxes=[LayoutBox(
                        polygon=[[0, 0], [crop.width, 0], [crop.width, crop.height], [0, crop.height]],
                        label=box.label, raw_label=box.raw_label, position=0, count=box.count)]))
                readings = RecognitionPredictor(manager)(crops, full_page=False, layout_results=layouts) if crops else []
            finally:
                for crop in crops:
                    crop.close()
            if len(readings) != len(text_boxes):
                raise RuntimeError('Primary page text recovery result count mismatch')
            size = doc.pages[page].size
            sx, sy = size.width/image.width, size.height/image.height
            recovered, visual_items = [], []
            # Validate all text readings before inserting the recovered items.
            for box, pixels, reading in zip(text_boxes, bounds, readings):
                blocks = reading.blocks
                if len(blocks) != 1 or blocks[0].error or not blocks[0].html:
                    raise RuntimeError('Primary page text block could not be recovered completely')
                html = _safe_markup(blocks[0].html, api)
                text = api.semantic_ocr_text(html)
                if not text.strip():
                    raise RuntimeError('Primary page text recovery returned an empty block')
                recovered.append(dict(page_no=page, bbox=[pixels[0]*sx, pixels[1]*sy,
                    pixels[2]*sx, pixels[3]*sy], label=box.label, html=html, text=text,
                    confidence=blocks[0].confidence))
            for record in recovered:
                item = _insert(doc, page, record['bbox'], record['label'], record['text'], api)
                routes[item.self_ref] = record['html']
            for box in layout.bboxes:
                if box.label not in VISUAL:
                    continue
                bounds_ = [box.bbox[0]*sx, box.bbox[1]*sy, box.bbox[2]*sx, box.bbox[3]*sy]
                item = _insert(doc, page, bounds_, box.label, '', api)
                visual_items.append((item, box, bounds_))
            # Caption recovery can materialize a complete primary Table packet;
            # it needs the real document's insertion and reference interfaces.
            protected = {ref: entry for ref, entry in capture_form_figures(
                doc, recovered, {}, api, reader=reader, regions=regions).items()
                if entry['page'] == page}
            for item, box, bounds_ in visual_items:
                if box.label != 'Table' or item.self_ref in protected:
                    continue
                result = RecognitionPredictor(manager)([image], full_page=False,
                    layout_results=[LayoutResult(image_bbox=layout.image_bbox, bboxes=[box])])[0]
                if len(result.blocks) != 1 or result.blocks[0].error or not result.blocks[0].html:
                    raise RuntimeError('An ordinary table on the recovered page remains unreadable')
                html = _safe_markup(result.blocks[0].html, api)
                if not BeautifulSoup(html, 'html.parser').table:
                    raise RuntimeError('Primary table recovery returned no table')
                recovered.append(dict(page_no=page, bbox=bounds_, label='Table', html=html,
                                      text=api.semantic_ocr_text(html), confidence=result.blocks[0].confidence))
            records.extend(recovered)
            proof = dict(page=page, method='primary_layout_then_separate_text_and_table_blocks',
                recovered_text_blocks=sum(r['label'] not in VISUAL for r in recovered),
                source_objects=len(visual_items), caption_owned_objects=list(protected),
                source_rgb_sha256=sha256(image.tobytes()).hexdigest(), hidden_pdf_text_published=False)
            manifest.setdefault('recovered_missing_pages', []).append(proof)
            if furniture_only:
                # A source page with only a running header or page number is
                # complete only after layout and every text block were read.
                # Existing furniture alone never supplies this evidence.
                furniture_proof = dict(proof,
                    method='primary_layout_and_recognition_verified_furniture_only',
                    layout_labels=[box.label for box in layout.bboxes],
                    text_record_sha256=[sha256(r['html'].encode()).hexdigest() for r in recovered])
                manifest.setdefault('recovered_furniture_only_pages', []).append(furniture_proof)
                api.diagnostic_event('PRIMARY_FURNITURE_ONLY_PAGE_VERIFIED', **furniture_proof)
            api.diagnostic_event('PRIMARY_MISSING_PAGE_RECOVERED', **proof)
        finally:
            image.close()
    return routes
