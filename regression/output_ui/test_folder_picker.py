import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('desktop_under_test', ROOT/'local_web/desktop.py')
desktop=importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)
class FolderPickerTests(unittest.TestCase):
    def test_native_folder_selection_returns_path_without_changing_storage(self):
        window=SimpleNamespace(create_file_dialog=Mock(return_value=(str(ROOT),)))
        view=SimpleNamespace(windows=[window], FileDialog=SimpleNamespace(FOLDER=20))
        bridge=desktop.Bridge('http://127.0.0.1:1',view)
        self.assertEqual(bridge.choose_output_directory(), str(ROOT))
        window.create_file_dialog.assert_called_once_with(20)
    def test_cancel_does_not_change_the_output_path(self):
        window=SimpleNamespace(create_file_dialog=Mock(return_value=None))
        view=SimpleNamespace(windows=[window], FileDialog=SimpleNamespace(FOLDER=20))
        self.assertIsNone(desktop.Bridge('http://127.0.0.1:1',view).choose_output_directory())
if __name__=='__main__': unittest.main()
