"""Keep positioned equation packets inside their measured full-width table bands."""
import copy
from hashlib import sha256
import math

from bs4 import BeautifulSoup
import numpy as np

from docling_pdf_math_grid import raster_grid
from docling_pdf_table_layout import grid_ink, set_table


def formula_band_rows(fragment, header, frame, records, image, region, api):
    """Prove closed source bands, complete packet coverage and blank record gaps.

    Each OCR packet is atomic: a multi-line equation with its explanation is
    never split at a BR, math child, fraction, subscript or superscript.
    """
    soup = BeautifulSoup(fragment, 'html.parser')
    table = soup.table
    if table is None or table.thead is None:
        return None
    rows = table.find_all('tr')
    if not rows or any(row.find_parent('thead') is not table.thead for row in rows):
        return None
    profile = api.table_profile(str(table))
    xs, ys = header.get('x_grid', []), header.get('y_grid', [])
    box = frame.get('bbox', [])
    if (profile['issues'] or len(xs) != profile['columns'] + 1 or len(ys) != len(rows) + 1
            or len(box) != 4 or box[3] <= ys[-1] + 4
            or any(abs(a-b) > 3 for a, b in zip((xs[0], ys[0], xs[-1]), box[:3]))):
        return None
    source = grid_ink(image, region, xs)
    if source is None:
        return None
    grid = raster_grid(image, region)
    tolerance = max(2., 2 / source['sy'])
    # The four source borders must exist; the header's short dividers cannot
    # nominate an open chart or a disconnected formula below a table.
    if any(not any(abs(x-rail) <= tolerance and top <= box[1]+tolerance
                   and bottom >= box[3]-tolerance for x, top, bottom in grid['vertical'])
           for rail in (xs[0], xs[-1])):
        return None
    full = source['full_width_rules']
    if any(not any(abs(y-rule) <= tolerance for rule in full) for y in (box[1], box[3])):
        return None
    if any(xs[0]+3 < x < xs[-1]-3 and bottom > ys[-1]+3
           and bottom-max(top, ys[-1]) > 6 for x, top, bottom in grid['connected_vertical']):
        return None
    boundaries = [ys[-1]] + sorted(y for y in full if y > ys[-1]+tolerance)
    if len(boundaries) < 2:
        return None
    packets = []
    for record in records:
        bounds = record.get('bbox')
        if not bounds or len(bounds) != 4:
            continue
        left, top, right, bottom = bounds
        if bottom <= ys[-1] or top >= box[3]:
            continue
        if right <= xs[0] or left >= xs[-1]:
            continue
        # Anything intersecting the body must have an unambiguous owner.
        parsed = BeautifulSoup(record.get('html', ''), 'html.parser')
        if (str(record.get('label', '')).lower() != 'equation' or parsed.math is None
                or parsed.find(['table', 'img', 'svg', 'script', 'style', 'iframe', 'object'])
                or left < xs[0]-1 or right > xs[-1]+1 or top < ys[-1]-1 or bottom > box[3]+1):
            return None
        identity = sha256(record['html'].encode('utf-8')).hexdigest()
        if any(p['hash'] == identity and p['bbox'] == bounds for p in packets):
            continue
        bands = [i for i, (a, b) in enumerate(zip(boundaries, boundaries[1:]))
                 if top >= a-1 and bottom <= b+1]
        if len(bands) != 1:
            return None
        packets.append(dict(html=record['html'], bbox=bounds, hash=identity, band=bands[0]))
    if len(packets) < 2:
        return None
    packets.sort(key=lambda p: (p['bbox'][1], p['bbox'][0]))
    if any(b['bbox'][1] < a['bbox'][3] for a, b in zip(packets, packets[1:])):
        return None  # Competing OCR reads cannot acquire duplicate ownership.

    def pixels(bounds):
        return (max(0, math.floor((bounds[0]-region[0])*source['sx'])),
                max(0, math.floor((bounds[1]-region[1])*source['sy'])),
                min(image.width, math.ceil((bounds[2]-region[0])*source['sx'])),
                min(image.height, math.ceil((bounds[3]-region[1])*source['sy'])))

    covered = np.zeros(source['nongrid'].shape, dtype=bool)
    for packet in packets:
        b = packet['bbox']
        l, t, r, z = pixels([b[0]-1, b[1]-1, b[2]+1, b[3]+1])
        covered[t:z, l:r] = True
    l, t, r, z = pixels([xs[0]+1, ys[-1]+1, xs[-1]-1, box[3]-1])
    ink = source['nongrid'][t:z, l:r] > 0
    count = int(np.count_nonzero(ink))
    missing = int(np.count_nonzero(ink & ~covered[t:z, l:r]))
    if count == 0 or missing:
        return None  # A drawing, omitted line or note must stay visible.
    groups = []
    for index, packet in enumerate(packets):
        separate = index == 0 or packet['band'] != packets[index-1]['band']
        if index and not separate:
            previous = packets[index-1]['bbox']
            start = math.ceil((previous[3]-region[1])*source['sy'])
            stop = math.floor((packet['bbox'][1]-region[1])*source['sy'])
            blank = ~np.any(source['nongrid'][start:stop, l:r], axis=1)
            separate = len(blank) >= 2 and bool(np.any(blank[:-1] & blank[1:]))
        if separate:
            groups.append([])
        groups[-1].append(packet)
    body = soup.new_tag('tbody')
    for group in groups:
        row = soup.new_tag('tr')
        cell = soup.new_tag('td', attrs={'colspan': str(profile['columns'])})
        for packet in group:
            cell.append(BeautifulSoup(packet['html'], 'html.parser'))
        row.append(cell)
        body.append(row)
    for empty in list(table.find_all('tbody', recursive=False)):
        empty.decompose()
    table.append(body)
    result = api.clean_table_html(str(table))
    clean = BeautifulSoup(result, 'html.parser')
    primary_math = body.find_all('math')
    clean_math = clean.table.tbody.find_all('math')
    if len(primary_math) != len(clean_math):
        return None
    for original, rendered in zip(primary_math, clean_math):
        if original.get('display') == 'block':
            rendered['display'] = 'block'
    result = str(clean.table)
    final = api.table_profile(result)
    if final['issues'] or final['columns'] != profile['columns'] or final['rows'] != len(rows)+len(groups):
        return None
    return result, dict(bbox=list(box), physical_band_boundaries=list(map(float, boundaries)),
        records=len(packets), logical_rows=len(groups), source_ink_pixels=count,
        uncovered_ink_pixels=missing, source_record_sha256s=[p['hash'] for p in packets],
        source_bboxes=[list(p['bbox']) for p in packets],
        method='closed_source_frame_and_complete_positioned_equation_packets',
        atomic_primary_packets=True)


def recover_formula_band_routes(doc, routes, excluded, manifest, reader, regions, records, api):
    """Attach primary equations only after raster borders prove their table owner."""
    from docling_core.types.doc import BoundingBox, CoordOrigin

    if reader is None:
        return []
    items = {item.self_ref: item for item in doc.tables}
    repairs = []
    frames = {}
    for entry in manifest.get('tables', []):
        ref, page = entry['ref'], entry['page']
        if ref in excluded or entry.get('joined_into') or ref not in items:
            continue
        fragment = routes.get(ref, '')
        parsed = BeautifulSoup(fragment, 'html.parser')
        if parsed.table is None or parsed.table.thead is None:
            continue
        if any(row.find_parent('thead') is not parsed.table.thead for row in parsed.table.find_all('tr')):
            continue
        header = entry.get('join_geometry') or entry.get('pdf_geometry') or {}
        if not header.get('y_grid'):
            continue
        count = len(parsed.table.find_all('tr'))
        if len(header['y_grid']) < count + 1:
            continue
        header = dict(header, y_grid=header['y_grid'][:count+1])
        header['bbox'] = [header['x_grid'][0], header['y_grid'][0],
                          header['x_grid'][-1], header['y_grid'][-1]]
        if page not in frames:
            frames[page] = regions.scan_tables(page)
        relevant = [r for r in records if r.get('page_no') == page]
        for frame in frames[page]:
            box = frame['bbox']
            if box[3] <= header['y_grid'][-1] + 4:
                continue
            region = [max(0, box[0]-2), max(0, box[1]-2), box[2]+2, box[3]+2]
            image = regions.crop(page, region)
            try:
                planned = formula_band_rows(fragment, header, frame, relevant, image, region, api)
            finally:
                image.close()
            if planned is None:
                continue
            result, proof = planned
            # Remove complete source-owned layout copies, including mixed
            # TextItems; do not remove captions or an item crossing the frame.
            caption_refs = {r.cref for r in items[ref].captions}
            owned = []
            packets = sorted((r for r in relevant if r.get('bbox') in proof['source_bboxes']
                and sha256(r.get('html', '').encode('utf-8')).hexdigest() in proof['source_record_sha256s']),
                key=lambda r: (r['bbox'][1], r['bbox'][0]))
            keys = [''.join(api.semantic_ocr_text(r['html']).split()) for r in packets]
            represented = {''.join(keys[start:end]) for start in range(len(keys))
                           for end in range(start+1, len(keys)+1)}
            unowned = False
            for item in doc.texts:
                reg = api.item_region(item, doc)
                if item.self_ref in caption_refs or not reg or reg[0] != page:
                    continue
                b = reg[1]
                if (box[0]-1 <= b[0] and b[2] <= box[2]+1
                        and header['y_grid'][-1]-1 <= b[1] and b[3] <= box[3]+1):
                    key = ''.join(api.semantic_ocr_text(item.orig or item.text).split())
                    if len(item.prov) != 1 or key and key not in represented:
                        unowned = True
                        break
                    owned.append(item.self_ref)
            if unowned:
                continue
            routes[ref] = result
            set_table(items[ref], entry, result, api)
            items[ref].prov[0].bbox = BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], origin=CoordOrigin.TOPLEFT)
            entry['bbox'] = list(box)
            entry['source_formula_geometry'] = copy.deepcopy(proof)
            entry['source_header_bbox'] = list(header.get('bbox', []))
            excluded.update(owned)
            proof = dict(ref=ref, page=page, excluded_layout_refs=owned, **proof)
            repairs.append(proof)
            api.diagnostic_event('SOURCE_FORMULA_BANDS_RECOVERED', **proof)
            break
    manifest.setdefault('source_formula_band_repairs', []).extend(repairs)
    return repairs
