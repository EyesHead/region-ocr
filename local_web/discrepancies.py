"""Optional, persistent review of reported conversion differences; no OCR runs here.

Diagnostic causes stay in report_context().  The public review exposes generic
fragments, not instructions for users to repair the converter's table model.
"""
from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import threading
import unicodedata
from urllib.parse import quote
import uuid

from bs4 import BeautifulSoup

if __package__:
    from .file_paths import native_path, normal_path
else:
    from file_paths import native_path, normal_path

_LOCK = threading.RLock()
_PDF_LOCK = threading.RLock()
_DIGESTS = OrderedDict()
_SNAPSHOTS = OrderedDict()
_MAX_METADATA_BYTES = 128 * 1024 * 1024
_MAX_CACHE = 64
_SCHEMA = 3
_REFERENCE = re.compile(r'^#/[A-Za-z_]+/\d+$')
_STALE = 'Результат изменился. Откройте сравнение заново.'


def _root(value):
    return normal_path(value).resolve()


def _owned(root, value, *, exists=True):
    if value is None:
        return None
    path = normal_path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('Путь сравнения выходит за пределы задания')
    if exists and not native_path(path).is_file():
        return None
    return path


def _signature(path):
    if path is None:
        return None
    stat = native_path(path).stat()
    return str(path), stat.st_size, stat.st_mtime_ns, getattr(stat, 'st_ino', 0)


def _digest(path):
    signature = _signature(path)
    if signature is None:
        return None
    with _LOCK:
        if signature in _DIGESTS:
            _DIGESTS.move_to_end(signature)
            return _DIGESTS[signature]
    value = sha256()
    with native_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    with _LOCK:
        _DIGESTS[signature] = value.hexdigest()
        while len(_DIGESTS) > _MAX_CACHE:
            _DIGESTS.popitem(last=False)
    return value.hexdigest()


def _pdf_source(root, job):
    for value in (job.get('source'), job.get('source_pdf')):
        path = _owned(root, value)
        if path is not None and path.suffix.lower() == '.pdf':
            return path
    choices = []
    for folder in (root / 'result' / 'engine' / 'input', root / 'result' / 'input'):
        if native_path(folder).is_dir():
            for path in native_path(folder).glob('*.pdf'):
                candidate = _owned(root, path)
                if candidate is not None:
                    choices.append(candidate)
    return choices[0] if len(set(choices)) == 1 else None


def _diagnostic_files(root):
    result = set()
    for folder in (root / 'result' / 'engine' / 'diagnostics', root / 'result' / 'diagnostics', root / 'diagnostics'):
        if native_path(folder).is_dir():
            for path in native_path(folder).rglob('quality.json'):
                candidate = _owned(root, path)
                if candidate is not None:
                    result.add(candidate)
    for folder in (root / 'result' / 'engine' / 'html', root / 'result' / 'html', root / 'result'):
        if native_path(folder).is_dir():
            for path in native_path(folder).glob('*.report.json'):
                candidate = _owned(root, path)
                if candidate is not None:
                    result.add(candidate)
    # Some export failures have a single diagnostic report rather than quality.json.
    for value in (root / 'result' / 'report.json', root / 'result' / 'engine' / 'report.json'):
        candidate = _owned(root, value)
        if candidate is not None:
            result.add(candidate)
    return sorted(result, key=lambda path: str(path))


def _provenance_files(root, diagnostics):
    result = set()
    for diagnostic in diagnostics:
        if diagnostic.name != 'quality.json':
            continue
        try:
            path = _owned(root, diagnostic.parent / 'docling_before_export.json')
        except ValueError:
            continue  # Optional provenance never follows an external pointer.
        if path is not None and native_path(path).stat().st_size <= _MAX_METADATA_BYTES:
            result.add(path)
    return sorted(result, key=str)


def _provenance_texts(files, refs):
    result = {}
    for path in files:
        try:
            document = _read_json(path)
        except ValueError:
            continue  # A real-document fallback remains available without provenance.
        for text in document.get('texts', []) if isinstance(document.get('texts'), list) else []:
            if not isinstance(text, dict) or text.get('self_ref') not in refs or not isinstance(text.get('text'), str):
                continue
            pages = {_page(prov.get('page_no')) for prov in _values(text.get('prov')) if isinstance(prov, dict)} - {None}
            record = {'text': text['text'], 'pages': pages}
            old = result.get(text['self_ref'])
            if old is not None and old != record:
                result[text['self_ref']] = None  # Ambiguous snapshots cannot prove a match.
            elif text['self_ref'] not in result:
                result[text['self_ref']] = record
    return result


def _text_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def _exact_text_index(soup):
    result = {}
    for tag in soup.find_all(['p', 'figcaption', 'caption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li']):
        key = _text_key(tag.get_text(' ', strip=True))
        if key:
            result.setdefault(key, []).append(tag)
    # Nested figcaption/p are a single occurrence; repeated captions elsewhere are not.
    for key, tags in result.items():
        result[key] = [tag for tag in tags if not any(other is not tag and any(parent is tag for parent in other.parents) for other in tags)]
    return result


def _read_json(path):
    if native_path(path).stat().st_size > _MAX_METADATA_BYTES:
        raise ValueError('Диагностика слишком велика для безопасного сравнения')
    try:
        value = json.loads(native_path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise ValueError('Не удалось прочитать диагностику сравнения') from error
    if not isinstance(value, dict):
        raise ValueError('Некорректный формат диагностики сравнения')
    return value


def _page(value):
    return value if type(value) is int and value > 0 else None


def _bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(type(number) not in (int, float) or not math.isfinite(number) for number in value):
        return None
    left, top, right, bottom = value
    return list(value) if right > left and bottom > top and left >= 0 and top >= 0 else None


def _ref(value):
    return value if isinstance(value, str) and _REFERENCE.fullmatch(value) else None


def _final_ref(ref, entries):
    seen = set()
    while ref and entries.get(ref, {}).get('joined_into'):
        if ref in seen:
            raise ValueError('Циклическая связь фрагментов таблицы')
        seen.add(ref)
        ref = _ref(entries[ref]['joined_into'])
    return ref


def _combine_boxes(boxes):
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _merge_regions(regions):
    merged = {}
    for region in regions:
        page, box = region['page'], region['bbox']
        if page not in merged:
            merged[page] = box
        elif merged[page] is not None:
            merged[page] = _combine_boxes([merged[page], box]) if box is not None else None
    return [{'page': page, 'bbox': box} for page, box in sorted(merged.items())]


def _normalized_box(box, page, document, entry):
    """Ownership records use normalized coordinates, even source_bboxes of notes."""
    box = _bbox(box)
    if box is None:
        return None
    pages = document.get('pages')
    info = pages.get(str(page), pages.get(page)) if isinstance(pages, dict) else None
    if isinstance(info, dict):
        rotation, size = info.get('clockwise'), info.get('original_size')
        if rotation == 0:
            return box
        if rotation in (90, 180, 270) and isinstance(size, (list, tuple)) and len(size) == 2:
            width, height = size
            if all(type(number) in (int, float) and math.isfinite(number) and number > 0 for number in size):
                points = []
                for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])):
                    points.append((y, height-x) if rotation == 90 else
                                  (width-x, height-y) if rotation == 180 else (width-y, x))
                return _bbox([min(p[0] for p in points), min(p[1] for p in points),
                              max(p[0] for p in points), max(p[1] for p in points)])
    if entry and (not entry.get('source_bbox') or entry.get('source_bbox') == entry.get('bbox')):
        return box
    # Unknown orientation must not turn a normalized box into a wrong source crop.
    return None


def _ownership_regions(documents, entries):
    result = {}
    categories = {
        'source_caption_ownership_repairs': ('caption', ('caption_bbox',)),
        'recovered_table_titles': ('caption', ('bbox',)),
        'recovered_table_captions': ('caption', ('bbox',)),
        'recovered_source_notes': ('note', ('bbox',)),
        'primary_table_note_repairs': ('note', ('source_bbox',)),
        'verified_table_footnotes': ('note', ('bbox',)),
        'published_source_footnotes': ('note', ('bbox',)),
    }
    for document in documents:
        for category, (kind, keys) in categories.items():
            for record in _values(document.get(category)):
                if not isinstance(record, dict):
                    continue
                owner = _ref(record.get('output_table')) or _ref(record.get('table')) or _ref(record.get('ref'))
                if not owner or not owner.startswith('#/tables/'):
                    continue
                owner = _final_ref(owner, entries)
                related = [entry for ref, entry in entries.items() if _final_ref(ref, entries) == owner]
                pages = sorted({_page(p) for p in _values(record.get('pages')) if _page(p)})
                if _page(record.get('page')):
                    pages = [record['page']]
                if not pages:
                    known = sorted({_page(entry.get('page')) for entry in related if _page(entry.get('page'))})
                    pages = known[:1] if kind == 'caption' else known[-1:]
                for page in pages:
                    entry = next((entry for entry in related if entry.get('page') == page), None)
                    boxes = [_normalized_box(record.get(key), page, document, entry) for key in keys]
                    if category == 'primary_table_note_repairs':
                        boxes += [_normalized_box(box, page, document, entry) for box in _values(record.get('source_bboxes'))]
                    result.setdefault(owner, []).append({'page': page, 'bbox': _combine_boxes(boxes), 'kind': kind})
    return result


def _table_regions(ref, final_ref, issue, entries, ownership):
    related = [entry for entry_ref, entry in entries.items()
               if _final_ref(entry_ref, entries) == final_ref] if final_ref else []
    if not related and isinstance(issue, dict):
        related = [issue]
    regions = []
    for entry in related:
        page = _page(entry.get('page'))
        # source_bbox is mapped back from normalized orientation to the original PDF.
        box = _bbox(entry.get('source_bbox')) or _bbox(entry.get('bbox'))
        if page is None:
            continue
        # Preserve explicitly recorded full-table title and note regions as well.
        extras = [_bbox(entry.get(key)) for key in ('source_caption_bbox', 'source_note_bbox')]
        if entry.get('source_bbox') == entry.get('bbox') or not entry.get('source_bbox'):
            extras += [_bbox(entry.get('caption_bbox')), _bbox(entry.get('source_header_bbox'))]
        box = _combine_boxes([box, *extras])
        regions.append({'page': page, 'bbox': box})
    known_pages = {region['page'] for region in regions}
    for entry in related:
        for page in _values(entry.get('source_pages')):
            if _page(page) and page not in known_pages:
                regions.append({'page': page, 'bbox': None})
                known_pages.add(page)
    regions += ownership.get(final_ref, [])
    return _merge_regions(regions)


def _values(value):
    """Accept a scalar warning as one warning, never as its individual letters."""
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None and item != '']
    if isinstance(value, (str, dict)) and value:
        return [value]
    return []


def _clean_reason(value):
    if isinstance(value, dict):
        return str(value.get('reason') or value.get('code') or value.get('issue') or 'diagnostic_warning')[:500]
    return str(value)[:500]


def _build(root, job, source, canonical, files, revision, provenance_files=()):
    documents = [_read_json(path) for path in files]
    entries = {}
    for document in documents:
        for kind in ('tables', 'formulas', 'pictures', 'texts'):
            for entry in document.get(kind, []) if isinstance(document.get(kind, []), list) else []:
                if isinstance(entry, dict) and _ref(entry.get('ref')):
                    entries.setdefault(entry['ref'], {}).update(entry)
    for document in documents:
        for link in _values(document.get('joined_tables')):
            if not isinstance(link, dict):
                continue
            child, owner = _ref(link.get('from')), _ref(link.get('into'))
            if child and owner and child.startswith('#/tables/') and owner.startswith('#/tables/'):
                entry = entries.setdefault(child, {'ref': child})
                if entry.get('joined_into') and entry['joined_into'] != owner:
                    raise ValueError('Противоречивая связь фрагментов таблицы')
                entry['joined_into'] = owner
    ownership = _ownership_regions(documents, entries)
    flagged = []
    for document in documents:
        for category, values in document.items():
            if not (category.endswith(('_warnings', '_issues', '_reviews')) or category in ('warnings', 'blocking_issues')):
                continue
            for value in _values(values):
                if isinstance(value, dict) and (value.get('resolved') is True or value.get('severity') in ('info', 'information')):
                    continue
                record = dict(value) if isinstance(value, dict) else {'reason': _clean_reason(value)}
                flagged.append((category, record))
        for table in document.get('tables', []) if isinstance(document.get('tables', []), list) else []:
            if not isinstance(table, dict):
                continue
            profile = table.get('selected') if isinstance(table.get('selected'), dict) else {}
            for cause in [*_values(profile.get('issues')), *_values(profile.get('warnings')), *_values(table.get('fallback_reason'))]:
                flagged.append(('table_profile', dict(table, reason=_clean_reason(cause))))
            if table.get('numeric_disagreement'):
                flagged.append(('table_profile', dict(table, reason='numeric_disagreement')))
    items = OrderedDict()
    for category, issue in flagged:
        reason = _clean_reason(issue)
        # Old snapshots also contain this optional figure-regrouping notice.
        # An unproven caption owner does not establish a conversion discrepancy.
        if reason == 'figure_owner_missing_or_ambiguous':
            continue
        ref = _ref(issue.get('ref')) or _ref(issue.get('element')) or _ref(issue.get('table_ref')) or _ref(issue.get('table'))
        final_ref = _ref(issue.get('final_ref')) or _final_ref(ref, entries)
        entry = entries.get(ref, {})
        page = _page(issue.get('page')) or _page(entry.get('page'))
        kind = 'table' if (final_ref or ref or '').startswith('#/tables/') else 'fragment'
        # The machine report repeats source_grid_reviews as table_warnings.  A
        # table is one visual candidate, with all distinct causes attached.
        identity = ('table', final_ref) if kind == 'table' and final_ref else (kind, ref, page, reason)
        key = sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
        if key not in items:
            regions = _table_regions(ref, final_ref, issue, entries, ownership)
            if not regions and page is not None:
                box = _bbox(issue.get('source_bbox')) or _bbox(issue.get('bbox')) or _bbox(entry.get('source_bbox')) or _bbox(entry.get('bbox'))
                regions = [{'page': page, 'bbox': box}]  # None explicitly means the entire known source page.
            pages = sorted({region['page'] for region in regions} or ({page} if page else set()))
            items[key] = {'id': key, 'kind': kind, 'ref': ref, 'final_ref': final_ref,
                          'page': pages[0] if pages else None, 'pages': pages,
                          'source_regions': regions, 'reasons': [], 'bbox': regions[0]['bbox'] if regions else None,
                          '_locator_refs': []}
        # A caption warning identifies its source text, not necessarily a table.
        # Keep the candidate identity stable while adding display-only provenance.
        for name in ('caption_ref', 'text_ref', 'picture_ref'):
            locator = _ref(issue.get(name))
            if locator and locator not in items[key]['_locator_refs']:
                items[key]['_locator_refs'].append(locator)
        cause = {'code': reason, 'category': category}
        if not any(existing['code'] == reason for existing in items[key]['reasons']):
            items[key]['reasons'].append(cause)
    ordered = sorted(items.values(), key=lambda item: (item['page'] or 10**9, str(item['final_ref'] or item['ref'] or ''), item['id']))
    html = native_path(canonical).read_text(encoding='utf-8-sig') if canonical is not None else ''
    soup = BeautifulSoup(html, 'html.parser')
    locator_refs = {ref for item in ordered for ref in item['_locator_refs']}
    provenance = _provenance_texts(provenance_files, locator_refs) if locator_refs else {}
    text_index = _exact_text_index(soup) if provenance else {}
    node_indices = {id(tag): index for index, tag in enumerate(soup.find_all(True))}
    for index, item in enumerate(ordered, 1):
        item['label'] = f'Фрагмент {index}'
        item['source_available'] = source is not None and bool(item['source_regions'])
        fragment = _find_element(soup, item)
        item['result_available'] = bool(html.strip())
        item['result_scope'] = 'fragment' if fragment is not None else 'document'
        item['result_notice'] = '' if fragment is not None else 'Показан весь результат. Точные границы фрагмента не определены.'
        item['result_anchor'] = None
        item['_result_targets'] = []
        if fragment is None:
            for ref in item['_locator_refs']:
                record = provenance.get(ref)
                if not record or not record['pages'] or not (record['pages'] & set(item['pages'])):
                    continue
                key = _text_key(BeautifulSoup(record['text'], 'html.parser').get_text(' ', strip=True))
                matches = text_index.get(key, [])
                if len(matches) == 1:
                    index = node_indices[id(matches[0])]
                    if index not in item['_result_targets']:
                        item['_result_targets'].append(index)
            if item['_result_targets']:
                item['result_anchor'] = 'region-review-target'
                item['result_notice'] = 'Показан весь результат. Подписи из исходной страницы отмечены; точные границы фрагмента не определены.'
        if fragment is not None and item['kind'] == 'table' and item['source_regions']:
            # The table's own bbox excludes external captions/notes.  If their
            # ownership is known but geometry is absent, retain a whole source
            # page rather than silently cut those parts out of the comparison.
            related = [entry for ref, entry in entries.items() if _final_ref(ref, entries) == item['final_ref']]
            covered = {region['kind'] for region in ownership.get(item['final_ref'], [])}
            if any(_bbox(entry.get('source_caption_bbox')) or
                   (_bbox(entry.get('caption_bbox')) and (not entry.get('source_bbox') or entry.get('source_bbox') == entry.get('bbox')))
                   for entry in related):
                covered.add('caption')
            if any(_bbox(entry.get('source_note_bbox')) for entry in related):
                covered.add('note')
            if fragment.find('caption') is not None and 'caption' not in covered:
                item['source_regions'][0]['bbox'] = None
            if (_note_siblings(fragment) or fragment.select('[data-primary-table-notes], .source-table-note')) and 'note' not in covered:
                item['source_regions'][-1]['bbox'] = None
            item['bbox'] = item['source_regions'][0]['bbox']
    return {'revision': revision, 'items': ordered, '_source': source, '_canonical': canonical}


def _snapshot(root, job):
    source = _pdf_source(root, job)
    canonical = _owned(root, job.get('replay_html'))
    if canonical is None and isinstance(job.get('artifacts'), dict):
        canonical = _owned(root, job['artifacts'].get('html'))
    files = _diagnostic_files(root)
    provenance_files = _provenance_files(root, files)
    content_key = [('source', _digest(source)), ('html', _digest(canonical)),
                   ('attempt', job.get('attempt_id') or job.get('started_at'))]
    content_key.extend((str(path.relative_to(root)), _digest(path)) for path in files)
    revision = sha256(json.dumps([_SCHEMA, content_key], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
    # Provenance only changes navigation, not source/result/diagnostic decisions.
    key = str(root), revision, tuple(_signature(path) for path in provenance_files)
    with _LOCK:
        if key in _SNAPSHOTS:
            _SNAPSHOTS.move_to_end(key)
            return _SNAPSHOTS[key]
    value = _build(root, job, source, canonical, files, revision, provenance_files)
    with _LOCK:
        _SNAPSHOTS[key] = value
        while len(_SNAPSHOTS) > _MAX_CACHE:
            _SNAPSHOTS.popitem(last=False)
    return value


def _state(root, snapshot):
    path = _owned(root, root / 'review' / 'state.json')
    if path is None:
        return {}
    try:
        state = _read_json(path)
    except ValueError:
        return {}  # Corrupt decisions never become false confirmations.
    if state.get('revision') != snapshot['revision']:
        return {}
    return state.get('decisions', {}) if isinstance(state.get('decisions'), dict) else {}


def _public(snapshot, decisions):
    items = []
    for item in snapshot['items']:
        decision = decisions.get(item['id'], {})
        value = decision.get('decision') if isinstance(decision, dict) else None
        items.append({key: item[key] for key in ('id', 'page', 'pages', 'label', 'source_available', 'result_available',
                                               'result_scope', 'result_notice', 'result_anchor')} |
                     {'decision': value if value in ('error', 'correct') else None,
                      'comment': str(decision.get('comment', ''))[:3000] if isinstance(decision, dict) else '',
                      'source_regions_count': len(item['source_regions'])})
    return {'revision': snapshot['revision'], 'items': items, 'total': len(items),
            'reviewed_count': sum(item['decision'] is not None for item in items),
            'error_count': sum(item['decision'] == 'error' for item in items)}


def get_review(job_root, job):
    root = _root(job_root)
    snapshot = _snapshot(root, job)
    with _LOCK:
        return _public(snapshot, _state(root, snapshot))


def review_summary(job_root, job):
    return {key: value for key, value in get_review(job_root, job).items() if key != 'items'}


def _item(root, job, item_id, revision):
    if not isinstance(item_id, str) or not re.fullmatch(r'[0-9a-f]{24}', item_id):
        raise KeyError('Фрагмент сравнения не найден')
    snapshot = _snapshot(root, job)
    if revision != snapshot['revision']:
        raise ValueError(_STALE)
    item = next((item for item in snapshot['items'] if item['id'] == item_id), None)
    if item is None:
        raise KeyError('Фрагмент сравнения не найден')
    return snapshot, item


def set_decision(job_root, job, item_id, decision, revision, comment=''):
    if decision not in ('error', 'correct', None):
        raise ValueError('Выберите «Есть ошибка» или «Всё верно»')
    if not isinstance(comment, str) or len(comment) > 3000:
        raise ValueError('Комментарий слишком длинный')
    root = _root(job_root)
    with _LOCK:
        snapshot, item = _item(root, job, item_id, revision)
        decisions = _state(root, snapshot)
        decisions[item['id']] = {'decision': decision, 'comment': comment}
        folder = _owned(root, root / 'review', exists=False)
        native_path(folder).mkdir(parents=True, exist_ok=True)
        target = _owned(root, folder / 'state.json', exists=False)
        temporary = _owned(root, folder / (uuid.uuid4().hex + '.tmp'), exists=False)
        native_path(temporary).write_text(json.dumps({'schema': _SCHEMA, 'revision': revision, 'decisions': decisions}, ensure_ascii=False), encoding='utf-8')
        native_path(temporary).replace(native_path(target))
        return _public(snapshot, decisions)


def _asset_path(root, snapshot, item, region_index):
    return _owned(root, root / 'review' / 'assets' / snapshot['revision'] / f'{item["id"]}-{region_index}.png', exists=False)


def source_image(job_root, job, item_id, region_index, revision):
    root = _root(job_root)
    snapshot, item = _item(root, job, item_id, revision)
    if type(region_index) is not int or not 0 <= region_index < len(item['source_regions']):
        raise KeyError('Снимок исходного фрагмента не найден')
    if snapshot['_source'] is None:
        raise FileNotFoundError('Исходный PDF для сравнения недоступен')
    target = _asset_path(root, snapshot, item, region_index)
    with _PDF_LOCK:
        if native_path(target).is_file():
            return target
        import pypdfium2 as pdfium
        region = item['source_regions'][region_index]
        pdf = pdfium.PdfDocument(str(native_path(snapshot['_source'])))
        try:
            if region['page'] > len(pdf):
                raise FileNotFoundError('Страница исходного PDF недоступна')
            page = pdf[region['page'] - 1]
            try:
                width, height = page.get_size()
                box = region['bbox'] or [0, 0, width, height]
                left, top, right, bottom = box
                # A small margin retains antialiasing at outer rules, never a cell crop.
                left, top, right, bottom = max(0, left-3), max(0, top-3), min(width, right+3), min(height, bottom+3)
                if right <= left or bottom <= top:
                    raise ValueError('Некорректная область исходного фрагмента')
                scale = min(2.0, math.sqrt(12_000_000 / ((right-left)*(bottom-top))))
                bitmap = page.render(scale=scale, crop=(left, height-bottom, width-right, top))
                try:
                    image = bitmap.to_pil()
                    try:
                        native_path(target.parent).mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(target.stem + '-' + uuid.uuid4().hex + '.tmp')
                        image.save(str(native_path(temporary)), format='PNG')
                        native_path(temporary).replace(native_path(target))
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            pdf.close()
    return target


def _find_element(soup, item):
    ref = item.get('final_ref') or item.get('ref')
    if ref:
        for attribute in ('data-pdf-table-ref', 'data-pdf-text-ref', 'data-pdf-figure-ref', 'data-pdf-formula-ref',
                          'data-docling-ref', 'data-self-ref', 'data-source-footnote'):
            found = soup.find(attrs={attribute: ref})
            if found is not None:
                return found
    return None


def _document_styles(soup):
    """Retain canonical CSS only when it cannot fetch resources or run legacy code."""
    styles = []
    total = 0
    for tag in soup.find_all('style'):
        value = re.sub(r'/\*.*?\*/', '', tag.get_text(), flags=re.S)
        total += len(value)
        if total > 256 * 1024:
            break
        if (re.search(r'@(?!page\b|media\b)|[\\<>]|(?:url|expression)\s*\(|(?:javascript|vbscript|data|https?)\s*:|(?:-moz-binding|behavior)\s*:', value, re.I)
                or any(ord(character) < 32 and character not in '\r\n\t' for character in value)):
            continue
        styles.append(value)
    return '\n'.join(styles)


def _safe_fragment(fragment, job_id):
    soup = BeautifulSoup(str(fragment), 'html.parser')
    for tag in soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'link', 'meta', 'base', 'audio', 'video', 'svg']):
        tag.decompose()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            if attribute.lower().startswith('on') or attribute.lower() not in {
                'rowspan', 'colspan', 'scope', 'headers', 'id', 'class', 'alt', 'title',
                'src', 'width', 'height', 'style', 'display', 'xmlns', 'mathvariant', 'data-latex', 'data-pdf-table-ref'}:
                del tag.attrs[attribute]
        if tag.has_attr('style'):
            declarations = []
            for declaration in str(tag['style']).split(';'):
                parts = declaration.split(':', 1)
                if len(parts) == 2 and parts[0].strip().lower() in ('width', 'min-width', 'text-align', 'vertical-align'):
                    value = parts[1].strip()
                    if re.fullmatch(r'[0-9.]+(?:%|px|em|rem)?|left|right|center|top|middle|bottom|auto', value):
                        declarations.append(parts[0].strip() + ':' + value)
            if declarations:
                tag['style'] = ';'.join(declarations)
            else:
                del tag.attrs['style']
        if tag.has_attr('src'):
            value = str(tag['src'])
            match = re.fullmatch(r'(?:\./)?src/([A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp|gif|bmp|tif|tiff))', value, re.I)
            if match and isinstance(job_id, str) and re.fullmatch(r'[0-9a-f]{32}', job_id):
                tag['src'] = '/api/assets/' + job_id + '/' + quote(match[1])
            else:
                del tag.attrs['src']
    return str(soup)


def _note_siblings(fragment):
    anchor = fragment
    parent = fragment.parent
    if parent and set(parent.get('class', [])) & {'table-scroll', 'table-container', 'table-wrapper'}:
        anchor = parent
    notes = []
    sibling = anchor.find_next_sibling()
    note_heading = False
    while sibling:
        note = (sibling.get('role') == 'note' or sibling.has_attr('data-table-note')
                or any('note' in name for name in sibling.get('class', [])))
        heading = (sibling.name in ('p', 'h2', 'h3', 'h4')
                   and re.fullmatch(r'Примечани[ея]\s*[:—-]?', sibling.get_text(' ', strip=True), re.I))
        if not (note or heading or (note_heading and sibling.name in ('ol', 'ul'))):
            break
        notes.append(sibling)
        note_heading = bool(heading)
        sibling = sibling.find_next_sibling()
    return notes


def _with_notes(fragment):
    parent = fragment.parent
    selected = parent if parent and set(parent.get('class', [])) & {'table-scroll', 'table-container', 'table-wrapper'} else fragment
    return str(selected) + ''.join(str(note) for note in _note_siblings(fragment))


def result_html(job_root, job, item_id, revision):
    root = _root(job_root)
    snapshot, item = _item(root, job, item_id, revision)
    if snapshot['_canonical'] is None:
        raise FileNotFoundError('HTML результата для сравнения недоступен')
    soup = BeautifulSoup(native_path(snapshot['_canonical']).read_text(encoding='utf-8-sig'), 'html.parser')
    fragment = _find_element(soup, item)
    styles = _document_styles(soup)
    if fragment is not None:
        body = _safe_fragment(_with_notes(fragment), job.get('id'))
    else:
        tags = soup.find_all(True)
        # Reserved markers cannot be supplied by the input document itself.
        for tag in tags:
            if str(tag.get('id', '')).startswith('region-review-target'):
                del tag['id']
        for index, target in enumerate(item.get('_result_targets', [])):
            if type(target) is int and 0 <= target < len(tags):
                tags[target]['id'] = 'region-review-target' + (f'-{index+1}' if index else '')
        content = soup.body or soup
        body = _safe_fragment(content.decode_contents(), job.get('id'))
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + \
        '<title>Результат конвертации</title><style>body{margin:0;padding:16px;font:14px/1.5 Arial,sans-serif;color:#17243d;background:white}' + \
        'table{border-collapse:collapse;width:max-content;min-width:100%;max-width:none;table-layout:auto;font-size:14px}td,th{border:1px solid #aab6c7;padding:6px;overflow-wrap:normal;word-break:normal;vertical-align:top}' + \
        'th{background:#f3f6fa}caption{text-align:left;font-weight:600;margin-bottom:12px}img{max-width:100%;height:auto}math{overflow-wrap:anywhere}' + styles + \
        'body{margin:0;padding:16px;max-width:none}[id^="region-review-target"]{outline:2px solid #3371dc;outline-offset:5px;background:#eef4ff;scroll-margin-top:16px}</style><body>' + body + '</body></html>'


def report_context(job_root, job, revision=None):
    root = _root(job_root)
    snapshot = _snapshot(root, job)
    if revision is not None and revision != snapshot['revision']:
        raise ValueError(_STALE)
    with _LOCK:
        public = _public(snapshot, _state(root, snapshot))
    context = dict(public)
    context['items'] = []
    for item, visible in zip(snapshot['items'], public['items']):
        value = json.loads(json.dumps({key: value for key, value in item.items() if not key.startswith('_')}, ensure_ascii=False))
        value.update(decision=visible['decision'], comment=visible['comment'])
        value['source_images'] = [str(path) for index in range(len(item['source_regions']))
                                  if native_path(path := _asset_path(root, snapshot, item, index)).is_file()]
        context['items'].append(value)
    return context
