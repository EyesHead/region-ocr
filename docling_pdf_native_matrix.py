"""Reconstruct centered matrices whose delimiters and entries are native PDF glyphs."""
from collections import Counter
from functools import lru_cache
from html import escape
import re
from statistics import median
import unicodedata


_SCALAR = re.compile(r'[−+\-]?\d+(?:[,.]\d+)?(?:[/⁄]\d+)?')
_TEX = {'‖': r'\Vert ', '⇒': r'\Rightarrow ', '→': r'\to ', '⋅': r'\cdot ',
        '×': r'\times ', '−': '-', '⁄': '{/}'}
_OPERATORS = frozenset('‖⇒→⋅×−+-,.=/⁄()[]')


def _centre(glyphs):
    return (glyphs[0]['x0'] + glyphs[-1]['x1']) / 2


def _rows(glyphs, size):
    # Dominant-size glyphs define rows. Lower indices must never create a row.
    body = [g for g in glyphs if .90 * size <= g['size'] <= 1.10 * size]
    small = [g for g in glyphs if not .90 * size <= g['size'] <= 1.10 * size]
    rows = []
    for g in sorted(body, key=lambda c: c['top']):
        if not rows or g['top'] - rows[-1][0]['top'] > .4 * size:
            rows.append([])
        rows[-1].append(g)
    baselines = [median(c['bottom'] for c in row) for row in rows]
    if any(b-a < .72 * size for a, b in zip(baselines, baselines[1:])):
        return None
    for g in small:
        if not .40 * size < g['size'] < .88 * size:
            return None
        # Only a true subscript on a neighboring identifier is supported.
        owners = [i for i, bottom in enumerate(baselines)
                  if .06 * size < g['bottom'] - bottom < .52 * size]
        if len(owners) != 1:
            return None
        rows[owners[0]].append(g)
    return [sorted(row, key=lambda g: g['x0']) for row in rows]


def _expression(glyphs, size):
    """Emit the literal inline expression; a diagonal fraction remains diagonal."""
    tokens = []
    for g in glyphs:
        s = g['text']
        if len(s) != 1 or (not s.isdecimal() and not s.isalpha() and s not in _OPERATORS):
            return None
        is_number = s.isdecimal()
        script = g['size'] < .88 * size
        # A decimal comma and its digits belong to the same MathML number.
        if tokens and not script and tokens[-1][0] == 'mn' and (
                is_number or s == ',' and ',' not in tokens[-1][1]):
            kind, value, first, _ = tokens[-1]
            tokens[-1] = kind, value + s, first, g
        else:
            kind = 'mi' if s.isalpha() else 'mn' if is_number else 'mo'
            tokens.append((kind, s, g, g))
    parts = []
    for kind, value, first, last in tokens:
        tex = ''.join(_TEX.get(s, unicodedata.normalize('NFKC', s)) for s in value)
        attr = ' stretchy="false"' if value in {'‖','(',')','[',']'} else ''
        if value in {'⁄','/'}:
            attr += ' lspace="0" rspace="0"'
        ml = f'<{kind}{attr}>{escape(value)}</{kind}>'
        if first['size'] < .88 * size:
            if not parts or parts[-1][2] != 'mi' or not -.12 * size <= first['x0'] - parts[-1][3] <= .45 * size:
                return None
            base_ml, base_tex, _, _ = parts.pop()
            parts.append((f'<msub>{base_ml}{ml}</msub>', f'{base_tex}_{{{tex}}}', 'msub', last['x1']))
        else:
            parts.append((ml, tex, kind, last['x1']))
    return ''.join(p[0] for p in parts), ''.join(p[1] for p in parts)


def _scalar_cells(rows, size):
    """Infer centered scalar columns, including adjacent cells with no whitespace."""
    seeds = set()
    for row in rows:
        runs = []
        for g in row:
            if not runs or g['x0'] - runs[-1][-1]['x1'] > .28 * size:
                runs.append([])
            runs[-1].append(g)
        if len(runs) > 1 and all(_SCALAR.fullmatch(''.join(g['text'] for g in run)) for run in runs):
            seeds.add(tuple(round(_centre(run), 3) for run in runs))
    if not seeds:
        # The printed row is a single, centered expression or scalar.
        centres = [_centre(row) for row in rows]
        if max(centres) - min(centres) > .23 * size:
            return None
        return [[row] for row in rows]

    def partition(row, axes):
        @lru_cache(maxsize=None)
        def scan(start, column):
            if column == len(axes):
                return [()] if start == len(row) else []
            choices = []
            for end in range(start + 1, len(row) - (len(axes) - column - 1) + 1):
                centre = (row[start]['x0'] + row[end - 1]['x1']) / 2
                if centre > axes[column] + .12 * size:
                    break
                if (abs(centre - axes[column]) <= .12 * size
                        and _SCALAR.fullmatch(''.join(g['text'] for g in row[start:end]))):
                    choices.extend((end, *tail) for tail in scan(end, column + 1))
                    if len(choices) > 1:
                        break
            return choices
        splits = scan(0, 0)
        return splits[0] if len(splits) == 1 else None

    solutions = set()
    for axes in seeds:
        divisions = tuple(partition(row, axes) for row in rows)
        if all(divisions):
            solutions.add(divisions)
    if len(solutions) != 1:
        return None
    return [[row[a:b] for a, b in zip((0, *ends[:-1]), ends)] for row, ends in zip(rows, solutions.pop())]


def _table(rows, size):
    grid = _scalar_cells(rows, size)
    if not grid:
        return None
    # Preserve the printed baseline spacing even in MathML engines that
    # ignore mtable's legacy rowspacing attribute.
    row_y = [median(g['top'] for g in row) for row in rows]
    pitch = median(b-a for a, b in zip(row_y, row_y[1:]))
    ml_rows, tex_rows = [], []
    for row in grid:
        values = [_expression(cell, size) for cell in row]
        if not all(values):
            return None
        # Keep the close-set column spacing of the printed expression.
        ml_rows.append(f'<mtr style="height:{pitch:.2f}pt">' + ''.join('<mtd style="padding:0 .12em"><mrow>' + ml
                            + '</mrow></mtd>' for ml, _ in values) + '</mtr>')
        tex_rows.append(' & '.join(tex for _, tex in values))
    return ('<mtable columnspacing=".24em" rowspacing="0em">' + ''.join(ml_rows) + '</mtable>',
            r'\begin{matrix}' + r' \\ '.join(tex_rows) + r'\end{matrix}', [len(grid), len(grid[0])])


def _band(rows, centre, size):
    middle = rows[centre]
    fences = [i for i, g in enumerate(middle) if g['text'] == '‖']
    if len(fences) < 2 or len(fences) % 2:
        return None
    # An odd-height matrix in this notation uses a pair of ordinary-height
    # double bars on its centre line. Solve each horizontal interval separately.
    intervals = list(zip(fences[::2], fences[1::2]))
    blocks = [[[] for _ in rows] for _ in intervals]
    for k, row in enumerate(rows):
        for g in row:
            if g['text'] == '‖':
                continue
            x = (g['x0'] + g['x1']) / 2
            owners = [i for i, (a, b) in enumerate(intervals)
                      if middle[a]['x1'] <= x <= middle[b]['x0']]
            if len(owners) == 1:
                blocks[owners[0]][k].append(g)
            elif k != centre:
                return None

    markup, latex, shapes = [], [], []
    pos = 0
    for (left, right), content in zip(intervals, blocks):
        if not any(content[i] for i in range(len(rows)) if i != centre):
            continue
        if not all(content):
            return None
        prefix, table = _expression(middle[pos:left + 1], size), _table(content, size)
        if prefix is None or table is None:
            return None
        markup.extend((prefix[0], table[0]))
        latex.extend((prefix[1], table[1]))
        shapes.append(table[2])
        pos = right
    suffix = _expression(middle[pos:], size)
    if not shapes or suffix is None:
        return None
    return ''.join(markup) + suffix[0], ''.join(latex) + suffix[1], shapes


def recover_native_matrix(reader, regions, page_no, bounds, api):
    """Rebuild only rectangular, source-verified matrices and column vectors."""
    from docling_pdf_cpu_route import _ink
    from docling_pdf_native_system import _painted_text_objects, _strict_ink_coverage

    if reader is None:
        return None
    page = reader.load_page(page_no)
    if page is None:
        return None
    box = [max(0., bounds[0]), max(0., bounds[1]),
           min(page.width, bounds[2]), min(page.height, bounds[3])]
    glyphs = [g for g in reader.chars if g['text'].strip()
              and box[0] <= (g['x0'] + g['x1']) / 2 <= box[2]
              and box[1] <= (g['top'] + g['bottom']) / 2 <= box[3]]
    if (sum(g['text'] == '‖' for g in glyphs) < 2 or any(
            not g.get('upright', True) or not g.get('native_script_eligible', True) for g in glyphs)):
        return None
    size = median(g['size'] for g in glyphs)
    if size <= 0 or any(g['x0'] < 0 or g['x1'] > page.width or g['top'] < 0
                         or g['bottom'] > page.height for g in glyphs):
        return None
    rows = _rows(glyphs, size)
    if not rows:
        return None
    anchors = [i for i, row in enumerate(rows) if any(g['text'] == '‖' for g in row)]
    bands = []
    top = 0
    for axis in anchors:
        end = axis + (axis - top) + 1
        if axis <= top or end > len(rows):
            return None
        part = rows[top:end]
        heights = [median(c['top'] for c in row if c['size'] >= .90 * size)
                   for row in (part[0], rows[axis], part[-1])]
        if abs((heights[0] + heights[2]) / 2 - heights[1]) > .43 * size:
            return None
        result = _band(part, axis - top, size)
        if not result:
            return None
        bands.append(result)
        top = end
    if top != len(rows) or not bands:
        return None
    inner = bands[0][0] if len(bands) == 1 else (
        '<mtable columnalign="center" rowspacing=".16em">' + ''.join(
            '<mtr><mtd style="padding:.08em 0"><mrow>' + ml + '</mrow></mtd></mtr>' for ml, _, _ in bands)
        + '</mtable>')
    from bs4 import BeautifulSoup

    # Every visible glyph must be used exactly once; geometrical delimiters and
    # unusual source rows carry information even when the arithmetic is inconsistent.
    if Counter(BeautifulSoup(inner, 'html.parser').get_text()) != Counter(''.join(g['text'] for g in glyphs)):
        return None
    painted = _painted_text_objects(regions, page_no, box)
    if not painted:
        return None
    with regions.crop(page_no, box, scale=2.5) as crop:
        if not _strict_ink_coverage(_ink(crop), glyphs, box,
                                    crop.width / (box[2] - box[0]), crop.height / (box[3] - box[1])):
            return None
    tex = bands[0][1] if len(bands) == 1 else (
        r'\begin{gathered}' + r' \\ '.join(b[1] for b in bands) + r'\end{gathered}')
    html = (f'<math xmlns="http://www.w3.org/1998/Math/MathML" display="block" data-latex="{escape(tex, quote=True)}" '
            f'style="width:fit-content;max-width:100%;margin-inline:auto;font-size:{size:.2f}pt"><mrow>{inner}</mrow></math>')
    return html, dict(method='native_matrix_glyphs_and_aligned_columns', page=page_no,
                      bbox=box, glyphs=len(glyphs), matrices=[shape for _, _, shapes in bands for shape in shapes],
                      painted_text_objects=painted)

