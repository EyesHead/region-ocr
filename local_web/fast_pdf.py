"""Export positioned PDF text, graphics and tables with page-level OCR routing."""
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from html import escape
import re

from docling_pdf_deskew import PageDeskew, source_bbox
from local_web.balanced_table_pages import grid_attributes, grid_enabled, read_pages
from local_web.fast_figures import FormulaBatches, _resource, page_graphics
from local_web.fast_formulas import EQUATION_NUMBER, coverage, ordinary_text
from local_web.fast_pdf_text import bind_tables, open_text_document
from local_web.fast_text_layout import TextLine, paragraph_html
from local_web.pdf_line_join import join_html_lines


def _merged_table_html(page_no, cells, attrs=''):
    by_row = defaultdict(list)
    for row, _, rowspan, colspan, html in sorted(cells):
        rs = f' rowspan="{rowspan}"' if rowspan > 1 else ''
        cs = f' colspan="{colspan}"' if colspan > 1 else ''
        by_row[row].append(f'<td{rs}{cs}>{html}</td>')
    rows = ['<tr>' + ''.join(by_row[row]) + '</tr>' for row in range(max(r + rs for r, _, rs, _, _ in cells))]
    return f'<table data-source-page="{page_no}"{attrs}><tbody>' + ''.join(rows) + '</tbody></table>'


def _scan_formula_words(line, polygons, sx, sy, *, split_cells=False):
    text = line[4]
    number = EQUATION_NUMBER.search(text)
    numbered = number is not None and bool(text[:number.start()].strip())
    prose_formula = ordinary_text(text) and any(ch in text for ch in '=≈≃≠≤≥±∓×÷∑∫∏√^_/')
    if len(polygons) < 2 or not (split_cells or numbered or prose_formula):
        return [line], ()
    row, spans = [], []
    offset = 0
    for word, x, y, w, h in polygons:
        start = text.find(word, offset)
        if not word or start < 0 or text[offset:start].strip():
            return [line], ()
        box = (x*sx, y*sy, (x+w)*sx, (y+h)*sy, word)
        if w <= 0 or h <= 0 or coverage(box, line) < .8 or (row and box[0] < row[-1][0]):
            return [line], ()
        offset = start + len(word)
        row.append(box)
        spans.append((start, offset))
    return (row, spans) if not text[offset:].strip() else ([line], ())


def _scan_cell_ranges(reading, sx, sy):
    ranges = {}
    for block in reading['blocks']:
        boxes = block.get('cell_boxes', {})
        for key, parts in block.get('cell_parts', {}).items():
            if key not in boxes:
                continue
            l,t,r,b = boxes[key]
            box = (l*sx,t*sy,r*sx,b*sy)
            for index,start,end in parts:
                ranges.setdefault(index, []).append((start,end,box))
    return ranges


def _scan_cell_words(line, polygons, sx, sy, ranges, owners, *, split_words=False):
    row, spans = _scan_formula_words(line,polygons,sx,sy,split_cells=split_words or bool(ranges))
    if not ranges:
        return row, spans
    text = line[4]
    if not spans:
        # A line-level OCR box can cross a ruling. Its cell slices already
        # identify the characters and the physical rectangle of each fragment.
        ends = sorted({0,len(text),*(a for a,_,_ in ranges),*(b for _,b,_ in ranges)})
        row, spans = [], []
        for start,end in zip(ends,ends[1:]):
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end-1].isspace():
                end -= 1
            if start == end:
                continue
            cell = next((box for a,b,box in ranges if a <= start < b), None)
            l,t,r,b = line[:4]
            if cell:
                l,t,r,b = max(l,cell[0]),max(t,cell[1]),min(r,cell[2]),min(b,cell[3])
            elif len(ends) > 2:
                l,r = l+(r-l)*start/len(text),l+(r-l)*end/len(text)
            box = (l,t,r,b) if r > l and b > t else line[:4]
            row.append((*box,text[start:end]))
            spans.append((start,end))
    for word,(start,end) in zip(row,spans):
        middle = (start+end)/2
        cell = next((box for a,b,box in ranges if a <= middle < b), None)
        if cell is not None:
            owners[id(word)] = cell
    return row, spans


def _scan_line_graphics(index, line, row, spans, anchors, formulas):
    before = ''.join(anchors.get((id(word), False), '') for word in row)
    after = ''.join(anchors.get((id(word), True), '') for word in row)
    replacement = None
    if not spans:
        replacement = formulas.get(id(line))
    elif any(id(word) in formulas for word in row):
        parts = []
        offset = 0
        for word, (start, end) in zip(row, spans, strict=True):
            parts.append(escape(line[4][offset:start]))
            parts.append(formulas.get(id(word), escape(line[4][start:end])))
            offset = end
        parts.append(escape(line[4][offset:]))
        replacement = ''.join(parts)
    return index, line[4], before, after, replacement


@dataclass(frozen=True)
class _ScanGraphics:
    index: int
    box: tuple
    text: str
    fragments: tuple
    anchors: tuple

    def render(self, start, end):
        if not self.fragments and not self.anchors:
            return escape(self.text[start:end])
        events = []
        for left,right,html in self.fragments:
            if right <= start or left >= end:
                continue
            # Place a fragment once, in the cell containing its first character.
            events.append((max(start,left), 2, min(end,right), html if left >= start else ''))
        for left,right,head,tail in self.anchors:
            if head and start <= left < end:
                events.append((left, 1, left, head))
            if tail and start < right <= end:
                events.append((right, 0, right, tail))
        parts = []
        cursor = start
        for left, _, right, html in sorted(events, key=lambda item: item[:2]):
            parts.extend((escape(self.text[cursor:left]), html))
            cursor = max(cursor, right)
        parts.append(escape(self.text[cursor:end]))
        return ''.join(parts)


def _positioned_scan_graphics(index, line, row, spans, anchors, formulas):
    text = line[4]
    spans = spans or ((len(text)-len(text.lstrip()),len(text.rstrip())),)
    fragments, pictures = [], []
    previous_formula = False
    for word,(left,right) in zip(row,spans,strict=True):
        key = id(word)
        if key in formulas:
            html = formulas[key]
            if previous_formula and not html:
                start,_,html = fragments[-1]
                fragments[-1] = (start,right,html)
            else:
                fragments.append((left,right,html))
            previous_formula = True
        else:
            previous_formula = False
        head,tail = anchors.get((key,False),''),anchors.get((key,True),'')
        if head or tail:
            pictures.append((left,right,head,tail))
    return _ScanGraphics(index,line[:4],text,tuple(fragments),tuple(pictures))


def _positioned_ocr_blocks(reading, page_no, graphic_lines, width, page_size, scale, source_boxes):
    source = reading['source_lines']
    graphics = {item.index: item for item in graphic_lines}
    blocks, pending = [], []
    table_count = 0

    def flush():
        if width is not None:
            blocks.extend(paragraph_html(pending,width,page_no,source_boxes=source_boxes))
        else:
            blocks.extend(f'<p data-source-page="{page_no}">{line.html}</p>' for line in pending)
        pending.clear()

    def source_slice(index, start, end):
        item = graphics.get(index)
        return item.render(start,end) if item is not None else escape(source[index][start:end])

    for item in reading['blocks']:
        if 'text' in item:
            index, line = item['line_id'], item['text']
            start,end = item.get('span',(len(line)-len(line.lstrip()),len(line.rstrip())))
            value = source_slice(index,start,end)
            if value:
                if index in graphics:
                    box = graphics[index].box
                    if 'span' in item and 'bbox' in item:
                        l,t,r,b = item['bbox']
                        box = (l*scale[0], t*scale[1], r*scale[0], b*scale[1])
                    pending.append(TextLine(box,line,value))
                else:
                    flush()
                    blocks.append(f'<p data-source-page="{page_no}">{value}</p>')
            continue
        flush()
        if 'rows' in item:
            cells = [(r,c,1,1,value) for r,row in enumerate(item['rows']) for c,value in enumerate(row)]
        elif 'table' in item:
            cells = item['table']['cells']
        else:
            for index in item.get('line_ids', ()):
                blocks.append(f'<p data-source-page="{page_no}">{source_slice(index,0,len(source[index]))}</p>')
            continue
        owned = item.get('cell_parts',{})
        rendered = []
        for row,col,rs,cs,value in cells:
            parts = [source_slice(index,a,b) for index,a,b in owned.get((row,col),())]
            if parts and width is not None:
                html = join_html_lines(parts)
            else:
                html = ' '.join(parts) if parts else escape(value)
            rendered.append((row,col,rs,cs,html))
        if rendered:
            attrs = ''
            if item.get('grid') is not None and page_size:
                attrs = grid_attributes(item['grid'],scale,page_size)
            blocks.append(_merged_table_html(page_no,rendered,attrs))
            table_count += 1
    flush()
    return blocks, table_count


def _ocr_blocks(reading,page_no, graphic_lines=(), *, width=None, page_size=None, scale=(1,1),source_boxes=False):
    """Accept plain OCR or measured blocks; never treat OCR strings as markup."""
    if isinstance(reading,dict) and 'source_lines' in reading:
        return _positioned_ocr_blocks(reading,page_no,graphic_lines,width,page_size,scale,source_boxes)
    if isinstance(reading,str):
        items = [{'text':reading}]
    elif isinstance(reading,dict) and isinstance(reading.get('blocks'),list):
        items = reading['blocks']
    else:
        items = []
    positioned = defaultdict(deque)
    for index, text, before, after, *replacement in graphic_lines:
        positioned[text.strip()].append((index,before,after,replacement[0] if replacement else None))
    blocks = []
    tables = 0
    placed = []
    def render_line(text):
        entries = positioned.get(text.strip())
        if entries:
            index,before,after,replacement = entries.popleft()
            placed.append((index,len(blocks)))
            return before+(escape(text) if replacement is None else replacement)+after
        return escape(text)

    def render_cell(text):
        if positioned.get(text.strip()):
            return render_line(text)
        matches = []
        for line, entries in positioned.items():
            if len(entries) == 1 and entries[0][3] is not None and line in text:
                hits = list(re.finditer(r'(?<!\S)' + re.escape(line) + r'(?!\S)', text))
                if len(hits) == 1:
                    matches.append((hits[0].start(), hits[0].end(), line))
        if len(matches) != 1:
            return escape(text)
        left, right, line = matches[0]
        return escape(text[:left]) + render_line(line) + escape(text[right:])

    for item in items:
        if isinstance(item,dict) and set(item)=={'text'} and isinstance(item['text'],str):
            for line in item['text'].splitlines():
                if line.strip():
                    html = render_line(line.strip())
                    if html:
                        blocks.append(f'<p data-source-page="{page_no}">{html}</p>')
        elif isinstance(item,dict) and set(item)=={'rows'}:
            rows = item['rows']
            cells = []
            for row in rows or ():
                row = [str(c) if c is not None else '' for c in (row if isinstance(row,(list,tuple)) else [row])]
                cells.append('<tr>'+''.join('<td>'+render_cell(c)+'</td>' for c in row)+'</tr>')
            blocks.append(f'<table data-source-page="{page_no}"><tbody>'+''.join(cells)+'</tbody></table>')
            tables += 1
        elif isinstance(item,dict) and set(item)=={'table'}:
            cells = item['table'].get('cells') or []
            if not cells:
                continue
            blocks.append(_merged_table_html(page_no, [(r, c, rs, cs, render_cell(text))
                          for r, c, rs, cs, text in cells]))
            tables += 1
    # A table may combine multiple OCR lines into one cell. Keep an anchored
    # source crop even when its complete OCR line is absent from the HTML.
    remaining = sorted((row for entries in positioned.values() for row in entries if row[1] or row[2]),
                       key=lambda row: row[0])
    for index, before, after, _ in reversed(remaining):
        dest = next((offset for n,offset in placed if n > index),len(blocks))
        blocks.insert(dest, f'<p data-source-page="{page_no}">{before}{after}</p>')
    return blocks, tables


def _fallback_image(image, image_assets):
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    src = _resource(image_assets, 'image/png', buffer.getvalue())
    return f'<img alt="Исходная страница PDF" src="{src}">'


def _page_range(pages, total):
    if not total:
        raise ValueError('PDF не содержит страниц')
    if not pages:
        return 1, total
    match = re.fullmatch(r'\s*([1-9]\d*)\s*-\s*([1-9]\d*)\s*', pages)
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= total:
        raise ValueError('Диапазон выходит за пределы PDF или указан неверно')
    return int(match[1]), int(match[2])


def _read_text(page, page_no, balanced, detailed_tables, ocr_page, stats):
    if page.text_layer is not None:
        try:
            bind_tables(page, page.text_layer, balanced=balanced)
        except Exception as error:
            print(f'PDF: страница {page_no}, разметка таблицы ({type(error).__name__}: {error})', flush=True)
            stats['warnings'].append(f'На странице {page_no} не удалось собрать таблицу. Текст сохранён.')
        return page.text_layer
    try:
        if ocr_page is not None:
            return ocr_page(page.image(), page_no)
        from local_web.fast_ocr import recognize_page_with_layout
        grids = page.grids_for_text(native=False)
        # Orientation has already read the winning raster, including a possible empty read.
        prepared = {'positioned_read': page.positioned_read} if balanced else {}
        return recognize_page_with_layout(page.image(), page_no, recover_merged_tables=balanced,
            balanced_grid=detailed_tables, physical_grids=grids if detailed_tables or grids else None,
            physical_grid_reasons=page.reasons, **prepared)
    except RuntimeError as error:
        print(f'RapidOCR: страница {page_no}; {error}', flush=True)
        stats['ocr_error_pages'] = stats.get('ocr_error_pages', 0) + 1
        return None


def _graphics(page, page_no, reading, scale, assets, batches, stats, links, cancelled):
    if not isinstance(reading, dict) or not reading.get('words'):
        return []
    sx, sy = scale
    rows, lines, scores, cells = [], [], {}, {}
    ranges = _scan_cell_ranges(reading, sx, sy)
    word_rows = reading.get('word_rows', ())
    ids = reading.get('line_ids', range(len(reading['words'])))
    for offset, (index, (l, t, r, b, text, confidence)) in enumerate(zip(ids, reading['words'], strict=True)):
        if not text.strip() or not (r > l and b > t):
            continue
        line = l*sx, t*sy, r*sx, b*sy, text
        polygons = word_rows[offset] if offset < len(word_rows) else ()
        row, spans = _scan_cell_words(line, polygons, sx, sy, ranges.get(index, ()), cells,
                                      split_words=page.text_layer is not None)
        rows.append(row)
        lines.append((index, line, row, spans))
        scores.update((id(word), confidence) for word in row)
    formulas = {}
    anchors = page_graphics(page.page, page_no, rows, page.objects, assets, raster=page.raster,
        scores=scores, stats=stats, links=links, cancelled=cancelled, formula_text=formulas,
        formula_batches=batches, formula_cells=cells, horizontal_rules=page.horizontal_rules)
    entry = _positioned_scan_graphics if 'source_lines' in reading else _scan_line_graphics
    return [entry(*line, anchors, formulas) for line in lines]


def _page_blocks(page, page_no, reading, *, assets, batches, stats, links, cancelled, source_boxes):
    sizes = page.width, page.height
    scale = (1., 1.) if page.text_layer is not None else page.pixel_scale
    graphics = _graphics(page, page_no, reading, scale, assets, batches, stats, links, cancelled)
    blocks, tables = _ocr_blocks(reading, page_no, graphics, width=page.width, page_size=sizes,
                                 scale=scale, source_boxes=source_boxes)
    vector = 0
    if isinstance(reading, dict):
        if page.source == 'vector':
            vector = sum(isinstance(item, dict) and item.get('grid') is not None for item in reading.get('blocks', ()))
        if source_boxes and reading.get('balanced_grid_reason'):
            page.reasons.extend(reading['balanced_grid_reason'].split(', '))
    if not blocks and page.grids:
        for grid in page.grids:
            cells = [(c.row, c.column, c.rowspan, c.colspan, '') for c in grid.cells]
            attrs = grid_attributes(grid, page.grid_scale, sizes)
            blocks.append(_merged_table_html(page_no, cells, attrs))
        tables = len(page.grids)
        vector = tables if page.source == 'vector' else 0
    stats['vector_tables'] += vector
    stats['scan_tables'] += tables - vector
    return blocks


def _table_diagnostics(directory, number, page, warnings):
    if not page.grids or page.reasons:
        reasons = ', '.join(dict.fromkeys(page.reasons)) or 'no_ruled_grid'
        print(f'Balanced tables: page {number}; {reasons}', flush=True)
    if directory is not None:
        from local_web.balanced_table_debug import write_page
        try:
            write_page(directory, number, page, page.grids, page.grid_scale)
        except OSError as error:
            warnings.append(f'Не удалось сохранить разметку таблиц страницы {number}: {error}')


def _stitch(sections, debug_tables, warnings):
    from local_web.balanced_table_stitch import stitch_pages
    decisions = []
    try:
        sections = stitch_pages(sections, decisions=decisions)
    except Exception as error:
        print(f'Balanced tables: separate page fragments ({type(error).__name__}: {error})', flush=True)
    for decision in decisions:
        if 'possible_continuation' in decision.values():
            print(f'Balanced tables: {decision}', flush=True)
    if debug_tables is not None:
        from local_web.balanced_table_debug import write_joins
        try:
            write_joins(debug_tables, decisions)
        except OSError as error:
            warnings.append(f'Не удалось сохранить решения о склейке таблиц: {error}')
    return sections


_BOUNDING_BOX = re.compile(r'\bdata-source-bbox="([-+\d.eE]+(?:,[-+\d.eE]+){3})"')
_SOURCE_PAGE = re.compile(r'\bdata-source-page="(\d+)"')


def _source_coordinates(section, views):
    """Map our numeric source attributes after stitching, including cells moved from another page."""
    def restore(match):
        start = section.rfind('<', 0, match.start())
        end = section.find('>', match.end())
        tag = section[start:end]
        page = _SOURCE_PAGE.search(tag)
        view = views.get(int(page[1])) if page else None
        if view is None:
            return match[0]
        box = source_bbox(tuple(map(float, match[1].split(','))), view)
        attrs = 'data-source-bbox="' + ','.join(format(v, '.8g') for v in box) + '"'
        if 'data-grid-x=' in tag:
            attrs += (f' data-layout-bbox="{match[1]}" data-grid-coordinate-system="reading"'
                      f' data-grid-rotation="{view["clockwise"]}" data-grid-deskew="{view.get("deskew_degrees", 0)}"')
        return attrs
    return _BOUNDING_BOX.sub(restore, section)


def convert_fast_pdf(source, pages='', *, mode=None, ocr_page=None, on_progress=None, check_cancel=None,
                     image_assets=None, formula_reader=None, balanced_grid=None, debug_tables=None,
                     recover_merged_tables=None):
    """Convert selected physical pages with the fast or balanced local PDF reader."""
    import pypdfium2 as pdf
    # Older API callers selected the whole mode with the table flag.
    if mode is None:
        mode = 'balanced' if recover_merged_tables else 'fast'
    if mode not in ('fast', 'balanced'):
        raise ValueError(f'Неизвестный режим обработки PDF: {mode}')
    balanced = mode == 'balanced'
    detailed = balanced and (grid_enabled() if balanced_grid is None else balanced_grid)
    if balanced and formula_reader is None:
        from docling_pdf_balanced_math import local_formula_reader
        formula_reader = local_formula_reader()
    stats = dict(vector_tables=0, scan_tables=0, pages=0, native_pages=0, ocr_pages=0, blank_pages=0,
                 image_fallback_pages=0, warnings=[])
    batches = FormulaBatches(formula_reader, image_assets, stats, check_cancel) if balanced else None
    sections, fallback, views = [], [], {}
    previous_figure = None
    with pdf.PdfDocument(source) as document, open_text_document(source) as text_document:
        total = len(document)
        first, last = _page_range(pages, total)
        if on_progress:
            on_progress(5, 'Проверяем текстовый слой PDF')
        def page_progress(number, native):
            percent = 5 + int(85 * (number-first) / (last-first+1))
            stage = 'Читаем текстовый слой страницы' if native else 'Выравниваем и распознаём страницу'
            on_progress(percent, f'{stage} {number} из {total}')
        prepared = read_pages(document, first, last, improved=detailed, deskew=PageDeskew(source, total),
            text_document=text_document, simple_vectors=not balanced, orient_scans=balanced and ocr_page is None,
            cancelled=check_cancel, on_page=page_progress if on_progress else None)
        with closing(prepared):
            for number, page in prepared:
                if check_cancel:
                    check_cancel()
                links = {'previous': previous_figure}
                method, blocks = 'blank', []
                if not page.blank:
                    method = 'native' if page.text_layer is not None else 'ocr'
                    reading = _read_text(page, number, balanced, detailed, ocr_page, stats)
                    blocks = _page_blocks(page, number, reading, assets=image_assets, batches=batches, stats=stats,
                                          links=links, cancelled=check_cancel, source_boxes=detailed)
                    if not blocks or reading is None:
                        blocks.append(_fallback_image(page.image(), image_assets))
                        fallback.append(number)
                if detailed:
                    _table_diagnostics(debug_tables, number, page, stats['warnings'])
                if links.get('match') and sections:
                    old, new = links['match']
                    sections[-1] = sections[-1].replace(old, new, 1)
                previous_figure = links.get('next')
                if page.source_view:
                    views[number] = page.source_view
                sections.append(f'<section id="page-{number}" data-source-page="{number}" data-text-method="{method}">'
                                + ''.join(blocks) + '</section>')
                stats[method + '_pages'] += 1
                stats['pages'] += 1
                if batches is not None:
                    batches.finish_page(number, sections)
        if check_cancel:
            check_cancel()
    if batches is not None:
        batches.flush(sections)
    if detailed:
        sections = _stitch(sections, debug_tables, stats['warnings'])
    if views:
        sections = [_source_coordinates(section, views) for section in sections]
        stats['orientation_corrected_pages'] = sum(bool(view['clockwise']) for view in views.values())
    if stats.get('image_original_unavailable'):
        stats['warnings'].append('Для изображений без доступного исходного потока сохранена видимая область PDF. '
                                 'Число размещений: '+str(stats['image_original_unavailable'])+'.')
    if fallback:
        stats['image_fallback_pages'] = len(fallback)
        stats['warnings'].append('Изображение сохранено для страниц, где OCR не вернул текст. Страницы '
                                 + ', '.join(map(str, fallback))+'.')
    head = '<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body>'
    return head + ''.join(sections) + '</body></html>', stats
