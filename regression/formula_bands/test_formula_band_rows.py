"""Portable pixel/packet coverage checks for full-width formula table rows."""
import copy
import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import convert_docling as core
from docling_pdf_formula_bands import formula_band_rows


FRAGMENT = ('<table><thead><tr><th>Point</th><th>Value</th><th>Total</th></tr>'
            '<tr><th>1</th><th>2</th><th>3</th></tr></thead></table>')
HEADER = {'x_grid': [20, 220, 420, 620], 'y_grid': [20, 60, 90]}
FRAME = {'bbox': [20, 20, 620, 400]}
REGION = [0, 0, 640, 420]


def packet(top, html, *, bottom=None):
    return {'page_no': 1, 'label': 'Equation',
            'bbox': [80, top, 540, bottom if bottom is not None else top + 25], 'html': html}


def source_image(records, *, right_side=True):
    image = Image.new('RGB', (640, 420), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((20, 20, 20, 400), fill='black', width=2)
    if right_side:
        draw.line((620, 20, 620, 400), fill='black', width=2)
    for y in (20, 60, 90, 400):
        draw.line((20, y, 620, y), fill='black', width=2)
    for x in (220, 420):
        draw.line((x, 20, x, 90), fill='black', width=2)
    for record in records:
        left, top, _, bottom = record['bbox']
        # Short glyph runs establish real nongrid ink without introducing
        # fraction bars or synthetic table-rule-shaped rectangles.
        for y in range(top + 4, bottom - 6, 16):
            draw.text((left + 8, y), 'a = b + c; value 123', fill='black')
    return image


class FormulaBandRowsTests(unittest.TestCase):
    def setUp(self):
        self.records = [packet(125, '<math display="block">a=b+c</math>'),
                        packet(230, '<math display="block">d=e+f</math>')]

    def plan(self, image, records=None):
        return formula_band_rows(FRAGMENT, copy.deepcopy(HEADER), copy.deepcopy(FRAME),
                                 self.records if records is None else records,
                                 image, REGION, core)

    def test_blank_gap_inside_one_physical_band_yields_two_fullwidth_rows(self):
        with source_image(self.records) as image:
            result = self.plan(image)
        self.assertIsNotNone(result)
        html, proof = result
        profile = core.table_profile(html)
        self.assertFalse(profile['issues'])
        self.assertEqual((4, 3), (profile['rows'], profile['columns']))
        body = BeautifulSoup(html, 'html.parser').table.tbody
        rows = body.find_all('tr', recursive=False)
        self.assertEqual(2, len(rows))
        self.assertEqual(['3', '3'], [row.td['colspan'] for row in rows])
        self.assertEqual(['a=b+c', 'd=e+f'], [row.math.get_text() for row in rows])
        self.assertEqual((2, 2, 0), (proof['records'], proof['logical_rows'], proof['uncovered_ink_pixels']))
        self.assertGreater(proof['source_ink_pixels'], 0)

    def test_missing_outer_rail_cannot_own_equations_below_header(self):
        with source_image(self.records, right_side=False) as image:
            self.assertIsNone(self.plan(image))

    def test_unassigned_visible_ink_blocks_formula_only_reconstruction(self):
        with source_image(self.records) as image:
            ImageDraw.Draw(image).text((100, 330), 'UNASSIGNED NOTE', fill='black')
            self.assertIsNone(self.plan(image))

    def test_omitted_equation_packet_leaves_pixels_uncovered(self):
        third = packet(330, '<math display="block">g=h+i</math>')
        with source_image([*self.records, third]) as image:
            self.assertIsNone(self.plan(image))

    def test_overlapping_competing_equations_cannot_gain_duplicate_ownership(self):
        overlapping = [packet(125, '<math display="block">a=b+c</math>', bottom=175),
                       packet(150, '<math display="block">a=b-c</math>', bottom=190)]
        with source_image(overlapping) as image:
            self.assertIsNone(self.plan(image, overlapping))

    def test_tail_vertical_separator_prevents_fullwidth_rows(self):
        with source_image(self.records) as image:
            ImageDraw.Draw(image).line((220, 90, 220, 400), fill='black', width=2)
            self.assertIsNone(self.plan(image))

    def test_multiline_math_packet_stays_atomic_across_internal_blank_lines(self):
        first_html = ('<math display="block"><mfrac><mi>a</mi><mi>b</mi></mfrac></math>'
                      '<p>explanation<br/>continued text</p>'
                      '<math display="block"><msub><mi>x</mi><mn>2</mn></msub>=c</math>')
        records = [packet(120, first_html, bottom=200), self.records[1]]
        with source_image(records) as image:
            result = self.plan(image, records)
        self.assertIsNotNone(result)
        html, proof = result
        rows = BeautifulSoup(html, 'html.parser').table.tbody.find_all('tr', recursive=False)
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len(rows[0].find_all('math')))
        self.assertEqual(1, len(rows[1].find_all('math')))
        self.assertIsNotNone(rows[0].find('mfrac'))
        self.assertIsNotNone(rows[0].find('msub'))
        self.assertIn('explanation', rows[0].get_text())
        self.assertIn('continued text', rows[0].get_text())
        self.assertTrue(all(math.get('display') == 'block' for row in rows for math in row.find_all('math')))
        self.assertTrue(proof['atomic_primary_packets'])
        self.assertEqual(2, proof['logical_rows'])


if __name__ == '__main__':
    unittest.main()
