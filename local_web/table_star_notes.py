"""Resolve table-local star and numbered notes without losing unmatched notes."""
from __future__ import annotations

import copy
import re

from bs4 import Comment, NavigableString


_TRANSPARENT = {'div', 'section', 'article', 'main', 'body', 'aside'}
_BLOCKS = {'p', 'li', 'dt', 'dd'}
_EXCLUDED = {'math', 'code', 'pre', 'script', 'style', 'kbd', 'samp',
             'img', 'svg', 'object', 'embed', 'iframe', 'audio', 'video', 'picture'}
_CELL_BLOCKS = _BLOCKS | _TRANSPARENT | {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'blockquote'}
_NOTE = re.compile(r'^\s*(?:п\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]|notes?)\b', re.I)
_TABLE_LABEL = re.compile(r'^\s*(?:(?:продолжение|окончание)\s+)?'
                          r'(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|table)\s*'
                          r'(?:[A-ZА-ЯЁ]\s*[.\-]\s*)?\d', re.I)
_DEFINITION = re.compile(r'^\s*(?:(?P<open><)?\s*(?P<marker>\*+)'
                         r'(?(open)\s*>|[)\]]?)|<\s*(?P<wrapped_number>[1-9]\d*)\s*>'
                         r'|(?P<number>[1-9]\d*)\))\s+(?P<body>\S)', re.S)
_REFERENCE = re.compile(r'(?<!\*)<\s*(?P<wrapped>\*+)\s*>(?!\*)|'
                        r'(?<![<*])(?P<plain>\*+)(?![*>])|'
                        r'<\s*(?P<wrapped_number>[1-9]\d*)\s*>|'
                        r'(?<!\d)(?P<number>[1-9]\d*)\)(?!\d)')


def _marker(match):
    groups = match.groupdict()
    return next(groups[name] for name in ('marker', 'wrapped', 'plain', 'wrapped_number', 'number')
                if groups.get(name) is not None)


def _meaningful(node):
    return not isinstance(node, Comment) and (getattr(node, 'name', None) or str(node).strip())


def _after(node, stop=None):
    while node is not None:
        if node is stop:
            return None
        sibling = next((item for item in node.next_siblings if _meaningful(item)), None)
        if sibling is not None:
            return sibling
        node = node.parent
        if getattr(node, 'name', None) in {'body', 'html', '[document]'}:
            return None
    return None


def _note_blocks_after(table):
    """Search the table's following section, preserving all intervening prose."""
    stop = table.find_parent(['td', 'th', 'section', 'article'])
    node = _after(table, stop)
    while node is not None:
        name = getattr(node, 'name', None)
        if name in {'table', 'caption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            break
        if name and _TABLE_LABEL.match(node.get_text(' ', strip=True)):
            break
        if name in {'section', 'article'} and not (node.get('role') == 'note'
                or node.has_attr('data-table-note') or 'table-notes' in node.get('class', [])):
            break
        if name in _TRANSPARENT | {'ul', 'ol', 'dl'}:
            child = next((item for item in node.children if _meaningful(item)), None)
            node = child if child is not None else _after(node, stop)
            continue
        if name in _BLOCKS:
            if node.find(['table', 'caption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
                break
            yield node
        elif name or _TABLE_LABEL.match(str(node)):
            break
        node = _after(node, stop)


def _blocks(root):
    if root.name in _BLOCKS:
        return [root]
    result = []
    for child in root.children:
        if getattr(child, 'name', None) in _BLOCKS | _TRANSPARENT:
            result.extend(_blocks(child))
    return result or [root]


def _linear(root, cell=False):
    text, records = [], []
    position = 0

    def add(value, node, kind):
        nonlocal position
        text.append(value)
        records.append((position, position + len(value), node, kind))
        position += len(value)

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            add(str(node), node, 'text')
            return
        if node.name in _EXCLUDED or (cell and node.name == 'table'):
            add('\0', node, 'excluded')
            return
        if node.name == 'br':
            add('\n', node, 'break')
            return
        if cell and node is not root and node.name in _CELL_BLOCKS:
            add('\n', node, 'boundary')
        for child in node.children:
            walk(child)
        if cell and node is not root and node.name in _CELL_BLOCKS:
            add('\n', node, 'boundary')
    walk(root)
    return ''.join(text), records


def _definitions(root, numbered_markers=()):
    definitions = []
    for block in _blocks(root):
        text, records = _linear(block)
        if '\0' in text:
            # Text-only notes: do not discard images, formulas or code on removal.
            continue
        lines = list(re.finditer(r'[^\n]*(?:\n|$)', text))
        index = 0
        while index < len(lines):
            line = lines[index]
            match = _DEFINITION.match(line.group())
            if not match:
                index += 1
                continue
            end_index = index + 1
            numbered_list = False
            while end_index < len(lines):
                following = lines[end_index].group()
                next_definition = _DEFINITION.match(following)
                if (next_definition and next_definition.group('number') == '1'
                        and lines[end_index - 1].group().rstrip().endswith(':')):
                    numbered_list = True
                # A reset to 1) after a colon starts a list in the note body.
                # Otherwise separate plain numbered notes need proof of a ref
                # before they may interrupt a multi-line star-note body.
                if next_definition and not (next_definition.group('number') and (numbered_list
                        or match.group('marker') and _marker(next_definition) not in numbered_markers)):
                    break
                if _NOTE.match(following):
                    break
                end_index += 1
            start = line.start()
            content_start = start + match.start('body')
            end = lines[end_index].start() if end_index < len(lines) else len(text)
            content_end = end
            while content_end > content_start and text[content_end - 1].isspace():
                content_end -= 1
            if text[content_start:content_end].endswith('.') and not text[content_start:content_end].endswith('..'):
                content_end -= 1
            definitions.append(dict(marker=_marker(match), block=block, text=text, records=records,
                                    start=start, end=end, content_start=content_start, content_end=content_end))
            index = end_index
    return definitions


def _fragment(definition):
    """Copy only the definition body, retaining the source inline formatting."""
    selected = {id(node): (start, end, kind) for start, end, node, kind in definition['records']}
    lo, hi = definition['content_start'], definition['content_end']

    def clip(node):
        entry = selected.get(id(node))
        if entry is not None:
            start, end, kind = entry
            if end <= lo or start >= hi:
                return []
            if kind == 'break':
                return [NavigableString(' ')]
            if kind == 'text':
                value = str(node)[max(0, lo - start):min(end, hi) - start]
                return [NavigableString(re.sub(r'\s+', ' ', value))]
            return []
        children = [part for child in node.children for part in clip(child)]
        if not children:
            return []
        if node is definition['block'] or node.name in _TRANSPARENT | _BLOCKS:
            return children
        cloned = copy.copy(node)
        cloned.clear()
        for child in children:
            cloned.append(child)
        return [cloned]
    return clip(definition['block'])


def _references(table, definitions, excluded_cell=None):
    result = []
    for cell in table.find_all(['td', 'th']):
        if cell is excluded_cell or cell.find_parent('table') is not table:
            continue
        text, records = _linear(cell, cell=True)
        for match in _REFERENCE.finditer(text):
            marker = _marker(match)
            if definitions is not None and marker not in definitions:
                continue
            before, after = text[:match.start()], text[match.end():]
            # An infix operator (including plain-text exponentiation) is not a note.
            if match.group('plain') and (after[:1].isalnum() or (
                    before[-1:].isspace() and after[:1].isspace() and after.lstrip()[:1].isalnum())):
                continue
            touched = [(start, end, node) for start, end, node, kind in records
                       if kind == 'text' and start < match.end() and end > match.start()]
            # A parenthesized number in normal text may be a list or data. Only
            # explicit superscript markup proves the requested footnote role.
            if match.group('number') and not all(any(parent.name == 'sup'
                    and _REFERENCE.fullmatch(parent.get_text().strip()) for parent in node.parents)
                    for _, _, node in touched):
                continue
            if touched:
                result.append((marker, match.start(), match.end(), touched,
                               bool(before and not before[-1].isspace())))
    return result


def referenced_star_footer(table, cell):
    """Prove a footnote footer by a matching reference in another source cell."""
    match = _DEFINITION.match(cell.get_text(' ', strip=True))
    return bool(match and (_references(table, {_marker(match): None}, excluded_cell=cell)
                          or _math_references(table, {_marker(match): None}, excluded_cell=cell)))


def _math_references(table, definitions, excluded_cell=None):
    """A final MathML exponent N) with a matching definition is a footnote."""
    result = []
    for math in table.find_all('math'):
        cell = math.find_parent(['td', 'th'])
        if cell is None or cell is excluded_cell or cell.find_parent('table') is not table:
            continue
        if math.find_parent('math') is not None or any(parent.name in
                (_EXCLUDED - {'math'}) | {'annotation', 'annotation-xml'} for parent in math.parents):
            continue
        for power in math.find_all(['msup', 'msubsup']):
            if power.find_parent('math') is not math or any(parent.name in {'annotation', 'annotation-xml'}
                    for parent in power.parents):
                continue
            parts = list(power.find_all(recursive=False))
            if len(parts) != (3 if power.name == 'msubsup' else 2):
                continue
            match = re.fullmatch(r'([1-9]\d*)\)', parts[-1].get_text('', strip=True))
            if not match or definitions is not None and match[1] not in definitions:
                continue
            # A marker inside an expression is ambiguous. Only the trailing
            # expression can shed its note without moving later math terms.
            branch = power
            while branch is not math:
                if any(_meaningful(item) and getattr(item, 'name', None) not in {'annotation', 'annotation-xml'}
                       for item in branch.next_siblings):
                    break
                parent = branch.parent
                if parent is not math and parent.name not in {'mrow', 'semantics', 'mstyle'}:
                    break
                branch = parent
            if branch is math:
                result.append((match[1], power, math))
    return result


def _replace_math_references(references, definitions):
    for marker, power, math in references:
        parts = list(power.find_all(recursive=False))
        if power.name == 'msubsup':
            parts[-1].decompose()
            power.name = 'msub'
        else:
            power.replace_with(parts[0].extract())
        # The old source annotation describes the formula including its note.
        # Exporters must derive LaTeX from the remaining MathML expression.
        for attr in ('data-latex', 'data-tex', 'latex', 'alttext'):
            math.attrs.pop(attr, None)
        for annotation in list(math.find_all(['annotation', 'annotation-xml'])):
            annotation.decompose()
        anchor = math
        for node in [NavigableString(' ('), *_fragment(definitions[marker]), NavigableString(')')]:
            anchor.insert_after(node)
            anchor = node
        if not math.get_text(strip=True):
            math.decompose()


def _replace_references(references, definitions):
    edits, unwrap = {}, {}
    for marker, start, end, touched, needs_space in references:
        for index, (node_start, node_end, node) in enumerate(touched):
            insertion = []
            if index == 0:
                insertion = [NavigableString(' (' if needs_space else '('), *_fragment(definitions[marker]), NavigableString(')')]
            edits.setdefault(id(node), (node, []))[1].append((max(0, start - node_start), min(end, node_end) - node_start, insertion))
            for parent in node.parents:
                if parent.name in {'td', 'th', 'table'}:
                    break
                if parent.name == 'sup' and _REFERENCE.fullmatch(parent.get_text().strip()):
                    unwrap[id(parent)] = parent
    for node, operations in edits.values():
        original, cursor, pieces = str(node), 0, []
        for lo, hi, insertion in sorted(operations, key=lambda item: item[0]):
            pieces.append(NavigableString(original[cursor:lo]))
            pieces.extend(insertion)
            cursor = hi
        pieces.append(NavigableString(original[cursor:]))
        for piece in pieces:
            node.insert_before(piece)
        node.extract()
    for node in unwrap.values():
        if node.parent is not None:
            node.unwrap()


def _remove_definitions(definitions):
    edits, breaks, blocks = {}, {}, {}
    for definition in definitions:
        lo, hi = definition['start'], definition['end']
        blocks[id(definition['block'])] = definition['block']
        for start, end, node, kind in definition['records']:
            if start >= hi or end <= lo:
                continue
            if kind == 'break':
                breaks[id(node)] = node
            elif kind == 'text':
                edits.setdefault(id(node), (node, []))[1].append((max(0, lo - start), min(end, hi) - start))
    for node, ranges in edits.values():
        value = str(node)
        for lo, hi in sorted(ranges, reverse=True):
            value = value[:lo] + value[hi:]
        node.replace_with(NavigableString(value))
    for node in breaks.values():
        node.extract()
    for block in blocks.values():
        for tag in reversed(list(block.find_all(True))):
            if tag.name not in {'br', 'img', 'hr'} and (not tag.contents or (not tag.get_text(strip=True) and not tag.find(['img', 'math', 'br']))):
                tag.decompose()
        while block.parent is not None and not block.get_text(strip=True) and not block.find(['img', 'math']):
            parent = block.parent
            block.decompose()
            if parent.name not in _TRANSPARENT or parent.name in {'body', 'main', 'article', 'section'}:
                break
            block = parent


def normalize_table_star_notes(soup):
    """Inline unambiguous table-local footnotes; return number of replacements.

    Run after table joins and footer-note separation. Unreferenced definitions,
    ordinary notes and ambiguous duplicate definitions stay in the source HTML.
    """
    count = 0
    for table in list(soup.find_all('table')):
        numbered_markers = {item[0] for item in _references(table, None) if item[0].isdecimal()}
        numbered_markers.update(item[0] for item in _math_references(table, None))
        definitions = [definition for block in _note_blocks_after(table)
                       for definition in _definitions(block, numbered_markers)]
        groups = {}
        for definition in definitions:
            groups.setdefault(definition['marker'], []).append(definition)
        unique = {marker: items[0] for marker, items in groups.items() if len(items) == 1}
        references = _references(table, unique)
        math_references = _math_references(table, unique)
        if not references and not math_references:
            continue
        _replace_references(references, unique)
        _replace_math_references(math_references, unique)
        table.smooth()
        used = {item[0] for item in [*references, *math_references]}
        _remove_definitions([unique[marker] for marker in used])
        count += len(references) + len(math_references)
    return count
