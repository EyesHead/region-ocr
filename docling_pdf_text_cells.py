"""Evidence for retaining positioned PDF text in otherwise raster tables."""
import hashlib
import math
import re

from bs4 import BeautifulSoup


def content_fingerprint(content):
    # The table cleaner joins only discretionary PDF word wraps here. Its
    # serialization must not invalidate the evidence for the same source text.
    content = re.sub(r'\u00ad\s*<br\s*/?>\s*', '', content, flags=re.I)
    parsed = BeautifulSoup(content, 'html.parser')
    for node in parsed.find_all(True):
        for name in list(node.attrs):
            if name in {'data-source-cell', 'data-cell-content', 'data-diagram-evidence'}:
                del node[name]  # Asset provenance annotations do not change the payload.
    return hashlib.sha256(str(parsed).encode('utf-8')).hexdigest()


def _source_script_math(parsed, glyphs, source_scripts):
    """Identify inline script MathML produced from these very positioned glyphs."""
    from collections import Counter
    from docling_pdf_native_scripts import script_terms

    glyph_keys = {(g.get('text'), tuple(g.get('bbox') or [g.get('x0'), g.get('top'), g.get('x1'), g.get('bottom')]))
                  for g in glyphs}
    allowed = {'math', 'mrow', 'mi', 'mn', 'mo', 'msub', 'msup', 'msubsup'}
    available = list(source_scripts)
    result = set()
    for node in parsed.find_all('math'):
        if any(child.name not in allowed for child in node.find_all(True)):
            continue
        for index, proof in enumerate(available):
            if (proof.get('method') != 'source_glyph_size_text_origin_and_adjacent_base'
                    or not proof.get('glyphs') or node.get('data-latex') != proof.get('expression')):
                continue
            if any((g.get('text'), tuple(g.get('bbox', []))) not in glyph_keys for g in proof['glyphs']):
                continue
            compact = lambda value: re.sub(r'\s+', '', value).replace('-', '−')
            if compact(node.get_text()) != compact(''.join(g['text'] for g in proof['glyphs'])):
                continue
            expected = Counter((proof['base'], role, value.replace('-', '−'))
                               for role, value in proof.get('scripts', {}).items())
            actual = Counter({(base, role, value.replace('-', '−')): count
                              for (base, role, value), count in script_terms(str(node)).items()})
            if actual != expected:
                continue
            result.add(id(node))
            available.pop(index)  # One measured script cannot prove a duplicated formula.
            break
    return result


def positioned_text_proof(content, glyphs, mask, region, cell_box, sx, sy, *, assets=(), source_scripts=()):
    """Accept visible source text and independently positioned native assets.

    This does not claim that the PDF text was read by the primary OCR. It
    retains the source's own text only when the raster and positioned layer
    agree geometrically. Callers must exclude unproven illustration/dimension
    pixels; an image alone never proves that adjacent prose was read.
    """
    import cv2
    import numpy as np

    parsed = BeautifulSoup(content, 'html.parser')
    text = parsed.get_text('', strip=True)
    glyphs = [g for g in glyphs if g.get('text', '').strip()]
    script_math = _source_script_math(parsed, glyphs, source_scripts)
    illustrations = [node for node in parsed.find_all(['img', 'math']) if id(node) not in script_math]
    if (not text or not glyphs or '\ufffd' in text or parsed.find('svg')
            or len(illustrations) != len(assets)
            or any(ord(c) < 32 and not c.isspace() for c in text)):
        return None
    for glyph in glyphs:
        matrix = glyph.get('matrix')
        if matrix and (abs(matrix[1]) > max(abs(matrix[0]) * 3, 1e-5)
                       or abs(matrix[2]) > max(abs(matrix[3]) * 3, 1e-5)):
            return None
    x0 = max(0, math.ceil((cell_box[0] - region[0] + 1) * sx))
    y0 = max(0, math.ceil((cell_box[1] - region[1] + 1) * sy))
    x1 = min(mask.shape[1], math.floor((cell_box[2] - region[0] - 1) * sx))
    y1 = min(mask.shape[0], math.floor((cell_box[3] - region[1] - 1) * sy))
    ink = mask[y0:y1, x0:x1].copy()
    if not ink.size:
        return None
    # Remove grid rails; a long stroke must not become proof of readable text.
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                 np.ones((1, max(12, round(ink.shape[1] * .8))), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                               np.ones((max(12, round(ink.shape[0] * .8)), 1), np.uint8))
    ink = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.bitwise_or(horizontal, vertical)))
    # Native positioned assets account only for their own pixels. The text
    # beside/above them must still have independent visible-glyph support.
    for asset in assets:
        box = asset.get('bbox')
        if (not box or len(box) != 4 or not all(math.isfinite(v) for v in box)
                or box[0] < cell_box[0] - 1 or box[1] < cell_box[1] - 1
                or box[2] > cell_box[2] + 1 or box[3] > cell_box[3] + 1):
            return None
        a = max(0, math.floor((box[0] - region[0]) * sx) - x0 - 1)
        b = max(0, math.floor((box[1] - region[1]) * sy) - y0 - 1)
        c = min(ink.shape[1], math.ceil((box[2] - region[0]) * sx) - x0 + 1)
        d = min(ink.shape[0], math.ceil((box[3] - region[1]) * sy) - y0 + 1)
        ink[b:d, a:c] = 0
    count = int(np.count_nonzero(ink))
    if count < 8:
        return None
    explained = np.zeros(ink.shape, np.uint8)
    supported = 0
    for glyph in glyphs:
        pad = max(.8, glyph.get('size', 9) * .12)
        a = max(0, math.floor((glyph['x0'] - region[0] - pad) * sx) - x0)
        b = max(0, math.floor((glyph['top'] - region[1] - pad) * sy) - y0)
        c = min(ink.shape[1], math.ceil((glyph['x1'] - region[0] + pad) * sx) - x0)
        d = min(ink.shape[0], math.ceil((glyph['bottom'] - region[1] + pad) * sy) - y0)
        if c > a and d > b:
            supported += bool(np.count_nonzero(ink[b:d, a:c]))
            explained[b:d, a:c] = 255
    covered = int(np.count_nonzero(cv2.bitwise_and(ink, explained)))
    coverage, support = covered / count, supported / len(glyphs)
    if coverage < .97 or support < .9:
        return None
    return dict(basis='positioned_pdf_text_covers_source_cell_ink',
                html_sha256=content_fingerprint(content), glyphs=len(glyphs),
                ink_coverage=round(coverage, 6), glyph_ink_support=round(support, 6),
                bbox=list(cell_box), positioned_assets=len(assets), positioned_source_scripts=len(script_math))


def annotate_native_text_cells(geometry, source_image, region, fragment=None):
    """Prove native cell payloads before any primary-grid alignment or masking.

    Vector and scanned tables share the same ownership check. In a native
    mixed cell its separately positioned images/formulas explain their own
    ink; they never justify rasterizing the surrounding visible PDF text.
    """
    import numpy as np

    if (source_image is None or geometry.get('hidden_ocr_direction_conflict')
            or geometry.get('text_method') not in {'pdfminer_source_glyphs', 'pdfium_source_glyphs'}):
        return
    fragment = fragment or geometry.get('source_physical_html')
    if not fragment:
        return
    table = BeautifulSoup(fragment, 'html.parser').table
    if table is None:
        return
    nodes = [n for n in table.find_all(['td', 'th']) if n.find_parent('table') is table]
    cells = sorted(geometry.get('cells', []), key=lambda c: (c['row'], c['column']))
    if len(nodes) != len(cells):
        return
    with source_image.convert('L') as gray:
        mask = (np.asarray(gray) < 180).astype('uint8') * 255
    sx = source_image.width / (region[2] - region[0])
    sy = source_image.height / (region[3] - region[1])
    for node, cell in zip(nodes, cells):
        if cell.get('source_text_unreliable'):
            continue
        same_cell = lambda e: (e.get('row'), e.get('column')) == (cell['row'], cell['column'])
        images = [e for e in geometry.get('retained_cell_images', []) if same_cell(e)]
        maths = [e for e in geometry.get('restored_cell_math', []) if same_cell(e)]
        scripts = [proof for line in cell.get('lines', []) for proof in line.get('source_scripts', [])]
        script_math = _source_script_math(node, cell.get('glyphs', []), scripts)
        if (len(node.find_all('img')) != len(images)
                or len(node.find_all('math')) - len(script_math) != len(maths)):
            continue
        proof = positioned_text_proof(node.decode_contents(), cell.get('glyphs', []),
            mask, region, cell['bbox'], sx, sy, assets=images + maths, source_scripts=scripts)
        if proof:
            cell['positioned_source_text'] = proof


def owns_positioned_text(node, source, geometry):
    proof = source.get('positioned_source_text') or {}
    return bool(proof.get('basis') == 'positioned_pdf_text_covers_source_cell_ink'
                and not source.get('source_text_unreliable')
                and not geometry.get('hidden_ocr_direction_conflict')
                and proof.get('bbox') == source.get('bbox')
                and proof.get('ink_coverage', 0) >= .97
                and proof.get('glyph_ink_support', 0) >= .9
                and proof.get('html_sha256') == content_fingerprint(node.decode_contents()))
