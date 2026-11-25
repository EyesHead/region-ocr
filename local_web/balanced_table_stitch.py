"""Reassemble text lines and adjacent physical fragments of measured PDF tables."""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from math import isfinite
import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from local_web.pdf_line_join import (
    BREAK_MARKS, INLINE_OBJECTS, join_html_lines as join_cell_lines, line_boundary as _boundary,
)
from local_web.structured_export import _grid
from local_web.table_continuations import _events, _payload


_CAPTION = re.compile(
    r'^\s*(?:(продолжение|окончание)\s+)?таблиц[аы]\b\s*(?:№\s*)?'
    r'((?:[а-яёa-z]\s*\.?\s*)?\d+(?:[.-]\d+)*)?', re.I)
_FOLIO = re.compile(r'\s*[-–—]?\s*(?:(?:стр\.?|страница)\s*)?\d{1,5}\s*[-–—]?\s*', re.I)
_STANDARD = re.compile(
    r'(?:\d{1,4}\s+)?(?:ГОСТ|СП|СНиП|СН|СТО|СТП|РД|ISO|IEC|EN|DIN|ASTM)\s+'
    r'(?:(?:Р|ИСО(?:/МЭК)?|МЭК|ISO(?:/IEC)?|IEC|EN|DIN)\s+)*[A-ZА-ЯЁ]{0,2}\s*'
    r'\d[\d./:–—−*-]*(?:\s*[-–—−]\s*\d{2,4}\*?)?(?:\s+\d{1,4})?', re.I)
_HEADER_TERMS = re.compile(
    r'\b(?:наименовани|показател|значени|категори|характеристик|обозначени|параметр|'
    r'критери|количеств|единиц|описани|размер|материал|номер|name\b|description\b|value\b|unit\b)', re.I)


def _visible(node, line_break=' '):
    if isinstance(node, Comment):
        return ''
    if isinstance(node, NavigableString):
        return str(node)
    if node.name in INLINE_OBJECTS or node.has_attr('data-formula'):
        return '\ufffc'
    if node.name == 'br':
        return line_break
    content = ''.join(_visible(child, line_break) for child in node.children)
    return ' ' + content + ' ' if node.name in {'p', 'div', 'li'} else content


def _numbers(value):
    try:
        result = tuple(float(field) for field in value.split(','))
        return result if all(isfinite(field) for field in result) else ()
    except (ValueError, AttributeError):
        return ()


def _physical_page(node):
    if node is None:
        return None
    value = node.get('data-source-page') or node.get('data-page') or str(node.get('id', '')).removeprefix('page-')
    return int(value) if re.fullmatch(r'[1-9]\d{0,8}', str(value)) else None


def _label(node):
    if node is None or isinstance(node, Tag) and (node.name in INLINE_OBJECTS or node.find(INLINE_OBJECTS)):
        return None
    text = ' '.join(_visible(node).split())
    match = _CAPTION.match(text)
    if match and (match[2] or match[1] and not text[match.end():].strip(' .')):
        return ((match[1] or 'таблица').casefold(), re.sub(r'\s+', '', match[2] or '').casefold(), text)
    return None


def _furniture(node, top, height):
    if isinstance(node, Tag):
        if (node.name in {'header', 'footer'} or node.get('data-page-furniture') == 'true'
                or node.get('role') in {'doc-pageheader', 'doc-pagefooter'}):
            return True
        if node.name in INLINE_OBJECTS or node.find(INLINE_OBJECTS):
            return False
    text = ' '.join(_visible(node).split())
    box = _numbers(node.get('data-source-bbox')) if isinstance(node, Tag) else ()
    at_margin = len(box) == 4 and (box[3] < .15 * height if top else box[1] > .82 * height)
    if _FOLIO.fullmatch(text):
        return not box or at_margin
    return at_margin and _STANDARD.fullmatch(text) is not None


def _near_edge(piece, top):
    if not piece.box:
        return False
    y = piece.box[1 if top else 3]
    margin = piece.height * (.20 if top else .80)
    if (y <= margin if top else y >= margin):
        return True
    # A measured running header/footer can occupy part of the printable area.
    for node in (piece.edge_before if top else piece.edge_after):
        if isinstance(node, Tag) and _furniture(node, top, piece.height):
            box = _numbers(node.get('data-source-bbox'))
            if len(box) == 4:
                edge = box[3] if top else box[1]
                gap = y - edge if top else edge - y
                if (edge <= margin if top else edge >= margin) and 0 <= gap <= .04 * piece.height:
                    return True
    return False


@dataclass
class _Layout:
    rows: list
    bands: list
    header_count: int
    explicit_header: bool


def _layout(table, width):
    if table.find('table') or width < 2:
        return None
    try:
        rows, entries, columns = _grid(table)
        if not rows or columns != width:
            return None
        bands = [[] for _ in rows]
        for cell in entries:
            for name in ('rowspan', 'colspan'):
                if cell[name] < 1 or int(cell['node'].get(name, '1')) != cell[name]:
                    return None
            for row in range(cell['row'], cell['row'] + cell['rowspan']):
                bands[row].append(cell)
    except (TypeError, ValueError, OverflowError):
        return None
    for band in bands:
        band.sort(key=lambda c: c['col'])
        if sum(c['colspan'] for c in band) != width:
            return None
    count = 0
    for row, band in zip(rows, bands):
        if row.parent.name != 'thead' and not all(
                c['node'].name == 'th' and c['node'].get('scope') != 'row' for c in band):
            break
        count += 1
    explicit = bool(count)
    if not count:
        labels = [_visible(c['node']).strip() for c in bands[0]]
        if (sum(bool(label) for label in labels) >= 2 and
                (any(_HEADER_TERMS.match(label.lstrip('№. ')) for label in labels)
                 or all(c['node'].find(['b', 'strong']) for c in bands[0]))):
            count = 1
    for index, band in enumerate(bands):
        if index >= count:
            break
        count = max(count, *(c['row'] + c['rowspan'] for c in band))
    return _Layout(rows, bands, count, explicit)


@dataclass(eq=False)
class _Piece:
    table: Tag
    index: int
    page: int | None
    x: tuple
    y: tuple
    box: tuple
    height: float
    opened: bool
    open_top: bool
    edge_before: list
    edge_after: list
    labels: list
    title: tuple | None
    grid: _Layout | None
    owner: _Piece | None = None


def _read_page(source, index):
    if 'data-balanced-grid' not in source:
        return None, None, None
    soup = BeautifulSoup(source, 'html.parser')
    section = soup.find('section') or soup
    events = list(_events(section))
    furniture = section.find_all(attrs={'data-page-furniture': 'true', 'data-source-bbox': True})
    positions = [i for i, node in enumerate(events) if getattr(node, 'name', None) == 'table']
    if not positions:
        return None, None, None
    pieces = {}
    for at in {positions[0], positions[-1]}:
        table = events[at]
        if not table.has_attr('data-balanced-grid'):
            continue
        page, table_page = _physical_page(section), _physical_page(table)
        page = (page or table_page) if not page or not table_page or page == table_page else None
        x, y = (_numbers(table.get(name)) for name in ('data-grid-x', 'data-grid-y'))
        box, height = _numbers(table.get('data-source-bbox')), _numbers(table.get('data-page-height'))
        width = _numbers(table.get('data-page-width'))
        h = height[0] if len(height) == 1 else 0
        if not (len(box) == 4 and 0 <= box[1] < box[3] <= h + 1.5 and len(width) == 1
                and 0 <= box[0] < box[2] <= width[0] + 1.5):
            box = ()
        before = events[:at] if at == positions[0] else []
        after = events[at + 1:] if at == positions[-1] else []
        for node in furniture:
            bounds = _numbers(node['data-source-bbox'])
            if box and len(bounds) == 4:
                if at == positions[0] and bounds[3] <= box[1]:
                    before.append(node)
                if at == positions[-1] and bounds[1] >= box[3]:
                    after.append(node)
        labels = [(node, value) for node in before if (value := _label(node))]
        caption = table.find('caption', recursive=False)
        title = _label(caption)
        if not title:
            for node in reversed(events[max(0, at - 2):at]):
                if getattr(node, 'name', None) == 'table':
                    break
                title = _label(node)
                if title:
                    break
        if caption is not None and _visible(caption).strip() and not _label(caption):
            before = [*before, caption]
        if caption is not None and (inside := _label(caption)):
            labels.append((caption, inside))
        pieces[at] = _Piece(table, index, page, x, y, box, h, table.get('data-open-bottom') == '1',
                            table.get('data-open-top') == '1', before, after, labels, title, _layout(table, len(x)-1))
    return soup, pieces.get(positions[0]), pieces.get(positions[-1])


def _aligned(a, b):
    if len(a.x) < 3 or len(a.x) != len(b.x) or not a.box or not b.box:
        return False
    tolerance = max(4 * 72 / 200, .005 * min(a.x[-1] - a.x[0], b.x[-1] - b.x[0]))
    for piece in (a, b):
        if (any(x >= y for x, y in zip(piece.x, piece.x[1:])) or abs(piece.box[0] - piece.x[0]) > tolerance
                or abs(piece.box[2] - piece.x[-1]) > tolerance):
            return False
    return all(abs(x - y) <= tolerance for x, y in zip(a.x, b.x))


def _head_signature(grid, count):
    if count > len(grid.rows):
        return None
    signature = []
    for index in range(count):
        for c in grid.bands[index]:
            if c['row'] != index:
                continue
            if c['row'] + c['rowspan'] > count:
                return None
            cell = c['node']
            links = [(node.get('href'), node.get('alt'), node.get('data-latex'))
                     for node in cell.find_all(['a', 'img', 'math', 'span']) if
                     any(node.has_attr(attr) for attr in ('href', 'alt', 'data-latex'))]
            signature.append((index, c['col'], c['rowspan'], c['colspan'], ' '.join(_payload(cell).split()),
                              ' '.join(cell.get_text().split()), links))
    return signature


def _numbered(band):
    return len(band) > 1 and all(c['rowspan'] == c['colspan'] == 1 and
                                 _visible(c['node']).strip() == str(i) for i, c in enumerate(band, 1))


def _has_text_column(band):
    return any(re.search(r'[^\W\d_]{2}', _visible(c['node'])) for c in band)


def _header_rows(base, new, continuation_caption):
    count = base.header_count or new.header_count
    if base.explicit_header and new.explicit_header and base.header_count != new.header_count:
        count = 0
    old = _head_signature(base, count) if count else None
    repeat = count if old and old == _head_signature(new, count) else 0
    number = False
    if repeat < len(new.rows) and _numbered(new.bands[repeat]):
        # Numbered columns can replace the entire heading. In an unmarked grid,
        # distinguish the numbering from an ordinary two-column numeric record.
        numbered_in_source = base.header_count and any(_numbered(row) for row in base.bands[:base.header_count + 1])
        captioned_text = (continuation_caption and base.header_count and base.header_count < len(base.rows)
                      and repeat + 1 < len(new.rows) and _has_text_column(new.bands[repeat + 1])
                      and _has_text_column(base.bands[base.header_count]))
        number = bool(repeat or new.rows[repeat].parent.name == 'thead'
                      or all(c['node'].name == 'th' for c in new.bands[repeat])
                      or captioned_text or numbered_in_source)
    return repeat, int(number)


def _unfinished(a, b):
    if not a or not b or a.rstrip('»”"\')]').endswith(('.', '?', '!', '…', ';', ':')):
        return False
    lead = b.lstrip('«“"( ')
    return bool(lead and (lead[0].islower() or (a[-1] in BREAK_MARKS and _boundary(a, lead)[1] == '')))


def _split_row(left, right):
    if ([c['colspan'] for c in left] != [c['colspan'] for c in right] or len(left) < 2
            or not _visible(left[0]['node']).strip() or _visible(right[0]['node']).strip()):
        return False
    pairs = [(_visible(a['node']).strip(), _visible(b['node']).strip()) for a, b in zip(left[1:], right[1:])]
    evidence = [_unfinished(a, b) for a, b in pairs if a and b]
    return bool(evidence) and all(evidence) and all(a or not b for a, b in pairs)


def _provenance(piece):
    grid = piece.grid
    for index, row in enumerate(grid.rows):
        row.attrs.setdefault('data-source-page', str(piece.page))
        for c in grid.bands[index]:
            if c['row'] != index:
                continue
            cell = c['node']
            cell.attrs.setdefault('data-source-page', str(piece.page))
            if len(piece.y) == len(grid.rows) + 1:
                coords = (piece.x[c['col']], piece.y[index], piece.x[c['col'] + c['colspan']],
                          piece.y[index + c['rowspan']])
                cell.attrs.setdefault('data-source-bbox', ','.join(format(value, '.8g') for value in coords))


def _merge_cell(left, right):
    a, b = _visible(left, line_break='\ufffc').rstrip(), _visible(right, line_break='\ufffc').lstrip()
    drop, separator = _boundary(a, b)
    if a and a[-1] in BREAK_MARKS and b[:1].isalnum():
        for node in reversed(list(left.strings)):
            text = str(node).rstrip()
            value = text[:-1] if text and drop else text
            if value != node:
                node.replace_with(value)
            if text:
                break
        for node in list(right.strings):
            text = str(node).lstrip()
            if text != node:
                node.replace_with(text)
            if text:
                break
    target = left
    visible = [node for node in left.children if isinstance(node, Tag) or str(node).strip()]
    if visible and getattr(visible[-1], 'name', None) == 'p':
        target = visible[-1]
    incoming = [node for node in right.children if isinstance(node, Tag) or str(node).strip()]
    first_paragraph = incoming[0] if incoming and getattr(incoming[0], 'name', None) == 'p' else None
    if first_paragraph is not None:
        # The first printed paragraph continues the interrupted one; additional
        # paragraphs in this physical cell still start on their own lines.
        first_paragraph.name = 'span'
    if separator and a and b:
        target.append(NavigableString(separator))
    # Keep the second physical cell as a provenance wrapper, including its
    # source box, styled markup, formulas, images, and any referenced anchor.
    right.name = 'div' if right.find(['p', 'div', 'ul', 'ol', 'figure']) else 'span'
    for attr in ('rowspan', 'colspan', 'headers', 'scope'):
        right.attrs.pop(attr, None)
    if right.name == 'div':
        if first_paragraph is not None:
            for attr in ('data-source-page', 'data-source-bbox'):
                if attr in right.attrs:
                    first_paragraph.attrs.setdefault(attr, right[attr])
            target.append(first_paragraph.extract())
        left.append(right.extract())
    else:
        target.append(right.extract())


def _redirect(source, target, redirects):
    if getattr(source, 'attrs', None) and source.get('id'):
        original = str(source['id'])
        retained = target.attrs.setdefault('id', original)
        if original != retained:
            redirects[original] = str(retained)


def _merge(base, previous, current, repeat, numbers, split, redirects):
    if previous.owner is None:
        _provenance(previous)
    _provenance(current)
    table, source = base.table, current.table
    if not base.title and current.title:
        base.title = current.title
    table['data-source-pages'] = table.get('data-source-pages', str(base.page)) + ',' + str(current.page)
    if source.has_attr('data-grid-double'):
        table['data-grid-double'] = '1'
    _redirect(source, table, redirects)
    offset = repeat + numbers
    for i, row in enumerate(current.grid.rows[:offset]):
        target = base.grid.rows[i] if i < repeat else table
        _redirect(row, target, redirects)
        for c in current.grid.bands[i]:
            if c['row'] == i:
                anchor = (next(old['node'] for old in base.grid.bands[i] if old['col'] == c['col'])
                          if i < repeat else table)
                for node in (c['node'], *c['node'].find_all(id=True)):
                    _redirect(node, anchor, redirects)
        row.extract()
    for label, _ in current.labels:
        _redirect(label, table.find('caption', recursive=False) or table, redirects)
        if isinstance(label, Tag):
            for node in label.find_all(id=True):
                _redirect(node, table, redirects)
        label.extract()
    target_row = previous.grid.rows[-1]
    cross_group = None
    if split:
        row = current.grid.rows[offset]
        target_row['data-source-pages'] = target_row.get('data-source-pages', str(previous.page)) + ',' + str(current.page)
        _redirect(row, target_row, redirects)
        cross_group = row.parent
        for a, b in zip(previous.grid.bands[-1], current.grid.bands[offset]):
            dest, part = a['node'], b['node']
            span = int(dest.get('rowspan', 1)) + b['rowspan'] - 1
            if span > 1:
                dest['rowspan'] = str(span)
            _merge_cell(dest, part)
            b['node'] = dest
        row.extract()
        current.grid.rows[offset] = target_row
    target_parent = target_row.parent
    for group in table.find_all('tfoot', recursive=False):
        group.name = 'tbody'
    for child in list(source.children):
        if isinstance(child, Tag) and child.name in {'thead', 'tbody', 'tfoot'} and child.find('tr'):
            if child is cross_group:
                for row in child.find_all('tr', recursive=False):
                    target_parent.append(row.extract())
            else:
                if child.name == 'thead':
                    child.name = 'tbody'
                table.append(child.extract())
        elif isinstance(child, Tag) and child.name == 'tr':
            target_parent.append(child.extract())
    source.extract()
    current.owner = base


def _evaluate(previous, current, redirects):
    base = previous.owner or previous
    caption = next((value for _, value in current.labels if value[0] != 'таблица'), None)
    title = base.title or previous.title
    scores = dict(open_bottom=int(previous.opened), page_bottom=int(_near_edge(previous, False)),
                  page_top=int(_near_edge(current, True)), column_dividers=2 * _aligned(previous, current),
                  open_top=int(current.open_top), continuation_caption=0, repeated_header=0,
                  column_numbers=0, split_row=0)
    reasons = [name for name, good in (
        ('non_adjacent_pages', previous.page is not None and current.page == previous.page + 1),
        ('closed_bottom', scores['open_bottom']), ('above_page_bottom', scores['page_bottom']),
        ('below_page_top', scores['page_top']), ('different_columns', scores['column_dividers']),
        ('invalid_cell_grid', previous.grid and current.grid),
        ('content_after_table', all(_furniture(node, False, previous.height) for node in previous.edge_after)),
        ('content_before_table', all(_label(node) or _furniture(node, True, current.height) for node in current.edge_before)),
        ('new_table_title', all(value[0] != 'таблица' for _, value in current.labels)),
        ('different_caption_number', not any(title and title[1] and value[1] and title[1] != value[1]
                                             for _, value in current.labels)),
        ('after_final_caption', not previous.title or previous.title[0] != 'окончание')) if not good]
    repeat = numbers = 0
    split = False
    if not reasons:
        repeat, numbers = _header_rows(base.grid, current.grid, caption)
        offset = repeat + numbers
        if offset >= len(current.grid.rows):
            reasons.append('no_body_rows')
        elif current.grid.explicit_header and current.grid.header_count > offset:
            reasons.append('unmatched_header')
        elif previous.owner or previous.grid.header_count < len(previous.grid.rows):
            split = _split_row(previous.grid.bands[-1], current.grid.bands[offset])
        scores.update(continuation_caption=(3 if caption[1] else 2) if caption else 0,
                      repeated_header=3 * bool(repeat), column_numbers=3 * bool(numbers), split_row=4 * split)
    score = sum(scores.values())
    action = 'kept_separate'
    if not reasons:
        if score >= 8:
            action = 'joined'
            _merge(base, previous, current, repeat, numbers, split, redirects)
        else:
            action = 'possible_continuation'
            reasons.append('weak_continuation_evidence')
    return dict(pages=[previous.page, current.page], action=action, scores=scores, score=score, reasons=reasons,
                removed_header_rows=repeat if action == 'joined' else 0,
                removed_number_rows=numbers if action == 'joined' else 0,
                merged_row=action == 'joined' and split, caption=caption[2] if caption else None)


def _rewrite_references(soup, redirects):
    changed = []
    for node in soup.find_all(True):
        updated = False
        for attr in ('headers', 'aria-labelledby', 'aria-describedby', 'data-annotation-owner', 'href'):
            value = node.get(attr)
            if not value:
                continue
            if attr == 'href':
                if value.startswith('#') and value[1:] in redirects:
                    node[attr] = '#' + redirects[value[1:]]
                    updated = True
            else:
                ids = value if isinstance(value, list) else value.split()
                if any(ident in redirects for ident in ids):
                    node[attr] = ' '.join(redirects.get(ident, ident) for ident in ids)
                    updated = True
        if updated:
            changed.append(node)
    return changed


class _Edits(HTMLParser):
    """Splice measured elements without reserializing unrelated text, SVG or MathML."""
    def __init__(self, raw, targets):
        super().__init__(convert_charrefs=False)
        self.raw = raw
        self.targets = targets.copy()
        self.lines = [0, *(match.end() for match in re.finditer('\n', raw))]
        self.spans = []
        self.depth = 0
        self.name = ''

    def _offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, name, attrs):
        pos = self.getpos()
        target = self.targets.get(pos)
        if target and name == target[0]:
            del self.targets[pos]
        if self.depth:
            if name == self.name:
                self.depth += 1
        elif target and name == target[0]:
            self.name, self.node = target
            self.start = self._offset()
            if name in {'area', 'br', 'col', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr'}:
                self.spans.append((self.start, self.start + len(self.get_starttag_text()), self.node))
            else:
                self.depth = 1

    def handle_endtag(self, name):
        if self.depth and name == self.name:
            self.depth -= 1
            if not self.depth:
                end = self.raw.index('>', self._offset()) + 1
                self.spans.append((self.start, end, self.node))

    def render(self, soup):
        self.feed(self.raw)
        self.close()
        if self.depth or self.targets:
            return str(soup)
        parts, offset = [], 0
        for start, end, node in self.spans:
            parts.extend((self.raw[offset:start], str(node) if node is not None else ''))
            offset = end
        return ''.join([*parts, self.raw[offset:]])


def _edit(edits, index, node, replacement):
    position = (getattr(node, 'sourceline', None), getattr(node, 'sourcepos', None))
    edits.setdefault(index, {})[position] = (getattr(node, 'name', None), replacement)


def stitch_pages(sections, *, decisions=None):
    """Join eligible boundary tables and keep all physical page sections in order."""
    output = list(sections)
    if len(output) < 2:
        return output
    dirty, redirects, edits = {}, {}, {}
    previous = previous_soup = None
    for index, source in enumerate(output):
        soup, first, last = _read_page(source, index)
        if previous is not None and first is not None:
            decision = _evaluate(previous, first, redirects)
            if decisions is not None:
                decisions.append(decision)
            if decision['action'] == 'joined':
                base = first.owner
                if base.index == previous.index:
                    dirty[base.index] = previous_soup
                dirty[index] = soup
                _edit(edits, base.index, base.table, base.table)
                _edit(edits, index, first.table, None)
                for label, _ in first.labels:
                    _edit(edits, index, label, None)
        previous, previous_soup = last, soup
    if redirects:
        for i, raw in enumerate(output):
            if i not in dirty and not any(ident in raw for ident in redirects):
                continue
            soup = dirty.get(i) or BeautifulSoup(raw, 'html.parser')
            changed = _rewrite_references(soup, redirects)
            if changed:
                dirty[i] = soup
                replacements = {id(node) for _, node in edits.get(i, {}).values() if node is not None}
                for node in changed:
                    if not any(id(parent) in replacements for parent in node.parents):
                        _edit(edits, i, node, node)
    for index, soup in dirty.items():
        changes = edits.get(index)
        output[index] = _Edits(output[index], changes).render(soup) if changes else str(soup)
    return output
