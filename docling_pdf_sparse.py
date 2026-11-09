"""Interval occupancy for HTML tables, independent of their numeric spans.

A colspan is a coordinate range, not a request to allocate that many cells.
This module contains no document names, model limits or maximum grid sizes.
"""
from bisect import bisect_left, bisect_right


class SparseGrid:
    """Disjoint half-open column intervals, indexed by actual HTML row."""

    def __init__(self):
        self._rows = {}

    def intervals(self, row):
        return tuple(self._rows.get(row, ()))

    def next_free(self, row, x):
        intervals = self._rows.get(row, ())
        index = bisect_right(intervals, (x, float('inf'))) - 1
        if index >= 0 and intervals[index][0] <= x < intervals[index][1]:
            return intervals[index][1]
        return x

    def add(self, row, start, end):
        if row < 0 or start < 0 or end <= start:
            raise ValueError('invalid_grid_interval')
        intervals = self._rows.setdefault(row, [])
        index = bisect_left(intervals, (start, end))
        if ((index and intervals[index-1][1] > start)
                or (index < len(intervals) and intervals[index][0] < end)):
            raise ValueError('overlapping_spans')
        # Adjacent cells can share one occupancy interval; their HTML nodes
        # remain separate. This also keeps ordinary rectangular rows compact.
        if index and intervals[index-1][1] == start:
            index -= 1
            start = intervals[index][0]
            del intervals[index]
        if index < len(intervals) and intervals[index][0] == end:
            end = intervals[index][1]
            del intervals[index]
        intervals.insert(index, (start, end))

    def has_gaps(self, row):
        intervals = self._rows.get(row, ())
        return bool(intervals and (intervals[0][0] != 0
                    or any(left[1] < right[0] for left, right in zip(intervals, intervals[1:]))))

    def width(self, row):
        intervals = self._rows.get(row, ())
        return intervals[-1][1] if intervals else 0


def _span(cell, name, remaining):
    try:
        value = int(cell.get(name, 1))
    except (ValueError, TypeError):
        value = 1
    if name == 'rowspan':
        return min(value, remaining) if value > 0 else remaining if value == 0 else 1
    return max(value, 1)


def _grid_for_rows(rows):
    """Validate real cells; work depends on rowspans, never on colspans."""
    grid = SparseGrid()
    for y, row in enumerate(rows):
        x = 0
        for cell in row.find_all(['td', 'th'], recursive=False):
            x = grid.next_free(y, x)
            cs, rs = int(cell.get('colspan', 1)), int(cell.get('rowspan', 1))
            if cs < 1 or not 1 <= rs <= len(rows)-y:
                raise ValueError('invalid_span')
            for yy in range(y, y+rs):
                grid.add(yy, x, x+cs)
            x += cs
    return grid


def repair_sparse_spans(fragment, expected_columns=None):
    """Repair invalid large spans while retaining every actual source cell.

    The caller uses this path when a span exceeds the number of HTML cells.
    Valid large tables return immediately. Rowspan work is bounded by the real
    rows; empty column ranges use one empty cell with a colspan attribute.
    ``expected_columns`` is source-grid evidence, not a processing ceiling.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fragment, 'html.parser')
    table = soup.find('table')
    if table is None:
        return fragment, []
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    if not rows:
        return fragment, []
    repairs = []
    groups = {}
    for row in rows:
        group = row.find_parent(['thead', 'tbody', 'tfoot'])
        if group is not None:
            groups.setdefault(id(group), []).append(row)
    group_remaining = {id(row): len(group)-index for group in groups.values()
                       for index, row in enumerate(group)}
    for y, row in enumerate(rows):
        for cell in row.find_all(['td', 'th'], recursive=False):
            for name in ('rowspan', 'colspan'):
                raw = cell.get(name, 1)
                remaining = group_remaining.get(id(row), len(rows)-y)
                value = _span(cell, name, remaining if name == 'rowspan' and str(raw) == '0' else len(rows)-y)
                if str(value) != str(raw):
                    cell[name] = str(value)
                    repairs.append(dict(operation='normalize_sparse_span', row=y, attribute=name,
                                        original=str(raw), value=value))
    try:
        current = _grid_for_rows(rows)
        widths = {current.width(y) for y in range(len(rows))}
        valid = len(widths) <= 1 and not any(current.has_gaps(y) for y in range(len(rows)))
        if expected_columns is not None:
            valid = valid and widths == {expected_columns}
        if valid:
            return (str(table) if repairs else fragment), repairs
    except ValueError:
        pass
    first_cells = rows[0].find_all(['td', 'th'], recursive=False)
    target = expected_columns or sum(int(cell.get('colspan', 1)) for cell in first_cells)
    if target < 1:
        target = max((sum(int(c.get('colspan', 1)) for c in row.find_all(['td', 'th'], recursive=False))
                      for row in rows), default=1)
    target = max(target, 1)
    active, placements = [], []
    for y, row in enumerate(rows):
        active = [entry for entry in active if entry['until'] > y]
        cells = row.find_all(['td', 'th'], recursive=False)
        explicit = sum(int(cell.get('colspan', 1)) for cell in cells)
        if explicit == target and active:
            for entry in active:
                entry['node']['rowspan'] = str(y-entry['row'])
            active = []
            repairs.append(dict(operation='terminate_sparse_rowspan', row=y))
        occupied = SparseGrid()
        for entry in active:
            occupied.add(0, entry['start'], entry['end'])
        if len(cells) == 1 and not active and explicit != target:
            cells[0]['colspan'] = str(target)
            repairs.append(dict(operation='complete_sparse_full_width_cell', row=y, columns=target))
        x = 0
        for index, cell in enumerate(cells):
            x = occupied.next_free(0, x)
            original = cs = int(cell.get('colspan', 1))
            rs = int(cell.get('rowspan', 1))
            next_wall = next((start for start, end in occupied.intervals(0) if start > x), None)
            if next_wall is not None:
                cs = min(cs, next_wall-x)
            if x < target:
                reserved = sum(max(0, min(end, target)-max(start, x))
                               for start, end in occupied.intervals(0))
                free = target-x-reserved
                remaining_cells = len(cells)-index-1
                if free >= remaining_cells+1:
                    cs = min(cs, free-remaining_cells)
            cs = max(cs, 1)
            if cs != original:
                cell['colspan'] = str(cs)
                repairs.append(dict(operation='shorten_sparse_colliding_span', row=y,
                                    column=x, original=original, value=cs))
            occupied.add(0, x, x+cs)
            placements.append(dict(row=y, start=x, end=x+cs, node=cell))
            if rs > 1:
                active.append(dict(row=y, start=x, end=x+cs, until=y+rs, node=cell))
            x += cs
    # Rebuild occupancy after any overlong rowspans have been terminated.
    final = SparseGrid()
    for entry in placements:
        for y in range(entry['row'], entry['row']+int(entry['node'].get('rowspan', 1))):
            final.add(y, entry['start'], entry['end'])
    width = max(target, max((final.width(y) for y in range(len(rows))), default=0))
    blanks = 0
    for y, row in enumerate(rows):
        nodes = [(entry['start'], entry['node']) for entry in placements if entry['row'] == y]
        cursor = 0
        for start, end in final.intervals(y) + ((width, width),):
            if start > cursor:
                blank = soup.new_tag('th' if row.find_parent('thead') else 'td')
                if start-cursor > 1:
                    blank['colspan'] = str(start-cursor)
                nodes.append((cursor, blank))
                blanks += 1
            cursor = max(cursor, end)
        for node in row.find_all(['td', 'th'], recursive=False):
            node.extract()
        for _, node in sorted(nodes, key=lambda pair: pair[0]):
            row.append(node)
    repairs.append(dict(operation='rebuild_sparse_rectangular_occupancy', rows=len(rows),
                        columns=width, empty_ranges=blanks, source_columns=expected_columns))
    return str(table), repairs
