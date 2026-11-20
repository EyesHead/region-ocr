"""Check conflicting OCR math against independently rendered vector source cells."""
from hashlib import sha256
from html import escape
import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString


class VectorContentConflict(RuntimeError):
    """Neither positioned source text nor primary OCR has pixel confirmation."""


_RAISED = '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻'
_LOWERED = '₀₁₂₃₄₅₆₇₈₉₊₋'
_SCRIPT_TEXT = str.maketrans(_RAISED + _LOWERED, '0123456789+−' * 2)


def _compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).replace('−', '-').replace('′′', '"')


def _visual(value):
    # These pairs share visible glyphs; the source's own Unicode is retained.
    return value.replace('′′', '"').translate(str.maketrans({'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E',
        'Н': 'H', 'К': 'K', 'М': 'M', 'О': 'O', 'Р': 'P', 'Т': 'T', 'Х': 'X',
        'а': 'a', 'с': 'c', 'е': 'e', 'о': 'o', 'р': 'p', 'х': 'x',
        'У': 'Y', 'у': 'y', '″': '"'}))


def content_signature(fragment, api):
    """Compare visible character order plus explicit script positions, not tags."""
    from docling_pdf_table_content import _safe_markup
    parsed = BeautifulSoup(_safe_markup(fragment, api), 'html.parser')
    output, scripts, structures = [], [], []
    def append(value):
        output.extend(_compact(value))
    def script(node, role):
        start = len(output)
        for child in node.children:
            visit(child)
        scripts.append((role, start, len(output)))
    def visit(node):
        if isinstance(node, NavigableString):
            text = str(node)
            cursor = 0
            for match in re.finditer('[' + _RAISED + ']+|[' + _LOWERED + ']+', text):
                append(text[cursor:match.start()])
                start = len(output)
                append(match[0].translate(_SCRIPT_TEXT))
                scripts.append(('sup' if match[0][0] in _RAISED else 'sub', start, len(output)))
                cursor = match.end()
            append(text[cursor:])
            return
        if node.name in {'sup', 'sub'}:
            script(node, node.name)
            return
        if node.name in {'msup', 'msub', 'msubsup'}:
            parts = node.find_all(True, recursive=False)
            if len(parts) == (3 if node.name == 'msubsup' else 2):
                visit(parts[0])
                for role, child in zip(('sub', 'sup') if node.name == 'msubsup' else
                                       ('sup',) if node.name == 'msup' else ('sub',), parts[1:]):
                    script(child, role)
                return
        children = list(getattr(node, 'children', ()))
        start, spans = len(output), []
        for child in children:
            child_start = len(output)
            visit(child)
            if not isinstance(child, NavigableString) or str(child).strip():
                spans.append((child_start, len(output)))
        # Fractions, roots, fences, matrices, accents and other MathML
        # operators are not interchangeable with their flattened characters.
        # Script nodes above share the canonical HTML sup/sub representation.
        basic_math = {'math', 'mrow', 'mi', 'mn', 'mo', 'mtext', 'mspace'}
        in_math = getattr(node, 'name', None) == 'math' or node.find_parent('math') is not None
        variant = node.get('mathvariant') if getattr(node, 'attrs', None) else None
        if in_math and (node.name not in basic_math or variant):
            attrs = tuple(sorted((str(k), str(v)) for k, v in node.attrs.items()
                                 if k not in {'xmlns', 'display', 'data-latex'}))
            structures.append((node.name, start, len(output), tuple(spans), attrs))
    visit(parsed)
    merged = []
    for role, start, end in sorted(scripts):
        if merged and merged[-1][0] == role and merged[-1][2] == start:
            merged[-1] = role, merged[-1][1], end
        else:
            merged.append((role, start, end))
    return ''.join(output), tuple(merged), tuple(structures)


def _script_positions(signature):
    text, scripts = signature[:2]
    return {(role, index) for role, start, end in scripts for index in range(start, end)
            if text[index].isalnum()}


def literal_script_markup(fragment, api):
    """Remove only script-only MathML wrappers, without changing source glyphs."""
    signature = content_signature(fragment, api)
    if signature[2]:
        return None
    parsed = BeautifulSoup(fragment, 'html.parser')
    allowed = {'math', 'mrow', 'mi', 'mn', 'mo', 'mtext', 'mspace', 'msub', 'msup', 'msubsup'}
    def render(node):
        if isinstance(node, NavigableString):
            return escape(str(node))
        if node.name in {'msub', 'msup', 'msubsup'}:
            children = node.find_all(True, recursive=False)
            if len(children) != (3 if node.name == 'msubsup' else 2):
                raise ValueError('Incomplete source script')
            roles = ('sub', 'sup') if node.name == 'msubsup' else ('sub',) if node.name == 'msub' else ('sup',)
            return render(children[0]) + ''.join('<' + role + '>' + render(child) + '</' + role + '>'
                for role, child in zip(roles, children[1:]))
        if node.name not in allowed:
            raise ValueError('Non-script source structure')
        return ''.join(render(child) for child in node.children)
    try:
        for math in list(parsed.find_all('math')):
            if math.find_parent('math'):
                continue
            # Raw LaTeX is not a measured arrangement of source glyphs.
            if not math.find(True):
                return None
            math.replace_with(BeautifulSoup(render(math), 'html.parser'))
    except ValueError:
        return None
    literal = str(parsed)
    return literal if content_signature(literal, api) == signature else None


def _confirms(reading, expected):
    if _visual(reading[0]) != _visual(expected[0]):
        return False
    if reading[2] != expected[2]:
        return False
    # The PDF's measured glyph sizes/baselines supply script roles when a
    # regional reader emits plain text. An explicitly contrary role fails.
    expected_roles = _script_positions(expected)
    return not reading[1] or _script_positions(reading) <= expected_roles


def _same_printed_literal(left, right):
    return _visual(_compact(left).upper()) == _visual(_compact(right).upper())


def _native_simple_fraction(primary, source, page, api):
    """Recover the literal numerator and denominator from either side of a native fraction rule."""
    parts = primary.find_all('mfrac')
    if len(parts) != 1 or len(primary.find_all('math')) != 1:
        return None
    math_node = primary.math
    allowed = {'mrow', 'mfrac', 'mi', 'mn', 'mo', 'mtext'}
    if any(tag.name not in allowed for tag in math_node.find_all(True)):
        return None
    terms = parts[0].find_all(True, recursive=False)
    if len(terms) != 2:
        return None
    left, top, right, bottom = source['bbox']
    glyphs = sorted((g for g in source['glyphs'] if g['text'].strip()), key=lambda g: g['bbox'][0])
    if not glyphs or any(not re.fullmatch(r'[\w,.+−-]', g['text']) for g in glyphs):
        return None
    literal = lambda items: ''.join(g['text'] for g in items)
    answers = []
    for bar in page.rects:
        x0, y0, x1, y1 = (bar[k] for k in ('x0', 'top', 'x1', 'bottom'))
        if (not bar.get('fill') or api.is_white_pdf_background(bar)
                or not (left+1 < x0 < x1 < right-1 and top+1 < y0 < y1 < bottom-1)
                or x1-x0 < 6*(y1-y0)):
            continue
        before, above, below, after = [], [], [], []
        for glyph in glyphs:
            a, b, c, d = glyph['bbox']
            if x0-1 <= a and c <= x1+1 and d <= y0+1:
                above.append(glyph)
            elif x0-1 <= a and c <= x1+1 and b >= y1-1:
                below.append(glyph)
            elif c <= x0+1 and b <= y1 and d >= y0:
                before.append(glyph)
            elif a >= x1-1 and b <= y1 and d >= y0:
                after.append(glyph)
            else:
                break
        else:
            # Each horizontal run must have one baseline. A stacked expression
            # on either side needs its own layout proof.
            if not above or not below or any(
                    max(g['bbox'][1] for g in run) >= min(g['bbox'][3] for g in run)
                    for run in (above, below)):
                continue
            num, den = literal(above), literal(below)
            if (not _same_printed_literal(num, terms[0].get_text())
                    or not _same_printed_literal(den, terms[1].get_text())
                    or not _same_printed_literal(literal(before)+num+den+literal(after), primary.get_text())):
                continue
            tex = literal(before) + r'\frac{' + num + '}{' + den + '}' + literal(after)
            html = api.latex_math(tex, 'inline')
            if html and BeautifulSoup(html, 'html.parser').find('mfrac'):
                answers.append((html, [x0, y0, x1, y1]))
    return answers[0] if len(answers) == 1 else None


def confirm_vector_cell(source_html, primary_html, source, geometry, reader, regions,
                        page, args, manifest, api, *, ref=None):
    """Return a confirmed choice only for a risky, fully owned vector-text cell."""
    from docling_pdf_text_cells import owns_positioned_text
    parsed = BeautifulSoup(source_html, 'html.parser')
    proposed = BeautifulSoup(primary_html, 'html.parser')
    if parsed.find(['svg', 'table']) or proposed.find(['img', 'svg', 'table']):
        return None
    source_images = parsed.find_all('img')
    if source_images and (len(source_images) != 1 or source_images[0].get('data-source-cell') != 'true'
                          or not proposed.find('mfrac')):
        return None
    short_symbols = (max(len(parsed.get_text(strip=True)), len(proposed.get_text(strip=True))) <= 3
                     and any(c.isalpha() for c in parsed.get_text() + proposed.get_text()))
    structured = parsed.select_one('math, sup, sub, .source-latex') or proposed.select_one('math, sup, sub, .source-latex')
    owner = BeautifulSoup('<td>' + source_html + '</td>', 'html.parser').td
    if not owns_positioned_text(owner, source, geometry) or reader is None or regions is None:
        return None
    source_page = reader.load_page(page)
    witness = source.get('positioned_source_text') or {}
    left, top, right, bottom = source['bbox']
    cell_has_raster = any(im['x0'] < right and left < im['x1'] and im['top'] < bottom and top < im['bottom']
                          for im in source_page.images)
    if cell_has_raster:
        return None
    fully_visible = (witness.get('ink_coverage') == 1 and witness.get('glyph_ink_support') == 1
                     and witness.get('positioned_assets', 0) == len(source_images)
                     and geometry.get('positioned_source_grid_verified'))
    one_line = len(source.get('lines', [])) == 1
    method, restored_html, rule = None, None, None
    if fully_visible and not (short_symbols or structured):
        if (one_line and parsed.get_text() != proposed.get_text() and not proposed.find('br')
                and len(proposed.find_all(['p', 'div', 'li'])) <= 1):
            method = 'complete_visible_native_glyph_line'
    elif fully_visible and (source_page.images or source_images):
        fraction = _native_simple_fraction(proposed, source, source_page, api) if proposed.find('mfrac') else None
        if source_images:
            # A cropped vector bar may be the one positioned asset in this cell.
            # It may disappear only if all its ink belongs to the native rule.
            assets = [a for a in geometry.get('retained_cell_images', [])
                      if (a['row'], a['column']) == (source['row'], source['column'])]
            if (not fraction or len(assets) != 1 or not assets[0].get('bbox')
                    or any(abs(a-b) > 1 for a, b in zip(assets[0]['bbox'], fraction[1]))):
                return None
            from io import BytesIO
            import base64
            import numpy as np
            from PIL import Image
            with Image.open(BytesIO(base64.b64decode(source_images[0]['src'].split(',', 1)[1]))) as bar:
                dark = np.asarray(bar.convert('L')) < 180
            ys, xs = np.nonzero(dark)
            if (not len(xs) or xs.max()-xs.min()+1 < 5*(ys.max()-ys.min()+1)
                    or not dark[ys.min():ys.max()+1, xs.min():xs.max()+1].all()):
                return None
        if fraction:
            restored_html, rule = fraction
            method = 'positioned_glyphs_on_both_sides_of_native_fraction_rule'
        elif (one_line and witness.get('positioned_source_scripts') and not proposed.find('mfrac')
                and literal_script_markup(source_html, api) is not None
                and literal_script_markup(primary_html, api) is not None
                and _same_printed_literal(parsed.get_text(), proposed.get_text())):
            method = 'complete_visible_native_script_positions'
    if method:
        proof = dict(method=method, choice='source', page=page, ref=ref,
                     bbox=list(source['bbox']), row=source['row'], column=source['column'],
                     source_html_sha256=sha256(source_html.encode()).hexdigest(),
                     primary_html_sha256=sha256(primary_html.encode()).hexdigest(),
                     glyphs=witness['glyphs'], ink_coverage=witness['ink_coverage'],
                     glyph_ink_support=witness['glyph_ink_support'])
        if restored_html is not None:
            proof.update(source_literal_html=restored_html, fraction_rule_bbox=rule)
        manifest.setdefault('vector_cell_content_confirmations', []).append(proof)
        api.diagnostic_event('VECTOR_CELL_SOURCE_LITERAL_RETAINED', **proof)
        return 'source', proof
    if not (short_symbols or structured) or source_page.images or source_images:
        return None
    source_signature = content_signature(source_html, api)
    primary_signature = content_signature(primary_html, api)
    if source_signature == primary_signature:
        return None
    symbol_case_family = (
        short_symbols
        and _visual(source_signature[0]).casefold() == _visual(primary_signature[0]).casefold()
        and source_signature[2] == primary_signature[2]
        and _script_positions(source_signature) == _script_positions(primary_signature)
        and ''.join(c for c in source_signature[0] if not c.isalpha())
            == ''.join(c for c in primary_signature[0] if not c.isalpha()))
    positioned = source.get('positioned_source_text', {})
    literal_html = literal_script_markup(source_html, api)
    literal_parsed = BeautifulSoup(literal_html, 'html.parser') if literal_html is not None else parsed
    literal = literal_parsed.get_text('', strip=True)
    literal_review_allowed = (
        literal_html is not None and not literal_parsed.find('math') and literal and '\ufffd' not in literal
        and all(ch.isprintable() or ch.isspace() for ch in literal)
        and positioned.get('ink_coverage', 0) >= .999
        and positioned.get('glyph_ink_support', 0) >= .999)
    model = getattr(args, 'surya_ocr_model', None) if args is not None else None
    context = dict(stage='vector_cell_content', page=page,
                   ref=ref, element=ref,
                   cell={k: source[k] for k in ('row', 'column', 'row_span', 'column_span')},
                   bbox=list(source['bbox']))
    def fail(reason, **details):
        api.diagnostic_event('VECTOR_CELL_CONTENT_CONFLICT', **context, reason=reason, severity='warning', **details)
        error = VectorContentConflict('Conflicting table math/symbol has no consistent source-pixel reading')
        error.context = dict(context, reason=reason, **details)
        raise error
    from surya.inference.schema import BatchInputItem
    from surya.inference.util import image_token_budget
    from surya.settings import settings
    estimated_count = max(4 * max(len(source_signature[0]), len(primary_signature[0])),
                          max(len(source_html), len(primary_html)) // 2)
    token_limit = image_token_budget(estimated_count,
        ceiling=settings.SURYA_MAX_TOKENS_BLOCK_CEILING, floor=256)
    evidence = []
    decisions = []
    counter_unavailable = None
    backend = None
    try:
        if model is None:
            raise RuntimeError('Regional OCR model unavailable')
        backend = api.local_surya_backend(model)
    except Exception as error:
        if not literal_review_allowed:
            fail('regional_ocr_unavailable', error_type=type(error).__name__)
        counter_unavailable = str(error)
        evidence.append(dict(status='failed', phase='backend', error_type=type(error).__name__,
                             error=str(error), token_limit=token_limit))
    for scale in ((3.0, 6.0) if backend is not None else ()):
        image = None
        digest = raw = None
        token_count = None
        try:
            image = regions.crop(page, source['bbox'], scale=scale)
            from docling_pdf_cell_crop import cell_content_crop
            measured = cell_content_crop(image, [0, 0, image.width, image.height],
                                         [0, 0, image.width, image.height])
            if measured:
                inner = image.crop(measured[0])
                image.close()
                image = inner
            digest = sha256(image.convert('RGB').tobytes()).hexdigest()
            outputs = backend.generate([BatchInputItem(image=image, prompt_type='block', max_tokens=token_limit,
                metadata=dict(context, purpose='vector_source_counterread', scale=scale))])
            if len(outputs) == 1:
                raw, token_count = outputs[0].raw, outputs[0].token_count
            if (len(outputs) != 1 or outputs[0].error or outputs[0].token_count >= token_limit
                    or not raw or not raw.strip()):
                raise RuntimeError('Regional OCR incomplete or response budget exhausted')
            # A cell reader sometimes returns a one-cell table. Its wrappers
            # do not change character order; a graphic cannot confirm text.
            answer = BeautifulSoup(raw, 'html.parser')
            if answer.find(['img', 'svg']):
                fail('regional_ocr_not_editable_text')
            for wrapper in list(answer.find_all(['table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th'])):
                wrapper.unwrap()
            signature = content_signature(str(answer), api)
            source_match = _confirms(signature, source_signature)
            primary_match = _confirms(signature, primary_signature)
            source_case_variation = False
            if not source_match and not primary_match:
                # Tiny isolated vector glyphs can be read in the other case.
                # This can confirm only the SOURCE candidate when every other
                # character/number and script position rules out the primary.
                # The source Unicode/case remains untouched in the output.
                source_case_variation = (
                    _visual(signature[0]).casefold() == _visual(source_signature[0]).casefold()
                    and _visual(signature[0]).casefold() != _visual(primary_signature[0]).casefold()
                    and signature[2] == source_signature[2]
                    and (not signature[1] or _script_positions(signature) <= _script_positions(source_signature)))
                source_match = source_case_variation
            # If flattened readings are identical but primary script roles
            # contradict measured source glyph positions, retain the source.
            if source_match and primary_match:
                primary_match = False  # Preserve the actual source Unicode for identical visible glyphs.
            choice = 'source' if source_match and not primary_match else 'primary' if primary_match and not source_match else None
            if (symbol_case_family
                    and _visual(signature[0]).casefold() == _visual(source_signature[0]).casefold()
                    and signature[2] == source_signature[2]
                    and (not signature[1] or _script_positions(signature) <= _script_positions(source_signature))):
                choice = 'source'  # Declared PDF Unicode; case ambiguity remains explicit REVIEW evidence.
            if choice is None:
                if literal_review_allowed:
                    choice = 'source_literal_review'
                else:
                    fail('regional_reading_confirms_neither_unique_candidate',
                         source_signature=source_signature, primary_signature=primary_signature,
                         regional_signature=signature)
            decisions.append(choice)
            evidence.append(dict(scale=scale, pixels=list(image.size), source_sha256=digest,
                response_sha256=sha256(raw.encode()).hexdigest(), signature=signature, choice=choice,
                source_case_variation=source_case_variation, status='complete', token_limit=token_limit,
                token_count=token_count))
        except Exception as error:
            if not literal_review_allowed:
                if isinstance(error, VectorContentConflict):
                    raise
                fail('regional_ocr_unavailable', error_type=type(error).__name__, token_limit=token_limit)
            counter_unavailable = str(error)
            evidence.append(dict(scale=scale, status='failed', error_type=type(error).__name__,
                error=str(error), source_sha256=digest,
                pixels=list(image.size) if image is not None else None,
                response_sha256=sha256(raw.encode()).hexdigest() if raw is not None else None,
                token_limit=token_limit, token_count=token_count))
            break  # A literal REVIEW does not justify another greedy request.
        finally:
            if image is not None:
                image.close()
    literal_review = bool(counter_unavailable or 'source_literal_review' in decisions)
    if len(set(decisions)) != 1:
        if literal_review_allowed:
            literal_review = True
        else:
            fail('regional_readings_disagree')
    choice = 'source' if literal_review else decisions[0]
    review_required = bool(symbol_case_family or literal_review)
    reason = ('counterread_unavailable' if counter_unavailable else
              'numeric_source_literal_not_verified' if literal_review else
              'regional_case_ambiguity_source_literal_retained' if symbol_case_family else
              'visible_vector_source_kept_after_regional_counterread')
    method = ('positioned_vector_source_literal_without_complete_counterread' if counter_unavailable else
              'positioned_vector_text_and_two_source_pixel_counterreads')
    proof = dict(context, method=method,
        choice=choice, source_signature=source_signature, primary_signature=primary_signature,
        source_html_sha256=sha256(source_html.encode()).hexdigest(),
        primary_html_sha256=sha256(primary_html.encode()).hexdigest(), readings=evidence,
        review_required=review_required, reason=reason, source_literal=literal)
    if counter_unavailable:
        proof['counterread_error'] = counter_unavailable
    if literal_review and literal_html != source_html:
        proof['source_literal_html'] = literal_html
        proof['literal_script_rendering'] = 'lossless_mathml_scripts_to_html_sup_sub'
    if not counter_unavailable:
        manifest.setdefault('vector_cell_content_confirmations', []).append(proof)
    api.diagnostic_event('VECTOR_CELL_SOURCE_LITERAL_REVIEW' if counter_unavailable else
                         'VECTOR_CELL_CONTENT_CONFIRMED', **proof)
    if choice == 'source':
        manifest.setdefault('primary_ocr_content_warnings', []).append(dict(page=page,
            row=source['row'], column=source['column'], reason=reason))
        if review_required:
            proof['message'] = ('В таблице сохранён буквальный текст исходного PDF. '
                'Повторное OCR не подтвердило однозначное чтение символов; '
                'проверьте обозначение по оригиналу. Нормативные значения не подставлялись.')
            manifest.setdefault('vector_source_literal_reviews', []).append(proof)
    return choice, proof
