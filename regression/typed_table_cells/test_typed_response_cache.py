from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import convert_docling as core
from docling_pdf_table_content import recognize_typed_crop, TableContentError


class TypedResponseCacheTests(unittest.TestCase):
    def test_malformed_response_does_not_poison_the_next_retry(self):
        responses = ['<p>unpositioned</p>',
                     '<div data-label="Text" data-bbox="0 0 1000 1000"><p>Valid text</p></div>']
        calls = []

        def generate(batch):
            calls.append(batch)
            return [SimpleNamespace(raw=responses.pop(0), error=False, token_count=10)]

        api = SimpleNamespace(local_surya_backend=lambda _: SimpleNamespace(generate=generate),
                              diagnostic_event=lambda *_args, **_kw: None, write_atomic=core.write_atomic)
        with tempfile.TemporaryDirectory(prefix='typed-response-cache-') as directory:
            args = SimpleNamespace(ocr_cache_dir=Path(directory), surya_ocr_model=object())
            with Image.new('RGB', (50, 50), 'white') as image:
                with self.assertRaises(TableContentError):
                    recognize_typed_crop(image, args, 1, {}, api)
                blocks = recognize_typed_crop(image, args, 1, {}, api)
        self.assertEqual(len(calls), 2)
        self.assertEqual(blocks[0]['html'], '<p>Valid text</p>')


if __name__ == '__main__':
    unittest.main()
