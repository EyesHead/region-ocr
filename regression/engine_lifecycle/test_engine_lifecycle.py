"""Windows worker exit diagnostics, verified through real child processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_web import converter
from local_web.engine_host import PersistentEngine

CRASH = (
    "import json,sys,ctypes,faulthandler;"
    "print('LOCAL_ENGINE_HOST '+json.dumps(dict(event='ready',message='ready')),flush=True);"
    "sys.stdin.readline();print('native worker stopped',flush=True);"
    "ctypes.windll.kernel32.ExitProcess(ctypes.c_uint(0xC0000005 if faulthandler.is_enabled() else 23))"
)
STARTUP_CRASH = (
    "import ctypes;print('model initialization',flush=True);"
    "ctypes.windll.kernel32.ExitProcess(ctypes.c_uint(0xC0000017))"
)
SUCCESS = (
    "import json,sys;"
    "print('LOCAL_ENGINE_HOST '+json.dumps(dict(event='ready',message='ready')),flush=True);"
    "r=json.loads(sys.stdin.readline());"
    "print('LOCAL_WEB_RESULT '+json.dumps(dict(html='verified.html',pages=1)),flush=True);"
    "print('LOCAL_ENGINE_HOST '+json.dumps(dict(event='complete',id=r['id'],ok=True)),flush=True);"
    "sys.stdin.read()"
)


@unittest.skipUnless(os.name == 'nt', 'Windows process exit status')
class EngineLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ocr-engine-lifecycle-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'synthetic.pdf'

    def engine(self, name, code):
        engine = PersistentEngine(self.root / name,
                                  command=[sys.executable, '-B', '-u', '-c', code])
        self.addCleanup(engine.close)
        return engine

    def run_crash(self, engine, name):
        destination = self.root / name
        destination.mkdir()
        with self.assertRaises(RuntimeError) as result:
            engine.run(self.source, destination, '1-1')
        return str(result.exception), destination

    def test_abrupt_exit_keeps_windows_status_in_error_and_log(self):
        message, destination = self.run_crash(self.engine('exit-host', CRASH), 'exit-job')
        self.assertIn('0xC0000005', message)
        self.assertIn('0xC0000005', (destination / 'engine.log').read_text('utf-8'))

    def test_crash_remains_visible_as_engine_error(self):
        engine = self.engine('state-host', CRASH)
        self.run_crash(engine, 'state-job')
        snapshot = engine.snapshot()
        self.assertEqual(snapshot['phase'], 'error')
        self.assertEqual(snapshot['exit_code'], 3221225477)

    def test_startup_exit_keeps_windows_status(self):
        message, _ = self.run_crash(self.engine('startup-host', STARTUP_CRASH), 'startup-job')
        self.assertIn('0xC0000017', message)

    def test_next_job_starts_a_fresh_process(self):
        engine = self.engine('retry-host', CRASH)
        self.run_crash(engine, 'retry-first')
        engine.command = [sys.executable, '-B', '-u', '-c', SUCCESS]
        destination = self.root / 'retry-second'
        destination.mkdir()
        result = engine.run(self.source, destination, '1-1')
        self.assertEqual(result['pages'], 1)
        self.assertEqual(engine.snapshot()['phase'], 'ready')

    def test_standalone_worker_keeps_windows_status_and_fault_handler(self):
        destination = self.root / 'standalone'
        destination.mkdir()
        original_spawn = subprocess.Popen
        code = (
            "import ctypes,faulthandler;print('standalone native worker',flush=True);"
            "ctypes.windll.kernel32.ExitProcess(ctypes.c_uint(0xC0000005 if faulthandler.is_enabled() else 23))"
        )

        def spawn(command, **kwargs):
            return original_spawn([sys.executable, '-B', '-u', '-c', code], **kwargs)

        with patch.object(converter.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(RuntimeError) as result:
                converter._run_engine(self.source, destination, '1-1', None, None)
        self.assertIn('0xC0000005', str(result.exception))
        self.assertIn('0xC0000005', (destination / 'engine.log').read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
