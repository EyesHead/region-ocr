import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

import convert_docling as api
from docling_pdf_scan_recovery import read_source_scan_grid


class ScanReader(api.NativeTableReader):
    def __init__(self, width, height):
        background = dict(object_type="image", x0=0, top=0, x1=width, bottom=height)
        self.page = SimpleNamespace(width=width, height=height, images=[background])
        self.chars = []
        self.raster_text_direction_conflicts = []

    def load_page(self, page_no):
        return self.page

    def graphics(self, page_no, region):
        return self.page.images


class RasterTableTest(unittest.TestCase):
    bounds = [4, 3, 100, 99]

    def make_scan(self, dashed=False):
        image = Image.new("L", (312, 306), 255)
        self.addCleanup(image.close)
        draw = ImageDraw.Draw(image)
        for x in (12, 300):
            draw.line((x, 9, x, 297), fill=0, width=2)
        for y in range(9, 298, 36):
            draw.line((12, y, 300, y), fill=0, width=2)
        if dashed:
            for y in range(9, 297, 36):
                draw.line((156, y + 7, 156, y + 29), fill=0, width=2)
        else:
            draw.line((156, 9, 156, 297), fill=0, width=2)
        for y in range(9, 297, 36):
            for x in (65, 211):
                draw.line((x, y + 8, x, y + 28), fill=0, width=2)
                draw.line((x - 6, y + 14, x, y + 8), fill=0, width=2)
                draw.ellipse((x + 8, y + 8, x + 19, y + 28), outline=0, width=2)
        return image

    def read(self, image, records=(), extra_refs=(), glyphs=(), reader=None):
        width, height = image.size
        if reader is None:
            reader = ScanReader(width / 3, height / 3)
        reader.chars = list(glyphs)
        regions = SimpleNamespace(crop=lambda page, roi: image.crop(tuple(round(v * 3) for v in roi)))
        refs = [record["html"] for record in records] + list(extra_refs)
        result = read_source_scan_grid(reader, regions, 1, self.bounds, refs, api, primary_records=records)
        self.assertIsNotNone(result)
        result["image"].close()
        return result["native"]

    def packet(self, html=None):
        row = "<tr><td>10</td><td>10</td></tr>"
        return dict(label="Table", page_no=1, bbox=list(self.bounds),
                    html=html or "<table>" + row * 8 + "</table>")

    def rotated_glyphs(self, text):
        result = []
        for row in range(8):
            for col in range(2):
                for i, char in enumerate(text):
                    x, y = 11 + 48 * col + 1.5 * i, 6 + 12 * row
                    result.append(dict(text=char, x0=x, x1=x + 1.5, top=y, bottom=y + 2,
                                       size=2, matrix=(0, 1, -1, 0, x, y)))
        return result

    def test_rotated_hidden_words_cannot_classify_headers_or_graphic_columns(self):
        for value in ('drawing', '9999999'):
            with self.subTest(value=value):
                _, geometry = self.read(self.make_scan(), glyphs=self.rotated_glyphs(value))
                self.assertTrue(geometry['hidden_ocr_direction_conflict'])
                self.assertEqual((geometry['header_rows'], len(geometry['cells'])), (0, 16))
                self.assertFalse(any(c['scan_layout_text'] for c in geometry['cells']))
                self.assertFalse(any(e.get('source_column_context', {}).get('graphic_column')
                                     for e in geometry['retained_cell_images']))

    def test_rotated_layer_keeps_headers_proved_by_positioned_primary_rows(self):
        row = '<tr><td>10</td><td>10</td></tr>'
        header = '<tr><th>First</th><th>Second</th></tr>'
        html = '<table>' + header + row * 6 + '<tr><td colspan="2">10</td></tr></table>'
        other = html.replace('First', 'drawing').replace('Second', 'dimension')
        for value in ('drawing', '9999999'):
            with self.subTest(value=value):
                _, geometry = self.read(self.make_scan(), [self.packet(html)], [other],
                                        glyphs=self.rotated_glyphs(value))
                self.assertTrue(geometry['hidden_ocr_direction_conflict'])
                self.assertEqual((geometry['header_rows'], len(geometry['cells'])), (1, 16))
                self.assertFalse(any(e.get('source_column_context', {}).get('graphic_column')
                                     for e in geometry['retained_cell_images']))

    def test_hidden_ctm_is_remembered_for_the_cells_of_its_own_table(self):
        image = self.make_scan()
        reader = ScanReader(image.width / 3, image.height / 3)
        self.read(image, [self.packet()], glyphs=self.rotated_glyphs('9999999'), reader=reader)
        self.assertTrue(reader.has_raster_text_direction_conflict(1, [7, 5, 20, 12]))
        self.assertFalse(reader.has_raster_text_direction_conflict(1, [7, 104, 20, 111]))
        self.assertFalse(reader.has_raster_text_direction_conflict(1, [0, 0, 300, 300]))
        self.assertFalse(reader.has_raster_text_direction_conflict(2, [7, 5, 20, 12]))

    def test_repeated_digit_stems_do_not_form_columns(self):
        _, geometry = self.read(self.make_scan())
        self.assertEqual(len(geometry["cells"]), 16)
        self.assertEqual([round(x) for x in geometry["x_grid"]], [4, 52, 100])

    def test_dashed_divider_between_digit_columns_survives(self):
        _, geometry = self.read(self.make_scan(dashed=True))
        self.assertEqual([round(x) for x in geometry["x_grid"]], [4, 52, 100])

    def test_dashed_divider_beside_a_repeated_digit_survives_snapping(self):
        image = self.make_scan()
        draw = ImageDraw.Draw(image)
        for y in range(9, 297, 36):
            draw.line((60, y + 8, 60, y + 28), fill=0)
        _, geometry = self.read(image)
        self.assertEqual(len(geometry["x_grid"]), 4)
        self.assertLess(abs(geometry["x_grid"][1] - 20), 2)

    def test_complete_primary_packet_needs_only_the_table_region_crop(self):
        image = self.make_scan()
        packet = self.packet()
        crop = Image.Image.crop
        with patch.object(Image.Image, "crop", autospec=True, side_effect=crop) as crops, \
                patch.object(api, "image_html", wraps=api.image_html) as pictures:
            html, geometry = self.read(image, [packet])
        self.assertEqual((crops.call_count, pictures.call_count), (1, 0))
        self.assertEqual([c["text"] for c in api.table_profile(html)["cells"]], ["10"] * 16)
        self.assertTrue(all(c.get("primary_ocr") for c in geometry["cells"]))
        self.assertEqual(geometry["retained_cell_images"], [])

    def test_conflicting_packets_wrong_regions_and_mismatching_spans_keep_the_source(self):
        packet = self.packet()
        changed = self.packet(packet["html"].replace("10", "12"))
        merged = self.packet(packet["html"].replace("<td>10</td><td>10</td>", '<td colspan="2">10</td>', 1))
        for records in ([changed, packet], [dict(packet, page_no=2)], [dict(packet, bbox=[4, 101, 100, 197])],
                        [dict(packet, bbox=[4, 11, 100, 97])], [merged],
                        [self.packet(packet["html"] + "<p>Note below the table</p>")],
                        [self.packet(packet["html"].replace("10", '<img/>10', 1))]):
            with self.subTest(records=records):
                _, geometry = self.read(self.make_scan(), records)
                self.assertFalse(any(c.get("primary_ocr") for c in geometry["cells"]))

    def test_unmarked_source_drawing_keeps_its_pixels(self):
        image = self.make_scan()
        ImageDraw.Draw(image).polygon(((160, 11), (199, 18), (185, 41)), outline=0, width=2)
        _, geometry = self.read(image, [self.packet()])
        self.assertFalse(any(c.get("primary_ocr") for c in geometry["cells"]))
        owner = next(c for c in geometry["cells"] if (c["row"], c["column"]) == (0, 1))
        self.assertTrue(owner["scan_layout_has_ink"])

    def test_a_source_graphic_reported_by_another_candidate_keeps_the_cell_review(self):
        packet = self.packet()
        other = packet["html"].replace("10", "<img/>10", 1)
        _, geometry = self.read(self.make_scan(), [packet], [other])
        self.assertFalse(geometry.get("primary_scan_grid"))

    def test_one_pixel_in_a_primary_blank_keeps_its_physical_owner(self):
        image = self.make_scan()
        draw = ImageDraw.Draw(image)
        draw.rectangle((54, 15, 93, 41), fill=255)
        draw.point((74, 20), fill=0)
        packet = self.packet(self.packet()["html"].replace("<td>10</td>", "<td></td>", 1))
        _, geometry = self.read(image, [packet])
        owner = next(c for c in geometry["cells"] if (c["row"], c["column"]) == (0, 0))
        self.assertTrue(owner["scan_layout_has_ink"])
        self.assertFalse(owner.get("primary_ocr"))


if __name__ == "__main__":
    unittest.main()
