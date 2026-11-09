"""Result delivery is selected-format only; previews remain private."""
import base64
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'local_web'))
from converter import export_formats
from saved_results import SavedResults
from file_paths import normal_path

SUFFIXES = {'html': 'html', 'markdown': 'md', 'json': 'json', 'plaintext': 'text'}
EXTENSIONS = {'html': '.html', 'markdown': '.md', 'json': '.json', 'plaintext': '.txt'}


class ResultPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'Example.pdf'
        self.source.write_bytes(b'Synthetic uploaded document')

    def tearDown(self):
        self.temporary.cleanup()

    def export(self, fmt, *, images=False, destination=None):
        image = ''
        if images:
            from PIL import Image
            output = BytesIO()
            Image.new('RGB', (4, 4), color='blue').save(output, format='PNG')
            image = '<img alt="Example" src="data:image/png;base64,' + base64.b64encode(output.getvalue()).decode() + '">'
        return export_formats('<html><body><p>Example result</p>' + image + '</body></html>',
                              self.source, destination or self.root / 'private', output_format=fmt)

    def test_each_package_contains_only_selected_output_and_no_empty_src(self):
        for fmt, suffix in SUFFIXES.items():
            with self.subTest(format=fmt):
                result = self.export(fmt)
                folder = Path(result['packages'][fmt])
                self.assertEqual(folder.name, 'Example_' + suffix)
                self.assertEqual({p.name for p in folder.iterdir()}, {'Example_converted' + EXTENSIONS[fmt]})
                self.assertEqual(set(result['artifacts']), {fmt})
                self.assertTrue(Path(result['replay_html']).is_file())

    def test_referenced_images_keep_src_and_resolve_in_selected_html(self):
        result = self.export('html', images=True)
        folder = Path(result['packages']['html'])
        self.assertEqual({p.name for p in folder.iterdir()}, {'Example_converted.html', 'src'})
        self.assertTrue(list((folder / 'src').iterdir()))
        from bs4 import BeautifulSoup
        image = BeautifulSoup(Path(result['artifacts']['html']).read_text('utf-8'), 'html.parser').find('img')
        self.assertTrue((folder / image['src']).is_file())

    def test_saved_delivery_excludes_source_and_companion_but_retains_private_preview_and_log(self):
        result = self.export('json', images=True)
        saved = SavedResults(self.root / 'output')
        record = saved.save(Path(result['packages']['json']), job_id='a' * 32, name=self.source.name,
                            format='json', log='Synthetic private log', canonical=Path(result['replay_html']))
        folder = Path(record['path'])
        self.assertEqual(folder.name, 'Example_json')
        self.assertEqual({p.name for p in folder.iterdir()}, {'Example_converted.json', 'src'})
        self.assertEqual(saved.read_log(record['id']), 'Synthetic private log')
        preview = saved.target_file(record['id'], 'html')
        self.assertTrue(normal_path(preview).is_relative_to(saved.index_dir))
        self.assertEqual(preview.read_bytes(), Path(result['replay_html']).read_bytes())
        self.assertFalse(saved.backfill_html(record['id'], package=Path(result['packages']['json']),
                                             canonical=Path(result['replay_html'])))
        self.assertFalse((folder / 'Example_converted.html').exists())
        saved.delete(record['id'])
        self.assertFalse(preview.parent.exists())
        self.assertFalse((saved.logs_dir / (record['id'] + '.log')).exists())

    def test_legacy_ocr_folder_and_source_reservations_still_load(self):
        import json
        output = self.root / 'legacy-output'
        folder = output / 'Example_ocr'
        folder.mkdir(parents=True)
        (folder / 'Example_source.pdf').write_bytes(self.source.read_bytes())
        (folder / 'Example_converted.json').write_text('{}', encoding='utf-8')
        (folder / 'Example_converted.html').write_text('<html>Legacy</html>', encoding='utf-8')
        status = folder.stat()
        index = output / '.index'
        index.mkdir()
        record = {'id': 'd' * 32, 'job_id': 'a' * 32, 'name': self.source.name, 'format': 'json',
                  'folder_name': folder.name, 'created_at': '2026-11-09T00:00:00Z', 'size': 40,
                  'package_key': 'legacy', 'directory_identity': {'device': status.st_dev, 'inode': status.st_ino}}
        (index / 'results.json').write_text(json.dumps({'schema_version': 1, 'results': [record]}), encoding='utf-8')
        saved = SavedResults(output)
        self.assertTrue(saved.get(record['id'])['exists'])
        self.assertEqual(saved.target_file(record['id'], 'source').read_bytes(), self.source.read_bytes())
        self.assertIn('Legacy', saved.target_file(record['id'], 'html').read_text('utf-8'))
        saved.reserve_source('c' * 32, self.source.name, self.source)
        restored = SavedResults(output)
        self.assertEqual(restored.upload_source('c' * 32).read_bytes(), self.source.read_bytes())

    def test_saved_collision_keeps_format_as_final_suffix_and_reload_works(self):
        saved = SavedResults(self.root / 'output')
        for fmt in ('json', 'plaintext'):
            for counter in range(2):
                result = self.export(fmt, destination=self.root / f'private-{fmt}-{counter}')
                record = saved.save(Path(result['packages'][fmt]), job_id=str(counter) * 32,
                                    name=self.source.name, format=fmt, log='log',
                                    canonical=Path(result['replay_html']))
                expected = ('Example' if counter == 0 else 'Example (2)') + '_' + SUFFIXES[fmt]
                self.assertEqual(record['folder_name'], expected)
        restored = SavedResults(saved.root)
        self.assertEqual(len(restored.list()), 4)

    def test_pruning_missing_result_removes_private_preview_and_log(self):
        result = self.export('json')
        saved = SavedResults(self.root / 'output')
        record = saved.save(Path(result['packages']['json']), job_id='a' * 32, name=self.source.name,
                            format='json', log='log', canonical=Path(result['replay_html']))
        preview = saved.target_file(record['id'], 'html')
        import shutil
        shutil.rmtree(record['path'])
        self.assertEqual(saved.prune_missing(), [record['id']])
        self.assertFalse(preview.parent.exists())
        self.assertFalse((saved.logs_dir / (record['id'] + '.log')).exists())


if __name__ == '__main__':
    unittest.main()
