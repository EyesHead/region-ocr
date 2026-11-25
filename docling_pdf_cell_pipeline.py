"""Route verified ruled tables from raster cells to the local PDF export."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docling_pdf_cell_geometry import Table


Box = tuple[float, float, float, float]
SOURCE_CELL_IMAGE_SCALE = 4.2  # Exported diagram PNGs use a separate source render.


@dataclass(frozen=True)
class CellContent:
    bbox: Box
    html: str = ''


@dataclass(frozen=True)
class RecognizedTable:
    page: int
    geometry: Table
    bbox: Box
    cell_bounds: tuple[Box, ...]
    contents: tuple[tuple[CellContent, ...], ...]


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _intersection(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _same_table(a, b):
    return _intersection(a, b) >= .78 * max(_area(a), _area(b), 1)


def _contained(inner, outer, tolerance=0):
    return (outer[0] - tolerance <= inner[0] < inner[2] <= outer[2] + tolerance
            and outer[1] - tolerance <= inner[1] < inner[3] <= outer[3] + tolerance)


def owns_grid(manifest, page, box):
    return any(entry['method'] == 'raster_cell_atlas' and entry['page'] == page
               and _same_table(entry['bbox'], box) for entry in manifest['tables'])


def _points(box, rect, scale):
    l, t, r, b = box
    return (l / scale + rect.l, t / scale + rect.t, r / scale + rect.l, b / scale + rect.t)


def _positioned_tables(layout):
    from docling_core.types.doc import DocItemLabel

    return [cluster for cluster in (layout.clusters if layout else ())
            if cluster.label == DocItemLabel.TABLE and cluster.confidence >= .5]


def _layout_owners(page, clusters):
    return [(cluster.id, cluster.bbox.to_top_left_origin(page.size.height).as_tuple()) for cluster in clusters]


def _unique_matches(tables, owners):
    matches = [[key for key, bounds in owners if _same_table(box, bounds)] for box in tables]
    return {index: hits[0] for index, hits in enumerate(matches)
            if len(hits) == 1 and sum(hits[0] in other for other in matches) == 1}


def stage_tables(work, page, scale, backend, api, *, source_pdf=None):
    """Recognize text in atlases and keep picture coordinates in PDF points."""
    owners = _layout_owners(page, _positioned_tables(page.predictions.layout))
    if not owners:
        return ()
    if source_pdf is not None:
        from docling_pdf_cpu_route import has_visible_pdf_text

        try:
            if has_visible_pdf_text(source_pdf, page.page_no):
                return ()
        except (OSError, RuntimeError, ValueError) as error:
            api.diagnostic_event('CELL_ATLAS_SOURCE_FALLBACK', page_no=page.page_no, reason=str(error))
            return ()

    from bs4 import BeautifulSoup
    from docling_pdf_cell_geometry import analyze_page
    from docling_pdf_cell_recognition import recognize_cell_regions
    from docling_pdf_table_content import TableContentError, _safe_markup

    tables = analyze_page(work.image)
    bounds = [_points(table.bbox, work.rect, scale) for table in tables]
    matched = _unique_matches(bounds, owners)
    selected = [(tables[index], bounds[index]) for index in matched]
    if not selected:
        return ()

    boxes = [block.bbox for table, _ in selected for cell in table.cells
             for block in cell.blocks if block.kind == 'text']
    reads = recognize_cell_regions(work.image, boxes, backend, page_no=page.page_no,
                                   diagnostic=api.diagnostic_event) if boxes else ()
    if reads is None or len(reads) != len(boxes):
        return ()

    readings = iter(reads)
    recognized = []
    try:
        for table, table_bounds in selected:
            contents = []
            for cell in table.cells:
                children = []
                for block in cell.blocks:
                    box = _points(block.bbox, work.rect, scale)
                    if block.kind == 'picture':
                        children.append(CellContent(box))
                        continue
                    parts = []
                    for read in next(readings):
                        raw = read.html or escape(read.text)
                        parsed = BeautifulSoup(_safe_markup(raw, api), 'html.parser')
                        if (parsed.find(['table', 'ul', 'ol', 'form', 'input', 'link', 'meta'])
                                or not (parsed.get_text(strip=True) or parsed.find('math'))):
                            raise TableContentError('Atlas cell text has incomplete or structural markup')
                        parts.append(str(parsed))
                    if not parts:
                        raise TableContentError('Atlas cell text is missing')
                    children.append(CellContent(box, '<br/>'.join(parts)))
                contents.append(tuple(children))
            cell_bounds = tuple(_points(cell.bbox, work.rect, scale) for cell in table.cells)
            recognized.append(RecognizedTable(page.page_no, table, table_bounds, cell_bounds, tuple(contents)))
    except TableContentError as error:
        api.diagnostic_event('CELL_ATLAS_TABLE_FALLBACK', page_no=page.page_no, reason=str(error))
        return ()

    return tuple(recognized)


def recognize_page(work, page, scale, backend, recognize, api, *, source_pdf=None):
    """Remove only fully read tables from the ordinary page OCR input."""
    from docling_pdf_ocr import InvalidLayoutOutput
    from docling_surya_ocr.errors import SuryaOcrRuntimeError
    from docling_surya_ocr.normalization import NormalizedOcrPage

    tables = stage_tables(work, page, scale, backend, api, source_pdf=source_pdf)
    if not tables:
        return recognize(work), ()
    try:
        with work.image.copy() as masked:
            for table in tables:
                masked.paste('white', table.geometry.mask_bbox)
            with masked.convert('L') as gray:
                blank = gray.getextrema() == (255, 255)
            prediction = (NormalizedOcrPage(blocks=(), image_bbox=(0, 0, *masked.size)) if blank
                          else recognize(replace(work, image=masked)))
    except (InvalidLayoutOutput, SuryaOcrRuntimeError) as error:
        api.diagnostic_event('CELL_ATLAS_PAGE_FALLBACK', page_no=page.page_no, reason=str(error))
        return recognize(work), ()

    outside = tuple(block for block in prediction.blocks
                    if not any(_contained(block.bbox, table.geometry.mask_bbox, tolerance=4) for table in tables))
    if not blank and not outside:
        api.diagnostic_event('CELL_ATLAS_PAGE_FALLBACK', page_no=page.page_no, reason='remaining_page_has_no_ocr_blocks')
        return recognize(work), ()
    api.diagnostic_event('CELL_ATLAS_TABLES_ACCEPTED', page_no=page.page_no, count=len(tables),
                         cell_count=sum(len(table.contents) for table in tables))
    return replace(prediction, blocks=outside), tables


def _table_cells(table):
    from bs4 import BeautifulSoup
    from docling_core.types.doc import BoundingBox, CoordOrigin, TableCell

    return [TableCell(text=BeautifulSoup(' '.join(child.html for child in contents), 'html.parser').get_text(' ', strip=True),
                      bbox=BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT),
                      row_span=cell.rowspan, col_span=cell.colspan,
                      start_row_offset_idx=cell.row, end_row_offset_idx=cell.row + cell.rowspan,
                      start_col_offset_idx=cell.column, end_col_offset_idx=cell.column + cell.colspan)
            for cell, box, contents in zip(table.geometry.cells, table.cell_bounds, table.contents, strict=True)]


def table_predictions(page, tables):
    """Use the raster grid in Docling's table stage when the layout still agrees."""
    from docling.datamodel.base_models import Table

    clusters = _positioned_tables(page.predictions.layout)
    matches = _unique_matches([table.bbox for table in tables], _layout_owners(page, clusters))
    by_id = {cluster.id: cluster for cluster in clusters}
    return {key: Table(id=key, page_no=page.page_no, label=by_id[key].label,
                       cluster=by_id[key], otsl_seq=[], table_cells=_table_cells(tables[index]),
                       num_rows=tables[index].geometry.rows, num_cols=tables[index].geometry.columns)
            for index, key in matches.items()}


def _render_table(table, ref, regions, orientation, api):
    import base64
    from bs4 import BeautifulSoup

    soup = BeautifulSoup('<table><tbody></tbody></table>', 'html.parser')
    rows = [soup.new_tag('tr') for _ in range(table.geometry.rows)]
    soup.tbody.extend(rows)
    assets = []
    picture_boxes = []
    for cell, contents in zip(table.geometry.cells, table.contents, strict=True):
        tag = soup.new_tag('td')
        if cell.rowspan > 1:
            tag['rowspan'] = str(cell.rowspan)
        if cell.colspan > 1:
            tag['colspan'] = str(cell.colspan)
        rows[cell.row].append(tag)
        for child in contents:
            if child.html:
                if tag.contents:
                    tag.append(soup.new_tag('br'))
                tag.extend(BeautifulSoup(child.html, 'html.parser').contents)
                continue
            box = child.bbox
            with regions.crop(table.page, box, scale=SOURCE_CELL_IMAGE_SCALE) as crop:
                fragment = api.image_html(crop, box[2] - box[0], 'cell')
            image = BeautifulSoup(fragment, 'html.parser').img
            image['data-table-content'] = 'picture'
            image['data-source-cell'] = 'true'
            image['data-cell-content'] = 'source-diagram'
            image['data-source-page'] = str(table.page)
            source = api.source_bbox(box, orientation)
            image['data-source-bbox'] = ','.join(f'{point:.4f}' for point in source)
            tag.append(image)
            assets.append(dict(page=table.page, ref=ref, cell=dict(row=cell.row, column=cell.column),
                               bbox=box, source_bbox=source,
                               source_sha256=sha256(image['src'].encode()).hexdigest(),
                               source_image_sha256=sha256(base64.b64decode(image['src'].split(',', 1)[1])).hexdigest()))
            picture_boxes.append(dict(bbox=box))
    return str(soup.table), assets, picture_boxes


def _join_geometry(table):
    left, top, right, bottom = table.bbox
    x0, y0, x1, y1 = table.geometry.bbox
    return dict(basis='raster_cell_atlas', bbox=table.bbox,
                x_grid=[left + (x - x0) * (right - left) / (x1 - x0) for x in table.geometry.x_grid],
                y_grid=[top + (y - y0) * (bottom - top) / (y1 - y0) for y in table.geometry.y_grid])


def install_export_routes(doc, tables_by_page, routes, excluded, manifest, regions, orientation, api):
    """Bind source-owned tables to the existing serializer and asset audit."""
    from docling_core.types.doc import PictureItem, TableData, TableItem
    from docling_pdf_forms import form_owns_region

    installed = set()
    for page, tables in sorted(tables_by_page.items()):
        if page not in doc.pages:
            continue
        items = {item.self_ref: item for item in doc.tables}
        candidates = [(item.self_ref, pos[1]) for item in doc.tables
                      if item.self_ref not in excluded and (pos := api.item_region(item, doc)) and pos[0] == page]
        matches = _unique_matches([table.bbox for table in tables], candidates)
        for index, table in enumerate(tables):
            if form_owns_region(manifest, page, table.bbox):
                continue
            ref = matches.get(index)
            item = items[ref] if ref else api.insert_source_item(doc, page, table.bbox, 'table')
            ref = item.self_ref
            item.data = TableData(num_rows=table.geometry.rows, num_cols=table.geometry.columns,
                                  table_cells=_table_cells(table))
            content, assets, pictures = _render_table(table, ref, regions, orientation[page], api)
            profile = api.table_profile(content, table.geometry.columns)
            routes[ref] = content
            installed.add(ref)
            manifest.setdefault('typed_table_picture_assets', []).extend(assets)
            manifest['tables'].append(dict(ref=ref, page=page, bbox=table.bbox,
                source_bbox=api.source_bbox(table.bbox, orientation[page]), method='raster_cell_atlas',
                selected=profile, raster_columns=table.geometry.columns, html=content,
                join_geometry=_join_geometry(table), retained_ocr_cell_graphics=pictures,
                regional_attempts=[], fallback_reason=[]))

            for other in (*doc.tables, *doc.pictures, *doc.texts):
                if (other.self_ref in installed or other.self_ref in excluded or len(other.prov) != 1
                        or other.children or getattr(other, 'captions', ()) or getattr(other, 'footnotes', ())):
                    continue
                pos = api.item_region(other, doc)
                if not pos or pos[0] != page or not _contained(pos[1], table.bbox, tolerance=1):
                    continue
                # Images may contain marks from outside the table even when a
                # layout box is approximate. Only suppress a matched picture.
                if isinstance(other, PictureItem) and not (_same_table(pos[1], table.bbox)
                        or any(_contained(pos[1], obj['bbox'], tolerance=1) for obj in pictures)):
                    continue
                excluded.add(other.self_ref)
                if isinstance(other, TableItem):
                    routes[other.self_ref] = ''
                api.diagnostic_event('CELL_ATLAS_SOURCE_COPY_EXCLUDED', page_no=page,
                                     ref=other.self_ref, owner=ref, bbox=pos[1])
    return installed
