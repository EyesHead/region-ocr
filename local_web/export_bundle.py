"""Selected-format document folders and reusable, image-externalized replay."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import uuid

if __package__:
    from . import formula_preview, quality_html
    from .file_paths import native_path, normal_path
    from .fast_resources import FAST_PATH, FORMULA_CROP_ATTR, exact_record, formula_crop_paths
else:
    import formula_preview
    import quality_html
    from file_paths import native_path, normal_path
    from fast_resources import FAST_PATH, FORMULA_CROP_ATTR, exact_record, formula_crop_paths

EXPORT_SCHEMA = 14
CANONICAL_MANIFEST_SCHEMA = 1
FAST_PDF_STAT_KEYS = ('native_pages', 'hybrid_pages', 'ocr_pages', 'blank_pages', 'image_fallback_pages', 'ocr_error_pages',
                      'orientation_corrected_pages',
                      'vector_tables', 'scan_tables', 'formula_crops', 'formulas_mathml', 'formulas_latex', 'formulas_text')
EXTENSIONS = {'html': '.html', 'markdown': '.md', 'json': '.json', 'plaintext': '.txt'}
FOLDER_SUFFIXES = {'html': 'html', 'markdown': 'md', 'json': 'json', 'plaintext': 'text'}


def _components():
    if __package__:
        from . import assets, structured_export
    else:
        import assets
        import structured_export
    return assets, structured_export


def _document_stats(soup, pages, structured):
    tables = soup.body.find_all('table')
    complex_count = sum(structured.table_is_complex(table) for table in tables)
    return dict(pages=pages, tables=len(tables), simple_tables=len(tables) - complex_count,
                complex_tables=complex_count, formulas=len(soup.body.find_all('math')),
                images=len(soup.body.find_all('img')), characters=len(soup.body.get_text()))


def _asset_path(replay, value):
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'src':
        raise ValueError('Некорректный путь изображения в каноническом документе')
    origin = (replay / relative).resolve()
    if not origin.is_relative_to(replay.resolve()) or not native_path(origin).is_file():
        raise ValueError('Кроп изображения не найден: ' + relative.as_posix())
    return relative, origin


def _save_manifest(canonical, manifest):
    native_path(canonical.parent / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


def _check_assets(replay, manifest):
    for asset in manifest.get('assets', []):
        _, origin = _asset_path(replay, asset['path'])
        if hashlib.sha256(native_path(origin).read_bytes()).hexdigest() != asset['sha256']:
            raise ValueError('Канонический кроп изображения изменён после обработки')


def _fast_references(soup):
    for image in soup.find_all('img'):
        for path in (image.get('src', ''), image.get('data-original-src', ''),
                     *image.get('data-original-srcs', '').split()):
            if FAST_PATH.fullmatch(path):
                yield path
    yield from formula_crop_paths(soup)


def _replay_fast_assets(canonical, soup):
    return {path: native_path(_asset_path(canonical.parent, path)[1]).read_bytes()
            for path in set(_fast_references(soup))}


def _legacy_manifest(canonical, content_bytes, soup, pages, structured):
    """Index an existing external-asset canonical without rewriting any pixels."""
    from PIL import Image
    records, seen = [], set()
    paths = [str(image.get('src', '')) for image in soup.find_all('img')]
    for value in [*paths, *_fast_references(soup)]:
        if not value or value in seen:
            continue
        if value.lower().startswith('data:'):
            raise ValueError('Старый HTML со встроенными изображениями требует создания нового канонического документа перед повторным экспортом.')
        relative, origin = _asset_path(canonical.parent, value)
        data = native_path(origin).read_bytes()
        if FAST_PATH.fullmatch(value):
            records.append(exact_record(value, data))
            seen.add(value)
            continue
        with Image.open(native_path(origin)) as opened:
            width, height, image_format = opened.width, opened.height, opened.format
        records.append(dict(path=relative.as_posix(), sha256=hashlib.sha256(data).hexdigest(),
                            width=width, height=height, bytes=len(data), format=str(image_format).lower(),
                            encoding='existing-canonical-asset', lossless=True, resized=False))
        seen.add(value)
    manifest = dict(schema_version=CANONICAL_MANIFEST_SCHEMA, export_schema=EXPORT_SCHEMA,
                    html_sha256=hashlib.sha256(content_bytes).hexdigest(), assets=records,
                    warnings=[], stats=_document_stats(soup, pages, structured), legacy_reconstructed=True)
    _save_manifest(canonical, manifest)
    return manifest


def build_exports(content, source, destination, *, pages=None, output_format='html', resource_root=None,
                  replay_html=None, initial_warnings=(), initial_joins=(), fast_pdf_stats=None, fast_assets=None,
                  quality_pdf=False):
    """Create a canonical once, then derive packages without mutating that source."""
    from bs4 import BeautifulSoup
    from docling_pdf_annotations import normalize_semantic_annotations
    from docling_pdf_table_rasters import remove_published_table_rasters
    source, destination = normal_path(Path(source).resolve()), normal_path(Path(destination).resolve())
    quality_pdf = quality_pdf and source.suffix.lower() == '.pdf'
    pdf_engine_output = source.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff',
                                               '.bmp', '.webp', '.gif'}
    if output_format is not None and output_format not in EXTENSIONS:
        raise ValueError('Неизвестный формат результата: ' + str(output_format))
    assets, structured = _components()
    if __package__:
        from .table_continuations import join_captioned_tables
        from .table_star_notes import normalize_table_star_notes
    else:
        from table_continuations import join_captioned_tables
        from table_star_notes import normalize_table_star_notes
    if replay_html:
        canonical = normal_path(Path(replay_html).resolve())
        canonical_bytes = native_path(canonical).read_bytes()
        soup = BeautifulSoup(canonical_bytes.decode('utf-8'), 'html.parser')
        quality_snapshot = (source.suffix.lower() == '.pdf'
                            and soup.find('meta', attrs={'name': quality_html.SOURCE_MARKER, 'content': '1'}) is not None)
        manifest_path = native_path(canonical.parent / 'manifest.json')
        if manifest_path.is_file():
            if soup.body is None:
                raise ValueError('Канонический HTML не содержит body')
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if manifest.get('schema_version') != CANONICAL_MANIFEST_SCHEMA:
                raise ValueError('Неподдерживаемая версия манифеста канонического документа')
            if manifest.get('html_sha256') != hashlib.sha256(canonical_bytes).hexdigest():
                raise ValueError('Канонический HTML изменён после обработки')
        else:
            if quality_snapshot:
                raise ValueError('Отсутствует манифест с исходными привязками PDF')
            marker = soup.find('meta', attrs={'name': 'document-export-schema'})
            embedded = any(str(image.get('src', '')).lower().startswith('data:') for image in soup.find_all('img'))
            embedded = embedded or any(formula[FORMULA_CROP_ATTR].lower().startswith('data:')
                                       for formula in soup.find_all('math', attrs={FORMULA_CROP_ATTR: True}))
            if marker is None or marker.get('content') != str(EXPORT_SCHEMA) or embedded:
                # Old published HTML is input to a new canonical stage. Keep
                # that historical HTML intact; sanitize/migrate it only once.
                if __package__:
                    from .converter import _safe_html
                else:
                    from converter import _safe_html
                sanitized, legacy_warnings = _safe_html(canonical_bytes.decode('utf-8'))
                return build_exports(str(sanitized), source, destination, pages=pages,
                                     output_format=output_format, resource_root=canonical.parent,
                                     quality_pdf=quality_pdf,
                                     fast_assets=_replay_fast_assets(canonical, sanitized) or None,
                                     initial_warnings=list(initial_warnings) + legacy_warnings)
            if soup.body is None:
                raise ValueError('Канонический HTML не содержит body')
            manifest = _legacy_manifest(canonical, canonical_bytes, soup, pages, structured)
        if quality_snapshot:
            quality_pdf = True
            sources = quality_html.checked_sources(manifest.get('quality_pdf_sources'), manifest['html_sha256'])
            quality_html.restore_quality_sources(soup, sources)
        indexed = {asset['path']: asset['sha256'] for asset in manifest.get('assets', ())}
        for path in set(_fast_references(soup)):
            if indexed.get(path) != FAST_PATH.fullmatch(path).group(1):
                raise ValueError('Ресурс PDF не соответствует манифесту')
        _check_assets(canonical.parent, manifest)
        comparison_info = manifest.get('formula_preview')
        preview_sources = None
        if comparison_info:
            formula_preview.preview_file(canonical.parent, comparison_info)
            for digest in comparison_info['images']:
                formula_preview.preview_image(canonical.parent, comparison_info, digest)
            if quality_snapshot:
                preview_sources = quality_html.checked_sources(manifest.get('quality_pdf_preview_sources'),
                                                              comparison_info['html_sha256'])
        elif quality_snapshot and 'quality_pdf_preview_sources' in manifest:
            raise ValueError('В манифесте отсутствует предпросмотр с исходными привязками PDF')
        if manifest.get('export_schema') != EXPORT_SCHEMA or (quality_pdf and 'quality_pdf_sources' not in manifest):
            # Rebuild only the document layer in a new private canonical.
            # Historical packages/source bytes remain available unchanged.
            normalize_semantic_annotations(soup, {}, {})
            content = formula_preview.migration_content(canonical, comparison_info, soup, preview_sources=preview_sources)
            return build_exports(content, source, destination,
                                 quality_pdf=quality_pdf,
                                 pages=manifest['stats'].get('pages', pages), fast_pdf_stats=manifest['stats'],
                                 output_format=output_format, resource_root=canonical.parent,
                                 fast_assets=_replay_fast_assets(canonical, soup) or None,
                                 initial_warnings=list(initial_warnings) + manifest.get('warnings', []),
                                 initial_joins=list(initial_joins) + manifest.get('table_joins', []))
        removed_reviews = structured.strip_review_annotations(soup)
        removed_rasters = remove_published_table_rasters(soup) if pdf_engine_output else 0
        joins = join_captioned_tables(soup)
        star_notes = normalize_table_star_notes(soup)
        if removed_reviews or removed_rasters or joins or star_notes:
            # Historical canonicals remain immutable. Republish only the
            # document layer; the OCR/layout snapshot is not involved.
            content = formula_preview.migration_content(canonical, comparison_info, soup, preview_sources=preview_sources)
            return build_exports(content, source, destination,
                                 quality_pdf=quality_pdf,
                                 pages=manifest['stats'].get('pages', pages), fast_pdf_stats=manifest['stats'],
                                 output_format=output_format, resource_root=canonical.parent,
                                 fast_assets=_replay_fast_assets(canonical, soup) or None,
                                 initial_warnings=list(initial_warnings) + manifest.get('warnings', []),
                                 initial_joins=list(manifest.get('table_joins', [])) + joins)
    else:
        soup = BeautifulSoup(content, 'html.parser')
        if soup.body is None:
            raise ValueError('Документ не содержит body после нормализации HTML')
        comparisons = (formula_preview.FormulaComparisons(soup)
                       if pdf_engine_output and not (fast_pdf_stats and 'native_pages' in fast_pdf_stats) else None)
        structured.strip_review_annotations(soup)
        if pdf_engine_output:
            remove_published_table_rasters(soup)
        normalize_semantic_annotations(soup, {}, {})
        warnings = list(initial_warnings) + list(structured.normalize_table_notes(soup))
        if comparisons:
            warnings.extend(comparisons.warnings)
        warnings.extend(notice.get_text(' ', strip=True) for notice in soup.select('aside[data-unrecognized-table]'))
        if pdf_engine_output:
            for figure in soup.select('figure[data-source-formula-review="true"][data-pdf-page]'):
                page = figure['data-pdf-page']
                if (page.isascii() and page.isdecimal() and page.strip('0')
                        and figure.find('img', src=True, recursive=False)):
                    warnings.append(f'Формула сохранена изображением (страница PDF {page}).')
        joins = list(initial_joins) + join_captioned_tables(soup)
        normalize_table_star_notes(soup)
        replay = destination / '.replay'
        if native_path(replay / 'document.html').exists():
            # Another fresh conversion has a separate canonical. Existing
            # completed packages and the canonical they came from stay intact.
            replay = destination / '.versions' / uuid.uuid4().hex / '.replay'
        native_path(replay / 'src').mkdir(parents=True, exist_ok=True)
        expected_images = [image for image in soup.find_all('img') if image.get('src')]
        media = assets.externalize_images(soup, source, native_path(replay / 'src'),
                                         resource_root=resource_root, fast_assets=fast_assets)
        warnings.extend(media.get('warnings', []))
        if any(image.get('data-image-unavailable') == 'true' for image in expected_images):
            raise ValueError('Не удалось сохранить изображение документа: ' + '; '.join(media.get('warnings', [])))
        if quality_pdf and soup.head is None:
            (soup.html or soup).insert(0, soup.new_tag('head'))
        if soup.head is not None:
            markers = {'document-export-schema': str(EXPORT_SCHEMA)}
            if quality_pdf:
                markers[quality_html.SOURCE_MARKER] = '1'
            for name, version in markers.items():
                marker = soup.head.find('meta', attrs={'name': name})
                if marker is None:
                    marker = soup.new_tag('meta', attrs={'name': name})
                    soup.head.append(marker)
                marker['content'] = version
        preview_sources = [] if quality_pdf else None
        comparison_info = comparisons.write(soup, replay, sources=preview_sources) if comparisons else None
        canonical = replay / 'document.html'
        sources = []
        if quality_pdf:
            canonical_bytes = quality_html.render_quality_html(soup, sources=sources).encode('utf-8')
        else:
            canonical_bytes = str(soup).encode('utf-8')
        native_path(canonical).write_bytes(canonical_bytes)
        manifest = dict(schema_version=CANONICAL_MANIFEST_SCHEMA, export_schema=EXPORT_SCHEMA,
                        html_sha256=hashlib.sha256(canonical_bytes).hexdigest(), assets=media.get('assets', []),
                        warnings=list(dict.fromkeys(warnings)), stats=_document_stats(soup, pages, structured),
                        table_joins=joins)
        if quality_pdf:
            manifest['quality_pdf_sources'] = quality_html.source_manifest(sources, manifest['html_sha256'])
            if comparison_info:
                manifest['quality_pdf_preview_sources'] = quality_html.source_manifest(
                    preview_sources, comparison_info['html_sha256'])
        if fast_pdf_stats:
            manifest['stats'].update((key, fast_pdf_stats[key]) for key in FAST_PDF_STAT_KEYS if key in fast_pdf_stats)
        if comparison_info:
            manifest['formula_preview'] = comparison_info
        _save_manifest(canonical, manifest)
    replay = canonical.parent
    stats = dict(manifest['stats'])
    warnings = list(dict.fromkeys([*initial_warnings, *manifest.get('warnings', [])]))
    media = dict(assets=manifest.get('assets', []))
    # Check the immutable assets before making any derivative package.
    if not replay_html:
        _check_assets(replay, manifest)
    artifacts, packages = {}, {}
    selected = list(EXTENSIONS) if output_format is None else [output_format]
    for fmt in selected:
        folder = destination / f'{source.stem}_{FOLDER_SUFFIXES[fmt]}'
        if native_path(folder).exists():
            # Keep an earlier completed package intact while building a new
            # version; its assets cannot leak into the next downloaded folder.
            folder = destination / '.versions' / uuid.uuid4().hex / folder.name
        folder = native_path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        # Copy only referenced assets, never a stale file from an earlier export.
        for asset in media.get('assets', []):
            relative, origin = _asset_path(replay, asset['path'])
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native_path(origin), target)
        if fmt == 'html':
            value = None
        elif fmt == 'markdown':
            render_html = quality_html.markdown_html_renderer(soup) if quality_pdf else str
            value = structured.to_markdown(soup, render_html=render_html)
        elif fmt == 'json':
            model = structured.to_json_document(soup, source, stats)
            # A native JSON image manifest: small paths, never encoded pixels.
            model['assets'] = media.get('assets', [])
            value = json.dumps(model, ensure_ascii=False, indent=2)
        else:
            value = structured.to_plaintext(soup)
        converted = folder / f'{source.stem}_converted{EXTENSIONS[fmt]}'
        if fmt == 'html':
            # Exact canonical bytes, including its newline/encoding choice.
            converted.write_bytes(canonical_bytes)
        else:
            converted.write_text(value, encoding='utf-8')
        artifacts[fmt] = str(converted.resolve())
        packages[fmt] = str(folder.resolve())
    return dict(artifacts=artifacts, packages=packages, replay_html=str(canonical.resolve()),
                export_schema=EXPORT_SCHEMA, stats=stats, assets=media.get('assets', []),
                warnings=list(dict.fromkeys(warnings)), table_joins=manifest.get('table_joins', []))
