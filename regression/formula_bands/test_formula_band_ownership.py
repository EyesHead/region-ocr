"""Source coverage and ownership checks using real planner/document models."""
import copy
import sys
import unittest
from pathlib import Path

from PIL import ImageDraw
from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel,
                                   DoclingDocument, ProvenanceItem, Size,
                                   TableData)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_formula_band_rows import (core, formula_band_rows, packet, source_image,
                                   FRAGMENT, HEADER, FRAME, REGION)
from docling_pdf_formula_bands import recover_formula_band_routes


def provenance(box, *, page=1, length=0):
    return ProvenanceItem(page_no=page, charspan=(0, length), bbox=BoundingBox(
        l=box[0], t=box[1], r=box[2], b=box[3], origin=CoordOrigin.TOPLEFT))


class SourceRegions:
    """Supply synthetic source pixels without replacing any planner logic."""
    def __init__(self, image):
        self.image = image

    def scan_tables(self, page):
        return [copy.deepcopy(FRAME)]

    def crop(self, page, bounds):
        return self.image.crop(tuple(round(value) for value in bounds))


class FormulaBandOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.records = [packet(105, '<math display="block">a=b+c</math>', bottom=225),
                        packet(250, '<math display="block">d=e+f</math>', bottom=380)]

    def document(self):
        doc = DoclingDocument(name='positioned-equation-table')
        doc.add_page(1, Size(width=640, height=420))
        table = doc.add_table(TableData(num_rows=2, num_cols=3, table_cells=[]),
                              prov=provenance([20, 20, 620, 90]))
        entry = dict(ref=table.self_ref, page=1, bbox=[20, 20, 620, 90],
                     join_geometry=copy.deepcopy(HEADER))
        return doc, table, {table.self_ref: FRAGMENT}, {'tables': [entry]}

    def recover(self, doc, routes, manifest, image):
        excluded = set()
        repairs = recover_formula_band_routes(doc, routes, excluded, manifest,
            object(), SourceRegions(image), self.records, core)
        return repairs, excluded

    def test_small_uncovered_digit_blocks_reconstruction(self):
        with source_image(self.records) as image:
            baseline = formula_band_rows(FRAGMENT, HEADER, FRAME,
                                         self.records, image, REGION, core)
            self.assertIsNotNone(baseline)
            ImageDraw.Draw(image).text((580, 360), '1', fill='black')
            result = formula_band_rows(FRAGMENT, HEADER, FRAME,
                                      self.records, image, REGION, core)
        self.assertIsNone(result, 'A small visible digit cannot be discarded as coverage noise')

    def test_unknown_contained_text_is_not_excluded_by_its_bbox(self):
        doc, table, routes, manifest = self.document()
        unknown = doc.add_text(DocItemLabel.TEXT, 'source annotation',
            prov=provenance([560, 330, 600, 345], length=17))
        with source_image(self.records) as image:
            _, excluded = self.recover(doc, routes, manifest, image)
        self.assertNotIn(unknown.self_ref, excluded,
            'Containment does not establish that the table represents this text')

    def test_matching_text_with_another_page_provenance_is_not_excluded(self):
        doc, table, routes, manifest = self.document()
        doc.add_page(2, Size(width=640, height=420))
        original = self.records[0]['html']
        text = doc.add_text(DocItemLabel.FORMULA, '', orig=original,
            prov=provenance(self.records[0]['bbox'], length=len(original)))
        text.prov.append(provenance([40, 40, 200, 60], page=2, length=len(original)))
        with source_image(self.records) as image:
            _, excluded = self.recover(doc, routes, manifest, image)
        self.assertNotIn(text.self_ref, excluded,
            'The first provenance cannot establish ownership of the complete item')

    def test_exact_consecutive_packet_copy_is_excluded_and_table_data_updated(self):
        doc, table, routes, manifest = self.document()
        original = ' '.join(record['html'] for record in self.records)
        text = doc.add_text(DocItemLabel.FORMULA, '', orig=original,
            prov=provenance([80, 105, 540, 380], length=len(original)))
        with source_image(self.records) as image:
            repairs, excluded = self.recover(doc, routes, manifest, image)
        self.assertEqual(1, len(repairs))
        self.assertIn(text.self_ref, excluded)
        self.assertEqual((4, 3), (table.data.num_rows, table.data.num_cols))
        self.assertEqual([3, 3], [cell.col_span for cell in table.data.table_cells
                                             if cell.start_row_offset_idx >= 2])
        box = table.prov[0].bbox
        self.assertEqual(FRAME['bbox'], [box.l, box.t, box.r, box.b])
        self.assertEqual(FRAME['bbox'], manifest['tables'][0]['bbox'])


if __name__ == '__main__':
    unittest.main()
