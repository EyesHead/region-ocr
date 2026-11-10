"""Replay a private saved OCR snapshot, without running recognition again."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def replay(case, pages, output):
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    import convert_docling as core
    from docling_core.types.doc import DoclingDocument
    core.DATA_DIR=case
    sys.argv=['convert_docling','--mode','local','--input',str(case/'source.pdf'),'--output-dir',str(output)]
    args=core.parse_args()
    records=json.loads((case/'baseline/surya_records.json').read_text('utf-8'))
    args.surya_records=[r for r in records if pages is None or r['page_no'] in pages]
    doc=DoclingDocument.model_validate_json((case/'baseline/docling_before_export.json').read_text('utf-8'))
    if pages is not None: doc=doc.filter(page_nrs=pages)
    orientation=json.loads((case/'baseline/input.json').read_text('utf-8'))['orientation']
    orientation={int(k):v for k,v in orientation.items() if pages is None or int(k) in pages}
    content,manifest=core.export_local_html(args,SimpleNamespace(document=doc),case/'normalized.pdf',orientation,case/'source.pdf')
    output.mkdir(parents=True,exist_ok=True)
    (output/'document.html').write_text(content,'utf-8')
    (output/'quality.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    return content,manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',type=Path,required=True)
    parser.add_argument('--pages',default='12',help='Comma-separated one-based pages, or all')
    parser.add_argument('--output',type=Path,required=True)
    options=parser.parse_args()
    pages=None if options.pages=='all' else {int(p) for p in options.pages.split(',')}
    content,manifest=replay(options.case.resolve(),pages,options.output.resolve())
    print('Replay complete:',len(manifest.get('source_formula_band_repairs',[])),'formula-band repairs')
