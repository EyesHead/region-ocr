"""HTTP and privacy regressions for manual-only discrepancy checking."""
import http.client
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
from server import JobStore, LocalServer
from converter import export_formats


def synthetic_converter(source, destination, *, on_progress, output_format='html', **options):
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'manual-diagnostics.txt').write_text('Synthetic diagnostic evidence', encoding='utf-8')
    if source.name == 'failure.html':
        raise RuntimeError('Synthetic conversion failure')
    result = export_formats('<html><body><p>Synthetic result</p></body></html>', source, destination,
                            output_format=output_format)
    result['technical_status'] = 'REVIEW'
    return result


class ManualReviewModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JobStore(self.root, synthetic_converter)
        self.server = LocalServer(('127.0.0.1', 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.stop()
        self.thread.join(5)
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, {'X-Local-App': '1'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def convert(self, name='synthetic.html'):
        status, raw = self.request('POST', '/api/upload?name=' + name, b'<p>Synthetic input</p>')
        self.assertEqual(status, 201)
        job_id = json.loads(raw)['id']
        status, _ = self.request('POST', '/api/start', {'ids': [job_id], 'format': 'html'})
        self.assertEqual(status, 200)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            job = self.store.jobs[job_id]
            if job['status'] in {'done', 'error'} and job_id not in self.store.scheduled:
                return job
            time.sleep(.01)
        self.fail('Synthetic job did not finish')

    def test_conversion_review_status_and_errors_do_not_create_report_copies(self):
        done = self.convert()
        with self.assertLogs('region-document-converter', level='ERROR'):
            failed = self.convert('failure.html')
        self.assertEqual(done['status'], 'done')
        self.assertEqual(failed['status'], 'error')
        for job in (done, failed):
            self.assertTrue((self.root / job['id'] / 'result/manual-diagnostics.txt').is_file())
            self.assertNotIn('technical_report_id', job)
        self.assertFalse((self.root / 'reports').exists(), 'Conversion created an external-report outbox')
        self.assertEqual(self.request('GET', '/api/document/' + done['id'] + '?format=html')[0], 200)
        self.assertEqual(self.request('GET', '/api/jobs/' + done['id'] + '/log')[0], 200)

    def test_old_review_and_complaint_routes_and_script_are_not_served(self):
        job = self.convert()
        base = '/api/jobs/' + job['id']
        for method, path, body in [('GET', base + '/review', None),
                                   ('POST', base + '/review', {}),
                                   ('POST', base + '/complaints', {}),
                                   ('GET', '/api/reports/' + 'a' * 32, None),
                                   ('GET', '/review.js', None)]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, body)[0], 404)

    def test_originals_results_and_manual_logs_remain_available(self):
        job = self.convert()
        self.assertEqual(self.request('GET', '/api/status')[0], 200)
        self.assertEqual(self.request('GET', '/api/installed')[0], 200)
        self.assertEqual(self.request('GET', '/api/download/' + job['id'])[0], 200)
        status, log = self.request('GET', '/api/jobs/' + job['id'] + '/log')
        self.assertEqual(status, 200)
        self.assertTrue(log)

    def test_existing_report_queue_is_not_read_on_default_startup(self):
        root = self.root / 'default-startup'
        history = root / 'reports' / ('e' * 32)
        history.mkdir(parents=True)
        marker = history / 'state.json'
        marker.write_bytes(b'Legacy report metadata must be ignored')
        actual_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.resolve().is_relative_to((root / 'reports').resolve()):
                raise AssertionError('The application accessed its removed report queue')
            return actual_open(path, *args, **kwargs)
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'empty-profile')}), \
             mock.patch.object(Path, 'open', guarded_open), \
             mock.patch.object(JobStore, '_probe_engine', lambda _: None):
            store = JobStore(root)
            store.stop()
        self.assertEqual(marker.read_bytes(), b'Legacy report metadata must be ignored')

    def test_ui_has_no_discrepancy_dialog_or_review_script(self):
        from bs4 import BeautifulSoup
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        page = BeautifulSoup(body, 'html.parser')
        self.assertIsNone(page.find(id='review-dialog'))
        self.assertIsNone(page.find('script', src='/review.js'))
        self.assertIsNotNone(page.find(id='installed-dialog'))


if __name__ == '__main__':
    unittest.main()
