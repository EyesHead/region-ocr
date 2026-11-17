"""Publish table-owned footnotes through the normal strict text serializer."""
import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString


def join_primary_note_table_continuations(soup, manifest, records, api):
    """Unwrap a source-proved continuation of an unfinished table footnote.

    Page OCR can classify the remainder of a full-width footer as a new
    one-column table. The preceding published note and both positioned
    primary packets must prove its owner before any markup is moved.
    """
    from hashlib import sha256
    from bs4 import Comment

    def flat(text):
        return re.sub(r'[\s\u00ad\u200b]+', '', unicodedata.normalize('NFKC', text))

    def outer(table):
        parent = table.parent
        if parent is not None and 'table-scroll' in parent.get('class', []):
            return parent
        return table

    def previous_visible(node):
        for sibling in node.previous_siblings:
            if isinstance(sibling, Comment) or (isinstance(sibling, NavigableString) and not sibling.strip()):
                continue
            return sibling
        return None

    entries = {entry['ref']: entry for entry in manifest.get('tables', [])}
    pages = manifest.get('pages', {})
    repaired = []
    tables = [table for table in soup.find_all('table') if table.find_parent('table') is None]
    for index, table in enumerate(tables):
        if index == 0 or table.parent is None or table.find('caption'):
            continue
        entry = entries.get(table.get('data-pdf-table-ref'))
        previous = tables[index - 1]
        if previous.parent is None:
            continue
        old = entries.get(previous.get('data-pdf-table-ref'))
        if not entry or not old or entry.get('joined_into'):
            continue
        page = entry['page']
        previous_page = old.get('source_pages', [old['page']])[-1]
        if page != previous_page + 1:
            continue
        info = pages.get(page, pages.get(str(page), {}))
        old_info = pages.get(previous_page, pages.get(str(previous_page), {}))
        if any(meta.get('clockwise') != 0 or len(meta.get('original_size', [])) != 2
               for meta in (info, old_info)):
            continue
        width, height = info['original_size']
        old_width, old_height = old_info['original_size']
        profile = api.table_profile(str(table))
        if (profile['issues'] or profile['columns'] != 1 or profile['rows'] < 2
                or api.table_profile(str(previous))['columns'] < 2
                or table.find(['img', 'svg', 'table', 'ul', 'ol', 'pre', 'code'])
                or any(c['row_span'] != 1 or c['column_span'] != 1 for c in profile['cells'])):
            continue
        cells = [cell for cell in table.find_all(['td', 'th']) if cell.find_parent('table') is table]
        if len(cells) != profile['rows']:
            continue
        texts = [cell.get_text(' ', strip=True) for cell in cells]
        numbers = [re.match(r'^(\d+)\s+\D', text) for text in texts[1:]]
        if (not re.match(r'^[а-яёa-z]', texts[0]) or not all(numbers)
                or [int(match[1]) for match in numbers] != list(range(2, len(cells) + 1))):
            continue
        holder = outer(table)
        note = previous_visible(holder)
        if (note is None or getattr(note, 'name', None) not in {'div', 'p'}
                or previous_visible(note) is not outer(previous) or note.find(['table', 'img', 'svg'])):
            continue
        prefix = note.get_text(' ', strip=True)
        if not re.match(r'^1\s+\D', prefix) or re.search(r'[.!?;:]\s*$', prefix):
            continue
        current_packets, preceding_packets = [], []
        for record in records or ():
            if record.get('page_no') not in {previous_page, page}:
                continue
            box = record.get('bbox')
            if not box or len(box) != 4 or '<table' not in record.get('html', '').lower():
                continue
            parsed = BeautifulSoup(record['html'], 'html.parser')
            if len(parsed.find_all('table')) != 1:
                continue
            text = parsed.get_text(' ', strip=True)
            if (record.get('page_no') == page and box[1] < .15 * height
                    and flat(text) == flat(table.get_text(' ', strip=True))):
                current_packets.append(record)
            if record.get('page_no') == previous_page and box[3] > .85 * old_height:
                marker = list(re.finditer(r'\bпримечани[ея]\b', text, re.I))
                if marker and flat(text[marker[-1].end():]) == flat(prefix):
                    preceding_packets.append(record)
        pairs = [(a, b) for a in preceding_packets for b in current_packets
                 if all(abs(a['bbox'][i] / old_width - b['bbox'][i] / width) < .015 for i in (0, 2))]
        if len(pairs) != 1:
            continue
        preceding_packet, current_packet = pairs[0]
        # Work on copies until a complete prose/MathML check proves that
        # replacing the wrapper changes presentation only.
        copied = BeautifulSoup(str(note), 'html.parser').find(note.name)
        if copied.name == 'p':
            wrapper = soup.new_tag('div', attrs={'class': 'pdf-source-table-notes'})
            wrapper.append(copied)
            copied = wrapper
        copied_table = BeautifulSoup(str(table), 'html.parser').table
        copied_cells = copied_table.find_all(['td', 'th'])
        for cell in copied_cells:
            for block in reversed(cell.find_all(['p', 'div'])):
                block.insert_before(' ')
                block.unwrap()
        target = copied.find_all('p')[-1] if copied.find('p') else copied
        target.append(' ')
        for child in list(copied_cells[0].contents):
            target.append(child.extract())
        for cell in copied_cells[1:]:
            paragraph = soup.new_tag('p', attrs={'class': 'source-table-note'})
            for child in list(cell.contents):
                paragraph.append(child.extract())
            copied.append(paragraph)
        if _semantic_key(str(note) + str(table), api) != _semantic_key(str(copied), api):
            continue
        copied['data-table-note'] = 'true'
        copied['data-note-owner'] = old['ref']
        copied['role'] = 'note'
        note.replace_with(copied)
        table.decompose()
        if holder is not table and not holder.get_text(strip=True) and not holder.find(['table', 'math', 'img']):
            holder.decompose()
        proof = dict(table=old['ref'], continuation=entry['ref'], source_pages=[previous_page, page],
                     paragraphs=len(cells), method='adjacent_primary_packets_of_unfinished_numbered_table_note',
                     preceding_record_sha256=sha256(preceding_packet['html'].encode()).hexdigest(),
                     continuation_record_sha256=sha256(current_packet['html'].encode()).hexdigest(),
                     source_bbox=list(current_packet['bbox']))
        entry['publication_role'] = 'table_note_continuation'
        entry['note_continuation_into'] = old['ref']
        repaired.append(proof)
        api.diagnostic_event('PRIMARY_TABLE_NOTE_CONTINUATION_JOINED', **proof)
    manifest.setdefault('primary_table_note_continuations', []).extend(repaired)
    return repaired


def _semantic_key(fragment, api):
    """Keep math structure and its place among prose when checking duplicates."""
    from docling_pdf_repairs import math_signature
    from latex2mathml.converter import convert

    parsed = BeautifulSoup(api.render_surya_math(fragment), 'html.parser')
    parts, prose = [], []
    def flush():
        text = ''.join(prose)
        text = re.sub(r'(?<=\w)[-\u00ad]\s*\n\s*(?=\w)', '', text)
        text = re.sub(r'[\s\u00ad\u200b]+', '', unicodedata.normalize('NFKC', text))
        prose.clear()
        if text:
            parts.append(('text', text))
    def visit(node):
        if isinstance(node, NavigableString):
            prose.append(str(node))
        elif getattr(node, 'name', None) == 'math':
            flush()
            math = str(node)
            if not node.find(['mi', 'mn', 'mo', 'mrow', 'mfrac', 'msub', 'msup', 'mtext']):
                try:
                    math = convert(node.get('data-latex') or node.get_text())
                except Exception:
                    parts.append(('unrenderable_math', node.get_text()))
                    return
            parts.append(('math', math_signature(math)))
        elif getattr(node, 'name', None) == 'br':
            prose.append('\n')
        else:
            for child in getattr(node, 'children', []):
                visit(child)
    visit(parsed)
    flush()
    return tuple(parts)


def append_owned_footnotes(fragment, item, doc, doc_serializer, excluded, manifest, api, kwargs):
    """Attach footnotes of every source fragment to its final routed table.

    Calling the configured text serializer directly keeps SourceFormulas and
    its strict checks, while bypassing the document serializer's deliberate
    omission of footnotes already owned by a floating item.
    """
    from docling_core.types.doc import DocItemLabel, TextItem

    if not fragment:
        return fragment
    parents = {entry['from']: entry['into'] for entry in manifest.get('joined_tables', [])}
    for entry in manifest.get('tables', []):
        if entry.get('joined_into'):
            if entry['ref'] in parents and parents[entry['ref']] != entry['joined_into']:
                raise ValueError('Conflicting joined-table ownership while publishing footnotes')
            parents[entry['ref']] = entry['joined_into']
    def owner(ref):
        seen = set()
        while ref in parents:
            if ref in seen:
                raise ValueError('Cyclic joined-table ownership while publishing footnotes')
            seen.add(ref)
            ref = parents[ref]
        return ref
    target = owner(item.self_ref)
    if target != item.self_ref:
        return fragment
    sources = [table for table in doc.tables if owner(table.self_ref) == target]
    if not any(table.self_ref == item.self_ref for table in sources):
        sources.insert(0, item)
    def order(table):
        prov = getattr(table, 'prov', None) or []
        return min((p.page_no for p in prov), default=0), table.self_ref
    sources.sort(key=order)
    notes = {}
    for table in sources:
        refs = list(getattr(table, 'footnotes', None) or [])
        refs += [ref for ref in (getattr(table, 'children', None) or [])
                 if isinstance(note := ref.resolve(doc), TextItem) and note.label == DocItemLabel.FOOTNOTE]
        for ref in refs:
            note = ref.resolve(doc)
            if isinstance(note, TextItem) and note.self_ref not in excluded:
                notes.setdefault(note.self_ref, (note, table.self_ref))
    if not notes:
        return fragment
    parsed = BeautifulSoup(fragment, 'html.parser')
    published_keys = {_semantic_key(str(node), api)
                      for node in parsed.find_all(['p', 'li', 'td', 'th', 'tfoot'])}
    additions = []
    for ref, (note, source_table) in notes.items():
        result = doc_serializer.text_serializer.serialize(item=note, doc_serializer=doc_serializer,
                                                          doc=doc, **kwargs)
        rendered = api.render_surya_math(result.text)
        key = _semantic_key(rendered, api)
        if not key:
            continue
        if key in published_keys:
            api.diagnostic_event('FOOTNOTE_ALREADY_PUBLISHED', ref=ref, table=target,
                                 source_table=source_table, reason='exact_prose_and_math_structure')
            continue
        content = BeautifulSoup(rendered, 'html.parser')
        for wrapper in list(content.find_all(['html', 'body', 'div'])):
            wrapper.unwrap()
        paragraphs = content.find_all('p', recursive=False)
        if not paragraphs:
            paragraph = content.new_tag('p')
            for node in list(content.contents):
                paragraph.append(node.extract())
            content.append(paragraph)
            paragraphs = [paragraph]
        for paragraph in paragraphs:
            classes = list(paragraph.get('class', []))
            if 'source-table-note' not in classes:
                classes.append('source-table-note')
            paragraph['class'] = classes
            paragraph['data-source-footnote'] = ref
        additions.append(str(content))
        published_keys.add(key)
        prov = getattr(note, 'prov', None) or []
        publication = dict(ref=ref, table=target, source_table=source_table,
                           pages=sorted({p.page_no for p in prov}),
                           span_source=ref, method='configured_text_serializer_of_owned_footnote')
        manifest.setdefault('published_source_footnotes', []).append(publication)
        api.diagnostic_event('SOURCE_FOOTNOTE_CHILD_PUBLISHED', **publication)
    return fragment+''.join(additions)
