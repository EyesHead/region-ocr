"""Recover logical records from measured text bands and original cell graphics."""

import base64
from contextlib import ExitStack
from hashlib import sha256
from io import BytesIO
import re
from statistics import median

import cv2
import numpy as np
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

from docling_pdf_typed_lines import _text_bands, _threshold, measured_cell_interior


RECORD = re.compile(r'^([1-9]\d{0,2})(?:[.)])?\s+[^\W\d_]')
VARIANT = re.compile(r'^([а-яё])\)\s+\S', re.I)
ALPHABET = 'абвгдежзиклмнопрстуфхцчшщэюя'
TEXT_BLOCKS = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span'}


def _blocks(node, *, description):
    blocks = []
    for child in node.contents:
        if isinstance(child, NavigableString) and not child.strip():
            continue
        if isinstance(child, Tag) and child.name == 'br':
            continue
        if (description and isinstance(child, Tag) and child.name == 'div' and not child.attrs
                and child.find(['div', 'p', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'], recursive=False)):
            descendants = _blocks(child, description=True)
            if descendants is None:
                return None
            blocks.extend(descendants)
            continue
        if (description and isinstance(child, Tag) and child.name == 'span'
                and not child.attrs and not child.get_text(strip=True) and not child.find(True)):
            continue
        allowed = TEXT_BLOCKS | ({'ol'} if description else {'img'})
        if (not isinstance(child, Tag) or child.name not in allowed
                or child.find(['img', 'svg', 'math', 'table', 'ol', 'ul', 'br', 'code', 'pre', 'input'])
                or child.select_one('.table-cell-review, .source-latex')
                or child.name != 'img' and not child.get_text(strip=True)):
            return None
        blocks.append(child)
    return blocks


def _confirmed_block_lines(blocks, bands, image_height, source_box, source_lines):
    """Match existing DOM text to every positioned source line before using its geometry."""
    def key(value):
        return ''.join(value.split()).casefold().replace('\u00ad', '')

    if not source_lines or len(source_lines) != len(bands):
        return None
    height = source_box[3] - source_box[1]
    size = median(b - a for a, b in bands)
    for line, (top, bottom) in zip(source_lines, bands, strict=True):
        anchor = (line['y'] - source_box[1]) * image_height / height
        if not top - .45 * size <= anchor <= bottom + .45 * size:
            return None
    plan = []
    cursor = 0
    for block in blocks:
        if block.name == 'ol':
            if (block.attrs or len(block.find_all('li')) != len(block.find_all('li', recursive=False))
                    or any(item.attrs or item.find('li') for item in block.find_all('li'))):
                return None
            text = ''.join(str(n) + item.get_text(' ', strip=True)
                           for n, item in enumerate(block.find_all('li'), 1))
        else:
            text = block.get_text(' ', strip=True)
        expected = key(text)
        start, matched = cursor, ''
        while cursor < len(source_lines) and len(matched) < len(expected):
            matched += key(source_lines[cursor]['text'])
            cursor += 1
        if matched != expected:
            return None
        plan.append((bands[start][0], bands[cursor-1][1]))
    return plan if cursor == len(bands) else None


def _isolated_rail_interior(image):
    from docling_pdf_cell_crop import cell_content_crop

    full = (0, 0, image.width, image.height)
    result = cell_content_crop(image, full, full)
    if result is None:
        return None
    (left, top, right, bottom), _, proof = result
    rails = proof['edge_rule_pixel_runs']
    if rails[0] is None or rails[2] is None or bottom - top < 7:
        return None
    with image.crop((left, top, right, bottom)) as crop:
        ink = _threshold(crop)
    if top <= 4 and ink[0].all() and not ink[1:3].any():
        return left, top + 1, right, bottom
    if image.height - bottom <= 4 and ink[-1].all() and not ink[-3:-1].any():
        return left, top, right, bottom - 1
    return None


def _measured_blocks(node, image, source_box=None, source_lines=None, *, align_only=False):
    blocks = _blocks(node, description=True)
    if not blocks:
        return None
    measured = measured_cell_interior(image)
    bounds = measured[0] if measured else _isolated_rail_interior(image) if align_only else None
    if bounds is None:
        return None
    with image.crop(bounds) as interior:
        bands = _text_bands(interior)
        positioned = bands is None and bool(source_lines)
        if positioned:
            ink = _threshold(interior).any(axis=1)
            bands = _occupied_runs(ink)
    if not bands:
        return None
    top = bounds[1]
    bands = [(a + top, b + top) for a, b in bands]
    line_height = median(end - start for start, end in bands)
    if positioned or len(bands) != len(blocks):
        bands = _confirmed_block_lines(blocks, bands, image.height, source_box, source_lines)
        if bands is None:
            return None

    return blocks, bands, line_height


def _descriptions(node, image, source_box, source_lines):
    measured = _measured_blocks(node, image, source_box, source_lines)
    if measured is None:
        return None
    blocks, bands, line_height = measured
    texts = [block.get_text(' ', strip=True) for block in blocks]
    numbered = None
    variant = -1
    group_title = None
    records = []
    for i, text in enumerate(texts):
        numeric = RECORD.match(text) if blocks[i].name != 'ol' else None
        letter = VARIANT.match(text) if blocks[i].name != 'ol' else None
        if numeric:
            value = int(numeric[1])
            if numbered is not None and value != numbered + 1:
                return None
            numbered, variant = value, -1
            next_letter = VARIANT.match(texts[i + 1]) if i + 1 < len(texts) else None
            if text.endswith(':') and next_letter and next_letter[1].lower() == ALPHABET[0]:
                group_title = i
                continue
            records.append((i, i))
        elif letter:
            spelling = letter[1].lower()
            if numbered is None or (variant < 0 and group_title is None) or spelling not in ALPHABET:
                return None
            order = ALPHABET.index(spelling)
            if order != variant + 1:
                return None
            if order == 0:
                if group_title is None:
                    return None
                records.append((group_title, i))
                group_title = None
            else:
                records.append((i, i))
            variant = order
        elif not records or group_title is not None:
            return None
    if len(records) < 2 or group_title is not None or records[0][0] != 0:
        return None

    descriptions = []
    for (start, anchor), stop in zip(records, [start for start, _ in records[1:]] + [len(blocks)], strict=True):
        descriptions.append((bands[anchor][0], bands[stop - 1][1],
                             '<br/>'.join(map(str, blocks[start:stop]))))
    return descriptions, line_height


def _graphic_source(picture, image, picture_assets):
    if picture.get('data-table-content') != 'picture':
        return None
    uri = picture.get('src', '')
    head, _, data = uri.partition(',')
    if head != 'data:image/png;base64' or not data:
        return None
    digest = sha256(uri.encode()).hexdigest()
    by_coordinates = {tuple(asset['source_pixel_bbox']): asset for asset in picture_assets
                      if asset.get('source_sha256') == digest
                      and len(asset.get('source_pixel_bbox', ())) == 4}
    if len(by_coordinates) != 1:
        return None
    bounds, asset = next(iter(by_coordinates.items()))
    if (any(not isinstance(n, int) for n in bounds)
            or not (0 <= bounds[0] < bounds[2] <= image.width and 0 <= bounds[1] < bounds[3] <= image.height)):
        return None
    try:
        data = base64.b64decode(data, validate=True)
        if sha256(data).hexdigest() != asset.get('source_image_sha256'):
            return None
        with Image.open(BytesIO(data)) as original, image.crop(bounds) as crop:
            if original.mode != crop.mode or original.size != crop.size or original.tobytes() != crop.tobytes():
                return None
    except (ValueError, OSError):
        return None
    return picture, bounds, asset


def _occupied_runs(occupied):
    changes = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
    return [(int(a), int(b)) for a, b in zip(changes[::2], changes[1::2], strict=True)]


def _graphic_edges(image, records, line_height, image_top, text_height, graphic_height):
    """Find a blank cut and an independently aligned image onset for each record."""
    source = _threshold(image)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(source, 8)
    # A speck cannot establish picture onset. It remains in its original crop.
    measured = (stats[:, cv2.CC_STAT_AREA] >= 3)
    measured[0] = False
    significant = measured[labels].any(axis=1)
    runs = _occupied_runs(significant)
    if len(runs) < len(records):
        return None

    ratio = graphic_height / text_height
    tolerance = max(6, .63 * line_height * ratio)
    required_blank = max(4, round(.35 * line_height * ratio))
    starts = []
    for row, (top, _, _) in enumerate(records):
        anchor = top * ratio - image_top
        choices = [idx for idx, (start, _) in enumerate(runs)
                   if abs(start - anchor) <= tolerance
                   and (idx == 0 or start - runs[idx - 1][1] >= required_blank)]
        if len(choices) != 1 or row == 0 and choices[0] != 0:
            return None
        starts.append(choices[0])
    if any(a >= b for a, b in zip(starts, starts[1:])):
        return None

    edges = [0]
    raw_occupied = source.any(axis=1)
    for index in starts[1:]:
        a, b = runs[index - 1][1], runs[index][0]
        clear = [j for j in range(max(a + 1, 1), b) if not raw_occupied[j - 1:j + 2].any()]
        if not clear:
            return None
        edges.append(clear[len(clear) // 2])
    edges.append(image.height)
    return edges


def _split_positioned_graphics(blocks, image, assets, description_height, records, line_height):
    # Typed blocks are emitted in source y order. Each image independently
    # proves that order; editable references must occupy their own ink band.
    if len(blocks) != len(records):
        return None
    measured = measured_cell_interior(image)
    if measured is None:
        return None
    with image.crop(measured[0]) as interior:
        edges = _graphic_edges(interior, records, line_height, measured[0][1],
                               description_height, image.height)
        if edges is None:
            return None
        evidence = []
        for block, (top, bottom) in zip(blocks, zip(edges, edges[1:]), strict=True):
            if block.name == 'img':
                original = _graphic_source(block, image, assets)
                if original is None:
                    return None
                bounds = original[1]
                if not top <= bounds[1] - measured[0][1] < bounds[3] - measured[0][1] <= bottom:
                    return None
                evidence.append(original[2])
                continue
            # Require one text-height source band, rather than moving prose
            # to a blank or to a segment containing part of a drawing.
            with interior.crop((0, top, interior.width, bottom)) as region:
                occupied = np.flatnonzero(_threshold(region).any(axis=1))
            if (not len(occupied) or occupied[-1] - occupied[0] + 1 > 1.7 * line_height
                    or len(block.get_text(' ', strip=True).split()) < 3):
                return None
    return [(record[2], str(block)) for record, block in zip(records, blocks, strict=True)], evidence


def split_illustrated_band(description, graphic, description_image, graphic_image, picture_assets, api,
                           *, source_box=None, source_lines=None, parallel=None):
    """Return new HTML payloads and crop provenance for one verified physical source band.

    Both images must be exact physical-cell renders of the same PDF y interval.
    The picture assets must already belong to the graphic cell.
    """
    graphics = _blocks(graphic, description=False)
    if not graphics or not any(block.name == 'img' for block in graphics):
        return None
    text = _descriptions(description, description_image, source_box, source_lines)
    if text is None:
        return None
    records, line_height = text
    if len(graphics) > 1:
        split = _split_positioned_graphics(graphics, graphic_image, picture_assets, description_image.height,
                                           records, line_height)
        if split is None or parallel is None:
            return split
        middle, middle_image = parallel
        measured = _measured_blocks(middle, middle_image, align_only=True)
        if measured is None:
            return None
        blocks, bands, _ = measured
        ratio = description_image.height / middle_image.height
        if len(bands) != len(records) or any(abs(top * ratio - record[0]) > .45 * line_height
                                             for (top, _), record in zip(bands, records, strict=True)):
            return None
        payloads, assets = split
        return [(a, str(b), c) for (a, c), b in zip(payloads, blocks, strict=True)], assets
    if parallel is not None:
        return None
    origin = _graphic_source(graphics[0], graphic_image, picture_assets)
    if origin is None or not origin[2].get('source_graphic_role'):
        return None
    picture, bounds, asset = origin
    left, top, right, bottom = bounds
    new_payloads, evidence = [], []
    with graphic_image.crop(bounds) as combined:
        edges = _graphic_edges(combined, records, line_height, top, description_image.height,
                               graphic_image.height)
        if edges is None:
            return None
        ink = _threshold(combined)
        for index, (start, end) in enumerate(zip(edges, edges[1:])):
            ys, xs = np.where(ink[start:end])
            if not len(xs):
                return None
            crop_left, crop_top = max(0, int(xs.min()) - 3), max(start, start + int(ys.min()) - 3)
            crop_right, crop_bottom = min(combined.width, int(xs.max()) + 4), min(end, start + int(ys.max()) + 4)
            with combined.crop((crop_left, crop_top, crop_right, crop_bottom)) as patch:
                # image_html's width is in points. Preserve the original pixel/display ratio.
                width = float(picture.get('width', combined.width)) * patch.width / combined.width * 72 / 96
                generated = BeautifulSoup(api.image_html(patch, width, 'cell'), 'html.parser').img
            generated.attrs.update({k: v for k, v in picture.attrs.items() if k not in {'src', 'width', 'height', 'id'}})
            digest = sha256(generated['src'].encode()).hexdigest()
            physical_pixels = [left + crop_left, top + crop_top, left + crop_right, top + crop_bottom]
            new_payloads.append((records[index][2], str(generated)))
            evidence.append(dict(asset, source_pixel_bbox=physical_pixels,
                normalized_bbox=[1000 * coordinate / dimension for coordinate, dimension in zip(
                    physical_pixels, [graphic_image.width, graphic_image.height] * 2)],
                source_sha256=digest, source_image_sha256=sha256(
                    base64.b64decode(generated['src'].partition(',')[2])).hexdigest(),
                source_graphic_role=dict(asset['source_graphic_role'], source_content_pixel_bbox=physical_pixels),
                logical_record=index, source_parent_image_sha256=asset['source_image_sha256']))
    return new_payloads, evidence



def _positioned_text(reader, page, box, api):
    try:
        reader.load_page(page)
    except (OSError, RuntimeError, ValueError):
        return []
    glyphs = [g for g in reader.chars if box[0] <= (g['x0'] + g['x1']) / 2 < box[2]
              and box[1] <= (g['top'] + g['bottom']) / 2 < box[3]]
    return api.native_cell_lines(glyphs)


def split_illustrated_table_rows(fragment, geometry, source_image, region, page, ref, manifest, api,
                                 *, source_reader=None):
    """Expand only source-proven bands; keep the physical grid in diagnostics."""
    if (not geometry or not geometry.get('source_grid_verified')
            or geometry.get('hidden_ocr_direction_conflict')
            or len(geometry.get('x_grid', ())) not in {3, 4}):
        return fragment
    from docling_pdf_table_content import TableContentError, _source_crop

    soup = BeautifulSoup(fragment, 'html.parser')
    table = soup.table
    if table is None:
        return fragment
    columns = len(geometry['x_grid']) - 1
    profile = api.table_profile(str(table), columns)
    position = lambda cell: (cell['row'], cell['column'], cell['row_span'], cell['column_span'])
    physical = {position(cell): cell for cell in geometry.get('cells', ())}
    positions = {position(cell) for cell in profile['cells']}
    if profile['issues'] or profile['columns'] != columns or positions != set(physical):
        return fragment

    table_rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    if len(table_rows) != profile['rows']:
        return fragment
    known = {}
    for asset in manifest.get('typed_table_picture_assets', ()):
        if asset.get('page') == page:
            cell = asset.get('cell', {})
            if all(key in cell for key in ('row', 'column', 'row_span', 'column_span')):
                known.setdefault(position(cell), []).append(asset)

    assets, proofs = [], []
    known_digests = {asset['source_sha256'] for group in known.values() for asset in group}
    for index, row in enumerate(table_rows):
        keys = [(index, column, 1, 1) for column in range(columns)]
        if (index < geometry.get('header_rows', 0)
                or not all(key in physical for key in keys) or not known.get(keys[-1])):
            continue
        nodes = row.find_all(['td', 'th'], recursive=False)
        if len(nodes) != columns or any(node.name != 'td' for node in nodes) or not nodes[-1].img:
            continue
        boxes = [physical[key]['bbox'] for key in keys]
        left, right = boxes[0], boxes[-1]
        if any(box[1::2] != left[1::2] for box in boxes[1:]):
            continue
        try:
            with ExitStack() as crops:
                images = [crops.enter_context(_source_crop(source_image, region, box)) for box in boxes]
                parallel = (nodes[1], images[1]) if columns == 3 else None
                inputs = (nodes[0], nodes[-1], images[0], images[-1], known[keys[-1]], api)
                repaired = split_illustrated_band(*inputs, parallel=parallel)
                if repaired is None and source_reader is not None:
                    source_lines = _positioned_text(source_reader, page, left, api)
                    if source_lines:
                        repaired = split_illustrated_band(*inputs, parallel=parallel, source_box=left,
                                                          source_lines=source_lines)
        except TableContentError:
            continue
        if repaired is None:
            continue
        payloads, image_evidence = repaired
        for number, payload in enumerate(payloads):
            new_row = soup.new_tag('tr', attrs=dict(row.attrs))
            new_row['data-source-row'] = str(index + 1)
            new_row['data-pdf-page'] = str(page)
            if number:
                new_row.attrs.pop('id', None)
            for old_cell, content in zip(nodes, payload, strict=True):
                cell = soup.new_tag(old_cell.name, attrs=dict(old_cell.attrs))
                if number:
                    cell.attrs.pop('id', None)
                cell.append(BeautifulSoup(content, 'html.parser'))
                new_row.append(cell)
            row.insert_before(new_row)
        row.decompose()
        assets.extend(entry for entry in image_evidence if entry['source_sha256'] not in known_digests)
        proofs.append(dict(page=page, ref=ref, physical_row=index, records=len(payloads),
                           source_bbox=[left[0], left[1], right[2], right[3]],
                           graphic_cell_bbox=list(right),
                           graphic_bboxes=[entry['source_pixel_bbox'] for entry in image_evidence]))

    if not proofs:
        return fragment
    table['data-source-logical-rows'] = 'true'
    rendered = str(soup)
    checked = api.table_profile(rendered, columns)
    if checked['issues'] or checked['rows'] != profile['rows'] + sum(p['records'] - 1 for p in proofs):
        return fragment
    manifest.setdefault('typed_table_picture_assets', []).extend(assets)
    manifest.setdefault('illustrated_logical_row_repairs', []).extend(proofs)
    for proof in proofs:
        api.diagnostic_event('ILLUSTRATED_LOGICAL_ROWS_RESTORED', **proof)
    return rendered
