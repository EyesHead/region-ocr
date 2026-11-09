"""Reconcile model-only empty columns against complete positioned PDF lines."""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations
import hashlib
import math
import re
import unicodedata

from bs4 import BeautifulSoup


def _key(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold().replace('\xad', '')


def _matches(primary, source):
    a, b = _key(primary), _key(source)
    if a == b:
        return True
    if not a or not b or abs(len(a) - len(b)) > max(1, math.ceil(len(b) * .015)):
        return False
    # Numeric columns establish exact source ownership. Small OCR differences
    # in prose are accepted only for mapping; the payload itself is unchanged.
    if not any(c.isalpha() for c in a) or not any(c.isalpha() for c in b):
        return False
    edits = sum(max(j - i, l - k) for kind, i, j, k, l in
                SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if kind != 'equal')
    return edits <= max(1, math.ceil(max(len(a), len(b)) * .015))


def normalize_primary_columns(fragment, geometry, api):
    """Return (HTML, proof), or the unchanged input and None.

    This is not an empty-column heuristic: a complete native glyph grid must
    prove the column and ordered source-line ownership of every primary cell.
    Model logical rows remain separate even inside one borderless source band.
    """
    if (not geometry or geometry.get('hidden_ocr_direction_conflict') or
            geometry.get('glyph_coverage', 0) < .995 or geometry.get('glyph_count', 0) <= 0):
        return fragment, None
    xs = geometry.get('x_grid', [])
    columns = len(xs) - 1
    source = geometry.get('cells', [])
    if (columns < 2 or not source or any(b <= a for a, b in zip(xs, xs[1:])) or
            any(c.get('source_images') or c.get('source_graphic_assets') for c in source)):
        return fragment, None
    parsed = BeautifulSoup(fragment, 'html.parser')
    if len(parsed.find_all('table')) != 1:
        return fragment, None
    if parsed.find(['img', 'svg', 'math', 'code', 'pre']):
        return fragment, None
    primary = api.table_profile(fragment)
    surplus = primary['columns'] - columns
    if primary['issues'] or not 1 <= surplus <= 3 or any(c['row_span'] != 1 for c in primary['cells']):
        return fragment, None
    physical = []
    for cell in source:
        lines = [line for line in cell.get('lines', []) if _key(line.get('text', ''))]
        if cell.get('row_span') != 1 or (not lines and str(cell.get('text', '')).strip()):
            return fragment, None
        physical.append((cell, lines))
    empty_columns = []
    for column in range(primary['columns']):
        covering = [c for c in primary['cells'] if c['column'] <= column < c['column'] + c['column_span']]
        singles = [c for c in covering if c['column_span'] == 1]
        if len(singles) >= 2 and all(not c['text'].strip() and not c['html'].strip() for c in singles):
            empty_columns.append(column)
    if len(empty_columns) < surplus or len(empty_columns) > 6:
        return fragment, None
    outcomes = []
    for dropped in combinations(empty_columns, surplus):
        kept = [c for c in range(primary['columns']) if c not in dropped]
        cursors, assignments = [0] * len(physical), {}
        valid = True
        row_bands = {}
        for index, cell in enumerate(primary['cells']):
            if not cell['text'].strip():
                continue
            projected = [kept.index(c) for c in range(cell['column'], cell['column'] + cell['column_span']) if c in kept]
            if not projected:
                valid = False
                break
            matches = []
            for s_index, (native, lines) in enumerate(physical):
                native_columns = set(range(native['column'], native['column'] + native['column_span']))
                if not native_columns <= set(projected):
                    continue
                start = cursors[s_index]
                for end in range(start + 1, len(lines) + 1):
                    value = ' '.join(line['text'] for line in lines[start:end])
                    if _matches(cell['text'], value):
                        matches.append((s_index, start, end))
            if len(matches) != 1:
                valid = False
                break
            s_index, start, end = matches[0]
            native, lines = physical[s_index]
            old_band = row_bands.setdefault(cell['row'], native['row'])
            if old_band != native['row']:
                valid = False
                break
            cursors[s_index] = end
            assignments[index] = dict(column=native['column'], column_span=native['column_span'],
                source_cell=s_index, source_line_start=start, source_line_end=end)
        if not valid or any(cursor != len(lines) for cursor, (_, lines) in zip(cursors, physical)):
            continue
        candidate = BeautifulSoup(fragment, 'html.parser')
        nodes = [n for n in candidate.table.find_all(['td', 'th']) if n.find_parent('table') is candidate.table]
        rows = [r for r in candidate.table.find_all('tr') if r.find_parent('table') is candidate.table]
        for row_index, row in enumerate(rows):
            occupied, values = set(), []
            for index, cell in enumerate(primary['cells']):
                if cell['row'] != row_index:
                    continue
                if index in assignments:
                    proof = assignments[index]
                    start, span = proof['column'], proof['column_span']
                    if occupied & set(range(start, start + span)):
                        valid = False
                        break
                    node = nodes[index].extract()
                    node.attrs.pop('colspan', None)
                    if span > 1:
                        node['colspan'] = str(span)
                    values.append((start, node))
                    occupied.update(range(start, start + span))
            if not valid:
                break
            for col in range(columns):
                if col not in occupied:
                    values.append((col, candidate.new_tag('td')))
            row.clear()
            for _, node in sorted(values, key=lambda pair: pair[0]):
                row.append(node)
        if not valid:
            continue
        for group in candidate.table.find_all('colgroup', recursive=False):
            group.decompose()
        html = str(candidate.table)
        checked = api.table_profile(html, columns)
        if (checked['issues'] or checked['rows'] != primary['rows'] or
                Counter(''.join(parsed.table.stripped_strings)) != Counter(''.join(candidate.table.stripped_strings))):
            continue
        proof = dict(method='complete_positioned_source_lines_prove_primary_columns',
            source_columns=columns, primary_columns=primary['columns'], removed_empty_columns=list(dropped),
            source_line_count=sum(len(lines) for _, lines in physical), logical_rows=checked['rows'],
            mapped_cells=len(assignments), source_glyph_coverage=geometry['glyph_coverage'],
            primary_html_sha256=hashlib.sha256(fragment.encode('utf-8')).hexdigest(),
            repaired_html_sha256=hashlib.sha256(html.encode('utf-8')).hexdigest(), assignments=assignments)
        outcomes.append((html, proof))
    return outcomes[0] if len(outcomes) == 1 else (fragment, None)
