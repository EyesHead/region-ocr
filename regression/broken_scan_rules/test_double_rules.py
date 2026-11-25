import unittest

from PIL import Image, ImageDraw

from local_web.balanced_table_grid import raster_grids
from local_web.balanced_table_pages import grid_attributes


class DoubleRuleTest(unittest.TestCase):
    def grid(self, *, letters=False, transpose=False):
        with Image.new('L', (600, 480), 'white') as raster:
            draw = ImageDraw.Draw(raster)
            for y in (80, 140, 144, 220, 300):
                draw.line((65, y, 540, y), fill='black')
            for x in (65, 200, 380, 540):
                draw.line((x, 80, x, 300), fill='black')
            if letters:
                draw.line((248, 141, 250, 143), fill='black')
                draw.line((250, 141, 248, 143), fill='black')
            reasons = []
            if transpose:
                with raster.transpose(Image.Transpose.TRANSPOSE) as upright:
                    return raster_grids(upright, diagnostics=reasons), reasons
            return raster_grids(raster, diagnostics=reasons), reasons

    def test_collapses_clear_pairs_in_each_axis_and_keeps_the_flag(self):
        for transpose in (False, True):
            with self.subTest(transpose=transpose):
                found, reasons = self.grid(transpose=transpose)
                self.assertEqual(len(found), 1, reasons)
                grid, = found
                self.assertEqual((grid.rows, grid.columns, len(grid.cells)), (3, 3, 9))
                self.assertEqual(grid.double_x if transpose else grid.double_y, (142.,))
                attrs = grid_attributes(grid, (1., 1.), (600, 480))
                self.assertIn('data-grid-double="1"', attrs)
                axis = 'x' if transpose else 'y'
                self.assertIn(f'data-grid-double-{axis}="142.000"', attrs)

    def test_leaves_a_pair_separate_if_there_is_ink_between_it(self):
        found, reasons = self.grid(letters=True)
        self.assertEqual(found, [])
        self.assertIn('indistinct_grid_axes', reasons)


if __name__ == '__main__':
    unittest.main()
