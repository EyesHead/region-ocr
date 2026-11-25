import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bs4 import BeautifulSoup
from PIL import Image

from docling_pdf_scan_recovery import verify_scan_cell_literals


class QualityScanWordsTest(unittest.TestCase):
    def compare(self, recognized, positioned, *, rotated=False):
        soup = BeautifulSoup(f'<table><tr><td>Единица <b>{recognized}</b> СИ</td></tr></table>', 'html.parser')
        original = str(soup)
        position = {'row': 0, 'column': 0}
        region = (0, 0, 200, 20)
        geometry = {'cells': [dict(position, bbox=region)], 'hidden_ocr_direction_conflict': rotated}
        words = []
        for start, text in [(0, 'ЕДИНИЦА'), (60, positioned), (150, 'СИ')]:
            chars = [dict(x0=start+i*4, x1=start+i*4+4, top=2, bottom=14) for i in range(len(text))]
            words.append(dict(text=text, chars=chars))
        page = SimpleNamespace(images=[object()], extract_words=Mock(return_value=words))
        reader = SimpleNamespace(load_page=Mock(return_value=page), is_page_scan=Mock(return_value=True))
        api = SimpleNamespace(diagnostic_event=Mock(), local_surya_backend=Mock(
            side_effect=AssertionError('A word difference must not trigger crop OCR')))
        manifest = {}
        with Image.new('L', (200, 20)) as image:
            verify_scan_cell_literals(soup.table, {'cells': [position]}, geometry, image, region, 9,
                                      reader, SimpleNamespace(surya_ocr_model=object()), manifest, api, '#/tables/4')
        self.assertEqual(str(soup), original)
        api.local_surya_backend.assert_not_called()
        return manifest, reader

    def test_keep_recognized_word_and_markup_for_one_or_two_different_letters(self):
        for recognized, positioned in [('ньутон', 'ньютон'), ('ЗИВЕРТ', 'зивеот'),
                                       ('паскапь', 'паскаль'), ('Узстандарт', 'Узстамдарт'),
                                       ('эталонный', 'аталонмый')]:
            with self.subTest(recognized=recognized):
                manifest, _ = self.compare(recognized, positioned)
                record, = manifest['retained_scan_literals']
                self.assertEqual((record['primary_literal'], record['positioned_literal']), (recognized, positioned))

    def test_case_only_difference_is_the_same_word(self):
        manifest, _ = self.compare('НЬЮТОН', 'ньютон')
        self.assertFalse(manifest.get('retained_scan_literals'))

    def test_rotated_hidden_layer_is_never_consulted(self):
        _, reader = self.compare('паскапь', 'паскаль', rotated=True)
        reader.load_page.assert_not_called()


if __name__ == '__main__':
    unittest.main()
