"""Publish table-owned footnotes through the normal strict text serializer."""
import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString


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
