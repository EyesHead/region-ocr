from types import SimpleNamespace
import unittest

from PIL import Image
import pymupdf

from docling_pdf_balanced_math import local_formula_reader
from local_web.fast_figures import FormulaBatches, page_graphics
from local_web.fast_pdf import convert_fast_pdf


class BalancedFormulaEvidenceTest(unittest.TestCase):
    def candidates(self, words, cells=None):
        sent = []

        def read(crops):
            sent.extend(crop.text for crop in crops)
            return [None] * len(crops)

        replacements = {}
        batch = FormulaBatches(read, {}, {}, None)
        with Image.new('L', (200, 100), 'white') as raster:
            page = SimpleNamespace(get_size=lambda: raster.size)
            page_graphics(page, 1, [words], {}, {}, raster=raster, formula_batches=batch,
                          formula_text=replacements, formula_cells=cells)
        batch.flush([''.join(replacements.values())])
        return sent, replacements

    def test_weak_ocr_fragments_do_not_request_surya(self):
        for text in ('CD q _', 'X >го', '600 мм ± 3 мм.', 'С02, см3/м3 СО, мг/м3 N02, кг/м3'):
            with self.subTest(text=text):
                self.assertEqual(self.candidates([(20, 20, 100, 30, text)]), ([], {}))

    def test_expression_in_one_cell_requests_surya(self):
        words = [(20, 20, 30, 30, 'x'), (32, 20, 42, 30, '='), (44, 20, 54, 30, '2')]
        whole = (18, 18, 60, 32)
        self.assertEqual(self.candidates(words, dict.fromkeys(map(id, words), whole))[0], ['x = 2'])
        self.assertEqual(self.candidates([(20, 20, 60, 30, '1/2 А46')])[0], ['1/2 А46'])
        self.assertEqual(self.candidates([(20, 20, 70, 30, '~ x_a + x_b')])[0], ['~ x_a + x_b'])

    def test_expression_cannot_claim_words_from_two_cells(self):
        words = [(20, 20, 30, 30, 'x'), (32, 20, 42, 30, '='), (44, 20, 54, 30, '2')]
        left, right = (18, 18, 42, 32), (43, 18, 60, 32)
        self.assertEqual(self.candidates(words, {id(words[0]): left, id(words[1]): left, id(words[2]): right}), ([], {}))

    def test_pdf_without_confirmed_formulas_never_loads_surya(self):
        calls = []

        def prepare_backend():
            calls.append('prepare')
            return None

        with pymupdf.open() as document:
            page = document.new_page(width=420, height=410)
            page.insert_text((35, 55), 'Normal source document, without a mathematical expression.')
            page.insert_text((35, 92), 'CD q _')
            pdf = document.tobytes()
        html, stats = convert_fast_pdf(pdf, mode='balanced', image_assets={},
                                       formula_reader=local_formula_reader(prepare_backend))
        self.assertEqual(calls, [])
        self.assertEqual(stats['formula_unconfirmed'], 1)
        self.assertIn('CD q _', html)


if __name__ == '__main__':
    unittest.main()
