"""Replace a damaged scanned table layer using one bounded read in reading coordinates."""

from math import ceil, floor
import re

from local_web.fast_formulas import bounds, coverage


_WORD = re.compile(r'(?:[А-Яа-яЁё]{3,}|[A-Za-z]{3,})')


def _contains(box, x, y):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _damaged(tokens):
    count = len(tokens)
    if count < 40:
        return False
    words = sum(bool(_WORD.fullmatch(token.strip('.,:;()!?«»—-'))) for token in tokens)
    short = sum(len(token) <= 2 and token.isalpha() for token in tokens)
    return words < .2 * count and short > .45 * count


def _wrong_column_numbers(grid, native, fresh):
    """An OCR row numbered 1..N independently identifies broken adjacent table text."""
    for top,bottom in zip(grid.y_grid, grid.y_grid[1:]):
        cells = [(l,top,r,bottom) for l,r in zip(grid.x_grid, grid.x_grid[1:])]
        if len(cells) < 4:
            continue
        def values(words):
            return [''.join(word for word,x,y in words if _contains(cell,x,y)) for cell in cells]
        expected = [str(i+1) for i in range(len(cells))]
        if values(fresh) == expected:
            return sum(a != b for a,b in zip(values(native),expected)) >= len(cells)/2
    return False


def _read_crop(page, page_no, region, reader):
    from PIL import Image
    from local_web.fast_ocr import _image_key, _positioned_read

    sx, sy = page.pixel_scale
    l, t, r, b = region
    with page.image().crop((l, t, r, b)) as crop:
        crop.thumbnail((1650, 1650), Image.Resampling.LANCZOS)
        if reader is None:
            _, boxes, rows, ids, _ = _positioned_read(crop, page_no, _image_key(crop))
            word_rows = [rows[i] if rows else () for i in ids]
        else:
            result = reader(crop, page_no)
            if not isinstance(result, dict) or 'words' not in result:
                raise RuntimeError('Для восстановления ячеек нужны координаты OCR')
            boxes = result['words']
            word_rows = result.get('word_rows') or [()] * len(boxes)
        dx, dy = (r-l)/crop.width, (b-t)/crop.height
        output = []
        for (x,y,u,v,text,score), row in zip(boxes, word_rows, strict=True):
            box = ((l+x*dx)*sx, (t+y*dy)*sy, (l+u*dx)*sx, (t+v*dy)*sy, text, score)
            row = [(word,(l+x*dx)*sx,(t+y*dy)*sy,w*dx*sx,h*dy*sy) for word,x,y,w,h in row]
            output.append((box, row or [(text, box[0], box[1], box[2]-box[0], box[3]-box[1])]))
    return output


def repair_table_layer(page, page_no, reader, warnings):
    reading = page.text_layer
    grids = [g for g in page.grids_for_text(native=True) if g.evidence == 'verified']
    words = [(word, x+w/2, y+h/2) for row in reading['word_rows'] for word,x,y,w,h in row]
    damaged = [g for g in grids if _damaged([word for word,x,y in words if _contains(g.bbox,x,y)])]
    if not damaged:
        return
    # Include nearby grids sharing the same columns and their captions in one read.
    l,t,r,b = bounds([g.bbox for g in damaged])
    targets = [g for g in grids if g in damaged or (
        min(r,g.bbox[2])-max(l,g.bbox[0]) >= .7 * (r-l)
        and max(t-g.bbox[3],g.bbox[1]-b) <= .14 * page.height)]
    l,t,r,b = bounds([g.bbox for g in targets])
    image = page.image()
    sx,sy = page.pixel_scale
    region = (max(0,floor((l-6)/sx)), max(0,floor((t-.17*page.height)/sy)),
              min(image.width,ceil((r+6)/sx)), min(image.height,ceil((b+4)/sy)))
    try:
        fresh = _read_crop(page, page_no, region, reader)
    except RuntimeError as exc:
        warnings.append(f'Страница {page_no}. Не удалось прочитать область таблицы: {exc}')
        fresh = []
    fresh_words = [(word,x+w/2,y+h/2) for box,row in fresh for word,x,y,w,h in row if box[5] >= .80]
    target_boxes = [g.bbox for g in targets if g in damaged or _wrong_column_numbers(g,words,fresh_words)]
    healthy = [g.bbox for g in grids if g.bbox not in target_boxes]
    # Outside the damaged grids, prefer existing readable lines wherever the
    # source and OCR describe the same printed line.
    reliable = {box for box,row in zip(reading['words'],reading['word_rows'])
                if box[2]-box[0] > box[3]-box[1]
                and sum(bool(_WORD.fullmatch(w.strip('.,:;()'))) for w,*_ in row) >= 2
                and not any(coverage(box, rect) > .2 for rect in target_boxes)}
    accepted = []
    for box,row in fresh:
        kept = [(word,x,y,w,h) for word,x,y,w,h in row
                if not any(_contains(rect,x+w/2,y+h/2) for rect in healthy)
                and not any(coverage((x,y,x+w,y+h),rect) >= .5 for rect in reliable)]
        if len(kept) == len(row):
            accepted.append((box,row))
        elif kept:
            rect = bounds([(x,y,x+w,y+h) for _,x,y,w,h in kept])
            accepted.append(((*rect,' '.join(w[0] for w in kept),box[5]),kept))
    # Keep words outside the tables unless OCR actually covered them. Never discard
    # the uncropped half of a native line crossing the edge of the selected region.
    all_rows = list(accepted)
    for box, row in zip(reading['words'], reading['word_rows'], strict=True):
        keep = []
        for word,x,y,w,h in row:
            if any(_contains(rect,x+w/2,y+h/2) for rect in target_boxes):
                continue
            if box not in reliable and any(coverage((x,y,x+w,y+h), new) >= .5 for new,_ in accepted):
                continue
            keep.append((word,x,y,w,h))
        if keep:
            text = ' '.join(word[0] for word in keep)
            rect = bounds([(x,y,x+w,y+h) for _,x,y,w,h in keep])
            all_rows.append(((*rect,text,box[5]),keep))
    all_rows.sort(key=lambda item: ((item[0][1]+item[0][3])/2, item[0][0]))
    lines = [box[4] for box,_ in all_rows]
    reading.update(words=[box for box,_ in all_rows], word_rows=[row for _,row in all_rows],
                   source_lines=lines, blocks=[dict(line_id=i,text=s) for i,s in enumerate(lines)],
                   recovered_tables=target_boxes)
    page.route_reason = 'recovered_table_layer'


def unbound_table_blocks(reading):
    from local_web.fast_ruled_tables import _line_fragments

    boxes = reading['recovered_tables']
    ordered = [(rect[1], rect[0], {'unbound_table': rect}) for rect in boxes]
    for i,(line,row) in enumerate(zip(reading['source_lines'], reading['word_rows'])):
        tokens = [(word, any(_contains(rect,x+w/2,y+h/2) for rect in boxes), (x,y,x+w,y+h))
                  for word,x,y,w,h in row]
        for inside,start,end,rect in _line_fragments(line, tokens) if tokens else ():
            if not inside:
                ordered.append((rect[1],rect[0],dict(line_id=i,text=line,span=(start,end),bbox=rect)))
    return [block for _,_,block in sorted(ordered, key=lambda entry: entry[:2])]


def protect_cells(page, page_no, reading, assets, warnings):
    """Save visible ink when a repaired cell has no dependable textual representation."""
    import numpy as np
    from local_web.fast_figures import _crop, _markup
    from local_web.fast_formulas import formula_evidence

    targets = reading.get('recovered_tables', ())
    if not targets:
        return
    protected = 0
    sx,sy = page.pixel_scale
    pixels = np.asarray(page.image())
    for block in reading['blocks']:
        if 'unbound_table' in block:
            box = block['unbound_table']
            png = _crop(page.page, page.raster, box, (page.width,page.height))
            block['source_image'] = _markup(assets, ('image/png',png,None,None), 'Фрагмент таблицы',box,page_no)
            continue
        if block.get('bbox') not in targets or 'table' not in block:
            continue
        images = {}
        for row,col,rs,cs,text in block['table']['cells']:
            key = row,col
            box = block['cell_boxes'][key]
            parts = block['cell_parts'].get(key, ())
            uncertain = (not text.strip() or any(reading['words'][i][5] < .80 for i,_,_ in parts)
                         or formula_evidence(text, standalone=True)
                         or re.search(r'[=∑√^α-ω]|\b(?:cos|sin|tg)(?=[(\sa-zа-я])', text, re.I)
                         or (cs > 1 and re.search(r'формул[уеы]|\bMF\b', text, re.I))
                         or ('%' in text and not re.search(r'\d|[А-Яа-яЁё]{4,}|[A-Za-z]{4,}', text)
                             and not text.strip().isupper()))
            if not uncertain:
                continue
            l,t,r,b = box
            interior = pixels[ceil((t+1)/sy):floor((b-1)/sy), ceil((l+1)/sx):floor((r-1)/sx)]
            ink = interior < 175
            # A double ruling can leave a solid stripe just inside the measured edge.
            # Remove only full-length stripes at the edge, preserving interior minus signs.
            for axis in (ink, ink.T):
                for edge in (axis[:3], axis[-3:]):
                    if edge.size:
                        edge[edge.mean(axis=1) > .85] = False
            if np.count_nonzero(ink) < 5:
                continue
            png = _crop(page.page, page.raster, box, (page.width,page.height))
            images[key] = _markup(assets, ('image/png',png,None,None), 'Фрагмент ячейки',box,page_no)
        block['cell_images'] = images
        protected += len(images)
    if protected:
        warnings.append(f'Страница {page_no}. Ячеек с фрагментом исходного PDF: {protected}.')
