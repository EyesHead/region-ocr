from types import SimpleNamespace
import unittest

from PIL import Image, ImageDraw

from local_web.balanced_table_pages import _ruled_preview
from local_web.balanced_table_recovery import repair_table_layer, unbound_table_blocks


class BalancedTableRecoveryTest(unittest.TestCase):
    def test_form_with_empty_values_has_ruled_preview(self):
        with Image.new('L', (455, 640), 255) as page:
            draw = ImageDraw.Draw(page)
            for x in (30, 250, 420):
                draw.line((x,100,x,355), fill=100)
            self.assertFalse(_ruled_preview(page))
            for y in range(100, 356, 15):
                draw.line((30,y,420,y), fill=100)
            self.assertTrue(_ruled_preview(page))

    def test_failed_read_keeps_healthy_neighbour_and_caption(self):
        damaged = SimpleNamespace(bbox=(5,25,155,65), evidence='verified')
        healthy = SimpleNamespace(bbox=(5,72,155,110), evidence='verified',
                                  x_grid=(5,55,105,155), y_grid=(72,110))
        rows = [[('Readable caption',10,12,80,6)],
                [('CD',10+i*3,30,2,6) for i in range(45)],
                [('Healthy original',10,80,90,6)]]
        lines = [' '.join(w[0] for w in row) for row in rows]
        reading = dict(word_rows=rows, source_lines=lines,
                       words=[(10,12,90,18,lines[0],1), (10,30,145,36,lines[1],1),
                              (10,80,100,86,lines[2],1)])
        with Image.new('L', (200,200), 255) as raster:
            page = SimpleNamespace(width=200,height=200,pixel_scale=(1,1),image=lambda: raster,
                                   text_layer=reading, grids_for_text=lambda **kw: [damaged,healthy])
            def unavailable(image, number):
                raise RuntimeError('unavailable')
            warnings = []
            repair_table_layer(page, 1, unavailable, warnings)
        self.assertTrue(warnings)
        self.assertEqual(reading['source_lines'], ['Readable caption','Healthy original'])
        self.assertEqual(reading['recovered_tables'], [damaged.bbox])
        blocks = unbound_table_blocks(reading)
        self.assertEqual([block.get('text', block.get('unbound_table')) for block in blocks],
                         ['Readable caption',damaged.bbox,'Healthy original'])
