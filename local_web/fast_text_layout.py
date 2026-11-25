"""Reassemble positioned PDF text lines in reading order."""
from dataclasses import dataclass
from statistics import median
import re

from local_web.pdf_line_join import join_html_lines


@dataclass(frozen=True)
class TextLine:
    box: tuple[float, float, float, float]
    text: str
    html: str


_HEADING = re.compile(r'^(?:глава\s+\d+\b|§\s*\d+|введение\s*$|заключение\s*$|содержание\s*$)', re.I)
_BOUNDARY = re.compile(r'^(?:(?:рис\.?|таблица|табл\.?|приложение)\s+\d|(?:\d+[.)]|\d+\.\d+\.?|[а-я]\)|[•–—])\s+)', re.I)
_EQUATION_NUMBER = re.compile(r'^\(\d+(?:[.\-]\d+)*\)$')
_DISPLAY = re.compile(r'<(?:img|figure)\b|<math\b[^>]*\bdisplay=["\']block["\']', re.I)


def _kind(line):
    text = line.text.strip()
    if _DISPLAY.search(line.html) or text.isdecimal() or _EQUATION_NUMBER.fullmatch(text):
        return 'separate'
    if len(text) <= 120 and _HEADING.match(text):
        return 'heading'
    return 'separate' if _BOUNDARY.match(text) else 'prose'


def _join(*lines):
    return lines[0] if len(lines) == 1 else join_html_lines(lines)


def _read_columns(lines, width, em):
    """Use repeated tall lanes as proof of a gutter and preserve spanning rows."""
    visual = sorted(lines, key=lambda line: ((line.box[1]+line.box[3])/2, line.box[0]))
    wide = [line for line in visual if _kind(line) == 'prose' and line.box[2]-line.box[0] >= .1*width]
    if len(wide) < 6:
        return visual
    gap = max(1.3 * em, .018 * width)
    # Candidate cuts derived from adjacent measured intervals, plus page center.
    candidates = {.5 * width}
    for a, b in zip(wide, wide[1:]):
        left, right = sorted((a, b), key=lambda line: line.box[0])
        if right.box[0]-left.box[2] >= gap and abs(a.box[1]-b.box[1]) < 3*em:
            cut = (left.box[2]+right.box[0])/2
            if .25*width <= cut <= .75*width:
                candidates.add(round(cut / gap) * gap)
    best = None
    for cut in candidates:
        left = [line for line in wide if line.box[2] <= cut - .25*gap]
        right = [line for line in wide if line.box[0] >= cut + .25*gap]
        if min(len(left),len(right)) < 3 or len(left)+len(right) < .65*len(wide):
            continue
        first = max(left[0].box[1], right[0].box[1])
        last = min(left[-1].box[3], right[-1].box[3])
        if last-first < 3*em:
            continue
        score = (min(len(left),len(right)),len(left)+len(right),-abs(cut-width/2),cut)
        if best is None or score > best:
            best = score
    if best is None:
        return visual
    cut = best[-1]
    output, left, right = [], [], []
    for line in visual:
        if line.box[2] <= cut:
            left.append(line)
        elif line.box[0] >= cut:
            right.append(line)
        else:
            output.extend(left)
            output.extend(right)
            left.clear()
            right.clear()
            output.append(line)
    output.extend(left)
    output.extend(right)
    return output


def paragraphs(lines, width, *, preserve_order=False, with_boxes=False):
    """Yield HTML paragraph bodies and proven heading roles."""
    if not lines:
        return
    em = max(1., median(line.box[3]-line.box[1] for line in lines))
    ordered = lines if preserve_order else _read_columns(lines, width, em)
    prose = [line for line in ordered if _kind(line) == 'prose']
    previous = None
    body = []
    box = None
    def block(tag, html, bounds):
        return (tag, html, bounds) if with_boxes else (tag, html)

    margin = 0.
    for line in ordered:
        kind = _kind(line)
        if kind != 'prose':
            if body:
                yield block('p', _join(*body), box)
                body = []
            yield block('h2' if kind == 'heading' else 'p', line.html, line.box)
            previous = None
            continue
        left, top, right, bottom = line.box
        if previous is not None:
            a,b,c,d = previous.box
            # The local right edge is measured among neighbors in this lane.
            same_lane = [other.box[2] for other in prose if abs(other.box[0]-left) < 2*em
                         and abs(other.box[1]-top) < 8*em]
            same_lane.sort()
            edge = same_lane[(len(same_lane)*3)//4] if same_lane else right
            join = (top-b >= .33*em and -.35*em <= top-d <= .82*em
                    and abs((d-b)-(bottom-top)) < .55*em
                    and abs(left-margin) <= .7*em and c >= edge-2.3*em)
            # Offset first lines may start a paragraph before the ordinary left margin.
            if not join and len(body) == 1 and .65*em <= a-left <= 2.4*em:
                join = .4*em <= top-b <= 1.9*em and top-d <= .82*em and c >= edge-2.3*em
            if join:
                body.append(line.html)
                if with_boxes:
                    box = min(box[0],left), min(box[1],top), max(box[2],right), max(box[3],bottom)
                previous = line
                margin = left
                continue
        if body:
            yield block('p', _join(*body), box)
        body = [line.html]
        box = line.box
        margin = left
        previous = line
    if body:
        yield block('p', _join(*body), box)


def paragraph_html(lines, width, page_no, *, preserve_order=False, source_boxes=False):
    for tag, html, box in paragraphs(lines,width,preserve_order=preserve_order,with_boxes=True):
        coords = ','.join(f'{n:.3f}' for n in box) if source_boxes else ''
        attr = f' data-source-bbox="{coords}"' if coords else ''
        yield f'<{tag} data-source-page="{page_no}"{attr}>{html}</{tag}>'
