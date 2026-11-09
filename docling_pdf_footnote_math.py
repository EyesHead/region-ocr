"""Distinguish a table note marker from a genuine mathematical power."""
from hashlib import sha256
import re

from bs4 import BeautifulSoup


def _signature(text):
    # These source-layer lookalikes validate presentation only; the primary
    # characters and numbers are never replaced with hidden-layer content.
    text = text.translate(str.maketrans({'З': '3', 'з': '3', 'О': '0', 'O': '0'}))
    return re.sub(r'\s|\u00ad', '', text)


def _note_ids(table):
    result = set()
    for block in table.find_all(['td', 'th', 'p']):
        if block.find_parent('table') is not table:
            continue
        if block.name != 'p':
            try:
                if int(block.get('colspan', 1)) < 2:
                    continue
            except (TypeError, ValueError):
                continue
        result.update(re.findall(r'(?:^|\n)\s*(\d{1,2})\)\s+(?=[^\W\d_])',
                                 block.get_text('\n', strip=True)))
    return result


def _owner(ref, entries):
    seen = set()
    while entries.get(ref, {}).get('joined_into'):
        if ref in seen:
            return None
        seen.add(ref)
        ref = entries[ref]['joined_into']
    return ref


def restore_table_math_note_markers(soup, manifest, api):
    """Require both an owned primary note and source-cell closing parenthesis.

    A coincidentally numbered note cannot turn x**4, 10**4 or a unit power
    into a reference. Only an isolated numeric numerator is eligible, with
    the complete primary numeric cell matching the reliable source cell
    except for that missing closing parenthesis.
    """
    entries = {t['ref']: t for t in manifest.get('tables', [])}
    sources = {}
    for ref, entry in entries.items():
        geometry = entry.get('pdf_geometry') or {}
        if not geometry.get('source_grid_verified') or geometry.get('hidden_ocr_direction_conflict'):
            continue
        owner = _owner(ref, entries)
        for cell in geometry.get('cells', []):
            if cell.get('source_text_unreliable') or cell.get('primary_ocr') or cell.get('header'):
                continue
            signature = _signature(BeautifulSoup(cell.get('html', ''), 'html.parser').get_text('', strip=True))
            if re.match(r'^\d+(?:[,.]\d+)?\d{1,2}\)', signature):
                sources.setdefault((owner, signature), []).append(dict(page=entry['page'], ref=ref,
                    column=cell['column'], bbox=cell.get('bbox')))
    proofs = manifest.setdefault('table_math_note_repairs', [])
    for table in soup.select('table[data-pdf-table-ref]'):
        ref = table['data-pdf-table-ref']
        notes = _note_ids(table)
        if not notes:
            continue
        for script in list(table.find_all('msup')):
            if script.find_parent('table') is not table:
                continue
            children = script.find_all(recursive=False)
            if len(children) != 2:
                continue
            base, exponent = children
            marker = exponent.get_text('', strip=True)
            if marker not in notes or not re.fullmatch(r'\d+(?:[,.]\d+)?', base.get_text('', strip=True)):
                continue
            fraction = script.find_parent('mfrac')
            numerator = script
            while numerator.parent and numerator.parent.name == 'mrow' and len(numerator.parent.find_all(recursive=False)) == 1:
                numerator = numerator.parent
            if fraction is None or not fraction.find_all(recursive=False) or fraction.find_all(recursive=False)[0] is not numerator:
                continue
            cell = script.find_parent(['td', 'th'])
            if cell is None:
                continue
            # Compare the whole cell, including any denominator parentheses
            # emitted outside MathML. Source text supplies no replacement value.
            checked = BeautifulSoup(str(cell), 'html.parser')
            scripts = cell.find_all('msup')
            index = next(i for i, node in enumerate(scripts) if node is script)
            checked.find_all('msup')[index].find_all(recursive=False)[1].string = marker+')'
            signature = _signature(checked.get_text('', strip=True))
            evidence = sources.get((ref, signature), [])
            if not evidence:
                continue
            original = str(script)
            row = soup.new_tag('mrow')
            row.append(base.extract())
            raised = soup.new_tag('mpadded', attrs={'voffset': '0.5em', 'height': '+0.5em',
                'data-footnote-reference': marker, 'role': 'doc-noteref'})
            style = soup.new_tag('mstyle', attrs={'mathsize': '70%'})
            label = soup.new_tag('mtext')
            label.string = marker+')'
            style.append(label); raised.append(style); row.append(raised)
            math = script.find_parent('math')
            old_latex = math.attrs.pop('data-latex', None) if math else None
            if math:
                for annotation in math.find_all(['annotation', 'annotation-xml']):
                    annotation.decompose()
            script.replace_with(row)
            proof = dict(ref=ref, marker=marker+')', source_cells=evidence,
                         original_sha256=sha256(original.encode()).hexdigest(), original_latex=old_latex,
                         method='primary_note_definition_and_matching_source_cell_parenthesis')
            proofs.append(proof)
            api.diagnostic_event('TABLE_MATH_NOTE_MARKER_RESTORED', **proof)
    return proofs
