"""Join a sparse text continuation only across a measured open source row."""
from hashlib import sha256


_ASSETS = ['img', 'math', 'svg', 'table', 'object', 'embed', 'canvas']


def merge_open_sparse_row_tail(left, right, left_geometry, right_geometry, *, same_grid):
    """Retain a one-cell text tail and all drawings in the other source cells.

    The caller has already proved a table continuation on adjacent pages.
    Both measured row edges must be open. Closed borders, spans, several
    populated continuation cells or a target drawing remain separate rows.
    No column number, alphabet or document-specific wording decides ownership.
    """
    if not (same_grid and left_geometry.get('open_bottom') and right_geometry.get('open_top')):
        return None
    columns = len(right_geometry.get('x_grid', [])) - 1
    from docling_pdf_table_layout import _table_rows

    last, first = _table_rows(left), right.find('tr')
    if not last or first is None or first.find_parent('thead') is not None:
        return None
    last = last[-1]
    before = last.find_all(['td', 'th'], recursive=False)
    after = first.find_all(['td', 'th'], recursive=False)
    if columns < 2 or len(before) != columns or len(after) != columns:
        return None
    try:
        if any(int(cell.get(name, 1)) != 1 for cell in before + after for name in ('colspan', 'rowspan')):
            return None
    except (TypeError, ValueError):
        return None
    if any(cell.find(_ASSETS) for cell in after):
        return None
    populated = [index for index, cell in enumerate(after) if cell.get_text(strip=True)]
    if len(populated) != 1:
        return None
    column = populated[0]
    target, tail = before[column], after[column]
    if not target.get_text(strip=True) or target.find(_ASSETS):
        return None
    proof = dict(method='single_text_cell_across_measured_open_source_row', column=column,
                 left_row_sha256=sha256(str(last).encode()).hexdigest(),
                 right_row_sha256=sha256(str(first).encode()).hexdigest())
    # Preserve source inline markup; whitespace suffices between two source
    # line bands and does not invent a new paragraph or concatenate words.
    target.append(' ')
    for child in list(tail.contents):
        target.append(child.extract())
    first.decompose()
    return proof


def _cell_matches_source(node, cell):
    """Match every source glyph, allowing only measured line-end hyphens to wrap."""
    import re

    if node.find(lambda tag: tag.name != 'br') or cell.get('source_images') or cell.get('restored_math'):
        return False
    lines = [line['text'].strip() for line in cell.get('lines', []) if line['text'].strip()]
    text = ' '.join(node.get_text(' ', strip=True).split())
    if not lines:
        return not text and not cell.get('glyphs') and node.find('br') is None
    if not text or (cell.get('positioned_source_text') or {}).get('basis') != 'positioned_pdf_text_covers_source_cell_ink':
        return False
    pieces = []
    for line in lines[:-1]:
        if line.endswith(('-', '\u00ad')):
            pieces.append(re.escape(line[:-1]) + r'[-\u00ad]?\s*')
        else:
            pieces.append(re.escape(line) + r'\s+')
    return re.fullmatch(''.join(pieces) + re.escape(lines[-1]), text) is not None


def merge_positioned_row_tail(left, right, old, new, api):
    """Reconnect a physical row only when each populated column has a measured text continuation."""
    import re

    if not (old and new and old.get('open_bottom') and new.get('open_top')
            and old.get('positioned_source_grid_verified') and new.get('positioned_source_grid_verified')):
        return None
    columns = len(new['x_grid']) - 1
    boundary = len(old['y_grid']) - 1
    upper = {c['column']: c for c in old['cells'] if c['row'] + c['row_span'] == boundary}
    lower = {c['column']: c for c in new['cells'] if c['row'] == 0}
    if (len(upper) != columns or len(lower) != columns or set(upper) != set(range(columns))
            or set(upper) != set(lower) or any(c['column_span'] != 1 for c in upper.values())
            or any(c['column_span'] != 1 or c['row_span'] != 1 for c in lower.values())):
        return None
    first = right.find('tr')
    if first is None or first.find_parent('thead') or first.find_parent('table') is not right:
        return None
    tails = first.find_all(['td', 'th'], recursive=False)
    if len(tails) != columns or any(t.get(k, '1') != '1' for t in tails for k in ('colspan', 'rowspan')):
        return None
    if not all(_cell_matches_source(node, lower[col]) for col, node in enumerate(tails)):
        return None
    populated = [col for col, cell in lower.items() if cell.get('glyphs')]
    if len(populated) < 2 or any(upper[col]['row_span'] != 1 for col in populated):
        return None
    profile = api.table_profile(str(left))
    if profile['issues'] or profile['columns'] != columns:
        return None
    nodes = left.find_all(['td', 'th'])
    targets = {c['column']: (c, node) for c, node in zip(profile['cells'], nodes)
               if c['row'] + c['row_span'] == profile['rows']}
    if set(targets) != set(upper) or any(
            c['column_span'] != 1 or c['row_span'] != upper[col]['row_span']
            or not _cell_matches_source(node, upper[col]) for col, (c, node) in targets.items()):
        return None
    dictionary = api.russian_word_dictionary()
    if dictionary is None:
        return None
    split, spaced = [], []
    for col in populated:
        node = targets[col][1]
        before = node.find_all(string=True)
        after = tails[col].find(string=True)
        if not before or not after:
            return None
        prefix = re.search(r'\b([А-Яа-яЁё]+)([-\u00ad]?)$', str(before[-1]))
        suffix = re.match(r'([а-яё]+)\b', str(after))
        if not prefix or not suffix:
            return None
        if prefix[2]:
            word = prefix[1] + suffix[1]
            if api.joined_russian_word(prefix[0] + ' ' + suffix[1]) != word:
                return None
            # Independent occurrence on a measured line, outside both fragments being joined.
            occurrence = re.compile(r'\b' + re.escape(word) + r'\b', re.I)
            if not any(occurrence.search(line['text']) for geo in (old, new) for c in geo['cells']
                       if c is not upper[col] and c is not lower[col] and c.get('positioned_source_text')
                       for line in c['lines']):
                return None
            split.append(col)
        else:
            prep = dictionary.parse(prefix[1])[0]
            following = dictionary.parse(suffix[1])[0]
            if (not dictionary.word_is_known(prefix[1]) or not dictionary.word_is_known(suffix[1])
                    or prep.tag.POS != 'PREP' or prep.score < .9
                    or following.score < .9 or following.tag.POS not in {'NOUN', 'ADJF', 'NPRO', 'NUMR'}
                    or following.tag.case not in {'gent', 'datv', 'accs', 'ablt', 'loct'}):
                return None
            spaced.append(col)
    if not split:
        return None
    proof = dict(method='positioned_text_on_both_open_edges', word_columns=split,
                 preposition_columns=spaced, right_row_sha256=sha256(str(first).encode()).hexdigest())
    for col in populated:
        target = targets[col][1]
        if col in split:
            end = target.find_all(string=True)[-1]
            end.replace_with(str(end)[:-1])
        else:
            target.append(' ')
        for child in list(tails[col].contents):
            target.append(child.extract())
    first.decompose()
    return proof
