"""Synthetic HTTP regressions for result location and document log lifetime."""
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(os.environ.get('REGION_OCR_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'local_web'))
from converter import export_formats
from server import JobStore, LocalServer
import result_storage


def synthetic_converter(source, destination, *, output_format='html', **options):
    destination.mkdir(parents=True, exist_ok=True)
    logs = destination / 'engine/logs'
    logs.mkdir(parents=True, exist_ok=True)
    (logs / 'conversion.log').write_text('Synthetic per-document engine log', encoding='utf-8')
    return export_formats('<html><body><p>Synthetic result</p></body></html>', source, destination,
                          output_format=output_format)


class StorageLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.output = self.root / 'first-output'
        self.store = JobStore(self.data, synthetic_converter, output_dir=self.output)
        self.server = LocalServer(('127.0.0.1', 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.store.stop()
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode('utf-8')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, {'X-Local-App': '1'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def convert(self, name='synthetic.html'):
        source = b'<html><body>synthetic source</body></html>'
        job = self.store.upload(io.BytesIO(source), name, len(source))
        ident = job['id']
        self.store.start([ident], '', 'json')
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.store.jobs[ident]['status'] == 'done' and ident not in self.store.scheduled:
                return ident
            time.sleep(.01)
        self.fail('Synthetic conversion did not finish')

    def test_upload_does_not_copy_original_into_result_directory(self):
        ident = self.convert()
        self.assertTrue((self.data / ident / 'input/synthetic.html').is_file())
        self.assertEqual([p.name for p in self.output.iterdir() if p.name != '.index'], [])

    def test_setting_is_persistent_and_preserves_old_history_and_files(self):
        ident = self.convert()
        first = self.store.save_result(ident)
        status, settings = self.request('GET', '/api/settings')
        self.assertEqual(status, 200)
        self.assertEqual(Path(settings['output_dir']), self.output)
        second_output = self.root / 'second-output'
        status, settings = self.request('POST', '/api/settings', {'output_dir': str(second_output)})
        self.assertEqual(status, 200, settings)
        self.assertEqual(Path(settings['output_dir']), second_output)
        self.assertTrue(Path(first['path']).is_dir())
        second = self.store.save_result(ident)
        self.assertEqual(Path(second['path']).parent, second_output)
        self.assertEqual({x['id'] for x in self.store.history()}, {first['id'], second['id']})
        other = JobStore(self.data, synthetic_converter, output_dir=self.output)
        try:
            self.assertEqual(other.saved.root, second_output)
            self.assertEqual({x['id'] for x in other.history()}, {first['id'], second['id']})
        finally:
            other.stop()

    def test_invalid_location_is_rejected_without_changing_selection(self):
        existing_file = self.root / 'file.txt'
        existing_file.write_text('keep', encoding='utf-8')
        for candidate in ('relative-output', '', str(self.data), str(self.data / 'engine_runtime'),
                          str(ROOT / 'runtime'), str(existing_file), str(self.root.anchor)):
            with self.subTest(candidate=candidate):
                status, _ = self.request('POST', '/api/settings', {'output_dir': candidate})
                self.assertIn(status, (400, 409))
                self.assertEqual(self.store.saved.root, self.output)
        self.assertEqual(existing_file.read_text('utf-8'), 'keep')

    def test_unavailable_previous_or_selected_location_keeps_settings_usable(self):
        ident = self.convert()
        self.store.save_result(ident)
        second = self.root / 'second-output'
        self.store.change_output_dir(str(second))
        self.store.save_result(ident)
        original_init = result_storage.SavedResults.__init__
        for unavailable in (self.output, second):
            def blocked(manager, folder):
                if Path(folder) == unavailable:
                    raise PermissionError('Synthetic unavailable directory')
                return original_init(manager, folder)
            with self.subTest(unavailable=unavailable), \
                 mock.patch.object(result_storage.SavedResults, '__init__', blocked):
                other = JobStore(self.data, synthetic_converter, output_dir=self.output)
                try:
                    self.assertEqual(other.settings()['output_dir'], str(second))
                    if unavailable == second:
                        self.assertTrue(other.snapshot()['storage_error'])
                        from server import APIError
                        with self.assertRaises(APIError) as raised:
                            other.save_result(ident)
                        self.assertEqual(raised.exception.status, 409)
                    else:
                        self.assertFalse(other.snapshot()['storage_error'])
                    new_output = self.root / ('third-' + unavailable.name)
                    other.change_output_dir(str(new_output))
                    saved = json.loads((self.data / 'storage-settings.json').read_text('utf-8'))
                    self.assertIn(str(unavailable), saved['output_roots'])
                finally:
                    other.stop()
            # Restore the same selected root for the second startup scenario.
            self.store.change_output_dir(str(second))

    def _prepare_logs_in_two_locations(self):
        ident = self.convert()
        first = self.store.save_result(ident)
        other = self.root / 'second-output'
        status, result = self.request('POST', '/api/settings', {'output_dir': str(other)})
        self.assertEqual(status, 200, result)
        second = self.store.save_result(ident)
        archived = self.data / ident / 'log_history/first'
        archived.mkdir(parents=True)
        (archived / 'old.log').write_text('old engine log', encoding='utf-8')
        shared = self.data / 'application.log'
        shared.write_text('shared application evidence', encoding='utf-8')
        for root, record in ((self.output, first), (other, second)):
            self.assertTrue((root / '.index/logs' / (record['id'] + '.log')).is_file())
        return ident, first, second, other, shared

    def test_reconnected_old_location_is_reloaded_before_deleting_logs(self):
        ident, first, second, second_root, shared = self._prepare_logs_in_two_locations()
        original_init = result_storage.SavedResults.__init__
        def blocked(manager, folder):
            if Path(folder) == self.output:
                raise PermissionError('Synthetic offline output')
            return original_init(manager, folder)
        other = None
        try:
            with mock.patch.object(result_storage.SavedResults, '__init__', blocked):
                other = JobStore(self.data, synthetic_converter, output_dir=self.output)
                from server import APIError
                with self.assertRaises(APIError) as raised:
                    other.delete(ident)
                self.assertEqual(raised.exception.status, 409)
                self.assertTrue(Path(first['path']).is_dir())
                self.assertTrue((self.data / ident / 'conversion.log').is_file())
            other.refresh_history()
            self.assertEqual({item['id'] for item in other.history()}, {first['id'], second['id']})
            other.delete(ident)
            self.assertFalse((self.data / ident).exists())
            for root, record in ((self.output, first), (second_root, second)):
                self.assertFalse(Path(record['path']).exists())
                self.assertFalse((root / '.index/logs' / (record['id'] + '.log')).exists())
        finally:
            if other is not None:
                other.stop()

    def test_interrupted_upload_is_cleaned_even_when_an_old_location_is_offline(self):
        self.store.change_output_dir(str(self.root / 'second-output'))
        original_init = result_storage.SavedResults.__init__
        def blocked(manager, folder):
            if Path(folder) == self.output:
                raise PermissionError('Synthetic offline output')
            return original_init(manager, folder)
        with mock.patch.object(result_storage.SavedResults, '__init__', blocked):
            other = JobStore(self.data, synthetic_converter, output_dir=self.output)
            try:
                from server import APIError
                with self.assertRaises(APIError) as raised:
                    other.upload(io.BytesIO(b''), 'interrupted.html', 10)
                self.assertIn('оборвалась', str(raised.exception))
                self.assertEqual([folder for folder in self.data.iterdir() if folder.is_dir()], [])
            finally:
                other.stop()

    def _assert_cleaned(self, ident, first, second, other, shared):
        self.assertFalse((self.data / ident).exists())
        self.assertNotIn(ident, self.store.jobs)
        for root, record in ((self.output, first), (other, second)):
            self.assertFalse(Path(record['path']).exists())
            self.assertFalse((root / '.index/logs' / (record['id'] + '.log')).exists())
            self.assertFalse((root / '.index/previews' / record['id']).exists())
        self.assertTrue(shared.is_file())
        self.assertEqual(self.store.history(), [])

    def test_delete_from_document_menu_removes_all_document_logs(self):
        values = self._prepare_logs_in_two_locations()
        self.assertEqual(self.request('DELETE', '/api/jobs/' + values[0])[0], 200)
        self._assert_cleaned(*values)

    def test_delete_from_history_removes_all_document_logs(self):
        values = self._prepare_logs_in_two_locations()
        self.assertEqual(self.request('DELETE', '/api/installed/' + values[1]['id'])[0], 200)
        self._assert_cleaned(*values)

    def test_hide_keeps_log_and_history(self):
        ident = self.convert()
        saved = self.store.save_result(ident)
        status, _ = self.request('POST', '/api/jobs/' + ident + '/hide', {})
        self.assertEqual(status, 200)
        self.assertTrue((self.data / ident / 'conversion.log').is_file())
        self.assertTrue(Path(saved['path']).is_dir())
        self.assertEqual(self.store.history()[0]['id'], saved['id'])
        self.assertEqual(self.request('GET', '/api/jobs/' + ident + '/log')[0], 200)


if __name__ == '__main__':
    unittest.main()
