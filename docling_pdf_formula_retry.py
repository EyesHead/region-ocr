"""Additional recognition views grounded in the original PDF image objects.

These are retries, not text corrections. A view is accepted only by the same
Surya plus two independent CodeFormula comparisons as the initial reading.
"""
from io import BytesIO

from PIL import Image, ImageOps


def native_image(recognizer,metadata):
    reader=getattr(recognizer,'pdf_source_reader',None)
    page,bounds=metadata.get('source_page'),metadata.get('source_bbox')
    if reader is None or not page or not bounds:
        return None
    reader.load_page(page)
    # Visible native text inside the same canvas is a composite source, not
    # a standalone embedded formula. It must keep its native glyph route.
    if any(g['text'].strip() and bounds[0] <= (g['x0']+g['x1'])/2 <= bounds[2]
           and bounds[1] <= (g['top']+g['bottom'])/2 <= bounds[3] for g in reader.chars):
        return None
    objects=[o for o in reader.page.images if max(abs(a-b) for a,b in zip(
        [o['x0'],o['top'],o['x1'],o['bottom']],bounds)) <= .1]
    if (len(objects)!=1 or objects[0]['stream'].attrs.get('SMask') is not None
            or objects[0]['stream'].attrs.get('Mask') is not None):
        return None
    from docling_pdf_accents import source_layer
    return source_layer(reader,page,bounds,alpha_only=False,native=True)


def embedded_image_views(recognizer, metadata):
    source=native_image(recognizer,metadata)
    if source is None:
        return
    try:
        # Exact integer pixel replication retains tiny serifs/crossbars that
        # page antialiasing and interpolated resizing otherwise merge.
        enlarged=source.resize((source.width*2,source.height*2),Image.Resampling.NEAREST)
        image=ImageOps.expand(enlarged,border=24,fill='white')
        enlarged.close()
        buffer=BytesIO();image.save(buffer,'PNG')
        proof=dict(method='native_embedded_image_integer_pixels',native_size=list(source.size),
                   recognition_size=list(image.size),scale=2,interpolation='nearest',padding=24)
        image.close()
        yield buffer.getvalue(),proof
    finally:
        source.close()


def _crop_formula_reading(raw, api, *, primary=False):
    """Accept one complete expression, keeping its presentation tree for comparison."""
    import re
    from html import unescape
    from bs4 import BeautifulSoup
    from docling_office_math import parsed_formula
    from local_web.structured_export import math_to_latex, UnsupportedMath

    if (not isinstance(raw, str) or not raw.strip() or len(raw) > 16000
            or re.search(r'<\|[^<>]*\|>|[\ufffd\u0000]', unescape(raw))):
        return None
    has_math = bool(re.search(r'<math(?:\s|>)', raw, re.I))
    if has_math:
        if len(re.findall(r'</math\s*>', raw, re.I)) != 1:
            return None
        parsed = BeautifulSoup(raw, 'html.parser')
        if len(parsed.find_all('math')) != 1:
            return None
        node = parsed.math.extract()
        if parsed.get_text(strip=True) or parsed.find(['img', 'svg', 'table', 'script', 'style', 'object']):
            return None
        # Derive LaTeX from actual child nodes when receiving structured MathML.
        # An OCR-supplied attribute must not override the visible expression.
        if node.find(True):
            content = parsed_formula(str(node), api.latex_math)
            if not content:
                return None
            node = BeautifulSoup(content, 'html.parser').math
            try:
                latex = math_to_latex(node)
            except UnsupportedMath:
                return None
        else:
            latex = node.get_text().strip()
    elif primary and not re.search(r'</?[A-Za-z][^<>]*>', raw):
        latex = raw.strip()
    else:
        return None

    markup = api.latex_math(latex, 'block')
    if not markup:
        return None
    math = BeautifulSoup(markup, 'html.parser').math
    if math is None or not math.get_text(strip=True) or math.find(['merror', 'maction', 'annotation-xml']):
        return None
    for part in math.find_all(['mfrac', 'msub', 'msup', 'msubsup', 'mover', 'munder', 'munderover', 'mroot']):
        children = part.find_all(True, recursive=False)
        arity = 3 if part.name in {'msubsup', 'munderover'} else 2
        if len(children) != arity or any(not c.get_text(strip=True) for c in children):
            return None
    # Keep operator, fraction, script, alignment, and font differences significant.
    return markup, math.decode_contents()



def _clipped_crop_reading(raw, api):
    """Keep complete TeX atoms when the OCR response stops at the physical page edge."""
    import re
    from html import unescape

    if not isinstance(raw, str) or len(raw) > 16000 or re.search(r'<\|[^<>]*\|>|[\ufffd\x00]', unescape(raw)):
        return None
    full = _crop_formula_reading(raw, api)
    if full:
        return full
    # Only accept the requested math wrapper. Do not turn a diagnostic or an
    # incomplete MathML element into mathematical characters.
    match = re.fullmatch(r'\s*<math\s*>(.*?)(?:</math\s*>)?\s*', raw, re.I | re.S)
    if not match:
        return None
    tex = unescape(match.group(1)).strip()
    if not tex or re.search(r'<(?:/?[A-Za-z][\w:-]*(?:[\s/>]|$)|[!?])', tex):
        return None
    depth = 0
    ends = []
    for token in re.finditer(r'\\[A-Za-z]+|\\.|[^\s]', tex):
        atom = token.group()
        if atom == '{':
            depth += 1
        elif atom == '}':
            if not depth:
                break
            depth -= 1
        if not depth:
            ends.append(token.end())
    # A broken TeX construct can include its command and one or more
    # completed arguments. Let the regular parser check those boundaries.
    for end in reversed(ends[-12:]):
        reading = _crop_formula_reading(tex[:end], api, primary=True)
        if reading:
            return reading
    return None


def _common_math_prefix(readings):
    """Keep complete, identical leading MathML atoms from the bounded readings."""
    from bs4 import BeautifulSoup
    from local_web.structured_export import math_to_latex, UnsupportedMath

    if len(readings) < 2:
        return None
    rows = []
    for markup, _ in readings:
        node = BeautifulSoup(markup, 'html.parser').math
        if node is None:
            return None
        parts = node.find_all(True, recursive=False)
        while len(parts) == 1 and parts[0].name == 'mrow':
            parts = parts[0].find_all(True, recursive=False)
        rows.append(parts)
    common = []
    for group in zip(*rows):
        if any(part != group[0] for part in group[1:]):
            break
        common.append(str(group[0]))
    if not common:
        return None
    node = BeautifulSoup('<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mrow>'
                         + ''.join(common) + '</mrow></math>', 'html.parser').math
    try:
        node['data-latex'] = math_to_latex(node)
    except UnsupportedMath:
        return None
    return str(node)


def retry_formula_crop(args, regions, page_no, bounds, api, *, candidates=(), clipped=False):
    """Re-read a confirmed formula region using only the live primary OCR backend."""
    import math
    from contextlib import ExitStack
    from docling_backend_health import request_local_ocr
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
    from surya.recognition import _detect_repeat_loop

    model = getattr(args, 'surya_ocr_model', None)
    if model is None or not bounds or len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        return None
    x0, y0, x1, y1 = bounds
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return None
    page = regions.pdf[page_no - 1]
    try:
        page_width, page_height = page.get_size()
    finally:
        page.close()
    # The decoder must never complete symbols that disappear beyond the paper.
    if not clipped and min(x0, y0, page_width - x1, page_height - y1) <= 1:
        return None

    primary = {result[1] for value in candidates if (result := _crop_formula_reading(value, api, primary=True))}
    # A unique previous reading needs one independent view. Without one, two
    # differently sampled images must agree. Bound memory even for large sheets.
    views = [(3.0, 1280, 16), (4.2, 1600, 28)]
    if len(primary) == 1 and not clipped:
        views = views[1:]
    token_limit = 640
    readings = []
    try:
        backend = api.local_surya_backend(model)
        if backend.handle is None or backend._client is None:
            return None
        with ExitStack() as stack:
            batch = []
            for requested_scale, edge, border in views:
                scale = min(requested_scale, edge / max(width, height))
                image = stack.enter_context(regions.crop(page_no, bounds, scale=scale))
                with image.convert('L') as gray:
                    low, high = gray.getextrema()
                if high - low < 12:
                    return None
                view = stack.enter_context(ImageOps.expand(image, border=border, fill='white'))
                batch.append(BatchInputItem(image=view, prompt_type=PROMPT_TYPE_BLOCK, max_tokens=token_limit,
                    prompt='Transcribe this printed mathematical expression exactly as <math>LaTeX</math>. '
                           'Keep all visible symbols, rows, fractions and indices. Do not complete missing '
                           'symbols, simplify or explain. Return <img> if it cannot be read.',
                    metadata={'purpose': 'formula_crop_retry', 'page': page_no}))
            # Use the same transport as Office formula OCR. Surya's automatic
            # token expansion and repeated decoding would add latency here.
            outputs = request_local_ocr(backend, batch, 25, api.diagnostic_event)
            if not clipped and len(outputs) != len(batch):
                return None
            for output in outputs:
                reading = None
                if output is not None and not output.error and output.raw and not _detect_repeat_loop(output.raw):
                    if clipped and 0 < output.token_count <= token_limit and output.finish_reason in {None, 'stop', 'length'}:
                        reading = _clipped_crop_reading(output.raw, api)
                    elif not clipped and 0 < output.token_count < token_limit and output.finish_reason in {None, 'stop'}:
                        reading = _crop_formula_reading(output.raw, api)
                if reading:
                    readings.append(reading)
                elif not clipped:
                    return None
    except (OSError, RuntimeError, ValueError) as error:
        api.diagnostic_event('FORMULA_CROP_RETRY_UNAVAILABLE', page=page_no, error_type=type(error).__name__)
        return None

    if not readings:
        return None
    key = readings[0][1]
    if clipped and len(readings) == 1:
        return readings[0][0], dict(method='regional_ocr_visible_prefix', partial=True, views=1,
                                   primary_agreement=key in primary, unconfirmed=key not in primary)
    if clipped and any(value[1] != key for value in readings):
        prefix = _common_math_prefix(readings)
        if prefix:
            return prefix, dict(method='regional_ocr_visible_prefix', partial=True, views=len(readings))
        # The first glyph can itself be ambiguous. Prefer a reading supported
        # by the page transcription, then the more detailed crop as best effort.
        best = next((r for r in reversed(readings) if r[1] in primary), readings[-1])
        return best[0], dict(method='regional_ocr_clipped_reading', partial=True, views=len(readings),
                             primary_agreement=best[1] in primary, unconfirmed=best[1] not in primary)
    if any(value[1] != key for value in readings) or (len(views) == 1 and key not in primary):
        return None
    proof = dict(method='source_formula_regional_ocr', views=len(views),
                 primary_agreement=key in primary, token_limit=token_limit)
    if clipped:
        proof['partial'] = True
    return readings[0][0], proof
