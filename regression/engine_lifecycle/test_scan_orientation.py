from contextlib import redirect_stdout
from io import BytesIO, StringIO
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from PIL import Image
import pymupdf

from local_web.fast_pdf import convert_fast_pdf


class BalancedScanOrientationTest(unittest.TestCase):
    def test_native_scan_is_oriented_and_each_route_has_a_reason(self):
        with pymupdf.open() as document, BytesIO() as raster:
            with Image.new('L', (50, 60), 220) as image:
                image.save(raster, 'PNG')
            scan = document.new_page(width=240, height=330)
            scan.insert_image(scan.rect, stream=raster.getvalue())
            for x in (50, 100, 150, 200):
                for i, y in enumerate((110, 205, 300)):
                    scan.insert_text((x, y), f'Hidden text {i}', fontsize=8, rotate=90, render_mode=3)
            digital = document.new_page(width=240, height=330)
            digital.insert_text((24, 60), 'Ordinary text from a born digital page.')
            document.new_page(width=240, height=330)
            source = document.tobytes()

        with (patch('local_web.fast_ocr._orientation_hint', return_value=(90, .99)) as classify,
              patch('local_web.fast_ocr._infer', side_effect=AssertionError('native layer must not start OCR')) as recognize,
              redirect_stdout(StringIO()) as log):
            html, stats = convert_fast_pdf(source, mode='balanced', balanced_grid=False,
                                           image_assets={}, formula_reader=lambda crops: [])
        sections = BeautifulSoup(html, 'html.parser').select('section')
        self.assertEqual(classify.call_count, 1)
        recognize.assert_not_called()
        self.assertEqual([s['data-text-method'] for s in sections], ['native', 'native', 'blank'])
        self.assertEqual([s['data-text-reason'] for s in sections],
                         ['covered_text_layer', 'covered_text_layer', 'blank_raster'])
        self.assertEqual([s['data-page-turn'] for s in sections], ['90', '0', '0'])
        self.assertIn('Hidden text 0', sections[0].get_text(' ', strip=True))
        self.assertEqual(stats['orientation_corrected_pages'], 1)
        self.assertEqual(log.getvalue().count('PDF route: page'), 3)

    def test_rejected_layer_records_coverage_reason_on_ocr_route(self):
        with pymupdf.open() as document, BytesIO() as raster:
            with Image.new('L', (50, 60), 200) as image:
                image.save(raster, 'PNG')
            scan = document.new_page(width=240, height=330)
            scan.insert_image(scan.rect, stream=raster.getvalue())
            scan.insert_text((12, 20), 'Header with a usable hidden text layer')
            scan.insert_text((12, 30), 'A second line inside the same header')
            source = document.tobytes()
        with redirect_stdout(StringIO()):
            html, _ = convert_fast_pdf(source, mode='fast', image_assets={},
                                       ocr_page=lambda image, page: 'OCR result')
        page = BeautifulSoup(html, 'html.parser').section
        self.assertEqual(page['data-text-method'], 'ocr')
        self.assertEqual(page['data-text-reason'], 'insufficient_text_coverage')


if __name__ == '__main__':
    unittest.main()
