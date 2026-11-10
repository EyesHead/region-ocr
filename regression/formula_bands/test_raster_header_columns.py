"""Raster discovery must retain a closed table with columns only in its header."""
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from convert_docling import RasterTableRules


def table_image(dividers=(80, 260), *, right_side=True):
    image = Image.new('RGB', (1200, 800), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((80, 80, 80, 650), fill='black', width=2)
    if right_side:
        draw.line((1120, 80, 1120, 650), fill='black', width=2)
    for y in (80, 230, 260, 380, 450, 650):
        draw.line((80, y, 1120, y), fill='black', width=2)
    for index in range(1, 14):
        x = round(80 + index * 1040 / 14)
        draw.line((x, dividers[0], x, dividers[1]), fill='black', width=2)
    return image


class RasterHeaderColumnTests(unittest.TestCase):
    def test_closed_table_keeps_fourteen_header_columns_above_merged_bands(self):
        with table_image() as image:
            grids = RasterTableRules(image, 1).tables()
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0]['x_grid']), 15)
        for measured, expected in zip(grids[0]['bbox'], (80, 80, 1120, 650)):
            self.assertAlmostEqual(measured, expected, delta=3)

    def test_free_short_strokes_do_not_establish_columns(self):
        with table_image((100, 210)) as image:
            self.assertEqual(RasterTableRules(image, 1).tables(), [])

    def test_unconnected_aligned_formula_stroke_does_not_erase_header_column(self):
        with table_image() as image:
            ImageDraw.Draw(image).line((600, 500, 600, 550), fill='black', width=2)
            grids = RasterTableRules(image, 1).tables()
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0]['x_grid']), 15)

    def test_strokes_attached_at_only_one_end_do_not_establish_columns(self):
        with table_image((100, 260)) as image:
            self.assertEqual(RasterTableRules(image, 1).tables(), [])

    def test_short_columns_do_not_replace_a_missing_outer_side(self):
        with table_image(right_side=False) as image:
            self.assertEqual(RasterTableRules(image, 1).tables(), [])

    def test_existing_full_height_columns_still_form_a_table(self):
        with table_image((80, 650)) as image:
            grids = RasterTableRules(image, 1).tables()
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0]['x_grid']), 15)


if __name__ == '__main__':
    unittest.main()
