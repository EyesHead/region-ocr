"""Recognize isolated formula crops for the balanced PDF route."""

from collections import OrderedDict
from contextlib import ExitStack
from hashlib import sha256
from html import escape
from io import BytesIO
import re
from subprocess import SubprocessError


_CACHE = OrderedDict()
_CACHE_SIZE = 1024
_LOAD_ERRORS = (OSError, RuntimeError, ImportError, ValueError, SubprocessError)
_INFERENCE_ERRORS = (OSError, RuntimeError, ValueError)
_MATH_TAG = re.compile(r'\A<(?:math|chem)(?=[\s>])', re.I)
_CODE_FENCE = re.compile(r'\A```(?:html|latex|tex|math)?[ \t]*\r?\n(.*?)\s*```\Z', re.I | re.S)
_HTML_TAG = re.compile(r'</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>')
_ACTIVE_TEX = re.compile(r'\\(?:href|url|class|style|html(?:class|style|id|data)|includegraphics)\b', re.I)
_ACTIVE_ATTRS = {'href', 'xlink:href', 'src', 'class', 'style', 'id'}


def _formula_markup(output, inline):
    """Format the model response, retaining it as text if rendering is unavailable."""
    from bs4 import BeautifulSoup, Tag
    from docling_office_math import latex_math, parsed_formula
    from local_web.structured_export import _without_math_delimiters

    raw = (getattr(output, 'raw', None) or '').strip()
    if not raw:
        return None
    fenced = _CODE_FENCE.fullmatch(raw)
    value = fenced[1].strip() if fenced else raw
    display = 'inline' if inline else 'block'
    latex = None
    markup = None
    if _MATH_TAG.match(value):
        nodes = BeautifulSoup(value, 'html.parser').contents
        if len(nodes) == 1 and isinstance(nodes[0], Tag):
            node = nodes[0]
            if node.name == 'math' and node.find(True) is None:
                latex = node.get_text().strip()
            else:
                markup = parsed_formula(value, latex_math)
    elif not _HTML_TAG.search(value):
        latex = value
    if latex is not None:
        latex = _without_math_delimiters(latex)
        if latex and not _ACTIVE_TEX.search(latex) and not _HTML_TAG.search(latex):
            markup = latex_math(latex, display)
    if markup:
        math = BeautifulSoup(markup, 'html.parser').math
        if (math is not None and not _ACTIVE_TEX.search(math.get('data-latex', ''))
                and not math.find(['maction', 'annotation-xml'])
                and not any(key in _ACTIVE_ATTRS or key.startswith('on')
                            for node in [math, *math.find_all(True)] for key in node.attrs)):
            math['display'] = display
            return str(math)
    if latex and not (_ACTIVE_TEX.search(latex) or _HTML_TAG.search(latex) or '$' in latex):
        literal = escape(latex)
        kind = 'inline' if inline else 'display'
        return f'<span data-formula="{kind}" data-latex="{literal}">{literal}</span>'
    return '<code>' + escape(value or raw) + '</code>'


def _request(backend, requests, diagnostic):
    try:
        return list(backend.generate(requests) or ())
    except _INFERENCE_ERRORS as error:
        diagnostic('BALANCED_FORMULA_REQUEST_FAILED', count=len(requests), error=str(error))
        return []


def _predict(entries, backend, diagnostic):
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK

    requests = [BatchInputItem(image, PROMPT_TYPE_BLOCK, max_tokens=128 if inline else 256,
                              temperature=0., metadata={'purpose': 'balanced_formula', **location})
                for image, inline, location in entries]
    values = []
    outputs = _request(backend, requests, diagnostic)
    for index, (_, inline, _) in enumerate(entries):
        output = outputs[index] if index < len(outputs) else None
        try:
            values.append(_formula_markup(output, inline) if output is not None else None)
        except (TypeError, ValueError, RuntimeError, RecursionError):
            raw = str(getattr(output, 'raw', '') or '').strip()
            values.append('<code>' + escape(raw) + '</code>' if raw else None)
    return values


def _recognize(crops, inline, locations, backend_provider, diagnostic):
    results = [None] * len(crops)
    pending = {}
    for index, (image, is_inline, location) in enumerate(zip(crops, inline, locations, strict=True)):
        if not all(image.size):
            continue
        key = (image.mode, image.size, is_inline, sha256(image.tobytes()).digest())
        if key in _CACHE:
            _CACHE.move_to_end(key)
            results[index] = _CACHE[key]
        else:
            _, positions = pending.setdefault(key, ((image, is_inline, location), []))
            positions.append(index)
    if not pending:
        return results
    try:
        backend = backend_provider()
    except _LOAD_ERRORS as error:
        diagnostic('BALANCED_FORMULA_BACKEND_UNAVAILABLE', error=str(error))
        return results
    if backend is None:
        return results
    values = _predict([entry for entry, _ in pending.values()], backend, diagnostic)
    for (key, (_, positions)), markup in zip(pending.items(), values, strict=True):
        if markup is None:
            for index in positions:
                diagnostic('BALANCED_FORMULA_NO_RESPONSE', **locations[index])
            continue
        for index in positions:
            results[index] = markup
        _CACHE[key] = markup
        if len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return results


def _no_diagnostic(event, **fields):
    pass


def recognize_formulas(crops, backend_provider, *, inline=None, diagnostic=None):
    """Return one document fragment or None per source PIL image."""
    inline = [False] * len(crops) if inline is None else inline
    return _recognize(crops, inline, [{}] * len(crops), backend_provider, diagnostic or _no_diagnostic)


def recognize_formula_crops(crops, backend_provider, *, diagnostic=None):
    """Read the PDF route's positioned lossless crops in input order."""
    from PIL import Image

    diagnostic = diagnostic or _no_diagnostic
    with ExitStack() as stack:
        images = [stack.enter_context(Image.open(BytesIO(crop.png))) for crop in crops]
        inline = [crop.display in ('inline', False) for crop in crops]
        locations = [dict(page_no=crop.page_no, bbox=crop.bbox) for crop in crops]
        return _recognize(images, inline, locations, backend_provider, diagnostic)


def create_formula_reader(backend_provider, *, diagnostic=None):
    """Return a document callback, starting the backend only for an uncached crop."""
    diagnostic = diagnostic or _no_diagnostic
    backend = None
    unavailable = False

    def get_backend():
        nonlocal backend, unavailable
        if backend is None and not unavailable:
            try:
                candidate = backend_provider()
                if candidate is not None and hasattr(candidate, 'handle') and candidate.handle is None:
                    candidate.start()
                backend = candidate
                unavailable = candidate is None
            except _LOAD_ERRORS:
                unavailable = True
                raise
        return backend

    def recognize(crops):
        return recognize_formula_crops(crops, get_backend, diagnostic=diagnostic)

    return recognize


def local_formula_reader(manager_provider=None, *, diagnostic=None, install_missing=None):
    """Configure Surya on the first uncached crop, with the same runtime as the PDF worker."""
    diagnostic = diagnostic or _no_diagnostic
    manager = None

    def backend():
        nonlocal manager
        from docling_backend_health import bound_surya_backend
        if manager_provider is not None:
            manager = manager_provider()
        elif manager is None:
            from portable_runtime import configure_runtime
            from docling_surya_profile import SURYA_BACKEND, configure_local_surya, resolve_device
            configure_runtime()
            configure_local_surya(resolve_device(), install_missing=install_missing)
            from surya.inference import SuryaInferenceManager
            manager = SuryaInferenceManager(method=SURYA_BACKEND, lazy=True)
        return bound_surya_backend(manager.backend, diagnostic)

    return create_formula_reader(backend, diagnostic=diagnostic)
