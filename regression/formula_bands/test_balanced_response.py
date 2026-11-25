from types import SimpleNamespace
import unittest

from bs4 import BeautifulSoup

from docling_pdf_balanced_math import _formula_markup
from local_web.structured_export import to_markdown


class BalancedFormulaResponseTest(unittest.TestCase):
    def test_wrapped_math_keeps_the_suffix_in_html_and_markdown(self):
        response = SimpleNamespace(raw='<p><math>1/2</math> A<sub>46</sub></p>')
        markup = _formula_markup(response, inline=True)
        document = BeautifulSoup(f'<body><p>{markup}</p></body>', 'html.parser')
        self.assertIsNotNone(document.math)
        self.assertEqual(document.sub.get_text(), '46')
        markdown = to_markdown(document)
        self.assertIn(' A<sub>46</sub>', markdown)
        self.assertNotIn(chr(96), markdown)

    def test_mixed_unsafe_or_nonformula_html_uses_the_source_fallback(self):
        for value in ('<p>29</p>', '<table><tr><td><math>x</math></td></tr></table>',
                      '<p><math>x</math><img src="https://example.invalid/x"></p>'):
            with self.subTest(value=value):
                self.assertIsNone(_formula_markup(SimpleNamespace(raw=value), inline=True))


if __name__ == '__main__':
    unittest.main()
