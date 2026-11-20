"""Keep normative examples and notes with the source context they qualify."""
import re

from bs4 import BeautifulSoup


_EXAMPLE = r'п\s*р\s*и\s*м\s*е\s*р(?:\s*ы)?'
_NOTE = r'п\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]'
_LABEL = re.compile(r'^\s*(?P<label>' + _EXAMPLE + '|' + _NOTE + r')(?=\s|$|[:.\-–—(\d])', re.I)
_CLAUSE = re.compile(r'^\s*(\d+(?:\.\d+)+)(?=\s|$)')


def annotation_kind(text):
    match = _LABEL.match(text or '')
    if not match:
        return None
    return 'example' if re.sub(r'\s', '', match['label']).casefold().startswith('пример') else 'note'


def source_id(ref):
    return 'pdf-' + str(ref).lstrip('#/').replace('/', '-')


def _table_note_cell(cell):
    if (cell.has_attr('data-table-note') or cell.has_attr('data-primary-table-notes')
            or cell.get('role') == 'note' or cell.find_parent('tfoot')):
        return True
    table, row = cell.find_parent('table'), cell.find_parent('tr')
    if table is None or row is None or cell.name == 'th' or cell.find_parent('thead'):
        return False
    rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
    if not rows or rows[-1] is not row or row.find_all(['td', 'th'], recursive=False) != [cell]:
        return False
    def span(node):
        try:
            return max(1, int(node.get('colspan', 1)))
        except (ValueError, TypeError):
            return 1
    width = max(sum(span(c) for c in r.find_all(['td', 'th'], recursive=False)) for r in rows)
    return span(cell) == width


def annotation_context(doc, excluded, manifest):
    """Resolve source owners before serializing away list markers and labels."""
    joined = {entry['from']: entry['into'] for entry in manifest.get('joined_tables', [])}
    def table_owner(ref):
        seen = set()
        while ref in joined and ref not in seen:
            seen.add(ref)
            ref = joined[ref]
        return ref
    explicit = {}
    for table in doc.tables:
        for ref in [*(getattr(table, 'footnotes', None) or []),
                    *(getattr(table, 'children', None) or [])]:
            explicit[ref.cref] = table_owner(table.self_ref)
    context = {ref: {'list_value': number} for repair in manifest.get('split_illustrated_lists', ())
               for ref, number in repair['numeric_markers'].items()}
    for repair in manifest.get('split_illustrated_lists', ()):
        for ref, number in repair.get('clauses', {}).items():
            context.setdefault(ref, {})['clause'] = number
    section = clause = previous = None
    for item, _ in doc.iterate_items():
        ref = item.self_ref
        if ref in excluded or str(getattr(item, 'content_layer', '')) == 'furniture':
            continue
        label = str(getattr(item, 'label', ''))
        text = getattr(item, 'text', '')
        kind = annotation_kind(text)
        if kind:
            owner = explicit.get(ref)
            owner_kind = 'table' if owner else None
            if owner is None and previous and previous[1] == 'table':
                owner, owner_kind = previous[0], 'table'
            if owner is None:
                owner, owner_kind = ((clause, 'clause') if clause else
                                     (section, 'section') if section else
                                     (previous[0], 'paragraph') if previous else ('#/body', 'document'))
            context[ref] = dict(kind=kind, owner=owner, owner_kind=owner_kind)
            continue
        marker = str(getattr(item, 'marker', '') or '').strip()
        match = _CLAUSE.match(marker)
        if match is None and label in {'section_header', 'list_item'}:
            match = _CLAUSE.match(getattr(item, 'orig', '') or text)
        if label in {'section_header', 'title'}:
            section, clause = ref, ref if match else None
        elif match:
            clause = ref
        if match:
            context.setdefault(ref, {})['clause'] = match[1]
        if label == 'table':
            previous = table_owner(ref), 'table'
        elif text.strip() and label not in {'caption', 'footnote', 'page_header', 'page_footer'}:
            previous = ref, 'paragraph'
    for info in list(context.values()):
        if info.get('owner'):
            context.setdefault(info['owner'], {})['is_owner'] = True
    return context


def mark_source_text(fragment, ref, context):
    """Expose stable source identities without adding visible text."""
    info = context.get(ref)
    if not info:
        return fragment
    parsed = BeautifulSoup(fragment, 'html.parser')
    node = parsed.find(['p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div'])
    if node is None:
        return fragment
    node['id'] = source_id(ref)
    node['data-source-ref'] = ref
    if node.name == 'li' and 'list_value' in info:
        node['value'] = str(info['list_value'])
    if info.get('clause'):
        node['data-source-clause'] = info['clause']
    if info.get('kind'):
        node['data-annotation-kind'] = info['kind']
        node['data-annotation-owner'] = source_id(info['owner'])
        node['data-annotation-owner-kind'] = info['owner_kind']
    return str(parsed)


def normalize_table_typography(soup):
    """Give cell prose ordinary type; header cells retain their table role."""
    changed = 0
    for cell in soup.find_all(['td', 'th']):
        for node in [cell, *list(cell.find_all(True))]:
            if (node is not cell and node.find_parent(['td', 'th']) is not cell
                    or node.name in {'math', 'svg'} or node.find_parent(['math', 'svg'])):
                continue
            if re.fullmatch(r'h[1-6]', node.name):
                # Keep paragraph boundaries, which also separate words in
                # Markdown and in cells containing several OCR blocks.
                node.name = 'p'
                changed += 1
            if node.get('style'):
                style = ';'.join(part for part in node['style'].split(';')
                                 if not re.match(r'\s*(?:font(?:-[\w-]+)?|text-decoration)\s*:', part, re.I))
                if style != node['style']:
                    changed += 1
                    if style.strip():
                        node['style'] = style
                    else:
                        del node['style']
            if node.name in {'b', 'strong', 'i', 'em', 'u', 'big', 'small', 'font'}:
                node.unwrap()
                changed += 1
    return changed


def normalize_semantic_annotations(soup, context, manifest):
    """Correct semantic labels in the canonical HTML, preserving payload/order."""
    normalize_table_typography(soup)
    body = soup.body or soup
    if body.get('id') is None:
        body['id'] = source_id('#/body')
    for table in soup.select('table[data-pdf-table-ref]'):
        table['id'] = source_id(table['data-pdf-table-ref'])
    # Clause identifiers are source text, not an HTML list counter ("1.").
    for listing in list(soup.find_all(['ol', 'ul'])):
        items = listing.find_all('li', recursive=False)
        if (listing.name == 'ol' and items and 'start' not in listing.attrs and items[0].get('data-source-ref')
                and items[0].has_attr('value') and items[0]['value'] != '1'):
            listing['start'] = items[0]['value']
        if (not items or any(not item.has_attr('data-source-clause') or item.find(['ol', 'ul'])
                             for item in items)):
            continue
        for item in items:
            number = item['data-source-clause']
            if not re.match(r'^\s*' + re.escape(number) + r'(?=\s|$)', item.get_text(' ', strip=True)):
                item.insert(0, number + ' ')
            item.name = 'p'
        listing.unwrap()
    for cell in soup.find_all('td'):
        if (annotation_kind(cell.get_text(' ', strip=True)) and _table_note_cell(cell)
                and not cell.find(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
            paragraph = soup.new_tag('p')
            for child in list(cell.contents):
                paragraph.append(child.extract())
            cell.append(paragraph)
    repaired = []
    for node in list(soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
        kind = annotation_kind(node.get_text(' ', strip=True))
        if not kind:
            continue
        # A column named "Примечание" is a header, not a note about the table.
        cell = node.find_parent(['td', 'th'])
        if cell and not _table_note_cell(cell):
            if node.name.startswith('h'):
                node.name = 'p'
            continue
        node.name = 'p'
        for emphasis in list(node.find_all(['b', 'strong', 'i', 'em', 'u'])):
            if not emphasis.find_parent(['math', 'svg']):
                emphasis.unwrap()
        for styled in [node, *node.find_all(style=True)]:
            if (not styled.get('style') or styled.name in {'math', 'svg'}
                    or styled.find_parent(['math', 'svg'])):
                continue
            styled['style'] = ';'.join(part for part in styled['style'].split(';')
                                      if not re.match(r'\s*(?:font-(?:weight|style|size)|text-decoration)\s*:', part, re.I))
            if not styled['style'].strip():
                del styled['style']
        node['data-annotation-kind'] = kind
        if not node.get('id'):
            node['id'] = 'pdf-annotation-' + str(len(repaired) + 1)
        owner = node.get('data-annotation-owner')
        owner_kind = node.get('data-annotation-owner-kind')
        table = node.find_parent('table')
        note_group = node.find_parent(attrs={'data-table-note': True})
        if table:
            if not table.get('id'):
                table['id'] = 'pdf-table-' + str(list(soup.find_all('table')).index(table) + 1)
            owner, owner_kind = table['id'], 'table'
        elif note_group and note_group.get('data-annotation-owner'):
            owner, owner_kind = note_group['data-annotation-owner'], 'table'
        if not owner or soup.find(id=owner) is None:
            # A routed/excluded owner is never published as a dangling link.
            previous = node.find_previous(lambda tag: tag.has_attr('data-source-clause') or tag.name in {'table', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})
            if previous:
                if not previous.get('id'):
                    previous['id'] = 'pdf-context-' + str(len(repaired) + 1)
                owner = previous['id']
                owner_kind = 'table' if previous.name == 'table' else 'clause' if previous.has_attr('data-source-clause') else 'section'
            else:
                owner, owner_kind = body['id'], 'document'
        node['data-annotation-owner'] = owner
        node['data-annotation-owner-kind'] = owner_kind
        # A standalone label and its numbered examples/notes form one block.
        label = _LABEL.match(node.get_text(' ', strip=True))
        tail = node.get_text(' ', strip=True)[label.end():].strip(' :.\u2014\u2013-')
        following = node.find_next_sibling()
        grouped = node.parent and node.parent.has_attr('data-source-annotation')
        if (not grouped and not tail and following and following.name in {'ol', 'ul'}
                and not following.select('[data-source-clause]')):
            wrapper = soup.new_tag('div', attrs={'data-source-annotation': kind,
                'data-annotation-owner': owner, 'data-annotation-owner-kind': owner_kind,
                'id': node['id'] + '-group'})
            node.wrap(wrapper)
            wrapper.append(following.extract())
            for emphasis in list(wrapper.find_all(['b', 'strong', 'i', 'em', 'u'])):
                if not emphasis.find_parent(['math', 'svg']):
                    emphasis.unwrap()
        repaired.append(dict(id=node['id'], kind=kind, owner_id=owner, owner_kind=owner_kind))
    manifest['semantic_annotations'] = repaired
    return repaired
