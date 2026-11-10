"""Editable table payload must survive uncertain grid or graphic ownership."""
import copy
from pathlib import Path
import sys
import unittest

from bs4 import BeautifulSoup
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import convert_docling as core
from docling_pdf_primary import mask_nonprimary_content, preserve_primary_cells


MATH = '<math><mfrac><mi>F</mi><mi>A</mi></mfrac></math>'


def fixture(body, heading='Описание'):
    html = '<table><thead><tr><th>' + heading + '</th></tr></thead><tbody><tr><td>' + body + '</td></tr></tbody></table>'
    cells = copy.deepcopy(core.table_profile(html)['cells'])
    for cell in cells:
        cell['bbox'] = [0, 50 * cell['row'], 200, 50 * (cell['row'] + 1)]
    return html, dict(cells=cells, x_grid=[0, 200], y_grid=[0, 50, 100],
                      bbox=[0, 0, 200, 100], retained_cell_images=[])


def source_image():
    image = Image.new('RGB', (10, 10), 'blue')
    try:
        return core.image_html(image, 10, 'cell').replace('<img ', '<img data-source-cell="true" data-cell-content="source-diagram" ')
    finally:
        image.close()


class PrimaryPayloadTests(unittest.TestCase):
    def preserve(self, target, geometry, primary, manifest=None):
        manifest = {} if manifest is None else manifest
        result, proofs = preserve_primary_cells(target, geometry,
            [dict(page_no=1, bbox=[0, 0, 200, 100], html=primary)],
            [0, 0, 200, 100], 1, manifest, core)
        return BeautifulSoup(result, 'html.parser'), proofs, manifest

    def test_unmapped_text_and_math_remain_editable_without_a_crop(self):
        fragment, geometry = fixture('Напряжение ' + MATH)
        with Image.new('RGB', (200, 100), 'white') as image:
            content, proofs = mask_nonprimary_content(fragment, geometry, image,
                [0, 0, 200, 100], 1, {}, core)
        cell = BeautifulSoup(content, 'html.parser').td
        self.assertIn('Напряжение', cell.get_text())
        self.assertIsNotNone(cell.find('mfrac'))
        self.assertIsNone(cell.find('img'))
        self.assertTrue(geometry['cells'][1].get('typed_content_recovery_required'))
        self.assertEqual(len(proofs), 2)

    def test_unmapped_mixed_cell_keeps_text_math_and_source_image_order(self):
        fragment, geometry = fixture('До ' + source_image() + ' после ' + MATH)
        with Image.new('RGB', (200, 100), 'white') as image:
            content, proofs = mask_nonprimary_content(fragment, geometry, image,
                [0, 0, 200, 100], 1, {}, core)
        cell = BeautifulSoup(content, 'html.parser').td
        self.assertIn('До', cell.get_text())
        self.assertIn('после', cell.get_text())
        self.assertIsNotNone(cell.find('mfrac'))
        self.assertEqual(len(cell.find_all('img')), 1)
        self.assertLess(str(cell).index('До'), str(cell).index('<img'))
        self.assertLess(str(cell).index('<img'), str(cell).index('после'))

    def test_primary_mixed_cell_uses_only_source_owned_image_in_placeholder_order(self):
        fragment, geometry = fixture('Старое ' + source_image(), heading='Схема')
        geometry['retained_cell_images'] = [dict(row=1, column=0, bbox=[50, 55, 90, 90], basis='source_graphic')]
        geometry['pending_cell_ocr'] = [dict(row=1, column=0, bbox=[0, 50, 200, 100])]
        primary, _ = fixture('До <img src="https://untrusted.invalid/diagram.png"/> после ' + MATH, heading='Схема')
        result, proofs, manifest = self.preserve(fragment, geometry, primary)
        self.assertIn('До', result.td.get_text())
        self.assertIn('после', result.td.get_text())
        self.assertIsNotNone(result.td.find('mfrac'))
        self.assertEqual(result.td.img['src'], BeautifulSoup(fragment, 'html.parser').td.img['src'])
        self.assertNotIn('untrusted.invalid', str(result))
        self.assertLess(str(result.td).index('До'), str(result.td).index('<img'))
        self.assertLess(str(result.td).index('<img'), str(result.td).index('после'))
        self.assertTrue(geometry['cells'][1].get('primary_ocr'))
        self.assertEqual(len(geometry['retained_cell_images']), 1)
        self.assertFalse(geometry['pending_cell_ocr'])

    def test_matching_primary_text_without_placeholder_keeps_existing_image_order(self):
        fragment, geometry = fixture('До ' + source_image() + ' после ' + MATH, heading='Схема')
        primary, _ = fixture('До  после ' + MATH, heading='Схема')
        result, proofs, manifest = self.preserve(fragment, geometry, primary)
        self.assertEqual(result.td.decode_contents(), BeautifulSoup(fragment, 'html.parser').td.decode_contents())
        self.assertTrue(geometry['cells'][1].get('primary_ocr'))

    def test_unmapped_content_does_not_require_source_pixels(self):
        fragment, geometry = fixture('Существующий текст ' + MATH)
        try:
            content, proofs = mask_nonprimary_content(fragment, geometry, None,
                [0, 0, 200, 100], 1, {}, core)
        except RuntimeError as error:
            self.fail('Keeping existing editable content must not require a source crop: ' + str(error))
        self.assertEqual(BeautifulSoup(content, 'html.parser').td.decode_contents(),
                         BeautifulSoup(fragment, 'html.parser').td.decode_contents())

    def test_formula_in_graphic_column_is_not_rejected_by_column_name(self):
        fragment, geometry = fixture('Старое значение', heading='Схема')
        primary, _ = fixture(MATH, heading='Схема')
        result, proofs, manifest = self.preserve(fragment, geometry, primary)
        self.assertIsNotNone(result.td.find('mfrac'))
        self.assertIsNone(result.td.find('img'))
        self.assertNotIn('Старое', result.td.get_text())

    def test_missing_primary_image_placeholder_keeps_known_mixed_content_for_recovery(self):
        fragment, geometry = fixture('Текст источника ' + source_image(), heading='Схема')
        primary, _ = fixture('Новое чтение без позиции рисунка', heading='Схема')
        result, proofs, manifest = self.preserve(fragment, geometry, primary)
        self.assertIn('Текст источника', result.td.get_text())
        self.assertEqual(len(result.td.find_all('img')), 1)
        self.assertTrue(geometry['cells'][1].get('typed_content_recovery_required'))
        self.assertTrue(manifest.get('typed_table_recovery_cells'))

    def test_ocr_image_without_a_source_asset_is_not_published(self):
        fragment, geometry = fixture('Текст источника')
        primary, _ = fixture('Другой текст <img src="https://untrusted.invalid/diagram.png"/>')
        result, proofs, manifest = self.preserve(fragment, geometry, primary)
        self.assertIn('Текст источника', result.td.get_text())
        self.assertIsNone(result.td.find('img'))
        self.assertTrue(geometry['cells'][1].get('typed_content_recovery_required'))


if __name__ == '__main__':
    unittest.main()
