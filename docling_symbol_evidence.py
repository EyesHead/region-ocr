"""Resolve isolated source-image letters by exact glyph pixels in equations.

This is transient evidence, not another OCR pass. A connected source glyph must
match byte-for-byte (including its dimensions and RGB antialiasing) a left-hand
letter read in a compound source equation. Conflicting letter evidence prevents
replacement. Numeric/composite expressions and unrelated drawings are untouched.
"""

import hashlib
import re
from io import BytesIO

import cv2
import numpy as np
from bs4 import BeautifulSoup
from PIL import Image


def _source_image(data):
    with Image.open(BytesIO(data)) as image:
        rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, "white")
    white.alpha_composite(rgba)
    return white.convert("RGB")


def _components(image):
    pixels = np.asarray(image)
    ink = (np.asarray(image.convert("L")) < 160).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    components = sorted((tuple(map(int, row)) for row in stats[1:] if row[4] >= 2), key=lambda row: (row[0], row[1]))
    return pixels, components


def _pattern(pixels, component):
    x, y, width, height, area = component
    if min(width, height) < 4 or area < 8:
        return None
    crop = pixels[y:y + height, x:x + width]
    return hashlib.sha256(f"{width}:{height}:".encode() + crop.tobytes()).hexdigest()


def _single_letter_math(math):
    if math is None:
        return None
    # A single Greek/Latin letter or a commonly confused triangular glyph.
    # Never simplify a subscript, exponent, number or composite expression.
    if math.find(["mfrac", "msub", "msup", "msubsup", "mroot", "msqrt", "mtable"]):
        return None
    text = math.get_text().strip()
    return text if len(text) == 1 and (text.isalpha() or text in "△∆∇") else None


def _definition_context(metadata):
    before, after = metadata.get("before", "").strip(), metadata.get("after", "").strip()
    return before in {"", "где", "здесь"} and bool(re.match(r"^[-—–]\s*[А-Яа-яЁё]", after))


class SourceGlyphEvidence:
    """Gather source glyph evidence, then resolve earlier inline image symbols.

    ``observe`` returns the same MathML with a temporary source marker only for
    isolated letters in explicit variable definitions. Return that value from
    the image recognizer callback. ``finalize`` resolves markers after all
    source assets have been observed and removes every temporary marker.
    """

    def __init__(self):
        self.templates = {}
        self.targets = {}

    def observe(self, data, metadata, mathml):
        if not mathml:
            return mathml
        soup = BeautifulSoup(mathml, "html.parser")
        math = soup.math
        if math is None:
            return mathml
        source_hash = hashlib.sha256(data).hexdigest()
        try:
            pixels, components = _components(_source_image(data))
        except (OSError, ValueError):
            return mathml
        latex = math.get("data-latex", "")
        compound = re.match(r"^\s*(\\[A-Za-z]+|[^\s{}])\s*=\s*\S", latex)
        if compound and components:
            first = components[0]
            # A disconnected/dotted LHS cannot be proved from just its leftmost
            # component. Reject overlapping component ranges conservatively.
            isolated_left = all(component[0] >= first[0] + first[2] + 1 for component in components[1:])
            first_token = math.find(["mi", "mn", "mo"])
            letter = first_token.get_text().strip() if first_token is not None else ""
            if isolated_left and len(letter) == 1 and letter.isalpha() and (pattern := _pattern(pixels, first)):
                self.templates.setdefault(pattern, {}).setdefault(letter, set()).add(source_hash)
        if len(components) == 1 and _definition_context(metadata) and _single_letter_math(math):
            pattern = _pattern(pixels, components[0])
            if pattern:
                self.targets[source_hash] = pattern
                math["data-source-asset"] = source_hash
                math["data-source-symbol-context"] = "definition"
                return str(soup)
        return mathml

    def finalize(self, content, latex_math, diagnostic=None):
        diagnostic = diagnostic or (lambda *args, **kwargs: None)
        soup = BeautifulSoup(content, "html.parser")
        touched = False
        for math in soup.find_all("math"):
            source_hash = math.attrs.pop("data-source-asset", None)
            context = math.attrs.pop("data-source-symbol-context", None)
            if source_hash is None and context is None:
                continue
            touched = True
            if context != "definition" or source_hash not in self.targets:
                continue
            pattern = self.targets[source_hash]
            evidence = self.templates.get(pattern, {})
            if len(evidence) != 1:
                if len(evidence) > 1:
                    diagnostic("OFFICE_FORMULA_SOURCE_GLYPH_CONFLICT", source_sha256=source_hash,
                               candidates=sorted(evidence), matching="exact_rgb")
                continue
            current = _single_letter_math(math)
            expected = next(iter(evidence))
            if not current or current == expected:
                continue
            replacement = latex_math(expected, "inline")
            if not replacement:
                continue
            replacement_math = BeautifulSoup(replacement, "html.parser").math
            if replacement_math is None:
                continue
            math.replace_with(replacement_math)
            diagnostic("OFFICE_FORMULA_SOURCE_GLYPH", source_sha256=source_hash,
                       recognized=current, source_label=expected, matching="exact_rgb",
                       evidence_assets=sorted(evidence[expected]))
        return str(soup) if touched else content
