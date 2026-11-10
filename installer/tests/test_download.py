"""Download regressions against a real, deliberately unreliable HTTP server.

Run with: python -m unittest discover -s installer/tests -v
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error


BOOTSTRAP_PATH = Path(__file__).resolve().parents[1] / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("download_test_bootstrap", BOOTSTRAP_PATH)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)

SEGMENT = 256 * 1024
PAYLOAD = b"A" * SEGMENT + b"B" * SEGMENT + b"C" * (SEGMENT // 2)


class DownloadServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode):
        super().__init__(("127.0.0.1", 0), DownloadHandler)
        self.mode = mode
        self.requests = []
        self.stop = threading.Event()


class DownloadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        requested_range = self.headers.get("Range")
        self.server.requests.append(requested_range)
        ordinal = len(self.server.requests)
        offset = int(requested_range.removeprefix("bytes=").removesuffix("-")) if requested_range else 0
        mode = self.server.mode
        status = 206 if requested_range else 200
        remaining = PAYLOAD[offset:]
        body = remaining
        content_range = f"bytes {offset}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}" if requested_range else None
        stall = False

        if mode in ("resume_disconnect", "resume_timeout") and len(remaining) > SEGMENT:
            body = remaining[:SEGMENT]
            stall = mode == "resume_timeout"
        elif mode in ("ignore_range", "wrong_range", "range_rejected") and ordinal == 1:
            body = remaining[:SEGMENT]
        elif mode == "ignore_range":
            # A common server behavior: return the whole file despite Range.
            status, remaining, body, content_range = 200, PAYLOAD, PAYLOAD, None
        elif mode == "wrong_range" and ordinal == 2 and requested_range:
            # The bytes do not belong at the requested offset. Reject the
            # headers before counting or appending this duplicate prefix.
            content_range = f"bytes 0-{len(remaining) - 1}/{len(PAYLOAD)}"
            body = PAYLOAD[:SEGMENT]
        elif mode == "range_rejected" and ordinal == 2 and requested_range:
            status, remaining, body, content_range = 416, b"", b"", None
        elif mode == "oversized_then_good" and ordinal == 1:
            remaining = body = PAYLOAD + b"!"
        elif mode == "corrupt_then_good" and ordinal == 1:
            body = b"!" + PAYLOAD[1:]
        elif mode == "timeout":
            body, stall = b"", True

        self.send_response(status)
        self.send_header("Content-Length", str(len(remaining)))
        self.send_header("Connection", "close")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
                self.wfile.flush()
            if stall:
                # The client timeout, not a long test sleep, ends the read.
                self.server.stop.wait(3)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True


@contextmanager
def serving(mode):
    server = DownloadServer(mode)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/llama-native.zip"
    finally:
        server.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="region-download-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        plan = {
            "packages": [], "desktop_packages": [], "torch": {"cpu": []},
            "models": [], "pip": {"bytes": 0}, "native": {"cpu": {"bytes": len(PAYLOAD)}},
        }
        self.installer = bootstrap.Installer(self.root, plan, "cpu")
        self.messages = []
        self.installer.report = lambda message, current=0: self.messages.append(message)
        real_urlopen = bootstrap.urllib.request.urlopen

        def short_timeout(request, *_args, **kwargs):
            # Keep the real urllib/socket stack while bounding every read.
            kwargs["timeout"] = 0.1
            return real_urlopen(request, **kwargs)

        patcher = mock.patch.object(bootstrap.urllib.request, "urlopen", side_effect=short_timeout)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(bootstrap.time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def item(self, url):
        return {"filename": "llama-native.zip", "url": url, "bytes": len(PAYLOAD),
                "sha256": hashlib.sha256(PAYLOAD).hexdigest()}

    def assert_valid_download(self, result):
        self.assertEqual(result.read_bytes(), PAYLOAD)
        self.assertEqual(self.installer.completed, len(PAYLOAD))

    def check_progressing_transfer(self, mode):
        with serving(mode) as (server, url):
            result = self.installer.download(self.item(url))
            self.assert_valid_download(result)
            self.assertEqual(server.requests, [None, f"bytes={SEGMENT}-", f"bytes={2 * SEGMENT}-"])

    def test_interrupted_responses_resume_from_saved_bytes(self):
        self.check_progressing_transfer("resume_disconnect")

    def test_read_timeouts_preserve_sub_megabyte_progress_and_resume(self):
        self.check_progressing_transfer("resume_timeout")

    def test_server_ignoring_range_restarts_safely_using_full_response(self):
        with serving("ignore_range") as (server, url):
            result = self.installer.download(self.item(url))
            self.assert_valid_download(result)
            self.assertEqual(server.requests, [None, f"bytes={SEGMENT}-"])

    def test_wrong_content_range_is_rejected_without_advancing_saved_offset(self):
        with serving("wrong_range") as (server, url):
            try:
                result = self.installer.download(self.item(url))
            except (RuntimeError, ValueError):
                # A safe refusal is acceptable; unverified data must never be
                # marked complete or returned as an installable archive.
                self.assertEqual(self.installer.completed, 0)
            else:
                self.assert_valid_download(result)
            self.assertGreaterEqual(len(server.requests), 2)
            self.assertEqual(server.requests[1], f"bytes={SEGMENT}-")
            subsequent_offsets = [int(value[6:-1]) for value in server.requests[2:] if value]
            self.assertTrue(all(value <= SEGMENT for value in subsequent_offsets), server.requests)

    def test_hash_corruption_is_discarded_and_clean_retry_is_accepted(self):
        with serving("corrupt_then_good") as (server, url):
            result = self.installer.download(self.item(url))
            self.assert_valid_download(result)
            self.assertEqual(server.requests, [None, None])

    def test_rejected_range_discards_partial_file_and_retries_from_zero(self):
        with serving("range_rejected") as (server, url):
            result = self.installer.download(self.item(url))
            self.assert_valid_download(result)
            self.assertEqual(server.requests, [None, f"bytes={SEGMENT}-", None])

    def test_oversized_response_is_discarded_before_clean_retry(self):
        with serving("oversized_then_good") as (server, url):
            result = self.installer.download(self.item(url))
            self.assert_valid_download(result)
            self.assertEqual(server.requests, [None, None])

    def test_resumed_cache_prefix_is_included_in_final_checksum(self):
        # A persisted prefix may belong to a different response/version. A
        # digest of only newly received suffix bytes cannot verify the file.
        (self.installer.cache / "llama-native.zip").write_bytes(b"X" * SEGMENT)
        with serving("plain") as (server, url):
            result = self.installer.download(self.item(url))
            self.assert_valid_download(result)
            self.assertEqual(server.requests, [f"bytes={SEGMENT}-", None])

    def test_exhausted_timeouts_include_diagnostics_and_preserve_original_cause(self):
        with serving("timeout") as (server, url):
            with self.assertRaises(Exception) as caught:
                self.installer.download(self.item(url))
            error = caught.exception
            log = self.installer.log.read_text("utf-8") if self.installer.log.exists() else ""
            diagnostics = str(error) + "\n" + log
            self.assertIn("llama-native.zip", diagnostics)
            self.assertIn(url, diagnostics)
            self.assertIn(str(len(PAYLOAD)), diagnostics)
            self.assertRegex(diagnostics, r"\b0\b", "Include the received byte count, not only the expected size")
            self.assertTrue(log, "Download failure must also be recorded in installation.log")
            self.assertEqual(self.installer.completed, 0)
            self.assertGreaterEqual(len(server.requests), 2)
            self.assertIsNotNone(error.__cause__, "Keep the original transport error as the exception cause")
            cause = error.__cause__
            causes = []
            while cause is not None:
                causes.append(cause)
                if isinstance(cause, urllib.error.URLError):
                    causes.append(cause.reason)
                cause = cause.__cause__
            self.assertTrue(any(isinstance(cause, (TimeoutError, socket.timeout)) for cause in causes), causes)


if __name__ == "__main__":
    unittest.main()
