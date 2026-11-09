"""Source-owned XLSX tables: OOXML merged ranges are authoritative.

No spreadsheet calculation or model inference is performed. Cached values and
formula expressions remain separate; unsupported display formats are disclosed.
"""

from __future__ import annotations

import html
import mimetypes
import posixpath
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET


S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
D = '{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
BUILTIN_FORMATS = {0: 'General', 1: '0', 2: '0.00', 3: '#,##0', 4: '#,##0.00',
                   9: '0%', 10: '0.00%', 11: '0.00E+00', 49: '@'}


def _xml(archive, part):
    info = archive.getinfo(part)
    if info.file_size > 64 * 1024 * 1024:
        raise ValueError('Слишком большая XML-часть XLSX: ' + part)
    data = archive.read(part)
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XLSX содержит неподдерживаемое объявление XML-сущностей')
    return ET.fromstring(data)


def _rels(archive, part):
    folder, name = posixpath.split(part)
    rel_part = posixpath.join(folder, '_rels', name + '.rels')
    if rel_part not in archive.namelist():
        return {}
    result = {}
    for relationship in _xml(archive, rel_part):
        if relationship.get('TargetMode') == 'External':
            continue
        target = relationship.get('Target', '')
        resolved = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(folder, target))
        if not resolved.startswith('../'):
            result[relationship.get('Id')] = resolved
    return result


def _coordinate(value):
    match = re.fullmatch(r'\$?([A-Z]+)\$?([1-9]\d*)', value or '', re.I)
    if not match:
        raise ValueError('Некорректный адрес ячейки XLSX: ' + str(value))
    column = 0
    for character in match[1].upper():
        column = column * 26 + ord(character) - ord('A') + 1
    row = int(match[2])
    if row > 1048576 or column > 16384:
        raise ValueError('Адрес XLSX выходит за границы листа')
    return row, column


def _strings(archive):
    if 'xl/sharedStrings.xml' not in archive.namelist():
        return []
    return [''.join(item.itertext()) if not item.findall('.//' + S + 't')
            else ''.join(node.text or '' for node in item.iter(S + 't'))
            for item in _xml(archive, 'xl/sharedStrings.xml').findall(S + 'si')]


def _styles(archive):
    if 'xl/styles.xml' not in archive.namelist():
        return [0], dict(BUILTIN_FORMATS)
    root = _xml(archive, 'xl/styles.xml')
    formats = dict(BUILTIN_FORMATS)
    custom = root.find(S + 'numFmts')
    if custom is not None:
        formats.update({int(node.get('numFmtId')): node.get('formatCode', '') for node in custom})
    cell_formats = root.find(S + 'cellXfs')
    return ([int(node.get('numFmtId', '0')) for node in cell_formats] if cell_formats is not None else [0]), formats


def _formatted_number(raw, style, style_ids, formats, warnings):
    try:
        identifier = style_ids[int(style)]
    except (ValueError, IndexError):
        warnings.add('Некоторые стили XLSX неизвестны; числовые значения сохранены без форматирования.')
        return raw
    code = formats.get(identifier, f'numFmtId={identifier}')
    if code in {'General', '@'}:
        return raw
    try:
        number = Decimal(raw)
        if not number.is_finite():
            return raw
        if code in {'0', '0.00', '#,##0', '#,##0.00'}:
            digits = 2 if '.00' in code else 0
            return format(number, (',' if ',' in code else '') + f'.{digits}f')
        if code in {'0%', '0.00%'}:
            return format(number * 100, '.2f' if '.00' in code else '.0f') + '%'
        if code == '0.00E+00':
            return format(number, '.2E')
    except (InvalidOperation, ValueError):
        pass
    warnings.add('Числовой формат XLSX «' + code[:80] + '» не применён; исходные значения сохранены в ячейках и data-raw-value.')
    return raw


def _sheet_images(archive, sheet_root, sheet_part, warnings, assets_dir):
    output = []
    relationships = _rels(archive, sheet_part)
    for drawing in sheet_root.findall(S + 'drawing'):
        drawing_part = relationships.get(drawing.get(R + 'id'))
        if not drawing_part or drawing_part not in archive.namelist():
            warnings.add('Некоторые изображения XLSX отсутствуют в архиве.')
            continue
        drawing_root = _xml(archive, drawing_part)
        images = _rels(archive, drawing_part)
        for anchor in drawing_root:
            origin = anchor.find(D + 'from')
            row = int(origin.findtext(D + 'row', '0')) + 1 if origin is not None else 1
            column = int(origin.findtext(D + 'col', '0')) + 1 if origin is not None else 1
            for blip in anchor.iter(A + 'blip'):
                image_part = images.get(blip.get(R + 'embed'))
                if not image_part or image_part not in archive.namelist():
                    warnings.add('Внешнее изображение XLSX не загружалось; обработка остаётся локальной.')
                    continue
                mime = mimetypes.guess_type(image_part)[0] or 'application/octet-stream'
                if mime not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp', 'image/tiff'}:
                    warnings.add('Изображение XLSX в формате ' + Path(image_part).suffix + ' не включено: формат не поддерживается браузером.')
                    continue
                if assets_dir is None:
                    warnings.add('Для изображений XLSX не задан каталог src; изображения не встроены в HTML и не сохранялись рядом с исходником.')
                    continue
                if __package__:
                    from .assets import store_image_bytes
                else:
                    from assets import store_image_bytes
                try:
                    asset = store_image_bytes(archive.read(image_part), assets_dir, name=image_part)
                except ValueError as exc:
                    warnings.add('Изображение XLSX не включено: ' + str(exc))
                    continue
                warnings.update(asset.get('warnings', []))
                reference = html.escape(asset['path'], quote=True)
                output.append(f'<figure data-source-row="{row}" data-source-column="{column}"><img src="{reference}" width="{asset["width"]}" height="{asset["height"]}" alt="Изображение листа"></figure>')
            if anchor.find('.//' + '{http://schemas.openxmlformats.org/drawingml/2006/chart}chart') is not None:
                warnings.add('Диаграммы Excel не растеризованы; исходные данные листов сохранены.')
    return ''.join(output)


def export_xlsx(source: Path, *, check_cancel=lambda: None, assets_dir: Path | None = None):
    """Return canonical HTML, warnings and the number of source worksheets."""
    sections, warnings, sheet_count = [], set(), 0
    with zipfile.ZipFile(source) as archive:
        workbook = _xml(archive, 'xl/workbook.xml')
        relationships = _rels(archive, 'xl/workbook.xml')
        strings = _strings(archive)
        style_ids, formats = _styles(archive)
        sheets = workbook.find(S + 'sheets')
        if sheets is None:
            raise ValueError('Книга XLSX не содержит листов')
        for sheet in sheets:
            check_cancel()
            name = sheet.get('name', f'Лист {sheet_count + 1}')
            part = relationships.get(sheet.get(R + 'id'))
            if not part or part not in archive.namelist():
                raise ValueError('Не найдены данные листа XLSX: ' + name)
            root = _xml(archive, part)
            if root.tag != S + 'worksheet':
                warnings.add('Лист «' + name + '» не является таблицей; он не экспортирован.')
                continue
            sheet_count += 1
            cells = {}
            sheet_data = root.find(S + 'sheetData')
            for row in sheet_data if sheet_data is not None else []:
                check_cancel()
                for cell in row.findall(S + 'c'):
                    value_node, formula = cell.find(S + 'v'), cell.find(S + 'f')
                    inline = cell.find(S + 'is')
                    if value_node is None and formula is None and inline is None:
                        continue
                    address = cell.get('r')
                    coordinate = _coordinate(address)
                    raw = value_node.text or '' if value_node is not None else ''
                    kind = cell.get('t', 'n')
                    if kind == 's':
                        try:
                            value = strings[int(raw)]
                        except (ValueError, IndexError):
                            raise ValueError('Некорректная ссылка sharedStrings XLSX: ' + address)
                    elif kind == 'inlineStr':
                        value = ''.join(node.text or '' for node in inline.iter(S + 't')) if inline is not None else ''
                    elif kind == 'b':
                        value = 'TRUE' if raw == '1' else 'FALSE'
                    elif kind == 'n':
                        value = _formatted_number(raw, cell.get('s', '0'), style_ids, formats, warnings) if raw else ''
                    else:
                        value = raw
                    attrs = {'data-cell': address, 'data-cell-type': kind}
                    if raw:
                        attrs['data-raw-value'] = raw
                    if formula is not None:
                        expression = formula.text or ''
                        attrs['data-spreadsheet-formula'] = expression
                        if not expression:
                            warnings.add('Связанные формулы XLSX сохранены с кэшированными значениями; выражения зависимых shared-formula ячеек не развёрнуты.')
                        if not raw:
                            value = '=' + expression if expression else '[формула без кэшированного значения]'
                            warnings.add('Формулы XLSX без кэшированных значений сохранены как выражения; пересчёт не выполнялся.')
                    cells[coordinate] = (value, attrs)
            merges = {}
            covered = {}
            for merge in root.findall('./' + S + 'mergeCells/' + S + 'mergeCell'):
                parts = merge.get('ref', '').split(':')
                start, end = _coordinate(parts[0]), _coordinate(parts[-1])
                if end[0] < start[0] or end[1] < start[1]:
                    raise ValueError('Некорректное объединение ячеек XLSX')
                if (end[0] - start[0] + 1) * (end[1] - start[1] + 1) > 2_000_000:
                    raise ValueError('Объединение XLSX слишком велико для HTML-таблицы')
                merges[start] = end
                for row in range(start[0], end[0] + 1):
                    for column in range(start[1], end[1] + 1):
                        coordinate = row, column
                        if coordinate in covered:
                            raise ValueError('Пересекающиеся объединения ячеек XLSX')
                        covered[coordinate] = start
            coordinates = set(cells) | set(covered)
            content = []
            if coordinates:
                min_row = min(coordinate[0] for coordinate in coordinates)
                max_row = max(coordinate[0] for coordinate in coordinates)
                min_col = min(coordinate[1] for coordinate in coordinates)
                max_col = max(coordinate[1] for coordinate in coordinates)
                if (max_row - min_row + 1) * (max_col - min_col + 1) > 2_000_000:
                    raise ValueError('Занятая область XLSX превышает 2 млн HTML-ячеек; разделите книгу на меньшие диапазоны.')
                content.append(f'<table data-sheet="{html.escape(name, quote=True)}" data-start-row="{min_row}" data-start-column="{min_col}"><tbody>')
                for row in range(min_row, max_row + 1):
                    check_cancel()
                    content.append(f'<tr data-source-row="{row}">')
                    for column in range(min_col, max_col + 1):
                        coordinate = row, column
                        if coordinate in covered and covered[coordinate] != coordinate:
                            if cells.get(coordinate, ('', {}))[0]:
                                warnings.add('В перекрытых объединением ячейках XLSX есть данные; книга требует проверки в Excel.')
                            continue
                        value, attrs = cells.get(coordinate, ('', {}))
                        if coordinate in merges:
                            end = merges[coordinate]
                            attrs = dict(attrs, rowspan=str(end[0] - row + 1), colspan=str(end[1] - column + 1))
                        attributes = ''.join(' ' + key + '="' + html.escape(str(value), quote=True) + '"' for key, value in attrs.items())
                        content.append('<td' + attributes + '>' + html.escape(value).replace('\n', '<br>') + '</td>')
                    content.append('</tr>')
                content.append('</tbody></table>')
            else:
                content.append('<p>Пустой лист</p>')
            content.append(_sheet_images(archive, root, part, warnings, assets_dir))
            state = html.escape(sheet.get('state', 'visible'), quote=True)
            sections.append('<section data-sheet-state="' + state + '"><h1>' + html.escape(name) + '</h1>' + ''.join(content) + '</section>')
    output = '<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>' + html.escape(source.stem) + '</title></head><body>' + ''.join(sections) + '</body></html>'
    return output, sorted(warnings), sheet_count
