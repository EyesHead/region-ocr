"""Recognition of source Office image equations using the existing local OCR.

Recognition results live only in this document's memory. Source diagrams are
kept, and a failed equation reading raises before publication instead of silently
publishing the same raster equation as a successful conversion.
"""

import hashlib
import json
import math
import re
from io import BytesIO

from bs4 import BeautifulSoup
from PIL import Image


def formula_image_candidate(metadata):
    """Use original pixels and placement, not the displayed size of a thumbnail."""
    if metadata.get('formula_item'):
        return True
    width, height = metadata.get('width'), metadata.get('height')
    if not width or not height:
        return False
    if height <= (180 if metadata.get('source_media_type') in {'image/x-wmf', 'image/wmf', 'image/emf', 'image/x-emf'} else 96) and width <= 1800:
        return True
    context = metadata.get('paragraph_text', '')
    numbered = re.fullmatch(r'\s*[,;]?\s*\([А-ЯA-Zа-яa-z]?\.?\d+(?:\.\d+)*[а-яa-z]?\)\s*', context)
    # Larger, higher-resolution equations need a source equation paragraph.
    return bool(numbered and height <= 320 and width / height >= 1.5)


def equation_context(metadata):
    if metadata.get('formula_item'):
        return True
    text = metadata.get('paragraph_text', '')
    if re.fullmatch(r'\s*[,;]?\s*\([А-ЯA-Zа-яa-z]?\.?\d+(?:\.\d+)*[а-яa-z]?\)\s*', text):
        return True
    before, after = metadata.get('before', '').strip(), metadata.get('after', '').strip()
    return bool((before in {'где', 'здесь', ''} and re.match(r'^[-—–]\s*[А-Яа-яЁё]', after))
                or re.search(r'по формуле\s*[:;]?$', before, re.I))


def prepare_formula_image(data, attempt=0):
    with Image.open(BytesIO(data)) as original:
        rgba = original.convert('RGBA')
    image = Image.new('RGBA', rgba.size, 'white')
    image.alpha_composite(rgba)
    image = image.convert('RGB')
    # Word's equation PNGs can be just 14 pixels high. Keep transparency white
    # and enlarge the actual glyphs, rather than asking OCR to read a tiny icon.
    scale = max(1, min(6, (120 if attempt == 0 else 180) / max(1, image.height)))
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    padded = Image.new('RGB', (image.width + 32, image.height + 32), 'white')
    padded.paste(image, (16, 16))
    return padded


def chemical_math(content, latex_math):
    """Render Surya's <chem> molecular notation without flattening atom counts.

    This is a notation parser, not chemical inference: element order and counts
    are copied as read. Ambiguous unmarked charges/bonds and structural drawing
    syntax decline rather than inventing a molecule or silently losing a bond.
    """
    value = content.strip()
    if value.startswith(r'\ce{') and value.endswith('}'):
        value = value[4:-1]
    if '\\' in value:
        commands = re.findall(r'\\([A-Za-z]+)', value)
        if not set(commands) <= {'mathrm', 'text', 'left', 'right', 'cdot', 'times'}:
            return None
        return latex_math(value, 'inline')
    token = re.compile(r'[A-Z][a-z]?|[_^]\{[A-Za-z0-9+\-]+\}|[_^][0-9]+|\d+|[()\[\].·]')
    parts, stack, offset, eligible, elements = [], [], 0, False, 0
    while offset < len(value):
        if value[offset].isspace():
            offset += 1
            continue
        match = token.match(value, offset)
        if not match:
            return None
        item = match[0]; offset = match.end()
        if re.fullmatch(r'[A-Z][a-z]?', item):
            parts.append(r'\mathrm{' + item + '}'); eligible = True; elements += 1
        elif item.isdigit():
            parts.append('_{' + item + '}' if eligible else item); eligible = False
        elif item[0] in '_^':
            if not parts:
                return None
            parts.append(item if '{' in item else item[0] + '{' + item[1:] + '}'); eligible = False
        elif item in '([':
            stack.append(item); parts.append(item); eligible = False
        elif item in ')]':
            if not stack or stack.pop() != {')': '(', ']': '['}[item]:
                return None
            parts.append(item); eligible = True
        else:
            parts.append(r'\cdot '); eligible = False
    return latex_math(''.join(parts), 'inline') if elements and not stack else None


def parsed_formula(content, latex_math, source_row_count=None):
    """Accept mathematical content only; never turn diagram labels into prose."""
    content = re.sub(r'^```(?:html|latex)?\s*|\s*```$', '', content.strip())
    soup = BeautifulSoup(content, 'html.parser')
    if soup.find(['img', 'table', 'svg', 'script', 'iframe', 'object']):
        return None
    chems = soup.find_all('chem')
    if chems:
        if len(chems) != 1:
            return None
        source = chems[0].get_text()
        chems[0].extract()
        if soup.get_text().strip(' \t\n.,;:'):
            return None
        return chemical_math(source, latex_math)
    maths = soup.find_all('math')
    if maths:
        if len(maths) > 1:
            # Separate formulas are a complete reading only when the actual
            # source contains that many separated rows. Inline math fragments
            # or an incomplete subset cannot become a synthetic matrix.
            if source_row_count != len(maths) or source_row_count < 2:
                return None
            rows, owners = [], set()
            for node in maths:
                owner = node.find_parent(['div', 'p'])
                if owner is None or id(owner) in owners or len(owner.find_all('math')) != 1:
                    return None
                owners.add(id(owner))
                rendered = parsed_formula(str(owner), latex_math)
                if rendered is None:
                    return None
                rows.append(rendered)
            for node in maths:
                node.extract()
            if soup.get_text().strip():
                return None
            matrix = BeautifulSoup('<math xmlns="http://www.w3.org/1998/Math/MathML" '
                'display="inline"><mtable columnalign="left"></mtable></math>', 'html.parser')
            for rendered in rows:
                row = matrix.new_tag('mtr')
                cell = matrix.new_tag('mtd')
                source_math = BeautifulSoup(rendered, 'html.parser').math
                for child in list(source_math.contents):
                    cell.append(child.extract())
                row.append(cell)
                matrix.mtable.append(row)
            return str(matrix.math)
        if len(maths) != 1:
            return None
        math = maths[0]
        if math.find(True):
            allowed = {'math', 'mrow', 'mi', 'mn', 'mo', 'msup', 'msub', 'msubsup', 'mfrac',
                       'msqrt', 'mroot', 'mover', 'munder', 'munderover', 'mtext', 'mspace',
                       'mfenced', 'menclose', 'mtable', 'mtr', 'mtd', 'mstyle', 'mpadded', 'mphantom'}
            if any(node.name not in allowed for node in math.find_all(True)):
                return None
            for node in [math, *math.find_all(True)]:
                node.attrs = {k: v for k, v in node.attrs.items() if k in {
                    'xmlns', 'display', 'mathvariant', 'stretchy', 'fence', 'separator',
                    'lspace', 'rspace', 'columnalign', 'rowalign', 'notation', 'linethickness'}}
            math['display'] = 'inline'
            math['xmlns'] = 'http://www.w3.org/1998/Math/MathML'
            rendered = str(math)
            math.extract()
            return rendered if not soup.get_text().strip(' \t\n.,;:') else None
        latex = math.get('data-latex') or math.get_text()
        math.extract()
        if soup.get_text().strip(' \t\n.,;:'):
            return None
    else:
        # OCR sometimes returns an ordinary heading/paragraph with genuine
        # sub/sup elements. Preserve their structure before extracting LaTeX;
        # get_text() alone silently turns gamma_n into two baseline letters.
        for node in reversed(soup.find_all(['sub', 'sup'])):
            marker = '_' if node.name == 'sub' else '^'
            node.replace_with(marker + '{' + node.get_text() + '}')
        latex = soup.get_text(' ', strip=True)
        if latex.startswith('\\(') and latex.endswith('\\)'):
            latex = latex[2:-2]
        if latex.startswith('\\[') and latex.endswith('\\]'):
            latex = latex[2:-2]
        # A few isolated letters/operators often arrive as <p>, rather than
        # <math>. Russian explanations and OCR narratives are not equations.
        if re.search(r'[А-Яа-яЁё]{3,}|[A-Za-z]{4,}', re.sub(r'\\[A-Za-z]+', '', latex)):
            return None
    if not latex.strip():
        return None
    return latex_math(latex.strip(), 'inline')


def source_equation_label(result, metadata, latex_math, diagnostic):
    """Resolve a confusable LHS only from an explicit adjacent source definition.

    This never changes numeric terms or searches unrelated paragraphs. Word's
    text immediately before a numbered image equation can explicitly name the
    variable, e.g. 'Л — ..., определяется по формуле'.
    """
    previous = metadata.get('previous_text', '')
    context = metadata.get('paragraph_text', '')
    numbered = re.fullmatch(r'\s*[,;]?\s*\([А-ЯA-Zа-яa-z]?\.?\d+(?:\.\d+)*[а-яa-z]?\)\s*', context)
    definition = re.fullmatch(r'\s*([A-Za-zА-Яа-яЁёΑ-Ωα-ω])\s*[-—–]\s*.+?'
                              r'(?:определяется|вычисляется|рассчитывается)\s+по формуле\s*[:;]?\s*', previous, re.I)
    if not numbered or not definition:
        return result
    math = BeautifulSoup(result, 'html.parser').math
    latex = math.get('data-latex', '')
    lhs = re.match(r'\s*(\\[A-Za-z]+|[^\s{}])\s*(?==)', latex)
    if not lhs:
        return result
    expected = definition[1]
    current = BeautifulSoup(latex_math(lhs[1], 'inline') or '', 'html.parser').get_text()
    if current == expected:
        return result
    # Restrict corrections to known visually confusable single glyphs; source
    # definitions do not authorize replacing arbitrary variables or expressions.
    confusables = ('ΠПЛ', 'Δ△∆', 'αaа', 'βBВ', 'νv', 'ρpр', 'ηn', 'λл',
                   'HН', 'PР', 'CС', 'BВ', 'XХ', 'AА', 'KК', 'MМ', 'OО', 'TТ', 'EЕ', 'yу')
    if not any(expected in group and current in group for group in confusables):
        return result
    fixed = latex[:lhs.start(1)] + expected + latex[lhs.end(1):]
    replacement = latex_math(fixed, 'inline')
    if replacement:
        diagnostic('OFFICE_FORMULA_CONTEXT_LABEL', media=metadata.get('media_name'),
                   source_definition=previous, recognized=current, source_label=expected)
        return replacement
    return result


class OfficeFormulaRecognizer:
    def __init__(self, args, configure, resolve_device, backend_for_model, latex_math, diagnostic):
        self.args = args
        self.configure = configure
        self.resolve_device = resolve_device
        self.backend_for_model = backend_for_model
        self.latex_math = latex_math
        self.diagnostic = diagnostic
        self.manager = None
        self.backend = None
        self.memory = {}
        self.layout_memory = {}
        from docling_layout import SuryaAssetLayout
        from docling_symbol_evidence import SourceGlyphEvidence
        self.layout = SuryaAssetLayout(self._backend, diagnostic)
        self.glyph_evidence = SourceGlyphEvidence()

    def _backend(self):
        if self.backend is None:
            model = getattr(self.args, 'surya_ocr_model', None)
            if model is not None:
                self.backend = self.backend_for_model(model)
            else:
                self.args.device = self.resolve_device()
                self.configure(self.args)
                from surya.inference import SuryaInferenceManager
                self.manager = SuryaInferenceManager(method='llamacpp', lazy=True)
                self.backend = self.manager.backend
        if self.backend.handle is None or self.backend._client is None:
            self.backend.start()
        return self.backend

    def __call__(self, data, metadata):
        key = hashlib.sha256(data).hexdigest()
        if key in self.memory:
            self.diagnostic('OFFICE_FORMULA_REUSE', media=metadata.get('media_name'), source_sha256=key)
            result = source_equation_label(self.memory[key], metadata, self.latex_math, self.diagnostic)
            return self.glyph_evidence.observe(data, metadata, result)
        try:
            if key not in self.layout_memory:
                with Image.open(BytesIO(data)) as original:
                    self.layout_memory[key] = self.layout(original, metadata)
            layout = self.layout_memory[key]
        except (OSError, ValueError) as exc:
            self.diagnostic('OFFICE_FORMULA_SKIP', media=metadata.get('media_name'),
                            reason='unsupported_source_image', error=str(exc))
            raise RuntimeError('Cannot classify Office asset ' + str(metadata.get('media_name'))) from exc
        candidate = formula_image_candidate(metadata)
        # Labels in a drawing are not the whole drawing. Never erase a figure
        # containing a mathematical axis/dimension by replacing it with math.
        figure_only = layout['graphics'] and not layout['math']
        localized_label = False
        if layout['math'] and not candidate:
            boxes = [b['bbox'] for b in layout['bboxes'] if b['label'] == 'Equation']
            area = max((max(0, b[2]-b[0])*max(0, b[3]-b[1]) for b in boxes), default=0)
            localized_label = area < metadata['width'] * metadata['height'] * .65
        if ((layout['math'] and layout['graphics']) or localized_label
                or (figure_only and not (candidate and equation_context(metadata)))
                or (not layout['math'] and not candidate)):
            self.diagnostic('OFFICE_FORMULA_SKIP', media=metadata.get('media_name'),
                            reason='mixed_figure' if layout['math'] else 'source_diagram',
                            labels=layout['labels'])
            return None
        backend = self._backend()
        from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
        from surya.inference.backends.openai_client import chat_completions_batch
        from surya.settings import settings
        from docling_backend_health import request_with_backend_recovery
        from docling_pdf_quantities import source_multiline_ink, single_symbol_ink
        multiline = source_multiline_ink(data)
        for attempt in range(2):
            variable_retry = bool(attempt and not metadata.get('chemical')
                                  and not metadata.get('source_script_kind')
                                  and multiline is None
                                  and single_symbol_ink(data) is not None
                                  and equation_context(metadata)
                                  and not re.fullmatch(r'\s*[,;]?\s*\([А-ЯA-Zа-яa-z]?\.?\d+(?:\.\d+)*[а-яa-z]?\)\s*',
                                                       metadata.get('paragraph_text', '')))
            variable_pattern = r'^[A-Za-zΑ-Ωα-ωА-Яа-яЁё](?:[_^]\{?[A-Za-zΑ-Ωα-ωА-Яа-яЁё0-9,]+\}?)*$'
            guided = None
            prompt = None if attempt == 0 else (
                'OCR this block image to HTML. Transcribe this mathematical expression '
                'exactly as <math>LaTeX</math>. Preserve all indices, exponents and fractions. '
                'Do not explain or simplify. If this is a diagram, return <img>.')
            if metadata.get('chemical'):
                prompt = ('Transcribe this source chemical formula exactly as <math>LaTeX</math>. '
                          'Preserve element capitalization, all subscripts, superscripts, brackets, '
                          'charges and bonds. Do not infer, complete or correct the chemistry. '
                          'Return <img> if the formula is unreadable.')
            # A source-confirmed equation must use the math prompt on its
            # first reading. Generic BLOCK OCR may return plausible prose
            # digits for operators and would prevent the mathematical retry.
            if metadata.get('source_script_kind') or metadata.get('formula_item'):
                prompt = ('OCR this block image to HTML. Transcribe this mathematical expression exactly as '
                          '<math>LaTeX</math>. Preserve the visible subscript/superscript structure, '
                          'source alphabet and letter case. Use \\mathrm{...} for upright multi-letter '
                          'index labels. Do not flatten indices into the base symbol, infer letters '
                          'from a familiar formula, add punctuation, or explain. '
                          'Return <img> if unreadable.')
            if variable_retry:
                prompt = ('Read this isolated mathematical variable in a prose definition. '
                          'Identify the Unicode Latin, Greek or Cyrillic LETTER and its indices. '
                          'Return JSON with key variable. Use _ and ^ for indices. '
                          'This is a variable, not a geometric shape or a list marker.')
                guided = {'type': 'object', 'properties': {'variable': {
                    'type': 'string', 'pattern': variable_pattern}},
                    'required': ['variable'], 'additionalProperties': False}
            max_tokens = settings.SURYA_MAX_TOKENS_BLOCK_CEILING
            seconds = max(90, math.ceil(max_tokens/80)+15)
            self.diagnostic('OFFICE_FORMULA_REQUEST', media=metadata.get('media_name'),
                            source_sha256=key, attempt=attempt + 1, timeout=seconds,
                            max_tokens=max_tokens)
            try:
                outputs = request_with_backend_recovery(backend, lambda: chat_completions_batch(
                    [BatchInputItem(image=prepare_formula_image(data, attempt), prompt_type=PROMPT_TYPE_BLOCK,
                                    prompt=prompt, max_tokens=max_tokens, guided_json=guided)],
                    client=backend._client.with_options(max_retries=0, timeout=seconds),
                    model_name=backend.handle.model_name, timeout=seconds, max_workers=1,
                    max_retries=0, request_logprobs_default=False), self.diagnostic)
                if outputs and outputs[0].token_count >= max_tokens:
                    raise RuntimeError('Formula reached model output capacity; partial MathML rejected')
                raw = outputs[0].raw if outputs and not outputs[0].error else ''
                payload = raw
                if variable_retry and raw.lstrip().startswith('{'):
                    parsed = json.loads(raw)
                    payload = parsed.get('variable', '')
                    if not re.fullmatch(variable_pattern, payload):
                        payload = ''
                if metadata.get('source_nested_index'):
                    # Only enabled by three actual source glyph components on
                    # progressively lower baselines. Preserve the read letters;
                    # no alphabet/case substitution is performed here.
                    linear = re.sub(r'[_^{}\s]', '', BeautifulSoup(payload,'html.parser').get_text())
                    variable = re.fullmatch(r'([A-Za-zΑ-Ωα-ωА-Яа-яЁё])([A-Za-zΑ-Ωα-ωА-Яа-яЁё])(\d)',linear)
                    if variable:
                        payload = variable[1] + '_{' + variable[2] + '_{' + variable[3] + '}}'
                result = parsed_formula(payload, self.latex_math,
                    source_row_count=multiline['row_count'] if multiline else None)
                if result and multiline:
                    math_node = BeautifulSoup(result, 'html.parser').math
                    if (re.fullmatch(r'[A-Za-zΑ-Ωα-ωА-Яа-яЁё]', math_node.get_text('', strip=True))
                            and not math_node.find(['msub','msup','msubsup','mfrac','mtable','mover','munder'])):
                        self.diagnostic('OFFICE_FORMULA_INCOMPLETE_SOURCE_LINES',
                            media=metadata.get('media_name'), source_proof=multiline, response=raw)
                        result = None
                    elif len(BeautifulSoup(payload, 'html.parser').find_all('math')) > 1:
                        self.diagnostic('OFFICE_FORMULA_SOURCE_ROWS_RESTORED',
                            media=metadata.get('media_name'), source_proof=multiline)
                self.diagnostic('OFFICE_FORMULA_RESPONSE', media=metadata.get('media_name'),
                                attempt=attempt + 1, response=raw[:1000], accepted=bool(result))
                # A single triangle-shaped variable in a prose definition is
                # ambiguous; ask for a variable reading before accepting it.
                ambiguous_shape = (result and attempt == 0 and equation_context(metadata)
                                   and re.fullmatch(r'\s*\\(?:triangle|bigtriangleup)\s*',
                                       BeautifulSoup(result, 'html.parser').math.get('data-latex', '')))
                if ambiguous_shape:
                    self.diagnostic('OFFICE_FORMULA_AMBIGUOUS_SYMBOL', media=metadata.get('media_name'),
                                    reason='geometric_shape_in_variable_definition')
                    continue
                if result:
                    result = source_equation_label(result, metadata, self.latex_math, self.diagnostic)
                    self.memory[key] = result
                    return self.glyph_evidence.observe(data, metadata, result)
            except Exception as exc:
                self.diagnostic('OFFICE_FORMULA_ERROR', media=metadata.get('media_name'),
                                attempt=attempt + 1, error=str(exc))
        raise RuntimeError('Could not recognize Office equation ' + str(metadata.get('media_name')) +
                           '; see OFFICE_FORMULA_* in the document log. Previous HTML was kept.')

    def finalize(self, content):
        return self.glyph_evidence.finalize(content, self.latex_math, self.diagnostic)

    def close(self):
        if self.manager is not None:
            self.manager.stop()
            self.manager = None
        self.memory.clear()
        self.layout_memory.clear()
        self.glyph_evidence.templates.clear()
        self.glyph_evidence.targets.clear()
