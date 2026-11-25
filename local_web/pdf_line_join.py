"""Resolve a word split at a measured PDF line or cell boundary."""
from functools import lru_cache
from html import unescape
from html.parser import HTMLParser
import re


HYPHENS = '-‐‑\u00ad'
BREAK_MARKS = HYPHENS + '–—'
INLINE_OBJECTS = {'img', 'math', 'svg', 'table'}
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
_BLOCK = {'div', 'li', 'p'}
_STEM = re.compile(r'([а-яё]{2,})[-‐‑]$', re.I)
_PRINTED_WRAP = re.compile(r'([а-яё]+)[-‐\u00ad]$', re.I)
_TAIL = re.compile(r'[а-яё]+', re.I)
_SEPARATE_WORDS = {'и', 'или', 'либо', 'а', 'но', 'да', 'с'}


@lru_cache(maxsize=1)
def _russian_dictionary():
    try:
        from pymorphy3 import MorphAnalyzer
        return MorphAnalyzer(lang='ru')
    except (ImportError, OSError):
        return None


@lru_cache(maxsize=2048)
def _spelling(left, right):
    dictionary = _russian_dictionary()
    if dictionary is None:
        return None
    known = dictionary.word_is_known
    if known(left + '-' + right, strict=True):
        return 'hyphenated'
    if known(left + right, strict=True):
        return 'joined'
    # Productive compounds can be absent even when both components are known,
    # for example взрыв + о + пожароопасности.
    for index in range(3, len(left) - 3):
        if (left[index] in 'ое' and known(left[:index], strict=True)
                and known(left[index + 1:] + right, strict=True)
                and not known(left[index + 1:] + '-' + right, strict=True)):
            return 'joined'
    return None


def line_boundary(left, right):
    """Return whether to drop the last visible character and the separator."""
    left, right = left.rstrip(), right.lstrip()
    if not left or not right:
        return False, ''
    if len(left) < 2 or not left[-2].isalnum() or not right[0].isalnum():
        return False, ' '
    mark = left[-1]
    if mark not in HYPHENS:
        number_range = mark in '–—' and left[-2].isdigit() and re.match(r'(?:[A-ZА-ЯЁ]{1,3})?\d', right)
        return False, '' if number_range else ' '
    if mark == '\u00ad':
        return True, ''
    tail = _TAIL.match(right)
    stem = _STEM.search(left)
    if stem and tail:
        a, b = stem[1], tail[0]
        if (not a.isupper() and b.islower()) or (a.isupper() and b.isupper()):
            spelling = _spelling(a.casefold(), b.casefold())
            if spelling:
                return spelling == 'joined' and mark != '‑', ''
    if tail and tail[0].casefold() in _SEPARATE_WORDS:
        return False, ' '
    return False, ''


class _InlineText(HTMLParser):
    """Map visible text to source spans so an edit leaves surrounding HTML intact."""

    def __init__(self, raw):
        super().__init__(convert_charrefs=False)
        self.raw = raw
        self.lines = [0, *(match.end() for match in re.finditer('\n', raw))]
        self.parts = []
        self.opaque = ''
        self.depth = 0
        self.feed(raw)
        self.close()

    def handle_starttag(self, tag, attrs):
        if self.opaque:
            self.depth += int(tag == self.opaque)
        elif tag == 'br' or tag in INLINE_OBJECTS or any(key == 'data-formula' for key, _ in attrs):
            self.parts.append(('\ufffc', None, None))
            if tag not in _VOID:
                self.opaque, self.depth = tag, 1
        elif tag in _BLOCK:
            self.parts.append((' ', None, None))

    def handle_endtag(self, tag):
        if self.opaque:
            if tag == self.opaque:
                self.depth -= 1
                if not self.depth:
                    self.opaque = ''
        elif tag in _BLOCK:
            self.parts.append((' ', None, None))

    def handle_data(self, data):
        if not self.opaque and data:
            line, column = self.getpos()
            start = self.lines[line - 1] + column
            self.parts.append((data, start, start + len(data)))

    def _reference(self, name):
        if not self.opaque:
            line, column = self.getpos()
            start = self.lines[line - 1] + column
            end = start + len(name) + 1
            if self.raw[end:end + 1] == ';':
                end += 1
            self.parts.append((unescape(self.raw[start:end]), start, end))

    def handle_entityref(self, name):
        self._reference(name)

    def handle_charref(self, name):
        self._reference('#' + name)


def _parts(raw):
    return _InlineText(raw).parts if '<' in raw or '&' in raw else [(raw, 0, len(raw))]


def _one_word(pieces):
    if not (all(part.isupper() for part in pieces)
            or (not pieces[0].isupper() and all(part.islower() for part in pieces[1:]))):
        return False
    dictionary = _russian_dictionary()
    word = ''.join(pieces).casefold()
    if dictionary is None or not dictionary.word_is_known(word, strict=True):
        return False
    cut = 0
    for piece in pieces[:-1]:
        cut += len(piece)
        if dictionary.word_is_known(word[:cut] + '-' + word[cut:], strict=True):
            return False
    return True


def _multiple_wraps(lines):
    """Check the complete word when a narrow column splits it over three or more lines."""
    confirmed = set()
    pieces = []
    for index, raw in enumerate(lines):
        if not pieces and raw[-1] not in BREAK_MARKS + '>;':
            continue
        text = ''.join(value for value, _, _ in _parts(raw)).strip()
        if pieces:
            middle = _PRINTED_WRAP.fullmatch(text)
            if middle:
                pieces.append(middle[1])
                continue
            tail = _TAIL.match(text)
            if len(pieces) > 1 and tail and _one_word([*pieces, tail[0]]):
                confirmed.update(range(index - len(pieces) + 1, index + 1))
            pieces = []
        stem = _PRINTED_WRAP.search(text)
        if stem:
            pieces.append(stem[1])
    return confirmed


def _trim_edge(raw, parts, *, at_end, drop=False):
    edits = []
    ordered = reversed(parts) if at_end else parts
    for text, start, end in ordered:
        if start is None:
            if text.isspace():
                continue
            break
        keep = text.rstrip() if at_end else text.lstrip()
        if keep and drop:
            keep = keep[:-1]
        if len(keep) != len(text):
            if at_end:
                edits.append((start + len(keep), end))
            else:
                edits.append((start, end - len(keep)))
        if text.strip():
            break
    for start, end in sorted(edits, reverse=True):
        raw = raw[:start] + raw[end:]
    return raw


def join_html_lines(parts):
    """Join measured lines, changing only boundary whitespace and a proven wrap."""
    lines = [part for raw in parts if (part := raw.strip())]
    confirmed = _multiple_wraps(lines) if len(lines) > 2 else set()
    output = []
    for index, raw in enumerate(lines):
        if output:
            previous = output[-1]
            separator = ' '
            if previous[-1] in BREAK_MARKS + '>;':
                before, after = _parts(previous), _parts(raw)
                left = ''.join(text for text, _, _ in before).rstrip()
                right = ''.join(text for text, _, _ in after).lstrip()
                drop, separator = (True, '') if index in confirmed else line_boundary(left, right)
                if left and left[-1] in BREAK_MARKS and right[:1].isalnum():
                    output[-1] = _trim_edge(previous, before, at_end=True, drop=drop)
                    raw = _trim_edge(raw, after, at_end=False)
            output.append(separator)
        output.append(raw)
    return ''.join(output)
