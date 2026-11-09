"""Selected-format document folders and reusable, image-externalized replay."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import uuid

if __package__:
    from .file_paths import native_path, normal_path
else:
    from file_paths import native_path, normal_path

EXPORT_SCHEMA = 9
CANONICAL_MANIFEST_SCHEMA = 1
EXTENSIONS = {'html': '.html', 'markdown': '.md', 'json': '.json', 'plaintext': '.txt'}


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


def _legacy_manifest(canonical, content_bytes, soup, pages, structured):
    """Index an existing external-asset canonical without rewriting any pixels."""
    from PIL import Image
    records, seen = [], set()
    for image in soup.find_all('img'):
        value = str(image.get('src', ''))
        if not value or value in seen:
            continue
        if value.lower().startswith('data:'):
            raise ValueError('Старый HTML со встроенными изображениями требует создания нового канонического документа перед повторным экспортом.')
        relative, origin = _asset_path(canonical.parent, value)
        data = native_path(origin).read_bytes()
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
                  replay_html=None, initial_warnings=(), initial_joins=()):
    """Create a canonical once, then derive packages without mutating that source."""
    from bs4 import BeautifulSoup
    source, destination = normal_path(Path(source).resolve()), normal_path(Path(destination).resolve())
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
            marker = soup.find('meta', attrs={'name': 'document-export-schema'})
            embedded = any(str(image.get('src', '')).lower().startswith('data:') for image in soup.find_all('img'))
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
                                     initial_warnings=list(initial_warnings) + legacy_warnings)
            if soup.body is None:
                raise ValueError('Канонический HTML не содержит body')
            manifest = _legacy_manifest(canonical, canonical_bytes, soup, pages, structured)
        _check_assets(canonical.parent, manifest)
        removed_reviews = structured.strip_review_annotations(soup)
        joins = join_captioned_tables(soup)
        star_notes = normalize_table_star_notes(soup)
        if removed_reviews or joins or star_notes:
            # Historical canonicals remain immutable. Republish only the
            # document layer; the OCR/layout snapshot is not involved.
            return build_exports(str(soup), source, destination,
                                 pages=manifest['stats'].get('pages', pages),
                                 output_format=output_format, resource_root=canonical.parent,
                                 initial_warnings=manifest.get('warnings', []),
                                 initial_joins=list(manifest.get('table_joins', [])) + joins)
    else:
        soup = BeautifulSoup(content, 'html.parser')
        if soup.body is None:
            raise ValueError('Документ не содержит body после нормализации HTML')
        structured.strip_review_annotations(soup)
        warnings = list(initial_warnings) + list(structured.normalize_table_notes(soup))
        joins = list(initial_joins) + join_captioned_tables(soup)
        normalize_table_star_notes(soup)
        replay = destination / '.replay'
        if native_path(replay / 'document.html').exists():
            # Another fresh conversion has a separate canonical. Existing
            # completed packages and the canonical they came from stay intact.
            replay = destination / '.versions' / uuid.uuid4().hex / '.replay'
        native_path(replay / 'src').mkdir(parents=True, exist_ok=True)
        expected_images = [image for image in soup.find_all('img') if image.get('src')]
        media = assets.externalize_images(soup, source, native_path(replay / 'src'), resource_root=resource_root)
        warnings.extend(media.get('warnings', []))
        if any(image.get('data-image-unavailable') == 'true' for image in expected_images):
            raise ValueError('Не удалось сохранить изображение документа: ' + '; '.join(media.get('warnings', [])))
        if soup.head is not None:
            marker = soup.find('meta', attrs={'name': 'document-export-schema'})
            if marker is None:
                marker = soup.new_tag('meta', attrs={'name': 'document-export-schema'})
                soup.head.append(marker)
            marker['content'] = str(EXPORT_SCHEMA)
        canonical = replay / 'document.html'
        canonical_bytes = str(soup).encode('utf-8')
        native_path(canonical).write_bytes(canonical_bytes)
        manifest = dict(schema_version=CANONICAL_MANIFEST_SCHEMA, export_schema=EXPORT_SCHEMA,
                        html_sha256=hashlib.sha256(canonical_bytes).hexdigest(), assets=media.get('assets', []),
                        warnings=list(dict.fromkeys(warnings)), stats=_document_stats(soup, pages, structured),
                        table_joins=joins)
        _save_manifest(canonical, manifest)
    replay = canonical.parent
    stats, warnings = dict(manifest['stats']), list(manifest.get('warnings', []))
    media = dict(assets=manifest.get('assets', []))
    # Check the immutable assets before making any derivative package.
    if not replay_html:
        _check_assets(replay, manifest)
    artifacts, packages = {}, {}
    selected = list(EXTENSIONS) if output_format is None else [output_format]
    for fmt in selected:
        folder = destination / f'{source.stem}_ocr'
        if native_path(folder).exists():
            # Keep an earlier completed package intact while building a new
            # version; its assets cannot leak into the next downloaded folder.
            folder = destination / '.versions' / uuid.uuid4().hex / folder.name
        folder = native_path(folder)
        src_dir = folder / 'src'
        src_dir.mkdir(parents=True, exist_ok=True)
        original = folder / f'{source.stem}_source{source.suffix}'
        if normal_path(source) != normal_path(original):
            shutil.copy2(native_path(source), original)
        # Copy only referenced assets, never a stale file from an earlier export.
        for asset in media.get('assets', []):
            relative, origin = _asset_path(replay, asset['path'])
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native_path(origin), target)
        if fmt == 'html':
            value = None
        elif fmt == 'markdown':
            value = structured.to_markdown(soup)
        elif fmt == 'json':
            model = structured.to_json_document(soup, source, stats)
            # A native JSON image manifest: small paths, never encoded pixels.
            model['assets'] = media.get('assets', [])
            value = json.dumps(model, ensure_ascii=False, indent=2)
        else:
            value = structured.to_plaintext(soup)
        converted = folder / f'{source.stem}_converted{EXTENSIONS[fmt]}'
        # Every delivery contains the exact HTML used for its selected export.
        # Relative src/ references work when that HTML is opened from disk.
        (folder / f'{source.stem}_converted.html').write_bytes(canonical_bytes)
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
