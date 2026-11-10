"""Exercise the real .NET downloader against a local HTTP server, without installing."""
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
DATA = b''.join(sha256(str(index).encode('ascii')).digest() for index in range(4096))
CUT = 65536


@contextmanager
def server_for(respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.headers.get('Range'))
            try:
                respond(self, len(requests), requests[-1])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/python.zip', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def reply(handler, data, *, code=200, content_range=None, length=None):
    handler.send_response(code)
    handler.send_header('Content-Length', str(len(data) if length is None else length))
    if content_range is not None:
        handler.send_header('Content-Range', content_range)
    handler.end_headers()
    handler.wfile.write(data)
    handler.wfile.flush()
    handler.close_connection = True


class SetupDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='region-ocr-dotnet-test-')
        cls.exe = Path(cls.build.name) / 'download-test.exe'
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        subprocess.run([
            str(compiler), '/nologo', '/target:exe', '/main:DownloadHarness', '/utf8output',
            '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
            '/reference:System.Web.Extensions.dll', '/reference:System.IO.Compression.dll',
            f'/resource:{ROOT / "local_web/assets/app-icon.ico"},icon.ico',
            f'/out:{cls.exe}', str(ROOT / 'installer/Setup.cs'),
            str(ROOT / 'installer/tests/download_harness.cs'),
        ], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def download(self, url, target, cancel=None):
        args = [str(self.exe), url, str(target), str(len(DATA)), sha256(DATA).hexdigest()]
        if cancel is not None:
            args.append(str(cancel))
        return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=45)

    def test_resumes_truncated_response(self):
        def respond(handler, _, byte_range):
            if byte_range is None:
                reply(handler, DATA[:CUT], length=len(DATA))
            else:
                self.assertEqual(byte_range, f'bytes={CUT}-')
                reply(handler, DATA[CUT:], code=206,
                      content_range=f'bytes {CUT}-{len(DATA) - 1}/{len(DATA)}')

        with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
            target = Path(folder) / 'python.zip'
            result = self.download(url, target)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertEqual(requests, [None, f'bytes={CUT}-'])

    def test_restarts_when_server_ignores_range(self):
        def respond(handler, attempt, _):
            reply(handler, DATA[:CUT] if attempt == 1 else DATA, length=len(DATA))

        with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
            target = Path(folder) / 'python.zip'
            result = self.download(url, target)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertEqual(requests, [None, f'bytes={CUT}-'])

    def test_discards_file_after_hash_mismatch(self):
        def respond(handler, attempt, _):
            reply(handler, b'X' * len(DATA) if attempt == 1 else DATA)

        with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
            target = Path(folder) / 'python.zip'
            result = self.download(url, target)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertEqual(requests, [None, None])

    def test_rejects_invalid_content_range_before_appending(self):
        for content_range in (None, f'bytes {CUT + 1}-{len(DATA) - 1}/{len(DATA)}',
                              f'bytes {CUT}-{len(DATA) - 1}/{len(DATA) + 1}',
                              f'bytes {CUT}-{len(DATA) - 2}/{len(DATA)}'):
            with self.subTest(content_range=content_range):
                def respond(handler, attempt, _):
                    if attempt == 1:
                        reply(handler, DATA[:CUT], length=len(DATA))
                    elif attempt == 2:
                        reply(handler, DATA[CUT:], code=206, content_range=content_range)
                    else:
                        reply(handler, DATA)

                with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
                    target = Path(folder) / 'python.zip'
                    result = self.download(url, target)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(target.read_bytes(), DATA)
                    self.assertEqual(requests, [None, f'bytes={CUT}-', None])

    def test_exhausts_eight_attempts_with_actionable_redacted_log(self):
        def respond(handler, _, __):
            reply(handler, b'', code=503)

        with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
            result = self.download(url + '?token=do-not-log', Path(folder) / 'python.zip')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(len(requests), 8)
            self.assertIn('8', result.stdout)
            self.assertIn('VPN', result.stdout)
            self.assertIn('installation.log', result.stdout)
            log = (Path(folder) / 'installation.log').read_text('utf-8')
            self.assertIn('8/8', log)
            self.assertIn('127.0.0.1', log)
            self.assertNotIn('do-not-log', log + result.stdout)

    def test_restarts_when_server_rejects_saved_range(self):
        def respond(handler, attempt, _):
            if attempt == 1:
                reply(handler, DATA[:CUT], length=len(DATA))
            elif attempt == 2:
                reply(handler, b'', code=416)
            else:
                reply(handler, DATA)

        with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
            target = Path(folder) / 'python.zip'
            result = self.download(url, target)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertEqual(requests, [None, f'bytes={CUT}-', None])

    def test_cancellation_aborts_read_without_retry(self):
        def respond(handler, _, __):
            handler.send_response(200)
            handler.send_header('Content-Length', str(len(DATA)))
            handler.end_headers()
            handler.wfile.write(DATA[:CUT])
            handler.wfile.flush()
            time.sleep(3)
            handler.wfile.write(DATA[CUT:])

        with tempfile.TemporaryDirectory() as folder, server_for(respond) as (url, requests):
            started = time.monotonic()
            result = self.download(url, Path(folder) / 'python.zip', cancel=350)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('OperationCanceledException', result.stdout)
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(requests, [None])


if __name__ == '__main__':
    unittest.main()
