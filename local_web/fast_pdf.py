"""Lightweight PDF text export. Load PDFium and the raster reader only when needed."""
from collections import Counter
from html import escape
from statistics import median
import math
import re
import unicodedata


def _page_box(box, crop, rotation):
    """Convert original PDF coordinates into displayed top-left coordinates."""
    l, b, r, t = box
    x0, y0, x1, y1 = crop
    if rotation == 90:
        return b-y0, l-x0, t-y0, r-x0
    if rotation == 180:
        return x1-r, b-y0, x1-l, t-y0
    if rotation == 270:
        return y1-t, x1-r, y1-b, x1-l
    return l-x0, y1-t, r-x0, y1-b


def _page_geometry(page, width, height, image_objects=None):
    """One walk for images and small vector table candidates, including nested forms."""
    from ctypes import c_int, byref
    from pypdfium2 import PdfiumError, raw

    crop,rotation = page.get_bbox(),page.get_rotation()
    parents = {0:None}
    active_parent = {0:True}
    images,paths,rules = set(),[],set()
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE,raw.FPDF_PAGEOBJ_PATH,raw.FPDF_PAGEOBJ_FORM]):
        parent = parents[obj.level]
        visible = active_parent[obj.level]
        if visible and obj.type in (raw.FPDF_PAGEOBJ_FORM,raw.FPDF_PAGEOBJ_IMAGE):
            active = c_int()
            visible = not raw.FPDFPageObj_GetIsActive(obj.raw,byref(active)) or active.value
        if obj.type == raw.FPDF_PAGEOBJ_FORM:
            active_parent[obj.level+1] = bool(visible)
        elif not visible:
            continue
        if obj.type==raw.FPDF_PAGEOBJ_FORM:
            matrix=obj.get_matrix()
            parents[obj.level+1]=matrix.multiply(parent) if parent else matrix
            continue
        try:
            bounds=obj.get_bounds()
        except PdfiumError:
            # An unmeasurable drawing cannot prove a complete ruling.
            paths=None
            continue
        box=_page_box(parent.on_rect(*bounds) if parent else bounds,crop,rotation)
        l,t,r,b=box
        if obj.type==raw.FPDF_PAGEOBJ_IMAGE:
            l,t,r,b=max(0,l),max(0,t),min(width,r),min(height,b)
            if r>l and b>t:
                box = (l,t,r,b)
                images.add(box)
                if image_objects is not None:
                    image_objects.setdefault(box,[]).append(obj)
        else:
            if paths is not None and len(paths)<=360:
                paths.append((obj,box,parent,crop,rotation))
            if (r-l)>.18*width and b-t < .015*(r-l):
                rules.add(box)
    return list(images),paths,rules


def _page_words(page, width, height):
    """Read Unicode and bounding boxes in one pass; retain explicit word boundaries."""
    from pypdfium2 import raw

    crop, rotation = page.get_bbox(), page.get_rotation()
    text = page.get_textpage()
    words, chars, box = [], [], None
    start = end = None
    total = bad = 0
    try:
        for i in range(text.count_chars()):
            value = raw.FPDFText_GetUnicode(text, i)
            ch = chr(value) if 0 < value <= 0x10ffff else '\ufffd'
            if ch in {'\u00ad', '\u200b', '\ufeff'}:
                continue
            if ch.isspace():
                if chars:
                    words.append((*box, ''.join(chars), start, end))
                    chars, box = [], None
                continue
            kind = unicodedata.category(ch)
            total += 1
            if ch == '\ufffd' or kind in {'Co', 'Cn', 'Cs', 'Cc'}:
                bad += 1
                ch = '\ufffd'
            l, t, r, b = _page_box(text.get_charbox(i), crop, rotation)
            if not (0 <= (l+r)/2 <= width and 0 <= (t+b)/2 <= height):
                continue
            if r <= l or b <= t:
                if chars and kind.startswith('M'):
                    chars.append(ch)
                continue
            if chars:
                size = max(b-t, box[3]-box[1])
                if (r < box[0]-.4*size or l-box[2] > .9*size
                        or t >= box[3] or b <= box[1]):
                    words.append((*box, ''.join(chars), start, end))
                    chars, box = [], None
            if box is None:
                box = (l,t,r,b)
                start = i
            else:
                box = min(box[0],l), min(box[1],t), max(box[2],r), max(box[3],b)
            chars.append(ch)
            end = i
        if chars:
            words.append((*box, ''.join(chars), start, end))
    finally:
        text.close()
    return words, total, bad


def _rows(words):
    rows = []
    for word in sorted(words, key=lambda w: (w[1]+w[3], w[0])):
        y = (word[1]+word[3])/2
        em = word[3]-word[1]
        if rows and abs(y-rows[-1][0]) <= .52 * max(em,rows[-1][1]):
            rows[-1][2].append(word)
        else:
            rows.append((y,em,[word]))
    return [sorted(row,key=lambda w:w[0]) for _,_,row in rows]


def _layout(words, width, path_boxes, format_row=None):
    """Read columns in order; keep ruled tables and ambiguous short fields row-wise."""
    if format_row is None:
        format_row = lambda row: ' '.join(w[4] for w in row)
    rows = _rows(words)
    if len(rows) < 3:
        return [format_row(row) for row in rows]
    em = median(w[3]-w[1] for w in words)
    space = max(1.8*em,.024*width)
    gaps = []
    for row in rows:
        for i in range(1,len(row)):
            if row[i][0]-row[i-1][2] >= space and i >= 2 and len(row)-i >= 2:
                gaps.append((row[i-1][2],row[i][0]))
    # Staggered columns often have no shared baseline. Neighboring lines with
    # disjoint horizontal extents still measure the same gutter.
    for a,b in zip(rows,rows[1:]):
        if len(a)<2 or len(b)<2 or abs((b[0][1]+b[0][3]-a[0][1]-a[0][3])/2)>2*em:
            continue
        left,right = (a,b) if a[0][0]<=b[0][0] else (b,a)
        if right[0][0]-left[-1][2]>=space:
            gaps.append((left[-1][2],right[0][0]))
    if not gaps:
        return [format_row(row) for row in rows]
    # Each candidate is a measured gutter between word boxes. Use an interval
    # sweep so a page full of narrow fields cannot trigger a quadratic search.
    events = []
    for l,r in gaps:
        events.extend(((l,1),(r,-1)))
    events.sort()
    active = best = 0
    cut = None
    for (x,change),(end,_) in zip(events,events[1:]):
        active += change
        if x < end and active > best and .20*width < (x+end)/2 < .80*width:
            cut,best = (x+end)/2,active
    if cut is None or best < 3:
        return [format_row(row) for row in rows]
    # Horizontal PDF rules crossing the gutter identify table bands. Keep their
    # two-dimensional contents in normal row order while reading prose columns.
    rules = sorted({(t+b)/2 for l,t,r,b in path_boxes
                    if b-t < .4*em and l < cut-space and r > cut+space})
    bands = []
    for a,b in zip(rules,rules[1:]):
        if .7*em < b-a < 9*em:
            if bands and bands[-1][1] == a:
                bands[-1] = (bands[-1][0],b)
            else:
                bands.append((a,b))
    out, group = [], []
    def flush():
        if not group:
            return
        halves = [[format_row(side) for side in pair] for pair in group]
        if min(sum(bool(pair[k]) for pair in halves) for k in (0,1)) >= 3:
            out.extend(pair[k] for k in (0,1) for pair in halves if pair[k])
        else:
            out.extend(' '.join(s for s in pair if s) for pair in halves)
        group.clear()
    prev_y = None
    band_index = 0
    for row in rows:
        y = median((w[1]+w[3])/2 for w in row)
        # _rows yields nondecreasing centre heights. Each band is visited once.
        while band_index < len(bands) and bands[band_index][1] < y:
            band_index += 1
        left = [w for w in row if w[2]<=cut]
        right = [w for w in row if w[0]>=cut]
        across = (len(left)+len(right)!=len(row)
                  or (left and right and right[0][0]-left[-1][2]<space)
                  or (band_index < len(bands) and bands[band_index][0] <= y))
        if across or (prev_y is not None and y-prev_y>3*em):
            flush()
        if across:
            out.append(format_row(row))
        else:
            group.append((left,right))
        prev_y = y
    flush()
    return out


def _vector_segments(objects, em):
    """Read only short, solid PDF paths; an unexplained drawing blocks its region."""
    from ctypes import byref, c_float, c_int, c_uint
    from pypdfium2 import raw

    axes = [[],[]]
    obstacles = []
    tol = .09*em
    for obj, box, parent, crop, rotation in objects:
        l,t,r,b = box
        wide, tall = r-l,b-t
        # Uniform page backgrounds and point-sized joins cannot be independent rails.
        if max(wide,tall)<.45*em:
            continue
        fill,stroke = c_int(),c_int()
        if not raw.FPDFPath_GetDrawMode(obj.raw,byref(fill),byref(stroke)):
            obstacles.append(box);continue
        rgba = [c_uint() for _ in range(4)]
        color = raw.FPDFPageObj_GetStrokeColor if stroke.value else raw.FPDFPageObj_GetFillColor
        if not color(obj.raw,*(byref(c) for c in rgba)) or not rgba[-1].value:
            continue
        if min(c.value for c in rgba[:3])>235:
            continue
        count = raw.FPDFPath_CountSegments(obj.raw)
        active = c_int()
        if raw.FPDFPageObj_GetIsActive(obj.raw,byref(active)) and not active.value:
            continue
        clip = raw.FPDFPageObj_GetClipPath(obj.raw)
        if (count<2 or count>5 or (clip and raw.FPDFClipPath_CountPaths(clip)>0)
                or (stroke.value and raw.FPDFPageObj_GetDashCount(obj.raw)>0)):
            obstacles.append(box);continue
        matrix = obj.get_matrix()
        if parent is not None:
            matrix = matrix.multiply(parent)
        points = []
        closed = False
        for i in range(count):
            seg = raw.FPDFPath_GetPathSegment(obj.raw,i)
            kind = raw.FPDFPathSegment_GetType(seg)
            if kind != (raw.FPDF_SEGMENT_MOVETO if i==0 else raw.FPDF_SEGMENT_LINETO):
                break
            x,y = c_float(),c_float()
            if not raw.FPDFPathSegment_GetPoint(seg,byref(x),byref(y)):
                break
            px,py = matrix.on_point(x.value,y.value)
            xx,yy,_,_ = _page_box((px,py,px,py),crop,rotation)
            points.append((xx,yy))
            closed = bool(raw.FPDFPathSegment_GetClose(seg))
        if len(points)!=count:
            obstacles.append(box);continue
        if closed and points[0]!=points[-1]:
            points.append(points[0])
        if any(abs(x-u)>tol and abs(y-v)>tol for (x,y),(u,v) in zip(points,points[1:])):
            obstacles.append(box);continue
        if not stroke.value:
            if (not fill.value or not closed or len(set(points))!=4 or min(wide,tall)>.35*em
                    or max(wide,tall)<3*min(wide,tall)):
                obstacles.append(box);continue
            if wide>tall:
                axes[0].append(((t+b)/2,l,r))
            else:
                axes[1].append(((l+r)/2,t,b))
        else:
            for (x,y),(u,v) in zip(points,points[1:]):
                if abs(x-u)>.45*em:
                    axes[0].append(((y+v)/2,min(x,u),max(x,u)))
                elif abs(y-v)>.45*em:
                    axes[1].append(((x+u)/2,min(y,v),max(y,v)))
    return axes,obstacles


def _unite(segments,tol):
    groups = []
    for pos,lo,hi in sorted(segments):
        if not groups or pos-groups[-1][0]>tol:
            groups.append((pos,[]))
        groups[-1][1].append((lo,hi))
    lines = []
    for pos,spans in groups:
        united = []
        for a,b in sorted(spans):
            if united and a<=united[-1][1]+tol:
                united[-1][1] = max(b,united[-1][1])
            else:
                united.append([a,b])
        lines.extend((pos,a,b) for a,b in united)
    return lines


def _closed_grids(objects, images, words, width, height):
    from bisect import bisect_right

    if not words or not objects or not (4<=len(objects)<=360):
        return []
    em = median(w[3]-w[1] for w in words)
    tol = .14*em
    axes,obstacles = _vector_segments(objects,em)
    hs,vs = (_unite(lines,tol) for lines in axes)
    if len(hs)<3 or len(vs)<3 or len(hs)*len(vs)>24000:
        return []
    # Group the *whole* connected ruling. This rejects partial borders and
    # row/column merges instead of accepting a rectangular subset of the form.
    links = [[] for _ in range(len(hs)+len(vs))]
    for i,(y,l,r) in enumerate(hs):
        for j,(x,t,b) in enumerate(vs,len(hs)):
            if l-tol<=x<=r+tol and t-tol<=y<=b+tol:
                links[i].append(j);links[j].append(i)
    seen = set()
    grids = []
    for index in range(len(hs)):
        if index in seen:
            continue
        component = set()
        pending = [index]
        while pending:
            i = pending.pop()
            if i in component:
                continue
            component.add(i)
            pending.extend(links[i])
        seen.update(component)
        horizontal = sorted(hs[i] for i in component if i<len(hs))
        vertical = sorted(vs[i-len(hs)] for i in component if i>=len(hs))
        if len(horizontal)<3 or len(vertical)<3:
            continue
        ys = [row[0] for row in horizontal]
        xs = [col[0] for col in vertical]
        l,t,r,b = xs[0],ys[0],xs[-1],ys[-1]
        if (l<0 or t<0 or r>width or b>height
                or any(abs(a-l)>tol or abs(z-r)>tol for _,a,z in horizontal)
                or any(abs(a-t)>tol or abs(z-b)>tol for _,a,z in vertical)
                or min(v-u for axis in (xs,ys) for u,v in zip(axis,axis[1:]))<=2*tol
                or any(min(r,d)>max(l,a) and min(b,e)>max(t,c) for a,c,d,e in obstacles+images)):
            continue
        cells = [[[] for _ in xs[1:]] for _ in ys[1:]]
        for w in words:
            if w[3]<=t or w[1]>=b:
                continue
            # A side column, nearby caption crossing the border, or a word
            # spanning two cells makes the reading order or ownership ambiguous.
            col = bisect_right(xs,(w[0]+w[2])/2)-1
            row = bisect_right(ys,(w[1]+w[3])/2)-1
            if (not 0<=row<len(ys)-1 or not 0<=col<len(xs)-1 or w[0]<xs[col] or w[2]>xs[col+1]
                    or w[1]<ys[row] or w[3]>ys[row+1]):
                break
            cells[row][col].append(w)
        else:
            if (all(any(row) for row in cells) and all(any(col) for col in zip(*cells))):
                grids.append((t,b,cells))
    grids.sort(key=lambda grid:grid[0])
    return [g for i,g in enumerate(grids) if not ((i and grids[i-1][1]>g[0])
            or (i+1<len(grids) and grids[i+1][0]<g[1]))]


def _native_blocks(words, width, rules, grids, page_no, anchors=None):
    def rendered(row):
        if not anchors:
            return escape(' '.join(w[4] for w in row))
        return ' '.join(anchors.get((id(w),False),'') + escape(w[4]) + anchors.get((id(w),True),'') for w in row)

    def prose(part):
        return [f'<p data-source-page="{page_no}">{line}</p>'
                for line in _layout(part,width,rules,rendered) if line]
    if not grids:
        return prose(words)
    out = []
    lower = -math.inf
    for top,bottom,cells in grids:
        out.extend(prose([w for w in words if w[1]>=lower and w[3]<=top]))
        out.append(f'<table data-source-page="{page_no}"><tbody>')
        for row in cells:
            out.append('<tr>')
            for cell in row:
                text = ' '.join(rendered(line) for line in _rows(cell))
                out.append('<td>'+text+'</td>')
            out.append('</tr>')
        out.append('</tbody></table>')
        lower = bottom
    out.extend(prose([w for w in words if w[1]>=lower]))
    return out


def _covered_image(words, box):
    l,t,r,b = box
    bands = set()
    characters = 0
    for w in words:
        y = (w[1]+w[3])/2
        if l <= (w[0]+w[2])/2 <= r and t <= y <= b:
            characters += len(w[4])
            if any(ch.isalnum() for ch in w[4]):
                bands.add(min(4,int((y-t)/(b-t)*5)))
            if len(bands) >= 3 and characters >= 40:
                return True
    return False


def _native_ok(images, words, total, bad, width, height):
    if not words or bad/max(1,total) > .03 or not any(c.isalnum() for w in words for c in w[4]):
        return False
    area = width*height
    checks = {(l,t,r,b) for l,t,r,b in images if (r-l)*(b-t) >= .42*area}
    if sum((r-l)*(b-t) for l,t,r,b in images) >= .60*area:
        checks.add((0,0,width,height))
    # Many distinct overlapping large rasters merit one full-page OCR reading.
    return len(checks) <= 8 and all(_covered_image(words,box) for box in checks)


def _overlay_insertions(page, words, ocr_lines, block_count, height):
    """Retain visible native labels that a whole-page OCR reading omitted."""
    from ctypes import byref, c_uint
    from pypdfium2 import raw

    text = page.get_textpage()
    try:
        visible = []
        for word in words:
            if '\ufffd' in word[4]:
                continue
            painted = True
            for i in range(word[5],word[6]+1):
                obj = text.get_textobj(i)
                mode = raw.FPDFTextObj_GetTextRenderMode(obj.raw) if obj else -1
                if mode not in (0,1,2,4,5,6):
                    painted = False
                    break
                color = [c_uint() for _ in range(4)]
                getter = raw.FPDFPageObj_GetStrokeColor if mode in (1,5) else raw.FPDFPageObj_GetFillColor
                if not getter(obj.raw,*(byref(c) for c in color)) or not color[-1].value:
                    painted = False
                    break
            if painted:
                visible.append(word)
    finally:
        text.close()
    joined = ' '.join(' '.join(s.split()) for s in ocr_lines)
    seen = Counter()
    exact_lines = Counter(' '.join(s.split()) for s in ocr_lines)
    insertions = {}
    for row in _rows(visible):
        label = ' '.join(w[4] for w in row)
        seen[label] += 1
        if (exact_lines[label]>=seen[label] or (len(row)>1 and len(re.findall(
                r'(?<!\w)'+re.escape(label)+r'(?!\w)',joined))>=seen[label])):
            continue
        y = median((w[1]+w[3])/2 for w in row)
        at = max(0,min(block_count,round(y/height*block_count)))
        insertions.setdefault(at,[]).append(label)
    return insertions


def _ocr_blocks(reading,page_no, graphic_lines=()):
    """Accept plain OCR or measured blocks; never treat OCR strings as markup."""
    if isinstance(reading,str):
        items = [{'text':reading}]
    elif isinstance(reading,dict) and isinstance(reading.get('blocks'),list):
        items = reading['blocks']
    else:
        raise TypeError('Модуль OCR должен вернуть текст или список блоков страницы')
    from collections import defaultdict, deque
    positioned = defaultdict(deque)
    for index, text, before, after in graphic_lines:
        positioned[text.strip()].append((index,before,after))
    blocks,texts = [],[]
    tables = 0
    placed = []
    def render_line(text):
        entries = positioned.get(text.strip())
        if entries:
            index,before,after = entries.popleft()
            placed.append((index,len(blocks)))
            return before+escape(text)+after
        return escape(text)

    for item in items:
        if isinstance(item,dict) and set(item)=={'text'} and isinstance(item['text'],str):
            for line in item['text'].splitlines():
                if line.strip():
                    texts.append(line.strip())
                    blocks.append(f'<p data-source-page="{page_no}">{render_line(line.strip())}</p>')
        elif isinstance(item,dict) and set(item)=={'rows'}:
            rows = item['rows']
            if (not isinstance(rows,list) or len(rows)<2 or not isinstance(rows[0],list) or len(rows[0])<2
                    or not all(isinstance(row,list) and len(row)==len(rows[0]) and all(isinstance(c,str) for c in row) for row in rows)
                    or not any(c.strip() for row in rows for c in row)):
                raise ValueError('Модуль OCR вернул неполную сетку таблицы')
            cells = []
            for row in rows:
                texts.extend(c for c in row if c.strip())
                cells.append('<tr>'+''.join('<td>'+render_line(c)+'</td>' for c in row)+'</tr>')
            blocks.append(f'<table data-source-page="{page_no}"><tbody>'+''.join(cells)+'</tbody></table>')
            tables += 1
        else:
            raise ValueError('Модуль OCR вернул неизвестный тип блока')
    # A table may combine multiple OCR lines into one cell. Keep an anchored
    # source crop even when its complete OCR line is absent from the HTML.
    remaining = sorted(row for entries in positioned.values() for row in entries if row[1] or row[2])
    for index, before, after in reversed(remaining):
        dest = next((offset for n,offset in placed if n > index),len(blocks))
        blocks.insert(dest, f'<p data-source-page="{page_no}">{before}{after}</p>')
    return blocks,texts,tables


def _render(page, largest, max_scale):
    width,height = page.get_size()
    scale = min(max_scale,largest/max(width,height))
    bitmap = page.render(scale=scale,grayscale=True)
    try:
        return bitmap.to_pil().convert('L')
    finally:
        bitmap.close()


def _fallback_image(image):
    import base64
    from io import BytesIO

    with image.copy() as preview:
        preview.thumbnail((1200,1200))
        buffer = BytesIO()
        preview.save(buffer,format='PNG')
        data = base64.b64encode(buffer.getvalue()).decode('ascii')
    return '<img alt="Исходная страница PDF" src="data:image/png;base64,'+data+'">'


def convert_fast_pdf(source, pages='', *, ocr_page, on_progress=None, check_cancel=None, image_assets=None):
    """Return escaped self-contained HTML and page counts; the caller owns publication."""
    import pypdfium2 as pdf
    if __package__:
        from .fast_figures import page_graphics
    else:
        from fast_figures import page_graphics

    stats = dict(vector_tables=0,scan_tables=0,pages=0,native_pages=0,ocr_pages=0,blank_pages=0,image_fallback_pages=0,warnings=[])
    output = ['<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body>']
    fallback = []
    previous_figure = None
    previous_start = 0
    with pdf.PdfDocument(source) as document:
        if not len(document):
            raise ValueError('PDF не содержит страниц')
        first,last = 1,len(document)
        if pages:
            match = re.fullmatch(r'\s*([1-9]\d*)\s*-\s*([1-9]\d*)\s*',pages)
            if not match or not (1<=int(match[1])<=int(match[2])<=last):
                raise ValueError('Диапазон выходит за пределы PDF или указан неверно')
            first,last = map(int,match.groups())
        count = last-first+1
        for page_no in range(first,last+1):
            if check_cancel:
                check_cancel()
            if on_progress:
                on_progress(5+int(85*stats['pages']/count),f'Чтение страницы {page_no} из {len(document)}')
            links = {'previous':previous_figure}
            page = document[page_no-1]
            try:
                width,height = page.get_size()
                if not all(math.isfinite(v) and v>0 for v in (width,height)):
                    raise ValueError(f'Неизвестный размер страницы {page_no}')
                words,total,bad = _page_words(page,width,height)
                image_html = ''
                blocks = []
                page._fast_image_page_index = page_no-1
                objects = {}
                images,paths,rules = _page_geometry(page,width,height,objects) if words else ([],[],[])
                if _native_ok(images,words,total,bad,width,height):
                    grids = _closed_grids(paths,images,words,width,height)
                    stats['vector_tables'] += len(grids)
                    anchors = page_graphics(page,page_no,_rows(words),objects,image_assets,stats=stats,links=links,cancelled=check_cancel)
                    blocks = _native_blocks(words,width,rules,grids,page_no,anchors)
                    method = 'native'
                else:
                    with _render(page,480,1) as probe:
                        lo,hi = probe.getextrema()
                    if lo>=254 and hi-lo<=1:
                        method = 'blank'
                    else:
                        if check_cancel:
                            check_cancel()
                        method = 'ocr'
                        with _render(page,2600,2.5) as image:
                            reading = ocr_page(image,page_no)
                            positioned = []
                            if isinstance(reading,dict) and reading.get('words'):
                                sx,sy = width/image.width,height/image.height
                                rows, scores = [],{}
                                for l,t,r,b,txt,confidence in reading['words']:
                                    if not txt.strip() or not (r > l and b > t):
                                        continue
                                    fragment = (l*sx,t*sy,r*sx,b*sy,txt)
                                    rows.append([fragment])
                                    scores[id(fragment)] = confidence
                                anchors = page_graphics(page,page_no,rows,{},image_assets,raster=image,
                                                        scores=scores,stats=stats,links=links,cancelled=check_cancel)
                                positioned = [(i,row[0][4],anchors.get((id(row[0]),False),''),
                                               anchors.get((id(row[0]),True),'')) for i,row in enumerate(rows)]
                            blocks,texts,ntables = _ocr_blocks(reading,page_no,positioned)
                            stats['scan_tables'] += ntables
                            if not blocks:
                                image_html = _fallback_image(image)
                                fallback.append(page_no)
                            if words and bad/max(1,total)<=.03:
                                additions = _overlay_insertions(page,words,texts,len(blocks),height)
                                for pos,labels in sorted(additions.items(),reverse=True):
                                    blocks[pos:pos] = [f'<p data-source-page="{page_no}">{escape(s)}</p>' for s in labels]
                stats[method+'_pages'] += 1
                stats['pages'] += 1
                if links.get('match'):
                    old,new = links['match']
                    for index in range(previous_start,len(output)):
                        if old in output[index]:
                            output[index] = output[index].replace(old,new,1)
                            break
                previous_start = len(output)
                previous_figure = links.get('next')
                output.append(f'<section id="page-{page_no}" data-source-page="{page_no}" data-text-method="{method}">')
                output.extend(blocks)
                output.extend((image_html,'</section>'))
            finally:
                page.close()
        if check_cancel:
            check_cancel()
    if stats.get('image_original_unavailable'):
        stats['warnings'].append('Для изображений без доступного исходного потока сохранена видимая область PDF. '
                                 'Число размещений: '+str(stats['image_original_unavailable'])+'.')
    if fallback:
        stats['image_fallback_pages'] = len(fallback)
        stats['warnings'].append('OCR не вернул текст. Изображение сохранено для страниц '+', '.join(map(str,fallback))+'.')
    output.append('</body></html>')
    return ''.join(output),stats
