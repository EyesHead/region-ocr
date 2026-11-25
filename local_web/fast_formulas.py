"""Locate formula crops using positioned source glyphs or OCR lines."""

from dataclasses import dataclass
from itertools import groupby
from math import ceil, floor
import re


EQUATION_NUMBER = re.compile(r'\(\s*(?:[A-ZА-ЯЁ]\s*\.\s*)?\d+(?:[.\-]\d+)*\s*\)\s*[,.;]?$')
_PROBLEM = re.compile(r'^\s*\d{1,3}(?:\.\d{1,3})+\.\s*')
_OPERATORS = '=≈≃≠≤≥<>±∓×÷∝≡'
_STRUCTURE = '∑∫∏√∂∇⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉^_'
_VARIABLE = r'\w'
_RELATION = re.compile(_VARIABLE + r'[)\]}]*\s*[' + _OPERATORS + r']\s*[-+−–(\[{]*\s*' + _VARIABLE)
_BASIC = re.compile(r'[A-Za-zА-Яа-яЁёα-ωΑ-Ω0-9]\s*[+/−⋅]\s*[A-Za-zА-Яа-яЁёα-ωΑ-Ω0-9]')
_ALPHA = re.compile(r'[^\W\d_]+')
_MATH_NAMES = {'sin', 'cos', 'tan', 'tg', 'ctg', 'cot', 'arcsin', 'arccos', 'arctg', 'sinh', 'cosh',
               'const', 'ln', 'log', 'lim', 'min', 'max', 'exp', 'mod', 'det', 'град', 'макс', 'мин'}
_PROSE_WORDS = {'и', 'или', 'на', 'по', 'от', 'до', 'при', 'для', 'из', 'где', 'если', 'не', 'но',
                'and', 'for', 'with', 'from', 'as', 'is', 'of', 'or', 'to', 'in', 'by', 'at', 'on'}


@dataclass(frozen=True)
class FormulaCrop:
    page_no: int
    bbox: tuple[float, float, float, float]
    text: str
    display: bool
    png: bytes


def bounds(words):
    return (min(word[0] for word in words), min(word[1] for word in words),
            max(word[2] for word in words), max(word[3] for word in words))


def coverage(box, owner):
    left, top = max(box[0], owner[0]), max(box[1], owner[1])
    right, bottom = min(box[2], owner[2]), min(box[3], owner[3])
    return max(0, right-left)*max(0, bottom-top) / max(.01, (box[2]-box[0])*(box[3]-box[1]))


def ordinary_text(text):
    for word in _ALPHA.findall(text):
        folded = word.casefold()
        if folded in _PROSE_WORDS:
            return True
        if len(word) >= 3 and folded not in _MATH_NAMES:
            if not (folded[:-1] in _MATH_NAMES and ord(folded[-1]) > 127):
                return True
    return False


def formula_evidence(text, *, standalone=False):
    text = _PROBLEM.sub('', text).strip()
    if not text or len(text) > 128 or ordinary_text(text) or EQUATION_NUMBER.fullmatch(text):
        return False
    if _RELATION.search(text):
        return True
    if any(symbol in text for symbol in _STRUCTURE) and any(char.isalnum() for char in text):
        return True
    return standalone and len(text) <= 42 and bool(_BASIC.search(text)) and (
        any(char.isdecimal() for char in text) or any(char in text for char in '+−⋅'))


def vector_fractions(rows, horizontal_rules, em, cells):
    """Locate stacked native glyphs around a short PDF ruling without rendering the page."""
    bars = [(l, y, r) for y, l, r in horizontal_rules if .55*em <= r-l <= 12*em]
    if not bars:
        return
    bands = {}
    for row in rows:
        inline = any(ordinary_text(word[4]) for word in row)
        for word in row:
            if ordinary_text(word[4]) or EQUATION_NUMBER.fullmatch(word[4]):
                continue
            cy = (word[1] + word[3]) / 2
            bands.setdefault(floor(cy/em), []).append((word, cy, inline))

    for left, y, right in bars:
        band = floor(y/em)
        nearby = [entry for i in range(band-2, band+3) for entry in bands.get(i, ())]
        tiers = [[], []]
        for word, cy, _ in nearby:
            if (.16*em < abs(cy-y) <= 1.65*em and word[0] >= left-.25*em
                    and word[2] <= right+.25*em and left < (word[0]+word[2])/2 < right):
                tiers[int(cy > y)].append(word)
        if not all(tiers):
            continue
        centered = all(abs(l+r-left-right) <= .55*(right-left) for l, _, r, _ in map(bounds, tiers))
        terms = [word for tier in tiers for word in tier]
        owner = cells.get(id(terms[0]))
        if not centered or any(cells.get(id(word)) != owner for word in terms):
            continue
        # Bind the contiguous baseline on either side of the bar, without taking an
        # independent number or a neighbouring cell into the formula.
        sides = []
        inline = False
        for before in (True, False):
            edge = left if before else right
            candidates = [(word, prose) for word, cy, prose in nearby if abs(cy-y) <= .48*em
                          and (word[2] <= left if before else word[0] >= right) and cells.get(id(word)) == owner]
            adjacent = []
            for word, prose in sorted(candidates, key=lambda item: item[0][0], reverse=before):
                gap = edge-word[2] if before else word[0]-edge
                if gap > (1.9 if not adjacent else .85)*em:
                    break
                adjacent.append((word, prose))
                edge = word[0] if before else word[2]
            if any(any(c in _OPERATORS + '+−⋅' for c in word[4]) for word, _ in adjacent):
                sides.extend(word for word, _ in (reversed(adjacent) if before else adjacent))
                inline |= any(prose for _, prose in adjacent)
        words = (*sides, *terms)
        yield bounds(((left, y, right, y), *words)), words, not inline


def attach_fraction_terms(box, words, rows, image, size, em):
    """Include split numerator and denominator only with a physical bar between them."""
    if image.mode != 'L':
        return box, words
    left, top, right, bottom = box
    center = (top + bottom) / 2
    known = {id(word) for word in words}
    pieces = [word for row in rows if len(row) == 1 for word in row
              if id(word) not in known and (left + right) / 2 <= word[0] <= right + 1.1*em
              and word[2] - word[0] <= 2.8*em and abs((word[1]+word[3])/2 - center) < 1.25*em
              and not ordinary_text(word[4]) and len(word[4]) <= 7 and not EQUATION_NUMBER.fullmatch(word[4])]
    above = [word for word in pieces if (word[1]+word[3])/2 < center-.20*em]
    below = [word for word in pieces if (word[1]+word[3])/2 > center+.20*em]
    sx, sy = image.width/size[0], image.height/size[1]
    for upper in above:
        for lower in below:
            start, end = max(upper[0], lower[0]), min(upper[2], lower[2])
            if end - start < .48 * min(upper[2]-upper[0], lower[2]-lower[0]):
                continue
            middle = (upper[1]+upper[3]+lower[1]+lower[3])/4
            area = (floor(start*sx), max(0, floor((middle-.25*em)*sy)), ceil(end*sx),
                    min(image.height, ceil((middle+.25*em)*sy)))
            if area[2] - area[0] < 6 or area[3] <= area[1]:
                continue
            with image.crop(area) as patch:
                ink = patch.tobytes()
                width = patch.width
            if not any(sum(pixel < 156 for pixel in ink[offset:offset+width]) >= .90*width
                       for offset in range(0, len(ink), width)):
                continue
            combined = bounds((*words, upper, lower))
            if any(coverage(word, combined) > .20 and ordinary_text(word[4]) for row in rows for word in row):
                continue
            return combined, (*words, upper, lower)
    return box, words


def equation_number_words(row):
    for count in range(1, min(4, len(row)) + 1):
        suffix = row[-count:]
        if not EQUATION_NUMBER.fullmatch(' '.join(word[4] for word in suffix)):
            continue
        if count == len(row):
            return suffix
        previous = row[-count - 1]
        height = max(previous[3] - previous[1], suffix[0][3] - suffix[0][1])
        if suffix[0][0] - previous[2] >= .65 * height:
            return suffix
    return ()


def text_regions(rows, excluded, *, occupied=()):
    """Yield formula box, the owned words, and display context.

    A single OCR line is atomic when no reliable word positions were returned.
    """
    for index, row in enumerate(rows):
        if index in excluded:
            continue
        end = len(row) - len(equation_number_words(row))
        expression = row[:end]
        text = ' '.join(word[4] for word in expression)
        if formula_evidence(text, standalone=True):
            first = 1 if len(expression) > 1 and _PROBLEM.fullmatch(expression[0][4]) else 0
            words = tuple(expression[first:])
            box = bounds(words)
            if not any(coverage(box, owner) > .65 for owner in occupied):
                yield box, words, True
            continue
        if len(expression) < 2:
            continue
        # Words in prose are barriers. Unequivocal operators or indexed glyphs
        # may bind only the adjacent positioned tokens inside those barriers.
        for is_prose, group in groupby(expression, key=lambda word: ordinary_text(word[4])):
            if is_prose:
                continue
            words = tuple(group)
            while words and not any(c.isalnum() or c in _OPERATORS + _STRUCTURE for c in words[0][4]):
                words = words[1:]
            while words and not any(c.isalnum() or c in _STRUCTURE for c in words[-1][4]):
                words = words[:-1]
            if not words or not formula_evidence(' '.join(word[4] for word in words)):
                continue
            box = bounds(words)
            if not any(coverage(box, owner) > .65 for owner in occupied):
                yield box, words, False
