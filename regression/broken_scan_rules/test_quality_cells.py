import unittest
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

import convert_docling as api
import docling_pdf_table_content as content
from docling_pdf_ocr import InvalidLayoutOutput, recovering_generate
from docling_pdf_typed_lines import recover_typed_text_lines


class QualityCellTest(unittest.TestCase):
    position = dict(row=0, column=0, row_span=1, column_span=1)
    source_asset = '<img data-source-cell="true" src="data:image/png;base64,YQ=="/>'

    def recognize(self, image, layout, *, failure=None, block='<p>105</p>', verified=True):
        from surya.inference.schema import BatchOutputItem

        def request(batch):
            replies = []
            for item in batch:
                if item.prompt_type == 'layout':
                    if isinstance(layout, BaseException):
                        raise layout
                    raw, stop = layout
                else:
                    self.assertEqual(item.prompt_type, 'block')
                    self.assertEqual(item.image.size, image.size)
                    self.assertEqual(item.image.tobytes(), image.tobytes())
                    if failure:
                        raise failure
                    raw, stop = block, 'stop'
                replies.append(BatchOutputItem(raw=raw, token_count=12, error=False,
                                               metadata=item.metadata, finish_reason=stop))
            return replies

        manager = SimpleNamespace(generate=lambda batch: recovering_generate(batch, request, api.diagnostic_event))
        model = SimpleNamespace(_get_runtime=lambda: SimpleNamespace(_manager=manager))
        cell = dict(self.position, source_grid_verified=verified, physical_cell_proven=verified)
        with patch.object(api, 'local_surya_backend', return_value=manager):
            return content.recognize_typed_crop(image, SimpleNamespace(surya_ocr_model=model), 1, cell, api)

    def test_invalid_cell_layout_sends_both_edges_to_block_ocr(self):
        bad_layouts = [
            ('{"incomplete":', 'stop'),
            ('[{"label":"Text","bbox":"0 0 1100 500"}]', 'stop'),
            ('[{"label":"Text","bbox":"0 0"}]', 'stop'),
            ('[{"label":"Text","bbox":"0 0 900 900"}]', 'length'),
        ]
        with Image.new('RGB', (48, 24), 'white') as image:
            draw = ImageDraw.Draw(image)
            draw.line((0, 4, 0, 19), fill='black')
            draw.line((47, 4, 47, 19), fill='black')
            for case in bad_layouts:
                with self.subTest(layout=case):
                    blocks = self.recognize(image, case)
                    self.assertEqual([b['html'] for b in blocks], ['<p>105</p>'])
                    self.assertEqual(blocks[0]['bbox'], [0, 0, 1000, 1000])

    def test_layout_and_block_backend_failure_propagate(self):
        failure = RuntimeError('llama-server exited')
        with Image.new('RGB', (48, 24), 'white') as image:
            for layout, block in ((failure, None), (('malformed', 'stop'), failure)):
                with self.subTest(layout=layout), self.assertRaisesRegex(RuntimeError, 'llama-server exited'):
                    self.recognize(image, layout, failure=block)

    def test_unlocated_region_does_not_borrow_physical_cell_coordinates(self):
        with Image.new('RGB', (30, 20), 'white') as image:
            with self.assertRaises(InvalidLayoutOutput):
                self.recognize(image, ('malformed', 'stop'), verified=False)

    def test_truncated_block_does_not_supply_a_numeric_value(self):
        with Image.new('RGB', (48, 24), 'white') as image:
            with self.assertRaises(content.TableContentError):
                self.recognize(image, ('malformed', 'stop'), block='<p>10')

    def normalize(self, image, markup, *, records=(), cell_data=None, geometry_data=None, recognize=None):
        box = [0, 0, image.width, image.height]
        cell = dict(self.position, bbox=box, **(cell_data or {}))
        geometry = dict(bbox=box, x_grid=[0, image.width], y_grid=[0, image.height],
                        source_grid_verified=True, cells=[cell], **(geometry_data or {}))
        manifest = {}
        encoder = Mock(return_value='<img src="data:image/png;base64,YQ=="/>')
        args = SimpleNamespace(surya_records=records)
        with patch.object(api, 'image_html', encoder):
            html = content.normalize_table_content('<table><tr><td>' + markup + '</td></tr></table>',
                geometry, image, box, 1, args, manifest, api, ref='#/tables/0',
                recognize=recognize or Mock(side_effect=content.TableContentError('unreadable block')))
        return BeautifulSoup(html, 'html.parser').td, manifest, cell, encoder

    def test_partial_cell_keeps_editable_content_without_encoding_a_review_image(self):
        with Image.new('RGB', (64, 40), 'white') as image:
            ImageDraw.Draw(image).rectangle((10, 10, 20, 20), fill='black')
            for existing, extra in (
                    ('<b>5</b><sup>2</sup>', {}),
                    ('', dict(pending_primary_html='<b>5</b><sup>2</sup>'))):
                with self.subTest(existing=existing):
                    td, manifest, cell, encoder = self.normalize(image, self.source_asset + existing, cell_data=extra)
                    self.assertEqual(td.decode_contents(), '<b>5</b><sup>2</sup>')
                    encoder.assert_not_called()
                    self.assertTrue(cell['unresolved_typed_content'])
                    self.assertEqual(manifest['unresolved_table_cells'][0]['source_bbox'], [0, 0, 64, 40])
                    self.assertFalse(manifest.get('table_assembly_reviews'))
                    self.assertFalse(manifest.get('typed_table_picture_assets'))

    def test_partial_cell_keeps_only_owned_primary_packets(self):
        records = [
            dict(page_no=1, bbox=[4, 4, 16, 16], html='<em>18</em>', label='Text'),
            dict(page_no=1, bbox=[4, 23, 20, 34], html='<b>мм</b>', label='Text'),
            dict(page_no=1, bbox=[58, 4, 92, 18], html='<b>999</b>', label='Text'),
        ]
        with Image.new('RGB', (64, 40), 'white') as image:
            ImageDraw.Draw(image).rectangle((38, 16, 44, 26), fill='black')
            td, manifest, cell, encoder = self.normalize(image, self.source_asset, records=records)
        self.assertEqual(td.get_text(' ', strip=True), '18 мм')
        self.assertEqual((td.em.string, td.b.string), ('18', 'мм'))
        self.assertEqual(len(manifest['typed_table_primary_ownership']), 2)
        self.assertTrue(cell['unresolved_typed_content'])
        encoder.assert_not_called()

    def test_absent_source_image_does_not_move_the_last_cells_ocr_into_another_cell(self):
        boxes = [[0, 0, 30, 30], [30, 0, 60, 30]]
        cells = [dict(self.position, column=i, bbox=box, scan_layout_has_ink=True) for i, box in enumerate(boxes)]
        records = [dict(page_no=1, bbox=[x+5, 5, x+20, 20], label='Text', html=word)
                   for x, word in ((0, 'west'), (30, 'east'))]
        geometry = dict(bbox=[0, 0, 60, 30], cells=cells, x_grid=[0, 30, 60], source_grid_verified=True)
        result = content.normalize_table_content('<table><tr><td></td><td></td></tr></table>',
            geometry, None, geometry['bbox'], 1, SimpleNamespace(surya_records=records), {}, api, ref='#/tables/0')
        self.assertEqual([td.get_text() for td in BeautifulSoup(result, 'html.parser').find_all('td')], ['west', 'east'])

    def test_existing_styled_packet_and_its_positioned_neighbor_each_appear_once(self):
        records = [dict(page_no=1, bbox=[6, 6, 20, 15], label='Text', html='18'),
                   dict(page_no=1, bbox=[6, 21, 22, 32], label='Text', html='<b>мм</b>')]
        with Image.new('RGB', (64, 40), 'white') as image:
            ImageDraw.Draw(image).rectangle((44, 19, 50, 28), fill='black')
            td, manifest, _, encoder = self.normalize(image, self.source_asset + '<em>18</em>', records=records)
        self.assertEqual(td.get_text(' ', strip=True), '18 мм')
        self.assertEqual((td.em.string, td.b.string), ('18', 'мм'))
        self.assertEqual(len(manifest['typed_table_primary_ownership']), 2)
        encoder.assert_not_called()

    def test_hidden_number_missing_a_digit_is_not_chosen_over_anchored_ocr(self):
        box = [0, 0, 64, 40]
        cell = dict(self.position, bbox=box, scan_layout_has_ink=True)
        td = BeautifulSoup('<td></td>', 'html.parser').td
        glyphs = [dict(text=value, x0=x, x1=x+5, top=8, bottom=23, size=15)
                  for x, value in ((10, '1'), (22, '5'))]
        reader = SimpleNamespace(chars=glyphs, load_page=lambda page: None,
                                 has_raster_text_direction_conflict=lambda page, bounds: False)
        numeric_ocr = SimpleNamespace(recognize_txt=lambda views: SimpleNamespace(txts=['15', '15'], scores=[1, 1]))
        with Image.new('RGB', (64, 40), 'white') as image:
            draw = ImageDraw.Draw(image)
            for glyph in glyphs:
                draw.rectangle((glyph['x0'], glyph['top'], glyph['x1'], glyph['bottom']), fill='black')
            with patch.object(api, 'native_cell_lines', return_value=[dict(text='15')]), patch.object(
                    content, '_decimal_line_recognizer', return_value=numeric_ocr):
                verified = content._verified_scan_cell_literals(
                    [(td, dict(self.position, header=False), cell)], dict(page_scan_transcription_required=True),
                    image, box, 1, SimpleNamespace(), '#/tables/0', reader, dict(verified=True, roles={}), api,
                    {(0, 0): ('105', dict(primary_row=0, anchor_column=1))})
        self.assertEqual(verified, {})

    def test_confirmed_diagram_survives_an_untyped_image_in_the_same_cell(self):
        proof = dict(page=1, ref='#/tables/0', row=0, column=0, bbox=[0, 0, 64, 40],
                     source_image_sha256=sha256(b'b').hexdigest())
        drawing = '<img data-source-cell="true" src="data:image/png;base64,Yg=="/>'
        with Image.new('RGB', (64, 40), 'white') as image:
            td, manifest, _, encoder = self.normalize(image, drawing + self.source_asset + '<i>ось</i>',
                                                    geometry_data=dict(source_graphic_assets=[proof]))
        self.assertEqual([img['src'] for img in td.find_all('img')], ['data:image/png;base64,Yg=='])
        self.assertEqual(td.i.string, 'ось')
        self.assertEqual(manifest['typed_table_picture_assets'][0]['source_image_sha256'], proof['source_image_sha256'])
        encoder.assert_not_called()

    def test_frame_or_unverified_hidden_number_never_supplies_a_value(self):
        with Image.new('RGB', (64, 40), 'white') as image:
            ImageDraw.Draw(image).rectangle((0, 0, 63, 39), outline='black')
            td, manifest, cell, encoder = self.normalize(image, self.source_asset,
                cell_data=dict(scan_layout_text='999', text='999'))
        self.assertEqual(td.decode_contents(), '')
        self.assertTrue(cell['unresolved_typed_content'])
        self.assertTrue(manifest['unresolved_table_cells'])
        encoder.assert_not_called()

    def test_ink_flag_requests_text_in_an_empty_cell_without_a_raster_placeholder(self):
        with Image.new('RGB', (64, 40), 'white') as image:
            ImageDraw.Draw(image).rectangle((4, 8, 15, 23), fill='black')
            recognize = lambda *args: [dict(label='Text', bbox=[0, 0, 1000, 1000], html='<b>105</b>')]
            td, manifest, cell, encoder = self.normalize(image, '', cell_data=dict(scan_layout_has_ink=True),
                                                        recognize=recognize)
        self.assertEqual(td.decode_contents(), '<b>105</b>')
        self.assertFalse(manifest.get('unresolved_table_cells'))
        encoder.assert_not_called()

    def test_primary_boxes_must_cover_an_edge_digit(self):
        with Image.new('RGB', (64, 40), 'white') as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((14, 12, 20, 23), fill='black')
            draw.line((1, 12, 1, 23), fill='black')
            with self.assertRaises(content.IncompleteCellCoverage):
                content._require_ink_coverage(image, [[200, 250, 350, 650]], physical_cell=True)

    def test_positioned_native_proof_recovers_after_a_failed_block_request(self):
        from docling_pdf_text_cells import content_fingerprint

        markup, box = '<em>47</em>', [0, 0, 64, 40]
        proof = dict(basis='positioned_pdf_text_covers_source_cell_ink', bbox=box,
                     html_sha256=content_fingerprint(markup), ink_coverage=1, glyph_ink_support=1)
        cell = dict(positioned_source_text=proof, lines=[dict(html=markup)],
                    glyphs=[dict(text='47', bbox=[8, 8, 20, 22])])
        with Image.new('RGB', (64, 40), 'white') as image:
            ImageDraw.Draw(image).rectangle((10, 10, 15, 20), fill='black')
            td, manifest, _, encoder = self.normalize(image, self.source_asset, cell_data=cell)
        self.assertEqual(td.decode_contents(), markup)
        self.assertFalse(manifest.get('unresolved_table_cells'))
        self.assertEqual(manifest['typed_table_text_anchors'][0]['method'],
                         'verified_native_cell_after_incomplete_ocr')
        encoder.assert_not_called()

    def test_source_glyphs_support_ocr_case_without_replacing_it(self):
        glyphs = [dict(text='А', x0=10, x1=16, top=9, bottom=20, size=11)]
        reader = SimpleNamespace(chars=glyphs, load_page=lambda page: None,
                                 has_raster_text_direction_conflict=lambda page, box: False)
        with Image.new('RGB', (38, 30), 'white') as image:
            ImageDraw.Draw(image).rectangle((10, 9, 16, 20), fill='black')
            with patch.object(api, 'native_cell_lines', return_value=[dict(text='А')]):
                result = content._source_text_coverage(
                    image, [{'label': 'Text'}], reader, [0, 0, 38, 30], 1, api, '<b>а</b>')
        self.assertIsNotNone(result)

    def test_failed_prose_line_only_leaves_a_diagnostic(self):
        with Image.new('RGB', (96, 80), 'white') as image:
            draw = ImageDraw.Draw(image)
            for y in (16, 43):
                for x in (12, 24, 36, 48):
                    draw.rectangle((x, y, x + 4, y + 7), fill='black')
            with patch('docling_pdf_primary_cells._complete_line_reading', side_effect=[
                    ('<em>AB</em>', ['digest'], ['surya']), None, None]):
                result = recover_typed_text_lines(image, SimpleNamespace(), 1, self.position, api)
        self.assertIsNotNone(result)
        fragment, proof = result
        self.assertEqual(fragment, '<em>AB</em>')
        self.assertEqual(proof['unconfirmed_source_lines'][0]['line'], 2)

    def test_small_hidden_layer_difference_preserves_recognized_word(self):
        chars = [dict(text=ch, x0=10+5*i, x1=14+5*i, top=7, bottom=18, size=11)
                 for i, ch in enumerate('слово')]
        reader = SimpleNamespace(chars=chars, load_page=lambda page: None)
        with Image.new('RGB', (80, 35), 'white') as image, patch.object(api, 'native_cell_lines',
                return_value=[{'text': 'тест слово'}]), patch.object(content, '_source_text_coverage',
                return_value=([], {'ink_coverage': 1})):
            for recognized in ('СЛОВО', 'слва', 'слобо'):
                with self.subTest(recognized=recognized):
                    self.assertIsNone(content._reconcile_cell_ending(
                        'тест <b>' + recognized + '</b>', 'тест слово', image, [0, 0, 80, 35], 1, reader, api))

    def test_single_letter_conflict_does_not_create_a_table_warning(self):
        manifest = {}
        with patch.object(content, '_source_text_coverage', return_value=([], {'ink_coverage': 1})):
            content._review_disputed_letter('Б', 'В', None, [0, 0, 10, 10], 1, object(),
                                           self.position, '#/tables/0', api, manifest)
        self.assertFalse(manifest.get('table_assembly_reviews'))


if __name__ == '__main__':
    unittest.main()
