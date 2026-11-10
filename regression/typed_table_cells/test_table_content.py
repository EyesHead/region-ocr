import base64
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import convert_docling as core


def raster():
    image = Image.new('RGB', (240, 160), 'white')
    ImageDraw.Draw(image).rectangle((80, 55, 150, 105), outline='black', width=3)
    return image


def geometry(html=''):
    return dict(bbox=[0, 0, 240, 160], cells=[dict(row=0, column=0, row_span=1, column_span=1,
                           bbox=[0, 0, 240, 160], html=html)],
                x_grid=[0, 240], y_grid=[0, 160], retained_cell_images=[])


class TypedTableContentTests(unittest.TestCase):
    def test_unmapped_source_cell_keeps_existing_text_and_math(self):
        fragment = '<table><tr><td>Pressure <math><mi>P</mi></math> remains editable</td></tr></table>'
        with raster() as image:
            actual, _ = core.mask_source_owned_cells(fragment, geometry(), image, [0, 0, 240, 160], 1, {}, core)
        soup = BeautifulSoup(actual, 'html.parser')
        self.assertIn('Pressure', soup.td.get_text())
        self.assertIsNotNone(soup.td.math)
        self.assertIsNone(soup.td.img)

    def normalize(self, blocks, existing=None):
        from docling_pdf_table_content import normalize_table_content
        image = raster()
        try:
            fragment = existing or '<table><tr><td>'+core.image_html(image, 240, 'cell')+'</td></tr></table>'
            return normalize_table_content(fragment, geometry(), image, [0, 0, 240, 160], 1,
                SimpleNamespace(), {}, core, recognize=lambda *_: blocks)
        finally:
            image.close()

    def test_text_crop_becomes_editable_text(self):
        actual = self.normalize([dict(label='Text', bbox=[0, 0, 1000, 1000], html='<p>printed words 42</p>')])
        soup = BeautifulSoup(actual, 'html.parser')
        self.assertEqual(soup.td.get_text(strip=True), 'printed words 42')
        self.assertFalse(soup.find('img'))

    def test_formula_crop_becomes_mathml(self):
        actual = self.normalize([dict(label='Equation', bbox=[0, 0, 1000, 1000], html=r'<math>\frac{P}{A}</math>')])
        soup = BeautifulSoup(actual, 'html.parser')
        self.assertTrue(soup.td.find('mfrac'))
        self.assertFalse(soup.find('img'))

    def test_mixed_cell_keeps_text_math_and_only_picture_pixels_in_order(self):
        actual = self.normalize([
            dict(label='Text', bbox=[0, 0, 1000, 250], html='<p>Before</p>'),
            dict(label='Diagram', bbox=[300, 300, 700, 700], html=''),
            dict(label='Equation', bbox=[0, 710, 1000, 820], html='<math>E=mc^2</math>'),
            dict(label='Text', bbox=[0, 850, 1000, 1000], html='<p>After</p>'),
        ])
        soup = BeautifulSoup(actual, 'html.parser')
        self.assertEqual(len(soup.find_all('img')), 1)
        img = soup.img
        self.assertEqual(img.get('data-table-content'), 'picture')
        with Image.open(BytesIO(base64.b64decode(img['src'].split(',', 1)[1]))) as cropped:
            self.assertEqual(cropped.size, (96, 64))
        self.assertTrue(soup.td.math.find('msup'))
        self.assertLess(str(soup.td).index('Before'), str(soup.td).index('<img'))
        self.assertLess(str(soup.td).index('<img'), str(soup.td).index('<math'))
        self.assertLess(str(soup.td).index('<math'), str(soup.td).index('After'))

    def test_bad_math_stays_literal_latex_instead_of_a_picture(self):
        actual = self.normalize([dict(label='Equation', bbox=[0, 0, 1000, 1000], html=r'<math>\frac{P}{</math>')])
        soup = BeautifulSoup(actual, 'html.parser')
        self.assertIn(r'\frac{P}{', soup.get_text())
        self.assertFalse(soup.find('img'))

    def test_ambiguous_image_does_not_silently_survive_failed_recognition(self):
        from docling_pdf_table_content import TableContentError
        with self.assertRaises(TableContentError):
            self.normalize([])

    def test_ocr_image_url_is_never_published_as_picture_proof(self):
        from docling_pdf_table_content import TableContentError
        with self.assertRaises(TableContentError):
            self.normalize([dict(label='Text', bbox=[0, 0, 1000, 1000], html='<p>text<img src="https://invalid.test/a.png"/></p>')])

    def test_invalid_picture_bbox_is_not_clamped_into_a_valid_crop(self):
        from docling_pdf_table_content import TableContentError
        with self.assertRaises(TableContentError):
            self.normalize([dict(label='Diagram', bbox=[-1, 0, 1000, 1000], html='')])

    def test_picture_provenance_uses_original_page_not_ocr_base64(self):
        from docling_pdf_table_content import normalize_table_content
        with Image.new('RGB', (240, 160), 'blue') as forged:
            fragment = '<table><tr><td>'+core.image_html(forged, 240, 'cell')+'</td></tr></table>'
        with Image.new('RGB', (240, 160), 'white') as source:
            actual = normalize_table_content(fragment, geometry(), source, [0, 0, 240, 160], 1,
                SimpleNamespace(), {}, core,
                recognize=lambda *_: [dict(label='Diagram', bbox=[0, 0, 1000, 1000], html='')])
        img = BeautifulSoup(actual, 'html.parser').img
        with Image.open(BytesIO(base64.b64decode(img['src'].split(',', 1)[1]))) as picture:
            self.assertEqual(picture.getpixel((120, 80)), (255, 255, 255))

    def test_partial_diagram_answer_cannot_omit_visible_source_text(self):
        from docling_pdf_table_content import normalize_table_content, TableContentError
        with raster() as source:
            ImageDraw.Draw(source).text((10, 10), 'LOST TEXT', fill='black')
            fragment = '<table><tr><td>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
            with self.assertRaisesRegex(TableContentError, 'omitted visible source content'):
                normalize_table_content(fragment, geometry(), source, [0, 0, 240, 160], 1,
                    SimpleNamespace(), {}, core,
                    recognize=lambda *_: [dict(label='Diagram', bbox=[300, 300, 700, 700], html='')])

    def test_pending_primary_words_cannot_disappear_in_mixed_recovery(self):
        from docling_pdf_table_content import normalize_table_content, TableContentError
        source_geometry = geometry()
        source_geometry['cells'][0]['pending_primary_html'] = '<p>Required words</p>'
        with raster() as source:
            fragment = '<table><tr><td>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
            with self.assertRaisesRegex(TableContentError, 'editable text'):
                normalize_table_content(fragment, source_geometry, source, [0, 0, 240, 160], 1,
                    SimpleNamespace(), {}, core,
                    recognize=lambda *_: [dict(label='Diagram', bbox=[0, 0, 1000, 1000], html='')])

    def test_late_serializer_cannot_add_an_unverified_table_image(self):
        from docling_pdf_table_content import validate_published_table_images, TableContentError
        with raster() as source:
            soup = BeautifulSoup('<table><tr><td>'+core.image_html(source, 240, 'formula')+'</td></tr></table>', 'html.parser')
        with self.assertRaises(TableContentError):
            validate_published_table_images(soup, {})

    def test_positioned_primary_text_does_not_need_matching_table_grid(self):
        from docling_pdf_table_content import normalize_table_content
        with Image.new('RGB', (240, 160), 'white') as source:
            ImageDraw.Draw(source).text((40, 60), 'Source text', fill='black')
            fragment = '<table><tr><td>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
            args = SimpleNamespace(surya_records=[dict(page_no=1, label='Text',
                bbox=[30, 50, 120, 90], html='<p>Source text</p>')])
            actual = normalize_table_content(fragment, geometry(), source, [0, 0, 240, 160], 1,
                args, {}, core, recognize=lambda *_: self.fail('Existing positioned text should be reused'))
        soup = BeautifulSoup(actual, 'html.parser')
        self.assertEqual(soup.td.get_text(strip=True), 'Source text')
        self.assertIsNone(soup.img)

    def test_known_formula_cannot_disappear_into_a_picture(self):
        from docling_pdf_table_content import TableContentError
        with raster() as source:
            existing = '<table><tr><td><math><mi>P</mi></math>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
        with self.assertRaisesRegex(TableContentError, 'formula'):
            self.normalize([dict(label='Diagram', bbox=[0, 0, 1000, 1000], html='')], existing=existing)

    def test_pending_primary_text_cannot_hide_an_existing_formula(self):
        from docling_pdf_table_content import normalize_table_content, TableContentError
        g = geometry()
        g['cells'][0]['pending_primary_html'] = '<p>New words</p>'
        with raster() as source:
            original = '<table><tr><td><math><mi>P</mi></math>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
            with self.assertRaisesRegex(TableContentError, 'formula'):
                normalize_table_content(original, g, source, [0, 0, 240, 160], 1, SimpleNamespace(), {}, core,
                    recognize=lambda *_: [dict(label='Text', bbox=[0, 0, 1000, 250], html='<p>New words</p>'),
                                         dict(label='Diagram', bbox=[300, 300, 700, 700], html='')])

    def test_blank_block_cannot_cover_unpublished_source_text(self):
        from docling_pdf_table_content import normalize_table_content, TableContentError
        with Image.new('RGB', (240, 160), 'white') as source:
            draw = ImageDraw.Draw(source)
            draw.text((20, 20), 'LOST TEXT', fill='black')
            draw.rectangle((80, 90, 150, 140), outline='black', width=3)
            fragment = '<table><tr><td>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
            with self.assertRaisesRegex(TableContentError, 'omitted visible'):
                normalize_table_content(fragment, geometry(), source, [0, 0, 240, 160], 1,
                    SimpleNamespace(), {}, core,
                    recognize=lambda *_: [dict(label='Blank', bbox=[0, 0, 1000, 500], html=''),
                                         dict(label='Diagram', bbox=[300, 520, 700, 950], html='')])

    def test_partial_source_region_cannot_claim_complete_cell_recognition(self):
        from docling_pdf_table_content import normalize_table_content, TableContentError
        g = geometry()
        g['cells'][0]['bbox'] = [0, 0, 260, 160]
        with raster() as source:
            fragment = '<table><tr><td>'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
            with self.assertRaisesRegex(TableContentError, '[Cc]omplete'):
                normalize_table_content(fragment, g, source, [0, 0, 240, 160], 1,
                    SimpleNamespace(), {}, core,
                    recognize=lambda *_: [dict(label='Diagram', bbox=[0, 0, 1000, 1000], html='')])

    def test_empty_formula_is_not_published_as_empty_latex_delimiters(self):
        from docling_pdf_table_content import TableContentError
        with self.assertRaisesRegex(TableContentError, 'Empty'):
            self.normalize([dict(label='Equation', bbox=[0, 0, 1000, 1000], html='<math> </math>')])

    def test_known_relation_operator_cannot_change_during_mixed_recovery(self):
        from docling_pdf_table_content import TableContentError
        with raster() as source:
            original = '<table><tr><td>Limit ≤ 5 mm'+core.image_html(source, 240, 'cell')+'</td></tr></table>'
        with self.assertRaisesRegex(TableContentError, 'editable text'):
            self.normalize([dict(label='Text', bbox=[0, 0, 1000, 250], html='<p>Limit ≥ 5 mm</p>'),
                            dict(label='Diagram', bbox=[300, 300, 700, 700], html='')], existing=original)


if __name__ == '__main__':
    unittest.main()
