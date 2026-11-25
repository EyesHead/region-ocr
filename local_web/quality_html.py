"""Semantic HTML for the local quality PDF route, after source evidence checks."""
from copy import copy
import hashlib
import json
import re

SOURCE_MARKER = 'document-pdf-source-metadata'
_PRESENTATION = frozenset({'class', 'style', 'width', 'height', 'align', 'valign', 'bgcolor',
                           'border', 'cellpadding', 'cellspacing', 'frame', 'rules', 'nowrap'})
_IDREFS = frozenset({'headers', 'for', 'list', 'aria-labelledby', 'aria-describedby', 'aria-details',
                     'aria-controls', 'aria-owns', 'aria-flowto', 'aria-activedescendant', 'aria-errormessage'})


def _source_attribute(name):
    return name in {'id', 'class', 'style'} or re.fullmatch(r'data-[a-z0-9_.:-]+', name)


def _referenced_ids(soup):
    result = set()
    for node in soup.find_all(True):
        for name, value in node.attrs.items():
            if name in _IDREFS:
                result.update(value if isinstance(value, list) else value.split())
            elif name in {'href', 'xlink:href'} and value.startswith('#'):
                result.add(value[1:])
            elif isinstance(value, str) and 'url(#' in value:
                result.update(re.findall(r'url\(#([^)]+)\)', value))
    return result


def _remove_layout(soup, referenced):
    for style in soup.find_all('style'):
        if not style.find_parent(['svg', 'math']):
            style.decompose()
    for wrapper in soup.select('div.table-scroll'):
        if set(wrapper.attrs) <= _PRESENTATION and wrapper.find('table', recursive=False):
            wrapper.unwrap()
    for table in soup.find_all('table'):
        # HTML associates scope=colgroup with the actual colgroup boundaries.
        groups = table.find_all(['colgroup', 'col'], recursive=False)
        if not groups or not table.find(['th', 'td']):
            continue
        if any(th.find_parent('table') is table for th in table.find_all('th', scope='colgroup')):
            continue
        for group in groups:
            columns = [group, *group.find_all(True)]
            if all(col.name in {'colgroup', 'col'} and col.get('id') not in referenced
                   and all(key in _PRESENTATION or key == 'id' for key in col.attrs) for col in columns):
                group.decompose()


def compact_quality_html(soup, *, sources=None, referenced=None):
    """Remove export decoration in place. Record private attributes on the final tag order."""
    if referenced is None:
        referenced = _referenced_ids(soup)
    _remove_layout(soup, referenced)
    for index, node in enumerate(soup.find_all(True)):
        foreign = node.name in {'svg', 'math'} or node.find_parent(['svg', 'math']) is not None
        private = {}
        for name, value in list(node.attrs.items()):
            if name in {'data-formula', 'data-latex', 'data-tex'} and node.name in {'math', 'span', 'div'}:
                continue
            if (name.startswith('data-') or (not foreign and name in _PRESENTATION)
                    or (name == 'id' and not foreign and value not in referenced)
                    or (name == 'alt' and not str(value).strip())):
                del node.attrs[name]
                if sources is not None and _source_attribute(name):
                    private[name] = value
        if private:
            sources.append([index, node.name, private])


def render_quality_html(soup, *, sources=None):
    view = copy(soup)
    compact_quality_html(view, sources=sources)
    return str(view)


def markdown_html_renderer(soup):
    from bs4 import BeautifulSoup

    # Fragments can link to a header, footnote or drawing in another fragment.
    referenced = _referenced_ids(soup)

    def render(node):
        fragment = BeautifulSoup('', 'html.parser')
        fragment.append(copy(node))
        compact_quality_html(fragment, referenced=referenced)
        return str(fragment)
    return render


def _source_digest(sources, html_sha256):
    encoded = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(html_sha256.encode('ascii') + b'\n' + encoded).hexdigest()


def source_manifest(sources, html_sha256):
    return {'attributes': sources, 'sha256': _source_digest(sources, html_sha256)}


def checked_sources(record, html_sha256):
    if not isinstance(record, dict) or not isinstance(record.get('attributes'), list):
        raise ValueError('В манифесте отсутствуют исходные привязки PDF')
    sources = record['attributes']
    if _source_digest(sources, html_sha256) != record.get('sha256'):
        raise ValueError('Исходные привязки PDF изменены после экспорта')
    return sources


def restore_quality_sources(soup, sources):
    """Restore only private metadata for serializers after the canonical hash was verified."""
    tags = soup.find_all(True)
    for record in sources:
        if not isinstance(record, list) or len(record) != 3:
            raise ValueError('Некорректная запись исходной привязки PDF')
        index, tag, attributes = record
        if (type(index) is not int or not 0 <= index < len(tags) or tags[index].name != tag
                or not isinstance(attributes, dict)
                or any(not _source_attribute(key)
                       or not (isinstance(value, str) or key == 'class' and isinstance(value, list)
                               and all(isinstance(item, str) for item in value))
                       for key, value in attributes.items())):
            raise ValueError('Исходная привязка PDF не соответствует каноническому HTML')
        tags[index].attrs.update(attributes)
