"""Partition flattened primary table payload using verified source-cell text.

Physical source rules determine the grid. Positioned glyphs are only anchors
for a lossless partition of an existing primary read; no source words replace
the primary payload and this module never invokes an OCR engine.
"""
from copy import copy
import math
import re

from bs4 import BeautifulSoup, Comment, NavigableString


_POSITION = ('row', 'column', 'row_span', 'column_span')
_NOTE = re.compile(r'^\s*(?:п\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]|notes?)\b', re.I)


def _text(node):
    pieces = []
    spans = {}

    def walk(item):
        if isinstance(item, Comment):
            return
        start = sum(map(len, pieces))
        if isinstance(item, NavigableString):
            pieces.append(str(item))
        elif item.name == 'br':
            pieces.append('\n')
        else:
            for child in item.children:
                walk(child)
        spans[id(item)] = (start, sum(map(len, pieces)))
    walk(node)
    value = ''.join(pieces)
    removed = set()
    for match in re.finditer(r'(?<=[A-Za-zА-Яа-яЁё])[-\u00ad][ \t]*\n\s*(?=[a-zа-яё])', value):
        removed.update(range(match.start(), match.end()))
    key, offsets = [], []
    for index, char in enumerate(value):
        if char.isspace() or index in removed or char == '\u00ad':
            continue
        # OCR uses either Unicode C for the same degree-C source glyph.
        if char == 'С' and re.search(r'°\s*$', value[:index]):
            char = 'C'
        key.append(char)
        offsets.append(index)
    return ''.join(key), offsets, spans


def _clip(node, spans, lo, hi):
    start, end = spans[id(node)]
    if end <= lo or start >= hi:
        return None
    if isinstance(node, NavigableString):
        return NavigableString(str(node)[max(0, lo-start):min(end, hi)-start])
    cloned = copy(node)
    cloned.clear()
    if node.name == 'br':
        return cloned
    for child in node.children:
        if id(child) not in spans:
            continue
        part = _clip(child, spans, lo, hi)
        if part is not None:
            cloned.append(part)
    return cloned


def align_flattened_primary(fragment, geometry, target, source_image, region, api):
    """Return a source-shaped profile only after a unique complete partition."""
    if (not geometry.get('source_grid_verified') or geometry.get('hidden_ocr_direction_conflict')
            or geometry.get('glyph_coverage', 0) < .99 or source_image is None):
        return None
    cells = geometry.get('cells', [])
    if not cells or any(c.get('source_text_unreliable') or c.get('restored_math')
                        or c.get('source_images') for c in cells):
        return None
    parsed = BeautifulSoup(fragment, 'html.parser')
    if parsed.table is None or parsed.table.find(['img', 'svg', 'math', 'table', 'code', 'pre']):
        return None
    profile = api.table_profile(fragment)
    if profile.get('issues') or not profile.get('cells'):
        return None
    nodes = parsed.table.find_all(['td', 'th'])
    body, notes = [], []
    for position, node in zip(profile['cells'], nodes):
        if (position['column'] == 0 and position['column_span'] == profile['columns']
                and position['row_span'] == 1 and _NOTE.match(node.get_text(' ', strip=True))):
            notes.append(node)
        elif node.get_text(strip=True):
            if notes:
                return None  # An interior note cannot be treated as a terminal tail.
            body.append(node)
    if not body:
        return None
    physical = {tuple(c[k] for k in _POSITION): c for c in cells}
    source_text = {}
    from docling_pdf_text_cells import positioned_text_proof
    from docling_pdf_table_layout import grid_ink
    ink = grid_ink(source_image, region, geometry['x_grid'])
    if ink is None:
        return None
    for position in target['cells']:
        key = tuple(position[k] for k in _POSITION)
        cell = physical.get(key)
        if cell is None:
            return None
        content = BeautifulSoup(position['html'], 'html.parser')
        normalized = _text(content)[0]
        if not normalized:
            box = cell.get('bbox')
            if not box or any(g.get('text', '').strip() for g in cell.get('glyphs', [])):
                return None
            x0 = max(0, math.ceil((box[0]-region[0]+1)*ink['sx']))
            y0 = max(0, math.ceil((box[1]-region[1]+1)*ink['sy']))
            x1 = min(source_image.width, math.floor((box[2]-region[0]-1)*ink['sx']))
            y1 = min(source_image.height, math.floor((box[3]-region[1]-1)*ink['sy']))
            if x1 <= x0 or y1 <= y0 or ink['nongrid'][y0:y1, x0:x1].any():
                return None
            continue
        # Anchor text must be genuinely positioned on the visible source ink.
        proof = positioned_text_proof(position['html'], cell.get('glyphs', []),
                                     ink['nongrid'], region, cell['bbox'], ink['sx'], ink['sy'])
        if proof is None:
            return None
        source_text[key] = normalized
    if not source_text:
        return None
    by_column = {}
    for key in sorted(source_text):
        by_column.setdefault(key[1], []).append(key)
    possibilities = []
    for node in body:
        value, offsets, spans = _text(node)
        choices = []
        for column in by_column.values():
            for start in range(len(column)):
                group, text = [], ''
                for key in column[start:]:
                    group.append(key)
                    text += source_text[key]
                    if text == value:
                        choices.append((tuple(group), offsets, spans))
                    if len(text) >= len(value):
                        break
        if not choices:
            return None
        possibilities.append(choices)
    solutions = []
    states = 0

    def solve(index, used, chosen):
        nonlocal states
        states += 1
        if states > 4096:
            return
        if len(solutions) > 1:
            return
        if index == len(possibilities):
            if used == set(source_text):
                solutions.append(chosen)
            return
        for item in possibilities[index]:
            keys = set(item[0])
            if not used & keys:
                solve(index+1, used | keys, chosen+[item])
    solve(0, set(), [])
    if len(solutions) != 1 or states > 4096:
        return None
    payloads = {}
    for node, (group, offsets, spans) in zip(body, solutions[0]):
        cursor = 0
        for key in group:
            length = len(source_text[key])
            clipped = _clip(node, spans, offsets[cursor], offsets[cursor+length-1]+1)
            payloads[key] = clipped.decode_contents()
            cursor += length
    out = BeautifulSoup('<table></table>', 'html.parser')
    for row in range(target['rows']):
        tr = out.new_tag('tr')
        out.table.append(tr)
        for position in [c for c in target['cells'] if c['row'] == row]:
            key = tuple(position[k] for k in _POSITION)
            node = out.new_tag('th' if position.get('header') else 'td')
            for attr, field in (('rowspan', 'row_span'), ('colspan', 'column_span')):
                if position[field] > 1:
                    node[attr] = str(position[field])
            node.append(BeautifulSoup(payloads.get(key, ''), 'html.parser'))
            tr.append(node)
    result = api.table_profile(str(out.table))
    if result['issues'] or {tuple(c[k] for k in _POSITION) for c in result['cells']} != set(physical):
        return None
    if notes:
        result['source_partition_external_notes'] = [node.decode_contents() for node in notes]
    result['source_partition_proof'] = dict(
        method='unique_complete_primary_partition_on_visible_positioned_source_glyphs',
        primary_cells=len(body), source_nonempty_cells=len(source_text), source_cells=len(cells),
        source_columns=target['columns'], source_rows=target['rows'], primary_payload_characters_preserved=True)
    return result


def attach_primary_external_notes(soup, manifest, api):
    """Attach proven terminal primary notes after all page-table joins.

    The original read's bbox describes the whole primary packet, not an
    invented location for its individual note. Ownership follows the joined
    table manifest and duplicates are checked only at that owning table.
    """
    entries = {entry['ref']: entry for entry in manifest.get('tables', [])}
    attached = []
    for ref, entry in entries.items():
        geometry = entry.get('pdf_geometry') or {}
        for note in geometry.get('primary_external_notes', []):
            if (note.get('basis') != 'terminal_primary_note_after_unique_complete_source_grid_partition'
                    or note.get('physical_table_bbox') != geometry.get('bbox')
                    or not re.fullmatch(r'[0-9a-f]{64}', note.get('source_record_sha256', ''))):
                continue
            owner = api._primary_note_owner(ref, entries)
            targets = soup.find_all('table', attrs={'data-pdf-table-ref': owner}) if owner else []
            if len(targets) != 1:
                continue
            table = targets[0]
            parsed = BeautifulSoup(note.get('html', ''), 'html.parser')
            if (not _NOTE.match(parsed.get_text(' ', strip=True))
                    or parsed.find(['table', 'script', 'style', 'iframe', 'object', 'img', 'svg', 'math'])):
                continue
            fragment = str(parsed)
            key = api._primary_note_key(fragment, api)
            inside = [node for node in table.find_all(['td', 'th', 'p', 'div'])
                      if _NOTE.match(node.get_text(' ', strip=True))]
            if (any(api._primary_note_key(str(node), api) == key for node in inside)
                    or api._primary_note_adjacent_duplicate(table, fragment, api)):
                continue
            wrapper = soup.new_tag('div', attrs={'class': 'table-notes', 'role': 'note',
                                                 'data-table-note': 'true'})
            paragraph = soup.new_tag('p')
            for node in list(parsed.contents):
                paragraph.append(node.extract())
            wrapper.append(paragraph)
            anchor = table.parent if 'table-scroll' in table.parent.get('class', []) else table
            anchor.insert_after(wrapper)
            proof = dict(ref=ref, owner=owner, page=entry.get('page'),
                         source_record_sha256=note['source_record_sha256'],
                         source_bbox=note.get('source_bbox'), physical_table_bbox=note['physical_table_bbox'],
                         method='terminal_primary_note_after_unique_complete_source_grid_partition')
            attached.append(proof)
            api.diagnostic_event('TABLE_PRIMARY_EXTERNAL_NOTE_ATTACHED', **proof)
    manifest.setdefault('primary_external_note_repairs', []).extend(attached)
    return attached
