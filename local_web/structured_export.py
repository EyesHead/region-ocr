"""Document serializers: semantic tables, separate notes and native JSON.

These functions consume the published document, after source-owned repairs and
asset extraction. They never infer missing values or repair a particular source.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString

if __package__:
    from .table_star_notes import referenced_star_footer
else:
    from table_star_notes import referenced_star_footer


NOTE_WORD = r'(?:п\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]|n\s*o\s*t\s*e(?:\s*s)?)'
NOTE_LABEL = re.compile(r'^' + NOTE_WORD + r'(?:\s*\d+)?\b', re.I)
NOTE_ANYWHERE = re.compile(r'\b' + NOTE_WORD + r'\b', re.I)
BLOCK_TAGS = {'p', 'div', 'section', 'article', 'main', 'header', 'footer', 'aside', 'body'}
MATH_CONTAINERS = {'math', 'mrow', 'mstyle', 'semantics', 'mpadded', 'merror'}
LATEX_CHARS = {'α': r'\alpha', 'β': r'\beta', 'γ': r'\gamma', 'δ': r'\delta', 'ε': r'\varepsilon',
               'ζ': r'\zeta', 'η': r'\eta', 'θ': r'\theta', 'ϑ': r'\vartheta', 'ι': r'\iota',
               'κ': r'\kappa', 'λ': r'\lambda', 'μ': r'\mu', 'ν': r'\nu', 'ξ': r'\xi',
               'π': r'\pi', 'ρ': r'\rho', 'σ': r'\sigma', 'ς': r'\varsigma', 'τ': r'\tau',
               'υ': r'\upsilon', 'φ': r'\varphi', 'ϕ': r'\phi', 'χ': r'\chi', 'ψ': r'\psi', 'ω': r'\omega',
               'Γ': r'\Gamma', 'Δ': r'\Delta', 'Θ': r'\Theta', 'Λ': r'\Lambda', 'Ξ': r'\Xi',
               'Π': r'\Pi', 'Σ': r'\Sigma', 'Υ': r'\Upsilon', 'Φ': r'\Phi', 'Ψ': r'\Psi', 'Ω': r'\Omega',
               '−': '-', '×': r'\times', '·': r'\cdot', '⋅': r'\cdot', '÷': r'\div',
               '≤': r'\leq', '≥': r'\geq', '≠': r'\neq', '≈': r'\approx', '≡': r'\equiv',
               '±': r'\pm', '∓': r'\mp', '∞': r'\infty', '∑': r'\sum', '∏': r'\prod',
               '∫': r'\int', '∂': r'\partial', '∇': r'\nabla', '∈': r'\in', '∉': r'\notin',
               '∪': r'\cup', '∩': r'\cap', '→': r'\to', '←': r'\leftarrow', '↔': r'\leftrightarrow',
               '∝': r'\propto', '∅': r'\varnothing', '∧': r'\land', '∨': r'\lor',
               '…': r'\ldots', '⋯': r'\cdots', '°': r'{}^\circ', '′': r'\prime', '″': r'\prime\prime',
               '{': r'\{', '}': r'\}', '\\': r'\backslash'}


class UnsupportedMath(ValueError):
    pass


def _tex_escape(text):
    return ''.join({'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '$': r'\$',
                    '%': r'\%', '&': r'\&', '#': r'\#', '_': r'\_', '^': r'\textasciicircum{}',
                    '~': r'\textasciitilde{}'}.get(char, char) for char in text)


def _math_atom(text):
    # A trailing space terminates commands before the next Latin identifier.
    return ''.join(LATEX_CHARS[char] + (' ' if LATEX_CHARS[char][-1:].isalpha() else '')
                   if char in LATEX_CHARS else '\\' + char if char in '%&#_$' else char for char in text)


def math_to_latex(node):
    """Prefer the engine's original TeX; otherwise serialize known MathML."""
    for attr in ('data-latex', 'data-tex', 'latex'):
        if node.get(attr):
            return _without_math_delimiters(str(node[attr]).strip())
    annotation = node.find('annotation', encoding=re.compile(r'(?:tex|latex)', re.I))
    if annotation and annotation.get_text().strip():
        return _without_math_delimiters(annotation.get_text().strip())

    def convert(item):
        if isinstance(item, Comment):
            return ''
        if isinstance(item, NavigableString):
            return _math_atom(str(item).strip())
        tag = item.name.split(':')[-1]
        children = [child for child in item.children
                    if not isinstance(child, Comment) and (getattr(child, 'name', None) or str(child).strip())]
        children = [child for child in children if getattr(child, 'name', None) not in {'annotation', 'annotation-xml'}]
        parts = [convert(child) for child in children]
        if tag in MATH_CONTAINERS:
            return ''.join(parts)
        if tag in {'mi', 'mn', 'mo'}:
            value = _math_atom(item.get_text())
            if tag == 'mi' and item.get('mathvariant') == 'normal' and len(item.get_text()) > 1:
                return r'\mathrm{' + value + '}'
            if item.get('mathvariant') in {'bold', 'bold-italic'}:
                return r'\boldsymbol{' + value + '}'
            return value
        if tag in {'mtext', 'ms'}:
            return r'\text{' + _tex_escape(item.get_text()) + '}'
        if tag == 'mspace':
            return r'\,'
        arity = {'mfrac': 2, 'msub': 2, 'msup': 2, 'msubsup': 3, 'mroot': 2,
                 'mover': 2, 'munder': 2, 'munderover': 3}
        if tag in arity and len(parts) != arity[tag]:
            raise UnsupportedMath('неверное число элементов ' + tag)
        if tag == 'mfrac':
            return r'\frac{' + parts[0] + '}{' + parts[1] + '}'
        if tag == 'msub':
            return '{' + parts[0] + '}_{' + parts[1] + '}'
        if tag == 'msup':
            return '{' + parts[0] + '}^{' + parts[1] + '}'
        if tag == 'msubsup':
            return '{' + parts[0] + '}_{' + parts[1] + '}^{' + parts[2] + '}'
        if tag == 'msqrt':
            return r'\sqrt{' + ''.join(parts) + '}'
        if tag == 'mroot':
            return r'\sqrt[' + parts[1] + ']{' + parts[0] + '}'
        if tag == 'mover':
            if children[1].get_text() in {'¯', '‾', '―', 'ˉ'}:
                return r'\overline{' + parts[0] + '}'
            if children[1].get_text() in {'^', 'ˆ'}:
                return r'\widehat{' + parts[0] + '}'
            return r'\overset{' + parts[1] + '}{' + parts[0] + '}'
        if tag == 'munder':
            return r'\underset{' + parts[1] + '}{' + parts[0] + '}'
        if tag == 'munderover':
            return '{' + parts[0] + '}_{' + parts[1] + '}^{' + parts[2] + '}'
        if tag == 'mfenced':
            opening, closing = item.get('open', '('), item.get('close', ')')
            separators = str(item.get('separators', ',')) or ','
            middle = ''.join(part + (separators[min(index, len(separators)-1)] if index < len(parts)-1 else '')
                             for index, part in enumerate(parts))
            left = '\\{' if opening == '{' else opening or '.'
            right = '\\}' if closing == '}' else closing or '.'
            return r'\left' + left + middle + r'\right' + right
        if tag == 'mtable':
            return r'\begin{matrix}' + r' \\ '.join(parts) + r'\end{matrix}'
        if tag in {'mtr', 'mlabeledtr'}:
            return ' & '.join(parts)
        if tag == 'mtd':
            return ''.join(parts)
        raise UnsupportedMath('неподдерживаемый элемент ' + tag)
    return convert(node).strip()


def _without_math_delimiters(value):
    for opening, closing in (('$$', '$$'), ('\\[', '\\]'), ('\\(', '\\)'), ('$', '$')):
        if value.startswith(opening) and value.endswith(closing) and len(value) > len(opening) + len(closing):
            return value[len(opening):-len(closing)].strip()
    return value


def _formula(node):
    result = {'type': 'formula', 'display': node.get('display') == 'block'}
    try:
        result['latex'] = math_to_latex(node)
    except UnsupportedMath as error:
        result.update(representation='mathml_tree', tree=_math_tree(node), warning=str(error))
    return result


def _math_tree(node):
    if isinstance(node, NavigableString):
        return {'text': str(node)}
    return {'element': node.name, 'attributes': {str(k): str(v) for k, v in node.attrs.items()},
            'children': [_math_tree(child) for child in node.children if not isinstance(child, Comment)]}


def _rows(table):
    return [row for row in table.find_all('tr') if row.find_parent('table') is table]


def _span(cell, name, default=1):
    try:
        value = int(cell.get(name, default))
    except (ValueError, TypeError):
        return default
    return max(0 if name == 'rowspan' else 1, value)


def _grid(table):
    rows, entries, occupied, width = _rows(table), [], set(), 0
    for row_index, row in enumerate(rows):
        group = row.parent
        group_rows = [candidate for candidate in rows if candidate.parent is group]
        remaining = len(group_rows) - group_rows.index(row)
        column = 0
        for cell in row.find_all(['td', 'th'], recursive=False):
            while (row_index, column) in occupied:
                column += 1
            colspan = _span(cell, 'colspan')
            raw_rowspan = _span(cell, 'rowspan')
            rowspan = remaining if raw_rowspan == 0 else min(max(1, raw_rowspan), remaining)
            if colspan * rowspan > 1_000_000 or column + colspan > 100_000:
                raise ValueError('Слишком большая сетка таблицы')
            coordinates = {(r, c) for r in range(row_index, row_index + rowspan) for c in range(column, column + colspan)}
            if coordinates & occupied:
                raise ValueError('Пересекающиеся объединения ячеек таблицы')
            occupied.update(coordinates)
            entries.append({'node': cell, 'row': row_index, 'col': column,
                            'rowspan': rowspan, 'colspan': colspan})
            column += colspan
            width = max(width, column)
    return rows, entries, width


def strip_review_annotations(soup):
    """Drop converter review notices and comparison crops, preserving source notes."""
    notices = list(soup.select('aside[data-ocr-review], aside[data-conversion-status="REVIEW"]'))
    for notice in notices:
        notice.decompose()
    for table in soup.select('table[data-ocr-review]'):
        del table['data-ocr-review']
    return len(notices)


def table_is_complex(table):
    """Only effective cell unions make a table complex; formulas do not."""
    return any(item['rowspan'] > 1 or item['colspan'] > 1 for item in _grid(table)[1])


def normalize_table_notes(soup):
    """Move proved full-width trailing notes outside the table before export."""
    warnings = []
    for table in reversed(list(soup.find_all('table'))):
        moved = []
        while True:
            rows, entries, width = _grid(table)
            if len(rows) < 2:
                break
            last = len(rows) - 1
            last_entries = [entry for entry in entries if entry['row'] == last]
            if len(last_entries) != 1:
                break
            item = last_entries[0]
            cell = item['node']
            if item['col'] != 0 or item['colspan'] != width or item['rowspan'] != 1:
                break
            if any(entry['row'] < last < entry['row'] + entry['rowspan'] for entry in entries):
                break
            text = cell.get_text(' ', strip=True)
            explicit = any(node.has_attr(attr) for node in (cell, rows[-1])
                           for attr in ('data-table-note', 'data-primary-table-notes')) or cell.get('role') == 'note'
            label = NOTE_LABEL.match(text)
            in_footer = rows[-1].find_parent('tfoot') is not None
            first = next((child for child in cell.children if getattr(child, 'name', None) or str(child).strip()), None)
            footnote_marker = getattr(first, 'name', None) == 'sup' and bool(re.fullmatch(r'(?:\d+[)\].]?|[*†‡]+)', first.get_text(strip=True)))
            footnote_preamble = bool(re.match(r'^\s*(?:<\s*[*†‡]+\s*>|[*†‡]+)(?:\s|[)\]])', text))
            numbered_preamble = bool(re.match(r'^\s*(?:<\s*[1-9]\d*\s*>|[1-9]\d*\))\s', text))
            if not (explicit or label or (in_footer and (NOTE_ANYWHERE.search(text) or footnote_marker))
                    or (footnote_preamble and (NOTE_ANYWHERE.search(text) or referenced_star_footer(table, cell)))
                    or (numbered_preamble and referenced_star_footer(table, cell))):
                break
            # The alleged note cannot be a container for a subordinate data table.
            if cell.find('table'):
                break
            wrapper = soup.new_tag('div', attrs={'class': 'table-notes', 'role': 'note', 'data-table-note': 'true'})
            if cell.find(['p', 'ul', 'ol', 'div'], recursive=False):
                for child in list(cell.contents):
                    wrapper.append(child.extract())
            else:
                paragraph = soup.new_tag('p')
                for child in list(cell.contents):
                    paragraph.append(child.extract())
                wrapper.append(paragraph)
            moved.insert(0, wrapper)
            parent = rows[-1].parent
            rows[-1].decompose()
            if parent.name in {'thead', 'tbody', 'tfoot'} and not parent.find('tr'):
                parent.decompose()
        anchor = table
        for note in moved:
            anchor.insert_after(note)
            anchor = note
    for math in soup.find_all('math'):
        try:
            math_to_latex(math)
        except UnsupportedMath as error:
            warning = 'Экспорт LaTeX: ' + str(error) + '; исходная формула доступна в HTML-предпросмотре.'
            if warning not in warnings:
                warnings.append(warning)
    return warnings


def _latex_markdown(node):
    try:
        latex = math_to_latex(node)
    except UnsupportedMath as error:
        return '[Формула требует проверки: ' + str(error) + ']'
    return ('$$\n' + latex + '\n$$') if node.get('display') == 'block' else '$' + latex + '$'


def _list_marker(number, kind):
    if kind in {'a', 'A'} and number > 0:
        label, value = '', number
        while value:
            value, digit = divmod(value - 1, 26)
            label = chr(ord('a') + digit) + label
        return label.upper() if kind == 'A' else label
    if kind in {'i', 'I'} and 0 < number < 4000:
        label, value = '', number
        for amount, digits in [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),
                               (90,'XC'),(50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]:
            while value >= amount:
                label += digits
                value -= amount
        return label.lower() if kind == 'i' else label
    return str(number)


def _list_entries(node):
    items = node.find_all('li', recursive=False)
    ordered = node.name == 'ol'
    step = -1 if ordered and node.has_attr('reversed') else 1
    try:
        counter = int(node.get('start', len(items) if step < 0 else 1))
    except (ValueError, TypeError):
        counter = len(items) if step < 0 else 1
    for item in items:
        if ordered and item.has_attr('value'):
            try:
                counter = int(item['value'])
            except (ValueError, TypeError):
                pass
        yield item, counter, _list_marker(counter, node.get('type')) + '.' if ordered else '-'
        counter += step


def _list_json(node, render_item):
    entries = list(_list_entries(node))
    result = {'type': 'list', 'ordered': node.name == 'ol',
              'items': [render_item(item) for item, _, _ in entries]}
    if node.name == 'ol':
        result['item_values'] = [number for _, number, _ in entries]
        if entries:
            result['start'] = entries[0][1]
        if node.has_attr('reversed'):
            result['reversed'] = True
        if node.get('type'):
            result['marker_type'] = str(node['type'])
    return result


def _plain(node, *, image_paths=False):
    if isinstance(node, Comment):
        return ''
    if isinstance(node, NavigableString):
        return str(node)
    if node.name == 'math':
        try:
            return math_to_latex(node)
        except UnsupportedMath:
            return '[Формула требует проверки]'
    if node.name == 'img':
        alt, src = node.get('alt', ''), node.get('src', '')
        if image_paths and src:
            return alt + ' (' + src + ')' if alt and alt != src else src
        return alt or src
    if node.name == 'br':
        return '\n'
    if node.name in {'ol', 'ul'}:
        return '\n'.join((marker + ' ' if node.name == 'ol' else '') + ''.join(_plain(child, image_paths=image_paths)
                           for child in item.children).strip() for item, _, marker in _list_entries(node))
    return ''.join(_plain(child, image_paths=image_paths) for child in node.children)


def _escape_markdown(text):
    for char in ('\\', '`', '*', '_', '[', ']'):
        text = text.replace(char, '\\' + char)
    return text


def _inline_md(node):
    if isinstance(node, Comment):
        return ''
    if isinstance(node, NavigableString):
        return _escape_markdown(str(node))
    if node.name == 'math':
        return _latex_markdown(node)
    if node.name in {'span', 'div'} and node.get('data-formula') and not node.find('math'):
        tex = node.get('data-latex') or node.get_text().strip()
        return '$' + tex + '$'
    if node.name == 'img':
        alt = _escape_markdown(node.get('alt', ''))
        path = str(node.get('src', '')).replace('>', '%3E').replace('<', '%3C')
        return '![' + alt + '](<' + path + '>)' if path else alt
    if node.name == 'br':
        return '<br>'
    if node.name in {'ol', 'ul'}:
        return '<br>'.join(marker + ' ' + ''.join(_inline_md(child) for child in item.children).strip()
                          for item, _, marker in _list_entries(node))
    text = ''.join(_inline_md(child) for child in node.children)
    if node.name in {'strong', 'b'}:
        return '**' + text + '**'
    if node.name in {'em', 'i'}:
        return '*' + text + '*'
    if node.name in {'sup', 'sub'}:
        # Footnote markers and ordinary text indices are distinct from formulas.
        return '<' + node.name + '>' + text + '</' + node.name + '>'
    if node.name in {'s', 'del'}:
        return '~~' + text + '~~'
    if node.name == 'code':
        fence = '`' * (max((len(value) for value in re.findall(r'`+', node.get_text())), default=0) + 1)
        return fence + ' ' + node.get_text() + ' ' + fence
    if node.name == 'a' and node.get('href'):
        path = str(node['href']).replace('>', '%3E').replace('<', '%3C').replace('\n', '%0A').replace('\r', '%0D')
        return '[' + text + '](<' + path + '>)'
    if node.name == 'table':
        return _table_md(node)
    if node.name in BLOCK_TAGS:
        return text + '<br>'
    return text


def _table_md(table):
    rows, entries, width = _grid(table)
    caption = table.find('caption', recursive=False)
    prefix = _inline_md(caption).strip() + '\n\n' if caption else ''
    # Nested tables have their own layout; a pipe cell cannot represent it.
    complex_table = table_is_complex(table) or any(entry['node'].find('table') for entry in entries)
    if complex_table:
        clone = BeautifulSoup(str(table), 'html.parser').find('table')
        for math in list(clone.find_all('math')):
            math.replace_with(_latex_markdown(math))
        for formula in clone.find_all(['span', 'div'], attrs={'data-formula': True}):
            if not formula.find('math'):
                formula.string = '$' + (formula.get('data-latex') or formula.get_text().strip()) + '$'
        # Caption remains part of the HTML table, not duplicated before it.
        return str(clone)
    if not rows or not width:
        return prefix
    matrix = [[''] * width for _ in rows]
    for entry in entries:
        value = _inline_md(entry['node']).strip()
        value = re.sub(r'\s*\n\s*', '<br>', value).replace('|', '\\|')
        value = re.sub(r'(?:<br>)+$', '', value)
        matrix[entry['row']][entry['col']] = value
    # GFM always has a header separator. An empty header preserves a source table
    # whose first row consists entirely of data cells, rather than inventing roles.
    has_header = any(entry['row'] == 0 and entry['node'].name == 'th' for entry in entries)
    header = matrix.pop(0) if has_header else [''] * width
    line = lambda values: '| ' + ' | '.join(values) + ' |'
    return prefix + '\n'.join([line(header), line(['---'] * width), *(line(row) for row in matrix)])


def _markdown_blocks(container, indent=''):
    output = []
    for node in container.children:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if node.strip():
                output.append(_escape_markdown(str(node).strip()))
            continue
        if node.name in {'style', 'title', 'meta', 'head', 'script'}:
            continue
        if node.name == 'table':
            output.append(_table_md(node))
        elif re.fullmatch('h[1-6]', node.name):
            output.append('#' * int(node.name[1]) + ' ' + _inline_md(node).strip())
        elif node.name in {'ul', 'ol'}:
            lines = []
            for item, number, marker in _list_entries(node):
                nested = []
                inline = []
                for child in item.children:
                    if getattr(child, 'name', None) in {'ul', 'ol'}:
                        nested.append(_markdown_blocks(BeautifulSoup(str(child), 'html.parser'), indent + '    '))
                    else:
                        inline.append(_inline_md(child))
                lines.append(indent + marker + ' ' + ''.join(inline).strip())
                lines.extend(nested)
            output.append('\n'.join(lines))
        elif node.name == 'pre':
            text = node.get_text()
            fence = '`' * max(3, max((len(part)+1 for part in re.findall(r'`+', text)), default=3))
            output.append(fence + '\n' + text + '\n' + fence)
        elif node.name == 'blockquote':
            output.append('\n'.join('> ' + line for line in _markdown_blocks(node).splitlines()))
        elif node.name in BLOCK_TAGS - {'p'} or node.name in {'figure', 'figcaption', 'li'}:
            output.append(_markdown_blocks(node, indent))
        elif node.name == 'hr':
            output.append('---')
        elif node.name == 'p':
            output.append(_inline_md(node).removesuffix('<br>').strip())
        else:
            output.append(_inline_md(node).strip())
    return '\n\n'.join(item for item in output if item).strip('\n')


def to_markdown(soup):
    return _markdown_blocks(soup.body or soup) + '\n'


def _rich(node):
    if isinstance(node, Comment):
        return None
    if isinstance(node, NavigableString):
        return {'type': 'text', 'value': str(node)} if str(node) else None
    if node.name == 'math':
        return _formula(node)
    if node.get('data-formula') and not node.find('math'):
        return {'type': 'formula', 'latex': node.get('data-latex') or node.get_text().strip(),
                'display': node.get('data-formula') == 'display'}
    if node.name == 'img':
        return {'type': 'image', 'src': node.get('src', ''), 'alt': node.get('alt', '')}
    if node.name == 'br':
        return {'type': 'break'}
    if node.name == 'table':
        return _table_json(node)
    if node.name in {'ol', 'ul'}:
        return _list_json(node, lambda item: [part for child in item.children
                          if (part := _rich(child)) is not None])
    children = [_rich(child) for child in node.children]
    children = [child for child in children if child is not None]
    types = {'strong': 'strong', 'b': 'strong', 'em': 'emphasis', 'i': 'emphasis',
             'sup': 'superscript', 'sub': 'subscript', 'code': 'code', 'a': 'link'}
    result = {'type': types.get(node.name, 'group'), 'children': children}
    if node.name == 'a' and node.get('href'):
        result['href'] = node['href']
    return result


def _table_json(table):
    rows, entries, width = _grid(table)
    root = table
    while root.parent is not None:
        root = root.parent
    table_index = next((index for index, item in enumerate(root.find_all('table'), 1) if item is table), 1)
    ident = table.get('id') or 'table-' + str(table_index)
    result = {'type': 'table', 'id': ident, 'complex': table_is_complex(table),
              'meta': {'rows': len(rows), 'cols': width}, 'cells': []}
    caption = table.find('caption', recursive=False)
    if caption:
        result['caption'] = _plain(caption).strip()
    headers = []
    for entry in entries:
        cell = entry['node']
        if cell.name == 'th':
            cell_id = cell.get('id') or f"{ident}-r{entry['row']}c{entry['col']}"
            headers.append((entry, cell_id))
    for entry in entries:
        cell = entry['node']
        text = _plain(cell).strip()
        rich = [_rich(child) for child in cell.children]
        rich = [child for child in rich if child is not None and not (child['type'] == 'text' and not child['value'].strip())]
        if not text and not cell.find(['img', 'math', 'table']) and cell.name != 'th' and entry['rowspan'] == entry['colspan'] == 1 and not cell.get('data-spreadsheet-formula'):
            continue
        item = {'row': entry['row'], 'col': entry['col'], 'value': text}
        source_type, raw_value = cell.get('data-cell-type'), cell.get('data-raw-value')
        if raw_value is not None:
            if source_type == 'b' and raw_value in {'0', '1'}:
                item['value'] = raw_value == '1'
            elif source_type == 'n':
                try:
                    numeric = Decimal(raw_value)
                    if numeric.is_finite():
                        if numeric == numeric.to_integral_value():
                            item['value'] = int(numeric)
                        elif Decimal(str(float(numeric))) == numeric:
                            item['value'] = float(numeric)
                except (InvalidOperation, ValueError, OverflowError):
                    pass
            if item['value'] != text:
                item['display_value'] = text
        if cell.name == 'th':
            item.update(header=True, id=next(ident for header, ident in headers if header is entry))
        for span in ('rowspan', 'colspan'):
            if entry[span] > 1:
                item[span] = entry[span]
        if rich and (len(rich) != 1 or rich[0]['type'] != 'text'):
            item['content'] = rich
        if cell.find(['strong', 'b']) or re.search(r'font-weight\s*:\s*(?:bold|[6-9]00)', cell.get('style', ''), re.I):
            item['bold'] = True
        if cell.find(['em', 'i']):
            item['italic'] = True
        for attr, field in (('data-cell', 'source_cell'), ('data-cell-type', 'source_type'),
                            ('data-raw-value', 'raw_value'), ('data-spreadsheet-formula', 'spreadsheet_formula')):
            if cell.get(attr) is not None:
                item[field] = str(cell[attr])
        if cell.get('headers'):
            item['header_ids'] = list(cell['headers']) if isinstance(cell['headers'], list) else cell['headers'].split()
        else:
            matched = []
            for header, header_id in headers:
                header_node = header['node']
                if header is entry:
                    continue
                column_match = (header['row'] < entry['row'] and header['col'] < entry['col'] + entry['colspan']
                                and entry['col'] < header['col'] + header['colspan'])
                row_match = (header['col'] < entry['col'] and header['row'] <= entry['row'] < header['row'] + header['rowspan'])
                scope = header_node.get('scope')
                if (column_match and (scope in {'col', 'colgroup'} or header_node.find_parent('thead') is not None)) or (row_match and scope in {'row', 'rowgroup'}):
                    matched.append(header_id)
            if matched:
                item['header_ids'] = matched
        result['cells'].append(item)
    for attr, field in (('data-sheet', 'source_sheet'), ('data-start-row', 'source_start_row'), ('data-start-column', 'source_start_column')):
        if table.get(attr) is not None:
            result[field] = str(table[attr])
    return result


def _json_blocks(container):
    output = []
    for node in container.children:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if node.strip():
                output.append({'type': 'paragraph', 'value': str(node).strip()})
            continue
        if node.name in {'style', 'title', 'head', 'meta', 'script'}:
            continue
        if node.name == 'table':
            output.append(_table_json(node))
        elif node.name == 'math':
            output.append(_formula(node))
        elif node.name == 'img':
            output.append(_rich(node))
        elif node.name in {'ul', 'ol'}:
            output.append(_list_json(node, _json_blocks))
        elif node.name in {'figure', 'blockquote'} or node.get('role') == 'note' or node.has_attr('data-table-note'):
            kind = 'note' if node.get('role') == 'note' or node.has_attr('data-table-note') else 'figure' if node.name == 'figure' else 'quote'
            output.append({'type': kind, 'children': _json_blocks(node)})
        elif node.name in BLOCK_TAGS - {'p'}:
            output.extend(_json_blocks(node))
        else:
            kind = 'heading' if re.fullmatch('h[1-6]', node.name) else 'code' if node.name == 'pre' else 'caption' if node.name == 'figcaption' else 'paragraph'
            item = {'type': kind, 'value': _plain(node).strip()}
            if kind == 'heading':
                item['level'] = int(node.name[1])
            rich = [_rich(child) for child in node.children]
            rich = [child for child in rich if child is not None]
            if rich and (len(rich) != 1 or rich[0]['type'] != 'text'):
                item['content'] = rich
            output.append(item)
    return output


def to_json_document(soup, source: Path, stats: dict):
    source = Path(source)
    return {'schema_version': 2, 'source': {'name': source.name, 'format': source.suffix.lower().lstrip('.')},
            'title': soup.title.get_text(strip=True) if soup.title else source.stem,
            'blocks': _json_blocks(soup.body or soup), 'stats': dict(stats)}


def _plain_blocks(container):
    output = []
    for node in container.children:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if node.strip():
                output.append(str(node).strip())
            continue
        if node.name in {'head', 'style', 'title', 'meta', 'script'}:
            continue
        if node.name == 'table':
            rows, entries, width = _grid(node)
            caption = node.find('caption', recursive=False)
            if caption:
                output.append(_plain(caption, image_paths=True).strip())
            matrix = [[''] * width for _ in rows]
            for entry in entries:
                matrix[entry['row']][entry['col']] = _plain(entry['node'], image_paths=True).strip()
            output.append('\n'.join('\t'.join(row) for row in matrix))
        elif node.name in {'ul', 'ol'}:
            output.append('\n'.join((marker + ' ' if node.name == 'ol' else '') + _plain_blocks(item)
                                    for item, _, marker in _list_entries(node)))
        elif node.name in BLOCK_TAGS - {'p'} or node.name in {'figure', 'blockquote'}:
            output.append(_plain_blocks(node))
        else:
            output.append(_plain(node, image_paths=True).strip())
    return '\n\n'.join(item for item in output if item)


def to_plaintext(soup):
    return _plain_blocks(soup.body or soup) + '\n'
