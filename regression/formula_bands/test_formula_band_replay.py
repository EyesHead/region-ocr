"""Optional integration check on a private OCR snapshot; no source PDF is shipped."""
import json
import os
import sys
import unittest
from collections import Counter
from pathlib import Path
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import convert_docling as core


class FormulaBandReplay(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('REGION_OCR_FORMULA_REPLAY'), 'Set REGION_OCR_FORMULA_REPLAY to the private replay case')
    def test_closed_table_keeps_seven_formula_records_as_fullwidth_rows(self):
        case=Path(os.environ['REGION_OCR_FORMULA_REPLAY'])
        content=(case/'replayed/document.html').read_text('utf-8')
        soup=BeautifulSoup(content,'html.parser')
        tables=soup.find_all('table')
        self.assertEqual(1,len(tables))
        profile=core.table_profile(str(tables[0]))
        self.assertFalse(profile['issues'])
        self.assertEqual((9,14),(profile['rows'],profile['columns']))
        body=tables[0].tbody.find_all('tr',recursive=False)
        self.assertEqual([14]*7,[int(row.td.get('colspan',1)) for row in body])
        self.assertEqual(7,len(body))
        self.assertEqual(8,len(tables[0].find_all('math',display='block')))
        self.assertTrue(all(node.find_parent('table') for node in soup.find_all('math')))
        records=json.loads((case/'baseline/surya_records.json').read_text('utf-8'))
        original=[node.get_text().strip() for rec in records if rec['page_no']==12 and rec['label']=='Equation'
                  for node in BeautifulSoup(rec['html'],'html.parser').find_all('math')]
        actual=[node.get('data-latex') for node in tables[0].tbody.find_all('math')]
        self.assertEqual(Counter(original),Counter(actual))
        manifest=json.loads((case/'replayed/quality.json').read_text('utf-8'))
        self.assertEqual(0,manifest['source_formula_band_repairs'][0]['uncovered_ink_pixels'])
        self.assertFalse(manifest['table_content_issues'])


if __name__=='__main__': unittest.main()
