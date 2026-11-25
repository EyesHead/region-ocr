import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from bs4 import BeautifulSoup
from PIL import Image

from docling_pdf_primary_rows import quarantine_scan_text
from docling_pdf_table_rasters import _complete_source_cells


class QualityScanPixelsTest(unittest.TestCase):
    def test_unread_cells_keep_their_coordinates_without_creating_pngs(self):
        region = [0, 0, 24, 16]
        cells = [dict(row=0, column=i, row_span=1, column_span=1, bbox=[i*8, 0, (i+1)*8, 16])
                 for i in range(3)]
        geometry = dict(cells=cells, x_grid=[0, 8, 16, 24], hidden_ocr_direction_conflict=True)
        fragment = '<table><tr><td>старый</td><td>слой</td><td></td></tr></table>'
        ink = np.zeros((16, 24), dtype=np.uint8)
        ink[6, 7] = 255  # A digit fragment at the right edge of the first cell.
        ink[4:12, 10:13] = 255
        api = SimpleNamespace(table_profile=lambda _: {'cells': cells}, diagnostic_event=Mock(),
                              image_html=Mock(side_effect=AssertionError('Encoded an unread cell as a picture')))
        with Image.new('L', (24, 16)) as image, patch('docling_pdf_table_layout.grid_ink',
                return_value=dict(sx=1, sy=1, nongrid=ink)):
            html, measured = quarantine_scan_text(fragment, geometry, image, region, 8, api)
        nodes = BeautifulSoup(html, 'html.parser').select('td')
        self.assertEqual([n.decode_contents() for n in nodes], ['', '', ''])
        self.assertEqual([c['scan_layout_has_ink'] for c in measured['cells']], [True, True, False])
        self.assertFalse(any(c['scan_layout_text'] for c in measured['cells']))
        api.image_html.assert_not_called()

    def test_partial_transcription_does_not_prove_a_complete_duplicate(self):
        cell = dict(row=0, column=0, row_span=1, column_span=1)
        api = SimpleNamespace(table_profile=lambda _: {'issues': [], 'cells': [cell]})
        entry = dict(html='<table><tr><td>часть строки</td></tr></table>',
                     pdf_geometry=dict(source_grid_verified=True, cells=[dict(cell, unresolved_typed_content=True)]))
        self.assertFalse(_complete_source_cells(entry, api))
        entry['pdf_geometry']['cells'] = [cell]
        self.assertTrue(_complete_source_cells(entry, api))

    def test_quarantine_keeps_pixel_evidence_of_a_drawing_without_encoding_it(self):
        cell = dict(row=0, column=0, row_span=1, column_span=1, bbox=[0, 0, 20, 16])
        asset = dict(row=0, column=0, bbox=[2, 2, 18, 14], basis='connected_source_strokes_over_ocr_text',
                     source_column_context=dict(graphic_column=True))
        geometry = dict(cells=[cell], x_grid=[0, 20], retained_cell_images=[asset])
        api = SimpleNamespace(table_profile=lambda _: {'cells': [cell]}, diagnostic_event=Mock())
        with Image.new('L', (20, 16)) as image, patch('docling_pdf_table_layout.grid_ink',
                return_value=dict(sx=1, sy=1, nongrid=np.zeros((16, 20), dtype=np.uint8))):
            html, measured = quarantine_scan_text('<table><tr><td>ось</td></tr></table>',
                                                  geometry, image, cell['bbox'], 1, api)
        self.assertTrue(measured['cells'][0]['scan_layout_has_ink'])
        self.assertEqual(measured['retained_cell_images'], [asset])
        self.assertIsNone(BeautifulSoup(html, 'html.parser').img)


if __name__ == '__main__':
    unittest.main()
