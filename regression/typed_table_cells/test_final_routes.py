"""Exercise real late serializers and explicit form ownership without models."""
import ast
import inspect
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from bs4 import BeautifulSoup
from PIL import Image
from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel,
                                   DoclingDocument, ImageRefMode, ProvenanceItem,
                                   Size, TableData)
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.transforms.serializer.html import HTMLTextSerializer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import convert_docling as core
from docling_pdf_forms import capture_form_figures
from docling_pdf_repairs import enabled as pdf_repair_enabled


def prov(box):
    return ProvenanceItem(page_no=1, charspan=(0, 0), bbox=BoundingBox(
        l=box[0], t=box[1], r=box[2], b=box[3], origin=CoordOrigin.TOPLEFT))


def document():
    doc = DoclingDocument(name='typed-final-route')
    doc.add_page(1, Size(width=200, height=200))
    return doc


class FormRouteTests(unittest.TestCase):
    def test_explicit_form_heading_does_not_rasterize_a_table(self):
        doc = document()
        doc.add_text(label=DocItemLabel.TEXT, text='Форма 1', prov=prov([10, 10, 190, 30]))
        table = doc.add_table(TableData(num_rows=2, num_cols=2, table_cells=[]), prov=prov([10, 50, 190, 150]))
        registry = capture_form_figures(doc, [], {}, core)
        self.assertNotIn(table.self_ref, registry)
        self.assertFalse(registry)
        self.assertFalse(doc.pictures)

    def test_raw_primary_table_after_form_heading_is_not_inserted_as_picture(self):
        doc = document()
        doc.add_text(label=DocItemLabel.TEXT, text='Форма 1', prov=prov([10, 10, 190, 30]))
        records = [dict(page_no=1, bbox=[10, 50, 190, 150], label='Table',
                        html='<table><tr><td>Текст</td><td>42</td></tr></table>')]
        self.assertFalse(capture_form_figures(doc, records, {}, core))
        self.assertFalse(doc.pictures)

    def test_form_heading_alone_does_not_create_a_raster_of_following_prose(self):
        doc = document()
        doc.add_text(label=DocItemLabel.TEXT, text='Форма 1', prov=prov([10, 10, 190, 30]))
        doc.add_text(label=DocItemLabel.TEXT, text='Текстовые поля формы', prov=prov([10, 50, 190, 80]))
        self.assertFalse(capture_form_figures(doc, [], {}, core))
        self.assertFalse(doc.pictures)

    def test_existing_picture_below_form_heading_remains_a_picture(self):
        doc = document()
        doc.add_text(label=DocItemLabel.TEXT, text='Форма 1', prov=prov([10, 10, 190, 30]))
        picture = doc.add_picture(prov=prov([10, 50, 190, 150]))
        registry = capture_form_figures(doc, [], {}, core)
        self.assertIn(picture.self_ref, registry)
        self.assertEqual(registry[picture.self_ref]['source_table_refs'], [])

    def test_form_picture_envelope_does_not_swallow_an_editable_table(self):
        doc = document()
        doc.add_text(label=DocItemLabel.TEXT, text='Форма 1', prov=prov([10, 10, 190, 30]))
        doc.add_picture(prov=prov([10, 50, 190, 180]))
        doc.add_table(TableData(num_rows=2, num_cols=2, table_cells=[]), prov=prov([20, 70, 180, 150]))
        self.assertFalse(capture_form_figures(doc, [], {}, core))


class FinalFormulaRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='final-formula-route-')
        self.source = Path(self.directory.name) / 'source.pdf'
        with Image.new('RGB', (200, 200), 'white') as image:
            image.save(self.source, 'PDF', resolution=72)
        self.regions = core.PdfRegions(self.source)
        # Execute the actual nested production serializer with its local
        # dependencies supplied explicitly. No OCR or rendering is replaced.
        tree = ast.parse(inspect.getsource(core.export_local_html))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == 'SourceFormulas')
        scope = dict(vars(core), HTMLTextSerializer=HTMLTextSerializer,
                     DocItemLabel=DocItemLabel, create_ser_result=create_ser_result,
                     pdf_repair_enabled=pdf_repair_enabled, args=SimpleNamespace(primary_ocr_only=True, pdf_repairs=()),
                     regions=self.regions, excluded=set(), note_routes={}, inline_math={},
                     inline_math_pages=set(), paragraph_refs=set(), manifest={'formulas': []}, native_reader=None,
                     orientation={1: {'clockwise': 0, 'original_size': [200, 200]}})
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        exec(compile(module, str(Path(core.__file__)), 'exec'), scope)
        self.serializer = scope['SourceFormulas']()

    def tearDown(self):
        self.regions.close()
        self.directory.cleanup()

    def formula(self, value):
        doc = document()
        item = doc.add_text(label=DocItemLabel.FORMULA, text=value, prov=prov([10, 10, 100, 60]))
        return self.serializer._process_formula(item=item, text=value, orig=value, doc=doc,
            image_mode=ImageRefMode.EMBEDDED, formula_to_mathml=True, is_inline_scope=False)

    def test_unrenderable_formula_with_repairs_disabled_stays_editable_latex(self):
        content = self.formula(r'\frac{P}{')
        parsed = BeautifulSoup(content, 'html.parser')
        self.assertIsNone(parsed.find('img'))
        self.assertIn(r'\frac{P}{', parsed.get_text())

    def test_late_caption_inline_formula_does_not_turn_whole_caption_into_crop(self):
        doc = document()
        value = r'Подпись <math>\frac{P}{</math> с продолжением'
        item = doc.add_text(label=DocItemLabel.CAPTION, text=value, prov=prov([10, 10, 190, 60]))
        result = self.serializer.serialize(item=item, doc_serializer=None, doc=doc)
        parsed = BeautifulSoup(result.text, 'html.parser')
        self.assertIsNone(parsed.find('img'))
        self.assertIn('Подпись', parsed.get_text())
        self.assertIn('с продолжением', parsed.get_text())
        self.assertIn(r'\frac{P}{', parsed.get_text())

    def test_renderable_formula_stays_mathml(self):
        parsed = BeautifulSoup(self.formula(r'\frac{P}{A}'), 'html.parser')
        self.assertIsNotNone(parsed.find('mfrac'))
        self.assertIsNone(parsed.find('img'))

    def test_existing_structured_mathml_keeps_fraction_structure(self):
        parsed = BeautifulSoup(self.formula('<math><mfrac><mi>P</mi><mi>A</mi></mfrac></math>'), 'html.parser')
        self.assertIsNotNone(parsed.find('mfrac'))
        self.assertIsNone(parsed.find('img'))


class CachedApiFormulaTests(unittest.TestCase):
    def export(self, body):
        with tempfile.TemporaryDirectory(prefix='cached-api-formula-') as directory:
            with Image.new('RGB', (200, 200), 'white') as image:
                page = SimpleNamespace(page_no=1, image=image, size=Size(width=200, height=200),
                    predictions=SimpleNamespace(vlm_response=SimpleNamespace(
                        text=body, stop_reason=SimpleNamespace(value='stop'))))
                result = SimpleNamespace(pages=[page], document=document())
                target = Path(directory) / 'converted'
                core.export_api_html(result, target)
                return BeautifulSoup(target.with_suffix('.html').read_text(encoding='utf8'), 'html.parser')

    def test_cached_api_unrenderable_formula_stays_editable_in_table_cell(self):
        parsed = self.export(r'<table><tr><td>Before <span data-formula="inline" '
            r'data-bbox="10,10,100,100">\frac{P}{</span> After</td></tr></table>')
        self.assertIsNone(parsed.find('img'))
        self.assertIn(r'\frac{P}{', parsed.td.get_text())
        self.assertIn('Before', parsed.td.get_text())
        self.assertIn('After', parsed.td.get_text())

    def test_cached_api_known_formula_crop_is_rejected_without_notation(self):
        with self.assertRaises(RuntimeError):
            self.export('<table><tr><td><img data-bbox="10,10,100,100" alt="source formula"/></td></tr></table>')

    def test_cached_api_valid_formula_stays_mathml(self):
        parsed = self.export(r'<table><tr><td><span data-formula="inline" '
            r'data-bbox="10,10,100,100">\frac{P}{A}</span></td></tr></table>')
        self.assertIsNotNone(parsed.find('mfrac'))
        self.assertIsNone(parsed.find('img'))


if __name__ == '__main__':
    unittest.main()
