import base64
import json
import unittest
from contextlib import ExitStack
from io import BytesIO
from os import stat_result
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from PIL import Image

from local_web import converter, formula_preview
from local_web.export_bundle import build_exports
from local_web.file_paths import normal_path


class QualityHtmlTest(unittest.TestCase):
    def setUp(self):
        self.files = {}
        root = Path.cwd() / 'virtual-quality-publication'
        self.source = root / 'source.pdf'
        self.engine = root / 'engine.html'
        self.destination = root / 'result'
        with Image.new('L', (2, 2), 140) as image, BytesIO() as encoded:
            image.save(encoded, format='PNG')
            uri = 'data:image/png;base64,' + base64.b64encode(encoded.getvalue()).decode()
        markup = f'''<html lang="ru"><head><style>.wide {{width:100%}}</style></head><body>
          <p id="clause" data-source-ref="#/texts/4" data-source-page="8" data-source-bbox="1,2,8,7"
             style="text-indent:12px">Вводный текст</p>
          <div class="table-scroll wide"><table class="pdf-native" data-pdf-table-ref="#/tables/2" style="width:50%">
          <caption>Параметры</caption><colgroup><col style="width:40%"><col width="60%"></colgroup>
          <thead><tr><th id="heading" scope="colgroup" colspan="2">Показатель</th></tr></thead><tbody>
          <tr class="row"><th rowspan="2" scope="row">A</th><td headers="heading" data-source-cell="true">
          <p class="cell"><img src="{uri}" width="200" alt="" data-source-cell="true"></p></td></tr>
          <tr><td><math display="inline" data-latex="x_2"><msub><mi>x</mi><mn>2</mn></msub></math></td></tr>
          </tbody></table></div><figure><img src="{uri}" alt="Разрез"><figcaption>Рисунок 1</figcaption></figure>
          <div data-formula-comparison="true" data-pdf-page="8"><math><mi>y</mi></math>
          <details data-preview-only="formula-source"><summary>Оригинал</summary><img src="{uri}"></details></div>
          </body></html>'''
        self.files[self.source] = b'%PDF'
        self.files[self.engine] = markup.encode('utf-8')
        self.markup = markup
        stack = self.enterContext(ExitStack())
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if normal_path(path) in self.files:
                size = len(self.files[normal_path(path)])
                return stat_result((0o100644, 0, 0, 1, 0, 0, size, 0, 0, 0))
            return original_stat(path, *args, **kwargs)

        # Only disk I/O and the OCR process are replaced. Both exporters, pixel
        # validation, manifest hashing and the replay reader run normally.
        for name, function in {
            'mkdir': lambda path, *args, **kwargs: None,
            'is_file': lambda path: normal_path(path) in self.files,
            'exists': lambda path: normal_path(path) in self.files,
            'stat': stat,
            'read_bytes': lambda path: self.files[normal_path(path)],
            'read_text': lambda path, encoding='utf-8': self.files[normal_path(path)].decode(encoding),
            'write_bytes': lambda path, data: self.files.__setitem__(normal_path(path), bytes(data)),
            'write_text': lambda path, value, encoding='utf-8': self.files.__setitem__(normal_path(path), value.encode(encoding)),
        }.items():
            stack.enter_context(patch.object(Path, name, function))
        stack.enter_context(patch('shutil.copy2', lambda src, dst: self.files.__setitem__(normal_path(dst), self.files[normal_path(src)])))
        stack.enter_context(patch.object(converter, '_run_engine', return_value={'html': str(self.engine), 'pages': 1}))

    def convert(self, fmt, replay=None):
        result = converter.convert_file(self.source, self.destination, output_format=fmt, replay_html=replay)
        return result, Path(result['artifacts'][fmt]).read_text('utf-8')

    def test_quality_html_carries_the_grid_and_source_images(self):
        result, html = self.convert('html')
        soup = BeautifulSoup(html, 'html.parser')
        self.assertEqual(soup.table.attrs, {})
        self.assertFalse(soup.select('style, [style], [class], [width], [data-source-cell], [alt=""]'))
        self.assertEqual(soup.table.parent.name, 'body')
        self.assertEqual(soup.caption.get_text(strip=True), 'Параметры')
        self.assertEqual(soup.th.attrs, {'id': 'heading', 'scope': 'colgroup', 'colspan': '2'})
        self.assertEqual(soup.tbody.th['rowspan'], '2')
        self.assertEqual(soup.td['headers'], ['heading'])
        self.assertEqual(soup.td.p.attrs, {})
        self.assertEqual(soup.td.img.attrs, {'src': result['assets'][0]['path']})
        self.assertEqual(soup.figure.img['alt'], 'Разрез')
        self.assertEqual(soup.figcaption.get_text(), 'Рисунок 1')
        self.assertEqual(soup.tbody.math['data-latex'], 'x_2')
        self.assertEqual(soup.tbody.math.msub.mn.string, '2')
        self.assertIsNone(soup.details)
        canonical = Path(result['replay_html'])
        self.assertEqual(canonical.read_text('utf-8'), html)
        info = formula_preview.preview_info(canonical)
        viewer = BeautifulSoup(formula_preview.preview_file(canonical.parent, info).read_bytes(), 'html.parser')
        self.assertEqual(viewer.table.attrs, {})
        self.assertIn('formula-src/', viewer.details.img['src'])

    def test_replay_keeps_json_provenance_and_compact_markdown(self):
        result, _ = self.convert('html')
        replay = result['replay_html']
        _, encoded = self.convert('json', replay)
        blocks = json.loads(encoded)['blocks']
        paragraph = next(block for block in blocks if block.get('id') == 'clause')
        self.assertEqual(paragraph['source_page'], 8)
        self.assertEqual(paragraph['source_bbox'], [1, 2, 8, 7])
        self.assertEqual(paragraph['source_ref'], '#/texts/4')
        table = next(block for block in blocks if block['type'] == 'table')
        self.assertEqual(table['id'], 'pdf-tables-2')
        self.assertEqual(table['meta'], {'rows': 3, 'cols': 2})
        _, markdown = self.convert('markdown', replay)
        inline_table = BeautifulSoup(markdown, 'html.parser').table
        self.assertEqual(inline_table.attrs, {})
        self.assertFalse(inline_table.select('[style], [width], [data-source-cell], [class]'))
        self.assertEqual(inline_table.tbody.th['rowspan'], '2')
        self.assertIn('x_2', str(inline_table))
        path = Path(replay).parent / result['assets'][0]['path']
        self.files[normal_path(path)] = b'changed'
        with self.assertRaisesRegex(ValueError, 'изменён'):
            self.convert('html', replay)

    def test_html_import_keeps_its_layout(self):
        source = self.source.with_suffix('.html')
        self.files[source] = self.markup.encode('utf-8')
        result = converter.convert_file(source, self.destination, output_format='html')
        html = Path(result['artifacts']['html']).read_text('utf-8')
        self.assertEqual(BeautifulSoup(html, 'html.parser').table['class'], ['pdf-native'])

    def test_replay_detects_changed_or_missing_source_attributes(self):
        result, _ = self.convert('html')
        canonical = Path(result['replay_html'])
        manifest_file = canonical.parent / 'manifest.json'
        original = manifest_file.read_text('utf-8')
        # Both are syntactically valid pages. Only the recorded one belongs to this paragraph.
        self.assertIn('"data-source-page": "8"', original)
        for changed in (original.replace('"data-source-page": "8"', '"data-source-page": "9"'),
                        json.dumps({key: value for key, value in json.loads(original).items()
                                    if key != 'quality_pdf_sources'})):
            manifest_file.write_text(changed, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'привяз'):
                self.convert('json', canonical)

    def test_replay_retains_comparison_crops_through_schema_migration(self):
        result, _ = self.convert('html')
        original = Path(result['replay_html'])
        _, baseline_json = self.convert('json', original)
        path = original.parent / 'manifest.json'
        manifest = json.loads(path.read_text('utf-8'))
        manifest['export_schema'] -= 1
        path.write_text(json.dumps(manifest), encoding='utf-8')
        updated, encoded = self.convert('json', original)
        canonical = Path(updated['replay_html'])
        self.assertNotEqual(canonical, original)
        self.assertEqual(json.loads(encoded), json.loads(baseline_json))
        info = formula_preview.preview_info(canonical)
        self.assertEqual(info['images'], manifest['formula_preview']['images'])
        preview = BeautifulSoup(formula_preview.preview_file(canonical.parent, info).read_bytes(), 'html.parser')
        self.assertEqual(preview.table.attrs, {})
        self.assertTrue(preview.details.img['src'].startswith('formula-src/'))

    def test_body_only_snapshot_retains_source_metadata_on_replay(self):
        content = '<body><p data-source-ref="#/texts/1">Источник</p></body>'
        result = build_exports(content, self.source, self.destination, output_format='html', quality_pdf=True)
        _, encoded = self.convert('json', result['replay_html'])
        self.assertEqual(json.loads(encoded)['blocks'][0].get('source_ref'), '#/texts/1')

    def test_fast_and_balanced_pdf_keep_their_published_attributes(self):
        for mode in ('fast', 'balanced'):
            response = {'content': self.markup, 'stats': {'pages': 1, 'native_pages': 1}, 'fast_assets': {}}
            with self.subTest(mode=mode), patch.object(converter, '_run_engine', return_value=response):
                result = converter.convert_file(self.source, self.destination, pdf_mode=mode, output_format='html')
                soup = BeautifulSoup(Path(result['artifacts']['html']).read_text('utf-8'), 'html.parser')
                self.assertEqual(soup.table.get('class'), ['pdf-native'])
                self.assertEqual(soup.td.get('data-source-cell'), 'true')

    def test_cli_discards_column_widths_and_keeps_math_spacing(self):
        from convert_docling import finalize_published_html
        content = '''<html><head></head><body>
          <table style="width:100%"><colgroup><col width="30"></colgroup><tr><td>
          <math><mrow><mi>x</mi><mspace width=".2em"></mspace><mi>y</mi></mrow></math>
          </td></tr></table></body></html>'''
        soup = BeautifulSoup(finalize_published_html(self.engine, content, quality_pdf=True), 'html.parser')
        self.assertEqual(soup.table.attrs, {})
        self.assertIsNone(soup.colgroup)
        self.assertEqual(soup.math.mspace['width'], '.2em')
        ordinary = BeautifulSoup(finalize_published_html(self.engine, content), 'html.parser')
        self.assertEqual(ordinary.col['width'], '30')
