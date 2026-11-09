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
    last, first = left.find_all('tr'), right.find('tr')
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
