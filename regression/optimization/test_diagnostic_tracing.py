"""Normal document logs keep semantic events without installing Python tracing."""
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import convert_docling as core


class DocumentTracingTests(unittest.TestCase):
    def check_document_log(self, setting, enabled):
        original_sys, original_thread = sys.gettrace(), threading.gettrace()
        with tempfile.TemporaryDirectory(prefix='document-tracing-') as directory:
            root = Path(directory)
            log = root/'document_log.txt'
            router = core.DiagnosticReportRouter()
            args = SimpleNamespace(api_key=None, output=root, format='html', pages=None, mode='local')
            with patch.dict(os.environ), redirect_stdout(StringIO()):
                os.environ.pop('DOCLING_TRACE_FUNCTIONS', None)
                if setting is not None:
                    os.environ['DOCLING_TRACE_FUNCTIONS'] = setting
                with core.diagnostic_document_log(router, threading.RLock(), log, root/'source.pdf', args):
                    if enabled:
                        self.assertIsNot(sys.gettrace(), original_sys)
                        self.assertIsNot(threading.gettrace(), original_thread)
                    else:
                        self.assertIs(sys.gettrace(), original_sys)
                        self.assertIs(threading.gettrace(), original_thread)
                    core.diagnostic_event('TABLE_CONTENT_VERIFIED', rows=2, columns=3)
            text = log.read_text(encoding='utf-8')
            self.assertIn('TABLE_CONTENT_VERIFIED', text)
            self.assertIn('document log started', text)
            self.assertIn('document log finished', text)
        self.assertIs(sys.gettrace(), original_sys)
        self.assertIs(threading.gettrace(), original_thread)
        self.assertIsNone(router.target)

    def test_normal_document_log_does_not_install_function_tracing(self):
        self.check_document_log(None, False)

    def test_explicit_zero_keeps_function_tracing_disabled(self):
        self.check_document_log('0', False)

    def test_explicit_one_enables_and_restores_function_tracing(self):
        self.check_document_log('1', True)


if __name__ == '__main__':
    unittest.main()
