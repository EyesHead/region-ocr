"""Exercise scan-grid recovery on actual raster-backed PDF fixtures.

Run from the installation root with:
    runtime\\python.exe -m unittest discover -s regression\\broken_scan_rules -v

The broken rule has the same failure shape as the Xerox scan: a narrow
gap inside a column and a sub-point displacement between its fragments.
The expected cell positions below come directly from the drawn fixture.
No OCR model, source document, or geometry implementation is mocked.
"""

from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from convert_docling import NativeTableReader


class BrokenScanRuleTests(unittest.TestCase):
    def read_fixture(self, rule):
        scale = 3
        image = Image.new("RGB", (595 * scale, 400 * scale), "white")
        draw = ImageDraw.Draw(image)

        def line(points):
            draw.line(tuple(round(value * scale) for value in points), fill="black", width=3)

        # One full-width header, followed by six ordinary two-column rows.
        line((40, 100, 550, 100))
        line((40, 100, 40, 240))
        line((550, 100, 550, 240))
        line((182, 120, 182, 240))
        for y in (120, 140, 160, 200, 220, 240):
            line((40, y, 550, y))
        line((40, 180, 182, 180))

        if rule == "complete":
            line((182, 180, 550, 180))
        elif rule == "narrow_gap":
            # 20 / 368 = 5.43% of the column; the ends meet both rails.
            line((182, 180, 497, 180))
            line((517, 180.7, 550, 180.7))
        elif rule == "wide_gap":
            line((182, 180, 400, 180))
            line((480, 180.7, 550, 180.7))
        elif rule == "short_unanchored_stroke":
            line((350, 180, 370, 180))
        elif rule != "absent":
            raise ValueError(rule)

        font = ImageFont.load_default(size=24)
        draw.text((48 * scale, 104 * scale), "Header", fill="black", font=font)
        for row, y in enumerate((124, 144, 164, 184, 204, 224), 1):
            draw.text((48 * scale, y * scale), f"Field {row}", fill="black", font=font)
            draw.text((192 * scale, y * scale), f"Value {row}", fill="black", font=font)

        region = [38, 98, 552, 242]
        source_image = image.crop(tuple(round(value * scale) for value in region))
        try:
            with tempfile.TemporaryDirectory(prefix="broken-scan-rules-") as directory:
                source = Path(directory) / "scan.pdf"
                image.save(source, "PDF", resolution=216, quality=100)
                reader = NativeTableReader(source)
                try:
                    recovered = reader.read_raster_table(1, region, source_image, _short_rules=True)
                finally:
                    reader.close()
        finally:
            source_image.close()
            image.close()
        self.assertIsNotNone(recovered, "The drawn raster table must be recovered")
        geometry = recovered[1]
        self.assertEqual(len(geometry["x_grid"]), 3)
        self.assertEqual(len(geometry["y_grid"]), 8)
        return {
            (cell["row"], cell["column"], cell["row_span"], cell["column_span"])
            for cell in geometry["cells"]
        }

    def test_complete_scan_preserves_header_colspan(self):
        self.assertEqual(self.read_fixture("complete"), {
            (0, 0, 1, 2),
            (1, 0, 1, 1), (1, 1, 1, 1),
            (2, 0, 1, 1), (2, 1, 1, 1),
            (3, 0, 1, 1), (3, 1, 1, 1),
            (4, 0, 1, 1), (4, 1, 1, 1),
            (5, 0, 1, 1), (5, 1, 1, 1),
            (6, 0, 1, 1), (6, 1, 1, 1),
        })

    def test_narrow_offset_rule_gap_does_not_merge_two_text_rows(self):
        self.assertEqual(self.read_fixture("narrow_gap"), {
            (0, 0, 1, 2),
            (1, 0, 1, 1), (1, 1, 1, 1),
            (2, 0, 1, 1), (2, 1, 1, 1),
            (3, 0, 1, 1), (3, 1, 1, 1),
            (4, 0, 1, 1), (4, 1, 1, 1),
            (5, 0, 1, 1), (5, 1, 1, 1),
            (6, 0, 1, 1), (6, 1, 1, 1),
        })

    def test_actual_rowspan_is_preserved_without_a_horizontal_rule(self):
        cells = self.read_fixture("absent")
        self.assertIn((3, 1, 2, 1), cells)
        self.assertNotIn((3, 1, 1, 1), cells)
        self.assertNotIn((4, 1, 1, 1), cells)
        self.assertEqual(len(cells), 12)

    def test_short_unanchored_stroke_does_not_split_a_merged_cell(self):
        cells = self.read_fixture("short_unanchored_stroke")
        self.assertIn((3, 1, 2, 1), cells)
        self.assertNotIn((3, 1, 1, 1), cells)
        self.assertNotIn((4, 1, 1, 1), cells)
        self.assertEqual(len(cells), 12)

    def test_wide_gap_does_not_invent_a_complete_horizontal_rule(self):
        cells = self.read_fixture("wide_gap")
        self.assertIn((3, 1, 2, 1), cells)
        self.assertNotIn((3, 1, 1, 1), cells)
        self.assertNotIn((4, 1, 1, 1), cells)
        self.assertEqual(len(cells), 12)


if __name__ == "__main__":
    unittest.main()
