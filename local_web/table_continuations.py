"""Join explicitly captioned HTML table continuations without pixel geometry."""
from __future__ import annotations

from dataclasses import dataclass
import re

from bs4 import Comment, NavigableString


CAPTION = re.compile(
    r'^\s*(?:(?P<kind>продолжение|окончание)\s+)?таблиц[аы]\s+(?:№\s*)?'
    r'(?P<number>(?:[a-zа-яё]\s*\.?\s*)?\d+(?:(?:\s*\.\s*|-)\d+)*)'
    r'\s*(?:[—–−-]\s*(?P<title>\S[\s\S]*))?\s*$', re.I)
_IDREFS = ('headers', 'aria-labelledby', 'aria-describedby')
_CLOSED = 'data-table-continuation-closed'


def _text(value):
    return re.sub(r'\s+', ' ', str(value)).strip()


def _caption(node):
    match = CAPTION.fullmatch(_text(node.get_text(' ', strip=True)))
    if not match:
        return None
    return dict(kind=(match['kind'] or 'таблица').casefold(),
                number=re.sub(r'\s+', '', match['number']).casefold(),
                title=_text(match['title'] or '').casefold().rstrip(' .'),
                text=node.get_text(' ', strip=True), node=node)


def _events(node):
    """Flatten layout wrappers while retaining every real intervening block."""
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            if str(child).strip():
                yield child
            continue
        if child.name in {'head', 'style', 'script', 'meta', 'title'}:
            continue
        if child.get('data-page-furniture') == 'true':
            continue
        if child.name == 'table':
            yield child
        elif child.name in {'html', 'body', 'div', 'section', 'article', 'main'}:
            # A rich standalone caption is one event, even when it is a div.
            if not child.find('table') and _caption(child):
                yield child
            else:
                yield from _events(child)
        elif child.name == 'hr' and ('page-break' in child.get('class', [])
                                      or child.get('data-page-break') == 'true'):
            continue
        elif child.get_text(strip=True) or child.find(['img', 'math', 'svg', 'table']) or child.name in {'img', 'math', 'svg', 'hr'}:
            yield child


def _span(cell, name):
    try:
        value = int(cell.get(name, 1))
        return value if 0 < value <= 10000 else None
    except (TypeError, ValueError):
        return None


def _payload(node):
    if isinstance(node, Comment):
        return ''
    if isinstance(node, NavigableString):
        return str(node).casefold()
    if node.name == 'img':
        return '\x00img:' + str(node.get('src', '')) + ':' + _text(node.get('alt', '')).casefold() + '\x00'
    if node.name == 'math':
        def math_tree(part):
            if isinstance(part, Comment):
                return None
            if isinstance(part, NavigableString):
                return _text(part) or None
            attrs = tuple(sorted((k, str(v)) for k, v in part.attrs.items()
                                 if k not in {'id', 'class', 'style', 'display', 'xmlns'} and not k.startswith('data-')))
            return part.name, attrs, tuple(x for c in part.children if (x := math_tree(c)) is not None)
        return '\x00math:' + repr(math_tree(node)) + '\x00'
    content = ''.join(_payload(child) for child in node.children)
    if node.name in {'sub', 'sup'}:
        return '\x00' + node.name + ':' + content + '\x00'
    if node.name in {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'ul', 'ol'}:
        return ' ' + content + ' '
    return ' ' if node.name == 'br' else content


@dataclass
class Grid:
    rows: list
    cells: list
    width: int
    header_count: int
    header_cells: list
    signature: tuple


def _grid(table):
    # Nested tables keep their own layout and are not inferred from outer rows.
    if table.find('table'):
        return None
    for child in table.children:
        if isinstance(child, Comment) or isinstance(child, NavigableString) and not str(child).strip():
            continue
        if getattr(child, 'name', None) not in {'caption', 'colgroup', 'col', 'thead', 'tbody', 'tfoot', 'tr'}:
            return None
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    if not rows:
        return None
    occupied, cells, widths = {}, [], []
    for number, row in enumerate(rows):
        column = 0
        for cell in row.find_all(['td', 'th'], recursive=False):
            while column in occupied:
                column += 1
            rowspan, colspan = _span(cell, 'rowspan'), _span(cell, 'colspan')
            if not rowspan or not colspan or any(c in occupied for c in range(column, column + colspan)):
                return None
            cells.append((number, column, rowspan, colspan, cell))
            for c in range(column, column + colspan):
                occupied[c] = rowspan
            column += colspan
        width = max(occupied, default=-1) + 1
        if not width or set(occupied) != set(range(width)):
            return None
        widths.append(width)
        occupied = {c: count - 1 for c, count in occupied.items() if count > 1}
    if occupied or len(set(widths)) != 1:
        return None
    heads = table.find_all('thead', recursive=False)
    explicit = [row for head in heads for row in head.find_all('tr') if row.find_parent('table') is table]
    if explicit:
        count = len(explicit)
        if any(rows[i] is not explicit[i] for i in range(count)):
            return None
    else:
        count = 0
        for row in rows:
            direct = row.find_all(['td', 'th'], recursive=False)
            if direct and all(cell.name == 'th' for cell in direct):
                count += 1
            else:
                break
        if not count:
            # Plain HTML/Office imports can use td for their entire header.
            # Only the first closed span band is eligible; equal data rows
            # below it must never enlarge the header by common-prefix matching.
            first_cells = [entry for entry in cells if entry[0] == 0]
            if (not first_cells or (any(entry[3] > 1 for entry in first_cells)
                                    and not any(entry[2] > 1 for entry in first_cells))):
                return None  # An unmarked spanning title may hide further header levels.
            count = 1
        # A rowspan in the first header band owns the rest of that band.
        index = 0
        while index < count:
            count = max([count] + [r + rs for r, _, rs, _, _ in cells if r == index])
            index += 1
    if not count or count > len(rows):
        return None
    header_cells = [entry for entry in cells if entry[0] < count]
    if any(r + rs > count for r, _, rs, _, _ in header_cells):
        return None
    signature = tuple((r, c, rs, cs, _text(_payload(cell)))
                      for r, c, rs, cs, cell in header_cells)
    return Grid(rows, cells, widths[0], count, header_cells, signature)


def _caption_for(table, events, index):
    inside = table.find('caption', recursive=False)
    contained = _caption(inside) if inside else None
    previous = events[index - 1] if index else None
    outside = _caption(previous) if getattr(previous, 'name', None) != 'table' and getattr(previous, 'name', None) else None
    if outside and (not contained or (outside['kind'] != 'таблица' and outside['number'] == contained['number']
                                      and (not outside['title'] or not contained['title'] or outside['title'] == contained['title']))):
        return outside
    return contained


def _unique_id(soup, preferred):
    value, index = preferred, 2
    while soup.find(id=value):
        value = preferred + '-' + str(index)
        index += 1
    return value


def _remap_header_ids(soup, first, current, old_grid, new_grid):
    mapping = {}
    for old, new in zip(old_grid.header_cells, new_grid.header_cells):
        old_cell, new_cell = old[-1], new[-1]
        identifiers = [str(n['id']) for n in [new_cell, *new_cell.find_all(id=True)] if n.get('id')]
        if not identifiers:
            continue
        old_id = old_cell.get('id')
        if not old_id:
            preferred = identifiers[0]
            old_id = preferred if len(soup.find_all(id=preferred)) == 1 else _unique_id(soup, 'joined-header')
            old_cell['id'] = old_id
        for value in identifiers:
            if value != old_id:
                mapping[value] = old_id
    if current.get('id'):
        if not first.get('id'):
            first['id'] = current['id']
        elif current['id'] != first['id']:
            mapping[str(current['id'])] = str(first['id'])
    new_caption = current.find('caption', recursive=False)
    if new_caption:
        removed_ids = [str(n['id']) for n in [new_caption, *new_caption.find_all(id=True)] if n.get('id')]
        if removed_ids:
            old_caption = first.find('caption', recursive=False)
            destination = old_caption if old_caption is not None else first
            if not destination.get('id'):
                destination['id'] = _unique_id(soup, 'joined-caption')
            mapping.update({value: str(destination['id']) for value in removed_ids if value != destination['id']})
    _rewrite_refs(soup, mapping)


def _rewrite_refs(soup, mapping):
    if not mapping:
        return
    for node in soup.find_all(True):
        for attr in _IDREFS:
            if not node.get(attr):
                continue
            values = node[attr] if isinstance(node[attr], list) else str(node[attr]).split()
            node[attr] = ' '.join(mapping.get(value, value) for value in values)
        href = node.get('href')
        if isinstance(href, str) and href.startswith('#') and href[1:] in mapping:
            node['href'] = '#' + mapping[href[1:]]


def _merge(soup, first, current, old_grid, new_grid):
    _remap_header_ids(soup, first, current, old_grid, new_grid)
    # A footer of an intermediate piece precedes the later body in source order.
    for footer in first.find_all('tfoot', recursive=False):
        footer.name = 'tbody'
        footer['data-source-role'] = 'continued-footer'
    for row in new_grid.rows[:new_grid.header_count]:
        row.decompose()
    for child in list(current.children):
        name = getattr(child, 'name', None)
        if name in {'tbody', 'tfoot'} and child.find('tr'):
            first.append(child.extract())
        elif name == 'tr':
            body = first.find_all('tbody', recursive=False)
            target = body[-1] if body else soup.new_tag('tbody')
            if not body:
                first.append(target)
            target.append(child.extract())
    current.decompose()


def join_captioned_tables(soup):
    """Join consecutive numbered fragments with identical semantic header grids.

    Unlabelled tables keep their existing converter policy. Unknown prose and
    different captions are hard boundaries; a final fragment closes its chain.
    """
    events = list(_events(soup.body or soup))
    active, diagnostics = None, []
    for index, table in enumerate(events):
        if getattr(table, 'name', None) != 'table':
            continue
        caption = _caption_for(table, events, index)
        if caption and not caption['title'] and table.get('data-table-continuation-title'):
            caption = dict(caption, title=str(table['data-table-continuation-title']))
        grid = _grid(table)
        joined = False
        if active and caption and grid and caption['kind'] in {'продолжение', 'окончание'}:
            first, base, old_grid, last_index, closed = active
            gap = events[last_index + 1:index]
            labels = []
            gap_ok = True
            for node in gap:
                label = _caption(node) if getattr(node, 'name', None) else None
                if (not label or label['number'] != caption['number']
                        or label['kind'] == 'таблица'
                        or label['title'] and caption['title'] and label['title'] != caption['title']):
                    gap_ok = False
                    break
                labels.append(node)
            title_ok = not (base['title'] and caption['title'] and base['title'] != caption['title'])
            if (not closed and gap_ok and base['number'] == caption['number'] and title_ok
                    and old_grid.width == grid.width and old_grid.signature == grid.signature
                    and old_grid.header_count == grid.header_count):
                record = dict(table_number=base['number'], continuation=caption['kind'],
                              caption=caption['text'], into=first.get('data-pdf-table-ref') or first.get('id'),
                              source_ref=table.get('data-pdf-table-ref') or table.get('id'),
                              removed_header_rows=grid.header_count, appended_rows=len(grid.rows) - grid.header_count)
                label_ids = [str(n['id']) for label in labels for n in [label, *label.find_all(id=True)] if n.get('id')]
                if label_ids:
                    destination = first.find('caption', recursive=False)
                    if destination is None:
                        destination = base['node'] if base['node'].parent is not None else first
                    if not destination.get('id'):
                        destination['id'] = _unique_id(soup, 'joined-caption')
                    _rewrite_refs(soup, {value: str(destination['id']) for value in label_ids
                                         if value != destination['id']})
                _merge(soup, first, table, old_grid, grid)
                for node in labels:
                    if node.parent is not None:
                        node.decompose()
                closed = caption['kind'] == 'окончание'
                if closed:
                    first[_CLOSED] = 'true'
                if caption['title'] and not base['title']:
                    base = dict(base, title=caption['title'])
                    first['data-table-continuation-title'] = caption['title']
                active = (first, base, old_grid, index, closed)
                diagnostics.append(record)
                joined = True
        if not joined:
            active = ((table, caption, grid, index, table.get(_CLOSED) == 'true')
                      if caption and caption['kind'] == 'таблица' and grid else None)
    return diagnostics
