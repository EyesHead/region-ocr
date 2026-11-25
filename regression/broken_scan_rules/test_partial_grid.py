import unittest

from PIL import Image, ImageDraw

from local_web.balanced_table_grid import partial_raster_grids


class PartialGridTest(unittest.TestCase):
    def infer(self, columns):
        with Image.new('L', (420, 330), 'white') as image:
            draw = ImageDraw.Draw(image)
            for x in (130, 245):
                draw.line((x, 50, x, 215), fill='black')
            draw.line((30, 73, 350, 73), fill='black')
            words = [[('value', x, y, 32, 11) for x in columns] for y in (82, 113, 144, 175, 202)]
            return partial_raster_grids(image, words)

    def test_infers_table_from_internal_rules_and_aligned_words(self):
        grids = self.infer((48, 170, 280))
        self.assertEqual(len(grids), 1)
        self.assertEqual((grids[0].rows, grids[0].columns, grids[0].evidence), (5, 3, 'inferred'))

    def test_does_not_turn_two_text_columns_into_a_table(self):
        self.assertEqual(self.infer((48, 280)), [])


if __name__ == '__main__':
    unittest.main()
