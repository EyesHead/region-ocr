"""Nonblocking table diagnostics and source previews for manual review."""
from hashlib import sha256
import json
from pathlib import Path

from bs4 import BeautifulSoup


def primary_fingerprint(profile):
    cells = [(c['row'], c['column'], c['row_span'], c['column_span'], c['header'], c['html'])
             for c in profile.get('cells', [])]
    return sha256(repr(cells).encode('utf-8')).hexdigest()


def register_primary_grid_review(content, image, bbox, page, ref, manifest, api):
    """Record the exact accepted primary candidate and its source pixels."""
    profile = api.table_profile(content)
    warning = dict(page=page, ref=ref, reason='primary_table_grid_cannot_be_mapped_to_source_cells',
        method='primary_surya_table_preserved', primary_fingerprint=primary_fingerprint(profile),
        bbox=list(bbox), primary_profile_issues=profile['issues'])
    if image is not None:
        copy = image.copy()
        try:
            warning['source_preview_html'] = api.image_html(copy, bbox[2]-bbox[0], 'table source')
        except (OSError, ValueError) as error:
            warning['source_preview_warning'] = str(error)
        finally:
            copy.close()
    manifest.setdefault('primary_structure_warnings', []).append(warning)
    api.diagnostic_event('TABLE_PRIMARY_GRID_FALLBACK', page=page, ref=ref,
                         reason=warning['reason'], fingerprint=warning['primary_fingerprint'])
    return warning


def matching_primary_review(profile, page, ref, manifest):
    if not manifest.get('primary_ocr_only') or profile['issues']:
        return None
    fingerprint = primary_fingerprint(profile)
    matches = [p for p in manifest.get('primary_structure_warnings', [])
               if p.get('page') == page and p.get('ref') == ref
               and p.get('method') == 'primary_surya_table_preserved'
               and p.get('primary_fingerprint') == fingerprint
               and BeautifulSoup(p.get('source_preview_html', ''), 'html.parser').find('img')]
    return matches[0] if len(matches) == 1 else None


def annotate_primary_reviews(soup, manifest, api):
    """Keep ownership and review status in diagnostics, outside the document."""
    strip_review_annotations(soup)
    reviews = manifest.get('source_grid_reviews', [])
    if not reviews:
        return
    entries = {t['ref']: t for t in manifest.get('tables', [])}
    for index, review in enumerate(reviews, 1):
        ref, seen = review['ref'], set()
        while entries.get(ref, {}).get('joined_into'):
            if ref in seen:
                review['annotation_warning'] = 'cyclic_table_ownership'
                ref = None
                break
            seen.add(ref)
            ref = entries[ref]['joined_into']
        table = soup.find('table', attrs={'data-pdf-table-ref': ref}) if ref else None
        review.pop('anchor', None)
        review['final_ref'] = ref
        if table is None:
            review.setdefault('annotation_warning', 'final_table_not_located')
    manifest['publication_status'] = 'REVIEW'
    api.diagnostic_event('PRIMARY_GRID_REVIEW_RECORDED', fragments=len(reviews), publication='diagnostics_only')


def strip_review_annotations(soup):
    """Remove only converter-owned notices, including their comparison crops."""
    from local_web.structured_export import strip_review_annotations as strip
    return strip(soup)


def write_quality_report(output, source, quality, api):
    if quality.get('deferred_math'):
        raise RuntimeError('Deferred standalone math cannot be published')
    output = Path(output)
    reviews = quality.get('source_grid_reviews', [])
    warnings = quality.get('table_quality_warnings', quality.get('table_content_issues', []))
    status = 'REVIEW' if reviews or warnings else 'OK'
    report = output.with_suffix('.report.md')
    machine = output.with_suffix('.report.json')
    clean = [{k: v for k, v in review.items() if k != 'source_preview_html'} for review in reviews]
    data = dict(status=status, source=str(source), output=str(output), review_required=bool(reviews or warnings),
                source_grid_reviews=clean, table_warnings=warnings, blocking_issues=[],
                table_checks_policy='diagnostic_only', primary_ocr_only=quality.get('primary_ocr_only'),
                table_cell_schema=quality.get('table_cell_schema'))
    lines = ['# Отчёт конвертации', '', f'Статус: **{status}**.', '', f'Источник: {source}', '']
    if reviews:
        lines += ['Требуется ручная сверка отмеченных таблиц. Первичный текст Surya сохранён, '
                  'но его сетка не подтверждена границами исходника. При расхождении могут быть '
                  'неверные объединения, сдвиги и значения. Диагностика и исходные кропы '
                  'остаются в служебных артефактах и не добавляются в документ.', '',
                  '| Страница PDF | Фрагмент | Сетка источника | Сетка Surya | Пропущенные / дополнительные позиции |',
                  '|---:|---|---|---|---|']
        for review in reviews:
            link = output.name+('#'+review['anchor'] if review.get('anchor') else '')
            lines.append(f'| {review["page"]} | [{review["ref"]}](<{link}>) | '
                         f'{review["source_rows"]} × {review["source_columns"]} | '
                         f'{review["published_rows"]} × {review["published_columns"]} | '
                         f'{len(review["missing_cells"])} / {len(review["extra_cells"])} |')
    if warnings:
        lines += ['', 'Проверки таблиц носят диагностический характер и не отклоняют результат.', '']
        for warning in warnings:
            lines.append(f'- Страница {warning.get("page", "?")}, '
                         f'{warning.get("ref", warning.get("element", "?"))}: {warning.get("reason", "table_quality_warning")}')
    if not reviews and not warnings:
        lines += ['Предупреждений таблиц не найдено. Это не посимвольная проверка точности первичного OCR.']
    api.write_atomic(report, '\n'.join(lines)+'\n')
    api.write_atomic(machine, json.dumps(data, ensure_ascii=False, indent=2))
    fragments = {(r.get('page'), r.get('ref', r.get('element'))) for r in [*reviews, *warnings]}
    return dict(status=status, report=str(report), quality_report=str(machine), review_fragments=len(fragments))
