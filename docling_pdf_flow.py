"""Conservative PDF flow repairs using retained layout and source glyphs.

The rules depend on physical layout and table structure, never on a document
name, table identifier or expected wording.
"""
import re

from bs4 import BeautifulSoup


def _key(text):
    return re.sub(r"[\s\u00ad]+", "", text).casefold()


_PROSE_WORD = re.compile(r'[А-Яа-яЁёA-Za-z]+')
_LITERAL_MATH = re.compile(r'<math(?:\s+[^<>]*)?>.*?</math\s*>', re.S)


def _only_prose(value):
    # Inline equations can still be escaped text at this serializer stage.
    # Keep offsets so any source-proved word can be replaced in its own node.
    return _LITERAL_MATH.sub(lambda m: ' ' * len(m[0]), value)


def _visible_source_word(regions, page_no, word):
    """Confirm the candidate with PDFium and the rendered ink in its own box."""
    import numpy as np
    from docling.utils.locks import pypdfium2_lock
    from docling_pdf_native_system import _painted_text_objects, _strict_ink_coverage

    box = [word[k] for k in ('x0', 'top', 'x1', 'bottom')]
    try:
        if not _painted_text_objects(regions, page_no, box):
            return False
        with pypdfium2_lock:
            page = regions.pdf[page_no - 1]
            try:
                height = page.get_height()
                text = page.get_textpage()
                try:
                    source_word = text.get_text_bounded(box[0], height - box[3], box[2], height - box[1])
                finally:
                    text.close()
            finally:
                page.close()
        if ''.join(source_word.split()) != ''.join(word['text'].split()):
            return False
        with regions.crop(page_no, box) as image:
            with image.convert('L') as gray:
                mask = (np.asarray(gray) < 180).astype('uint8') * 255
            return _strict_ink_coverage(mask, word['chars'], box,
                                        image.width / (box[2] - box[0]), image.height / (box[3] - box[1]))
    except (OSError, RuntimeError, ValueError):
        return False


_DESIGNATION = re.compile(r'(?<!\w)[A-Za-zА-Яа-яЁё]+[0-9][A-Za-zА-Яа-яЁё0-9]*(?!\w)')
_PROSE_HOMOGLYPHS = str.maketrans('АВСЕНКМОРТХасеорху', 'ABCEHKMOPTXaceopxy')


def _restore_source_designation(paragraph, words, page, regions, manifest):
    """Restore one letter in a designation in otherwise identical native prose."""
    if len(paragraph.contents) != 1 or paragraph.find(True) or paragraph.string is None:
        return False
    plain = str(paragraph.string)
    if not _DESIGNATION.search(plain) or '<math' in plain:
        return False
    native = [(ch, word) for word in words for ch in word['text'] if not ch.isspace()]
    # The serializer prints a list marker through CSS, outside the text node.
    prefix = ''
    if paragraph.name == 'li':
        marker = re.search(r"(?:^|;)\s*list-style-type:\s*'(\d+(?:\.\d+)*\.?)\s*'", paragraph.get('style',''))
        if marker:
            prefix = marker[1]+' '
    positions = [(i-len(prefix), c) for i, c in enumerate(prefix+plain) if not c.isspace()]
    if len(native) != len(positions):
        return False
    differences = [(i, before, after, word) for (i, before), (after, word) in zip(positions, native)
                   if before != after]
    if len(differences) != 1:
        return False
    i, before, after, word = differences[0]
    if i < 0 or before.translate(_PROSE_HOMOGLYPHS) != after.translate(_PROSE_HOMOGLYPHS):
        return False
    match = next((m for m in _DESIGNATION.finditer(plain) if m.start() <= i < m.end()), None)
    if (match is None or plain[match.start():i]+after+plain[i+1:match.end()] != word['text']
            or not _visible_source_word(regions, page, word)):
        return False
    paragraph.string.replace_with(plain[:i]+after+plain[i+1:])
    manifest.setdefault('source_prose_corrections', []).append(dict(
        page=page, bbox=[word[k] for k in ('x0','top','x1','bottom')],
        before=match[0], after=word['text'], method='painted_native_designation_in_matching_paragraph'))
    return True


def _restore_prose_words(paragraph, glyphs, page_no, box, regions, manifest, *, scan_witness=None):
    """Recover a misread word when its visible source and neighboring words agree."""
    from collections import Counter
    from pdfplumber.utils import extract_words

    if any('doctop' not in g or len(g['text']) != 1 for g in glyphs):
        return False
    spans = [(node, list(_PROSE_WORD.finditer(_only_prose(str(node))))) for node in paragraph.find_all(string=True)
             if not node.find_parent(['math', 'sub', 'sup', 'code', 'pre', 'a', 'svg'])]
    if not any(len(matches) >= 5 for _, matches in spans):
        return False
    words = extract_words(glyphs, return_chars=True, split_at_punctuation=True)
    if not scan_witness and _restore_source_designation(paragraph, words, page_no, regions, manifest):
        return True
    source = [word for word in words if _PROSE_WORD.fullmatch(word['text'])]
    def neighbors(a, b):
        em = max(a['bottom'] - a['top'], b['bottom'] - b['top'])
        dy = b['top'] - a['top']
        if abs(dy) <= .25 * em:
            return -.1 * em <= b['x0'] - a['x1'] <= 2 * em
        return (.5 * em <= dy <= 1.8 * em and box[2] - a['x1'] <= 2 * em
                and b['x0'] - box[0] <= 2 * em)

    balanced = (-2, -1, 1, 2)
    edge = (-1, 1, 2)
    contexts = {}
    output_contexts = Counter()
    for offsets in (balanced, edge):
        for i in range(-min(offsets), len(source) - max(offsets)):
            if not all(neighbors(source[j], source[j + 1]) for j in range(i + min(offsets), i + max(offsets))):
                continue
            if offsets == edge and abs(source[i - 1]['top'] - source[i + 2]['top']) > .25 * source[i]['chars'][0]['size']:
                continue
            key = offsets, tuple(source[i + j]['text'] for j in offsets)
            contexts.setdefault(key, []).append(source[i])
        for _, matches in spans:
            output_contexts.update((offsets, tuple(matches[i + j][0] for j in offsets))
                                   for i in range(-min(offsets), len(matches) - max(offsets)))
    changed = False
    for node, matches in spans:
        value = str(node)
        for i in reversed(range(1, len(matches) - 2)):
            match = matches[i]
            before = match[0]
            if len(before) < 4:
                continue
            # Unrelated scripts can be OCR language substitutions. Match the
            # surrounding prose on this line even if inline math precedes it.
            cross_script = before.isascii() and all(re.fullmatch(r'[А-Яа-яЁё]+', matches[i + j][0])
                                                     for j in edge)
            if scan_witness and not cross_script:
                continue
            offsets = edge if cross_script and not scan_witness else balanced
            if i + min(offsets) < 0:
                continue
            key = offsets, tuple(matches[i + j][0] for j in offsets)
            candidates = contexts.get(key, [])
            if len(candidates) != 1 or output_contexts[key] != 1:
                continue
            word = candidates[0]
            after = word['text']
            if cross_script:
                if not re.fullmatch(r'[А-Яа-яЁё]{4,}', after):
                    continue
            elif len(before) != len(after) or sum(a != b for a, b in zip(before, after)) != 1:
                continue
            method = 'painted_word_two_pdf_readers_and_crop_ink'
            evidence = {}
            if scan_witness:
                evidence = scan_witness(before, word)
                if not evidence:
                    continue
                method = 'source_context_and_matching_independent_ocr_word_image'
            elif not _visible_source_word(regions, page_no, word):
                continue
            value = value[:match.start()] + after + value[match.end():]
            manifest.setdefault('source_prose_corrections', []).append(dict(
                page=page_no, bbox=[word[k] for k in ('x0', 'top', 'x1', 'bottom')],
                before=before, after=after, method=method, **evidence))
        if value != str(node):
            node.replace_with(value)
            changed = True
    return changed


def _word_image(regions, page, box):
    import cv2
    import numpy as np

    with regions.crop(page, box, scale=5.) as image, image.convert('L') as gray:
        mask = cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    x, y, width, height = cv2.boundingRect(mask)
    if width < 8 or height < 4:
        return None
    return (width / height, cv2.resize(mask[y:y+height, x:x+width], (100, 28), interpolation=cv2.INTER_AREA))


def _scan_word_witness(before, word, words, records, page, regions):
    """Require the same printed Cyrillic spelling in an independently read region."""
    import cv2
    import math

    simple = str.maketrans('абвгдезийклмнопрстуфхыэ', 'abvgdezijklmnoprstufhye')
    after = word['text']
    if after.lower().translate(simple) != before.lower():
        return None
    box = [word[k] for k in ('x0', 'top', 'x1', 'bottom')]
    image = _word_image(regions, page, box)
    if image is None:
        return None
    def contains(bounds, center):
        return bounds[0] <= center[0] <= bounds[2] and bounds[1] <= center[1] <= bounds[3]
    centers = lambda b: ((b[0]+b[2])/2, (b[1]+b[3])/2)
    candidates = []
    for other in words:
        matches = [m for m in _PROSE_WORD.finditer(other['text']) if m[0] == after]
        if len(matches) != 1 or len(other['chars']) != len(other['text']):
            continue
        chars = other['chars'][matches[0].start():matches[0].end()]
        candidates.append([min(g['x0'] for g in chars), min(g['top'] for g in chars),
                           max(g['x1'] for g in chars), max(g['bottom'] for g in chars)])
    for record in records:
        bounds = record.get('bbox')
        if (record.get('page_no') != page or record.get('label') not in ('Text', 'Table')
                or not bounds or contains(bounds, centers(box))
                or _PROSE_WORD.findall(record.get('text', '')).count(after) != 1):
            continue
        located = [b for b in candidates if contains(bounds, centers(b))]
        if len(located) != 1:
            continue
        witness = _word_image(regions, page, located[0])
        if witness is None or abs(math.log(image[0] / witness[0])) > .15:
            continue
        if not image[1].std() or not witness[1].std():
            continue
        score = float(cv2.matchTemplate(image[1], witness[1], cv2.TM_CCOEFF_NORMED)[0, 0])
        if score >= .85:
            return dict(witness_bbox=located[0], template_correlation=round(score, 4))
    return None


def restore_owned_prose_words(soup, manifest, records, reader, regions):
    """Check a published table paragraph against its uniquely owned OCR packet."""
    from collections import defaultdict
    from hashlib import sha256
    from pdfplumber.utils import extract_words

    if reader is None or regions is None:
        return
    owners = defaultdict(set)
    for proof in manifest.get('typed_table_primary_ownership', []):
        owners[(proof['page'], tuple(proof['source_bbox']), proof['source_record_sha256'])].add(proof['ref'])
    cached_page, words = None, []
    for record in records or ():
        if (record.get('label') != 'Text' or not record.get('bbox') or not record.get('html')
                or not re.search(r'\b[A-Za-z]{4,}\b', record.get('text', ''))):
            continue
        page, box = record['page_no'], record['bbox']
        refs = owners.get((page, tuple(box), sha256(record['html'].encode()).hexdigest()))
        if not refs or len(refs) != 1:
            continue
        tables = soup.find_all('table', attrs={'data-pdf-table-ref': next(iter(refs))})
        if len(tables) != 1:
            continue
        original = BeautifulSoup(record['html'], 'html.parser').get_text(' ', strip=True)
        paragraphs = [p for p in tables[0].find_all('p') if p.find_parent('table') is tables[0]
                      and p.get_text(' ', strip=True) == original]
        if len(paragraphs) != 1:
            continue
        if page != cached_page:
            reader.load_page(page)
            words = extract_words(reader.chars, return_chars=True, split_at_punctuation=True)
            cached_page = page
        glyphs = [g for g in reader.chars if box[0] <= (g['x0']+g['x1'])/2 <= box[2]
                  and box[1] <= (g['top']+g['bottom'])/2 <= box[3]]
        if glyphs and all(g.get('upright', True) for g in glyphs):
            _restore_prose_words(paragraphs[0], glyphs, page, box, regions, manifest,
                scan_witness=lambda before, word: _scan_word_witness(before, word, words, records, page, regions))


def _restore_inline_source_letter(paragraph, glyphs, page, regions, manifest):
    """Restore an inline mathematical letter fused into otherwise identical prose."""
    import unicodedata
    from statistics import median

    if len(paragraph.contents) != 1 or paragraph.string is None or paragraph.find(True):
        return False
    ink = sorted((g for g in glyphs if g['text'].strip()), key=lambda g: g['x0'])
    if (len(ink) < 8 or any(len(g['text']) != 1 for g in ink)
            or max(g['top'] for g in ink) - min(g['top'] for g in ink) > .25 * median(g['size'] for g in ink)):
        return False
    plain = str(paragraph.string)
    positions = [(i, ch) for i, ch in enumerate(plain) if not ch.isspace()]
    if len(positions) != len(ink):
        return False
    edits = [(i, observed, g) for (i, observed), g in zip(positions, ink) if observed != g['text']]
    if len(edits) != 1:
        return False
    i, before, glyph = edits[0]
    value = unicodedata.normalize('NFKC', glyph['text'])
    if (not before.isalpha() or len(value) != 1 or not value.isalpha()
            or not unicodedata.name(glyph['text'], '').startswith('MATHEMATICAL ')
            or not _visible_source_word(regions, page, dict(glyph, chars=[glyph]))):
        return False
    math = BeautifulSoup(f'<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline" '
                         f'data-latex="{value}"><mi>{value}</mi></math>', 'html.parser').math
    paragraph.string.replace_with(plain[:i], math, plain[i+1:])
    manifest.setdefault('source_prose_corrections', []).append(dict(
        page=page, bbox=[glyph[k] for k in ('x0', 'top', 'x1', 'bottom')],
        before=before, after=value, method='painted_mathematical_letter_in_matching_source_line'))
    return True


def _matches_column_edges(glyphs, box, left, right, tolerance):
    """Disambiguate two fully justified lines with independent rows of this column."""
    from pdfplumber.utils import cluster_objects
    others = [g for g in glyphs if g['text'].strip() and g.get('upright', True)
              and (g['bottom'] < box[1] or g['top'] > box[3])]
    return any(abs(min(g['x0'] for g in row) - left) <= tolerance
               and abs(max(g['x1'] for g in row) - right) <= tolerance
               for row in cluster_objects(others, 'bottom', tolerance))


def _aligned_prose_lines(paragraph, glyphs, line_tolerance, continues):
    """Match ordinary words in native line order, leaving all inline math intact."""
    from difflib import SequenceMatcher
    from pdfplumber.utils import cluster_objects, extract_words

    if any('doctop' not in g for g in glyphs):
        return []
    plain = ' '.join(_only_prose(str(node)) for node in paragraph.find_all(string=True)
                     if not node.find_parent(['math', 'sup', 'sub', 'code']))
    def words(text):
        return [match[0].casefold() for match in _PROSE_WORD.finditer(text) if len(match[0]) >= 3]
    observed = words(plain)
    if len(observed) < 5:
        return []
    physical = cluster_objects(extract_words(glyphs, return_chars=True), 'bottom', line_tolerance)
    lines, anchors, owners = [], [], []
    for line in physical:
        line.sort(key=lambda w: w['x0'])
        terms = [token for word in line for token in words(word['text'])]
        if len(terms) < 2:
            continue
        owners.extend([len(lines)] * len(terms))
        anchors.extend(terms)
        lines.append([g for word in line for g in word['chars'] if g['text'].strip()])
    if len(lines) < 2:
        return []
    if continues:
        # Only this physical page supplies geometry for a joined paragraph.
        observed = observed[:len(anchors)]
    matches = SequenceMatcher(None, anchors, observed, autojunk=False).get_matching_blocks()
    aligned = {i for a, b, length in matches for i in range(a, a + length)}
    if (len(aligned) < .85 * max(len(observed), len(anchors))
            or not {0, 1}.issubset(aligned)
            or any(sum(i in aligned for i, n in enumerate(owners) if n == row) <
                   min(2, owners.count(row)) for row in range(len(lines)))):
        return []
    return lines


def restore_native_paragraph(fragment, reader, region, regions, manifest, *, continues=False):
    """Retain source-supported prose and its measured first-line indent."""
    from statistics import median
    from pdfplumber.utils import cluster_objects

    if reader is None or region is None or not fragment.lstrip().startswith(('<p', '<li')):
        return fragment
    soup = BeautifulSoup(fragment, 'html.parser')
    paragraph = soup.find(['p', 'li'])
    if (paragraph is None or soup.find_all(recursive=False) != [paragraph]
            or paragraph.find(['img', 'table', 'svg', 'ol', 'ul', 'li'])):
        return fragment

    page, box = region
    reader.load_page(page)
    glyphs = [g for g in reader.chars if g['text']
              and box[0] - 1 <= (g['x0'] + g['x1']) / 2 <= box[2] + 1
              and box[1] - 1 <= (g['top'] + g['bottom']) / 2 <= box[3] + 1]
    if not glyphs or any(not g.get('native_script_eligible', True)
                         or not g.get('upright', True) for g in glyphs):
        return fragment
    changed = _restore_prose_words(paragraph, glyphs, page, box, regions, manifest)
    if _restore_inline_source_letter(paragraph, glyphs, page, regions, manifest) or changed:
        fragment = str(soup)
    if paragraph.name == 'li':
        return fragment
    if (paragraph.find('br')
            or re.search(r'(?:^|;)\s*(?:text-indent|text-align)\s*:', paragraph.get('style', ''), re.I)):
        return fragment
    ink = [g for g in glyphs if g['text'].strip()]
    if not ink:
        return fragment
    size = median(g['size'] for g in ink)
    line_tolerance = max(0.5, size * .15)
    lines = cluster_objects(ink, 'bottom', line_tolerance)
    if len(lines) < 2:
        return fragment
    lines = [sorted(line, key=lambda g: g['x0']) for line in lines]
    # Printable hyphens at source line breaks and Latin/Cyrillic homoglyphs
    # may differ in OCR. They do not change the coordinates of the paragraph.
    geometry_letters = str.maketrans('асеорух', 'aceopyx')
    source_lines = [''.join(g['text'] for g in line) for line in lines]
    printed = ''.join(source_lines)
    joined = ''.join(line.removesuffix('-') if after and after[0].isalpha() else line
                     for line, after in zip(source_lines, source_lines[1:] + ['']))
    observed = _key(paragraph.get_text()).translate(geometry_letters)
    if (continues or paragraph.find(['math', 'sub', 'sup'])
            or all(_key(value).translate(geometry_letters) != observed for value in (printed, joined))):
        lines = _aligned_prose_lines(paragraph, glyphs, line_tolerance, continues)
        if len(lines) < 2:
            return fragment

    lefts = [line[0]['x0'] for line in lines]
    rights = [max(g['x1'] for g in line) for line in lines]
    centers = [(l + r) / 2 for l, r in zip(lefts, rights)]
    tolerance = max(1, size * .15)
    indent = lefts[0] - median(lefts[1:])
    if (indent < size * .5 or max(lefts[1:]) - min(lefts[1:]) > tolerance
            or (len(lines) == 2 and max(rights) - min(rights) <= tolerance
                and not _matches_column_edges(reader.chars, box, lefts[1], rights[1], line_tolerance))
            or max(centers) - min(centers) <= tolerance):
        return fragment
    style = paragraph.get('style', '').strip().rstrip(';')
    paragraph['style'] = (style + ';' if style else '') + f'text-indent:{indent:.2f}pt'
    return str(soup)


def _tagged_body_ids(page, glyphs, source_glyphs, value, anchors, api):
    """Verify disputed body text against the tagged PDF, without altering table-owned text."""
    from collections import defaultdict
    from functools import lru_cache
    from pdfminer.pdftypes import PDFException, PDFObjRef
    from pdfminer.psparser import literal_name

    if not glyphs or any(g.get('tag') == 'Artifact' or type(g.get('mcid')) is not int for g in glyphs):
        return None
    ids = {g['mcid'] for g in glyphs}
    groups = defaultdict(list)
    for g in source_glyphs:
        if type(g.get('mcid')) is int:
            groups[g['mcid']].append(g)
    if sum(len(groups[i]) for i in ids) != len(glyphs):
        return None
    literal = ''.join(line['text'] for line in api.native_cell_lines(glyphs))
    if ''.join(literal.split()) != ''.join(value.split()):
        return None
    top = min(g['top'] for g in glyphs)
    bottom = max(g['bottom'] for g in glyphs)
    artifacts = [g for g in source_glyphs if g.get('tag') == 'Artifact']
    if (not any(g['bottom'] <= top for g in artifacts)
            or any(g['top'] < bottom and g['bottom'] > top for g in artifacts)):
        return None
    following = [box for _, box, item in anchors if box[1] >= bottom
                 and str(getattr(item, 'label', '')) in {'text', 'section_header', 'list_item', 'title'}
                 and getattr(item, 'content_layer', None) == 'body']
    if not following:
        return None

    def resolved(obj):
        return obj.resolve() if isinstance(obj, PDFObjRef) else obj

    # Walk just the page's number-tree branch. Invalid or cyclic references
    # cannot turn an unverified margin into a body paragraph.
    try:
        root = resolved(page.pdf.doc.catalog.get('StructTreeRoot'))
        key = page.page_obj.attrs.get('StructParents')
        if not isinstance(root, dict) or type(key) is not int:
            return None
        pending, visited, owners = [root.get('ParentTree')], set(), []
        while pending:
            branch = resolved(pending.pop())
            if not isinstance(branch, dict) or id(branch) in visited:
                return None
            visited.add(id(branch))
            limits = resolved(branch.get('Limits'))
            if limits is not None and not limits[0] <= key <= limits[1]:
                continue
            nums = resolved(branch.get('Nums', []))
            if len(nums) % 2:
                return None
            owners.extend(resolved(v) for k, v in zip(nums[::2], nums[1::2]) if k == key)
            pending.extend(resolved(branch.get('Kids', [])))
        if len(owners) != 1 or not isinstance(owners[0], list):
            return None

        @lru_cache(maxsize=None)
        def roles(mcid):
            if not 0 <= mcid < len(owners[0]):
                return set()
            ref = owners[0][mcid]
            node = resolved(ref)
            if (not isinstance(node, dict) or type(node.get('K')) is not int or node['K'] != mcid
                    or getattr(node.get('Pg'), 'objid', None) != page.page_obj.pageid):
                return set()
            path, seen = set(), set()
            while isinstance(node, dict) and node is not root:
                if id(node) in seen or 'S' not in node:
                    return set()
                seen.add(id(node))
                path.add(literal_name(node['S']))
                if not isinstance(ref, PDFObjRef):
                    return set()
                child_id = ref.objid
                ref = node.get('P')
                node = resolved(ref)
                children = node.get('K') if isinstance(node, dict) else None
                children = children if isinstance(children, list) else (children,)
                if not any(isinstance(c, PDFObjRef) and c.objid == child_id for c in children):
                    return set()
            return path if node is root and 'Document' in path else set()

        for i in ids:
            path = roles(i)
            if not path or not path.issubset({'Document', 'Part', 'Art', 'Sect', 'Div', 'NonStruct',
                                            'P', 'H', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'Span'}):
                return None
        for i, group in groups.items():
            if max(g['bottom'] for g in group) <= top and roles(i).intersection(
                    {'P', 'H', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'TD', 'TH', 'LI', 'LBody'}):
                break
        else:
            return None
        for i, group in groups.items():
            if min(g['top'] for g in group) < bottom or not roles(i):
                continue
            if any(all(b[0] <= (g['x0'] + g['x1']) / 2 <= b[2] and
                       b[1] <= (g['top'] + g['bottom']) / 2 <= b[3] for g in group) for b in following):
                return sorted(ids)
    except (AttributeError, KeyError, TypeError, ValueError, IndexError, PDFException, RecursionError):
        return None
    return None


def page_furniture_parts(parts, doc, reader, regions, pages, manifest, api, make_part):
    """Place printed margin text between its actual source-page blocks."""
    from collections import defaultdict
    from html import escape
    from pdfplumber.utils import cluster_objects

    anchors = defaultdict(list)
    for index, part in enumerate(parts):
        for item in part.get_unique_doc_items():
            for prov in item.prov:
                box = prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
                anchors[prov.page_no].append((index, [box.l, box.t, box.r, box.b], item))

    removed = defaultdict(list)
    for entry in manifest.get('removed_page_furniture', []):
        removed[entry['page']].append(entry)
    section_refs = {item.self_ref for item in doc.texts if str(item.label) == 'section_header'}
    disputed = {_key(e['text']) for entries in removed.values() for e in entries
                if e.get('reason') == 'ocr_furniture' and e.get('ref') in section_refs}
    unique_headers = {}
    if disputed:
        matches = defaultdict(list)
        for record in manifest.get('ocr_blocks', []):
            text = _key(record.get('text') or '')
            if text in disputed:
                matches[text].append(record)
        unique_headers = {text: records[0] for text, records in matches.items()
                          if len(records) == 1 and records[0].get('label') == 'PageHeader'}
    inserts = defaultdict(list)
    published = []
    for page in sorted(pages):
        source_glyphs = []
        if reader is not None:
            reader.load_page(page)
            source_glyphs = [g for g in reader.chars if g['text'].strip() and g.get('upright', True)
                             and g.get('native_script_eligible', True)]
        candidates = list(removed[page])
        # Layout can miss a printed page number entirely. Read a standalone
        # line in the physical bottom margin, even when its number differs.
        if source_glyphs:
            margin = [g for g in source_glyphs if g['top'] >= .90 * reader.page.height]
            for line in cluster_objects(margin, 'bottom', 2):
                line.sort(key=lambda g: g['x0'])
                value = ''.join(g['text'] for g in line)
                if re.fullmatch(r'[0-9]{1,5}', value):
                    candidates.append(dict(text=value, bbox=[line[0]['x0'], min(g['top'] for g in line),
                        max(g['x1'] for g in line), max(g['bottom'] for g in line)]))
        placed = []
        for entry in sorted(candidates, key=lambda e: (e['bbox'][1], e['bbox'][0])):
            box, value = entry['bbox'], entry['text'].strip()
            if not value or any(api.overlap_ratio(box, old_box) > .75 and _key(value) == _key(old)
                                for old_box, old in placed):
                continue
            if any(api.overlap_ratio(box, other_box) > .75 and (
                    not hasattr(item, 'text') or _key(value) == _key(item.text))
                   for _, other_box, item in anchors[page]):
                continue
            glyphs = [g for g in source_glyphs if box[0] - 1 <= (g['x0'] + g['x1']) / 2 <= box[2] + 1
                      and box[1] - 1 <= (g['top'] + g['bottom']) / 2 <= box[3] + 1]
            if glyphs:
                source_box = [min(g['x0'] for g in glyphs), min(g['top'] for g in glyphs),
                              max(g['x1'] for g in glyphs), max(g['bottom'] for g in glyphs)]
                word = dict(zip(('x0', 'top', 'x1', 'bottom'), source_box), text=value, chars=glyphs)
                if not _visible_source_word(regions, page, word):
                    continue
                box = source_box
            elif not any(record.get('page_no') == page and record.get('label') in {'PageHeader', 'PageFooter'}
                         and _key(record.get('text', '')) == _key(value)
                         and api.overlap_ratio(record['bbox'], box) > .75 for record in manifest.get('ocr_blocks', [])):
                continue
            placed.append((box, value))
            # A joined table/list stays intact. Put its physical margin on
            # the nearest outer boundary, retaining the exact page and box.
            below = [index for index, bounds, _ in anchors[page] if bounds[1] >= box[3]]
            if below:
                slot = min(below)
            elif anchors[page]:
                slot = max(index for index, _, _ in anchors[page]) + 1
            else:
                slot = min((index for later, items in anchors.items() if later > page
                            for index, _, _ in items), default=len(parts))
            coords = ','.join(f'{v:.3f}' for v in box)
            ids = None
            header = unique_headers.get(_key(value))
            if (glyphs and entry.get('reason') == 'ocr_furniture' and entry.get('ref') in section_refs
                    and header and header.get('page_no') == page
                    and api.overlap_ratio(header['bbox'], entry['bbox']) > .75):
                ids = _tagged_body_ids(reader.page, glyphs, source_glyphs, value, anchors[page], api)
            attributes = ' data-page-furniture="true"' if ids is None else ''
            fragment = (f'<p{attributes} data-source-page="{page}" '
                        f'data-source-bbox="{coords}">{escape(value)}</p>')
            inserts[slot].append(make_part(text=fragment))
            if ids is None:
                published.append(dict(page=page, bbox=box, text=value))
            else:
                proof = dict(page=page, ref=entry['ref'], bbox=box, mcids=ids)
                manifest.setdefault('published_tagged_body_text', []).append(proof)
                api.diagnostic_event('TAGGED_BODY_PARAGRAPH_RETAINED', **proof)
    manifest['published_page_furniture'] = published
    result = []
    for index, part in enumerate(parts):
        result.extend(inserts[index])
        result.append(part)
    return result + inserts[len(parts)]


def _source_text(reader, page_no, box, api):
    if reader is None:
        return None
    reader.load_page(page_no)
    glyphs = [g for g in reader.chars if g.get("text", "").strip()
              and box[0] - 1 <= (g["x0"] + g["x1"]) / 2 <= box[2] + 1
              and box[1] - 1 <= (g["top"] + g["bottom"]) / 2 <= box[3] + 1]
    if not glyphs:
        return None
    return " ".join(line["text"] for line in api.native_cell_lines(glyphs))


def recover_caption_titles(doc, reader, excluded, manifest, api):
    """Retain a source-verified title mislabeled as furniture after a caption.

    A unique short line in the content area, directly after a recovered table
    label and before its table, belongs to the caption when its exact native
    glyphs agree. This also handles a label/title printed on the preceding page.
    The physical bottom margin, repeated furniture and prose are excluded.
    """
    from docling_core.types.doc import ContentLayer, DocItemLabel

    recovered = []
    pages_by_key = {}
    for text in doc.texts:
        region = api.item_region(text, doc)
        if region:
            pages_by_key.setdefault(_key(text.text), set()).add(region[0])
    for entry in manifest.get("recovered_table_captions", []):
        if not entry.get("table"):
            continue
        caption = doc.texts[int(entry["ref"].rsplit("/", 1)[1])]
        table = doc.tables[int(entry["table"].rsplit("/", 1)[1])]
        cap_reg, table_reg = api.item_region(caption, doc), api.item_region(table, doc)
        if not cap_reg or not table_reg:
            continue
        page_no, cap_box = cap_reg
        table_page, table_box = table_reg
        height, width = doc.pages[page_no].size.height, doc.pages[page_no].size.width
        if table_page not in {page_no, page_no + 1}:
            continue
        if table_page != page_no and table_box[1] > doc.pages[table_page].size.height * .25:
            continue
        candidates = []
        for text in doc.texts:
            if text.label not in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}:
                continue
            reg = api.item_region(text, doc)
            value = text.text.strip()
            if not reg or reg[0] != page_no or len(value) > 250:
                continue
            box = reg[1]
            if (not 0 <= box[1] - cap_box[3] <= 80
                    or box[3] > height * .92 or box[3] - box[1] > height * .08
                    or box[2] - box[0] < width * .25
                    or (table_page == page_no and box[3] > table_box[1] + 2)
                    or len(pages_by_key.get(_key(value), ())) != 1
                    or sum(c.isalpha() for c in value) < 8
                    or re.search(r"(?:https?://|www\.|@|[.!?;:]\s*$)", value, re.I)
                    or api.is_document_designation(value)):
                continue
            native = _source_text(reader, page_no, box, api)
            if native is not None and _key(native) == _key(value):
                candidates.append((box[1], text, box))
        if len(candidates) != 1:
            continue
        _, title, title_box = candidates[0]
        bundle = [(title, title_box)]
        # A short parenthetical immediately following the title is part of
        # the same caption block; retain its place when moving the title.
        for text in doc.texts:
            reg = api.item_region(text, doc)
            value = text.text.strip()
            if (text.label == DocItemLabel.TEXT and text.self_ref not in excluded
                    and reg and reg[0] == page_no and 0 <= reg[1][1] - title_box[3] <= 18
                    and reg[1][3] <= height * .94 and len(value) <= 250
                    and re.fullmatch(r"\([^\n]+\)", value)):
                native = _source_text(reader, page_no, reg[1], api)
                if native is not None and _key(native) == _key(value):
                    bundle.append((text, reg[1]))
        for text, box in bundle:
            if text.parent:
                parent = text.parent.resolve(doc)
                parent.children = [ref for ref in parent.children if ref.cref != text.self_ref]
            text.parent = table.get_ref()
            text.label, text.content_layer = DocItemLabel.CAPTION, ContentLayer.BODY
            if not any(ref.cref == text.self_ref for ref in table.children):
                table.children.append(text.get_ref())
            if not any(ref.cref == text.self_ref for ref in table.captions):
                table.captions.append(text.get_ref())
            excluded.discard(text.self_ref)
            recovered.append({"ref": text.self_ref, "table": table.self_ref, "page": page_no,
                              "bbox": box, "text": text.text, "method": "caption_sequence_and_native_glyphs"})
        def caption_order(ref):
            reg = api.item_region(ref.resolve(doc), doc)
            return (reg[0], reg[1][1]) if reg else (0, 0)
        table.captions.sort(key=caption_order)
    manifest["recovered_table_titles"] = recovered


def _flow_region(item, doc, excluded, api, visiting=None):
    """A group has source bounds only if all visible descendants share a page."""
    from docling_core.types.doc import ContentLayer

    if item.self_ref in excluded or getattr(item, "content_layer", None) == ContentLayer.FURNITURE:
        return None
    prov = getattr(item, "prov", None)
    if prov:
        if len({p.page_no for p in prov}) != 1:
            return None
        boxes = [p.bbox.to_top_left_origin(doc.pages[p.page_no].size.height) for p in prov]
        return prov[0].page_no, [min(b.l for b in boxes), min(b.t for b in boxes),
                               max(b.r for b in boxes), max(b.b for b in boxes)]
    refs = getattr(item, "children", None)
    if not refs:
        return None
    visiting = set() if visiting is None else visiting
    if item.self_ref in visiting:
        return None
    visiting.add(item.self_ref)
    regions = []
    for ref in refs:
        child = ref.resolve(doc)
        if child.self_ref in excluded or getattr(child, "content_layer", None) == ContentLayer.FURNITURE:
            continue
        reg = _flow_region(child, doc, excluded, api, visiting)
        if reg is None:
            return None
        regions.append(reg)
    visiting.discard(item.self_ref)
    if not regions or len({r[0] for r in regions}) != 1:
        return None
    return regions[0][0], [min(r[1][0] for r in regions), min(r[1][1] for r in regions),
                          max(r[1][2] for r in regions), max(r[1][3] for r in regions)]


def _heading_followers(records):
    from collections import defaultdict

    pages = defaultdict(list)
    for record in records:
        if record.get('label') not in {'PageHeader', 'PageFooter'}:
            pages[record.get('page_no')].append(record)
    followers = {}
    for page, blocks in pages.items():
        # Overlapping bands or a second column make vertical order ambiguous.
        if any(not block.get('bbox') or len(block['bbox']) != 4 for block in blocks):
            continue
        if any(a['bbox'][3] > b['bbox'][1] for a, b in zip(blocks, blocks[1:])):
            continue
        for heading, following in zip(blocks, blocks[1:]):
            if heading.get('label') == 'SectionHeader' and following.get('label') in {'SectionHeader', 'Text'}:
                key = _heading_key(page, heading['bbox'])
                followers[key] = (_heading_key(page, following['bbox']), heading, following)
    return followers


def _heading_key(page, box):
    return page, tuple(round(v, 3) for v in box)


def _restore_heading_order(parent, doc, regions, followers, excluded, manifest):
    from docling_core.types.doc import ContentLayer

    if not followers:
        return
    items = [ref.resolve(doc) for ref in parent.children]
    positions = {item.self_ref: i for i, item in enumerate(items)}
    by_source = {}
    for item in items:
        region = regions[item.self_ref]
        if region and getattr(item, 'text', '') and len(item.prov) == 1:
            by_source.setdefault(_heading_key(*region), []).append(item)
    for key, headings in by_source.items():
        if key not in followers or len(headings) != 1:
            continue
        next_key, primary_heading, primary_next = followers[key]
        following = by_source.get(next_key, [])
        if len(following) != 1:
            continue
        heading, anchor = headings[0], following[0]
        if str(heading.label) != 'section_header' or heading.children:
            continue
        left, right = positions[anchor.self_ref], positions[heading.self_ref]
        if (left >= right or _key(primary_heading.get('text') or '') != _key(heading.text)
                or _key(primary_next.get('text') or '') != _key(anchor.text)):
            continue
        page, box = regions[heading.self_ref]
        crossed = [regions[item.self_ref] for item in items[left:right]
                   if item.self_ref not in excluded and item.content_layer != ContentLayer.FURNITURE]
        if any(not loc or loc[0] != page or loc[1][1] < box[3] for loc in crossed):
            continue
        items.insert(left, items.pop(right))
        parent.children.insert(left, parent.children.pop(right))
        for i in range(left, right + 1):
            positions[items[i].self_ref] = i
        manifest.setdefault('heading_reading_order_repairs', []).append(
            dict(page=page, heading=heading.self_ref, before=anchor.self_ref))


def restore_table_reading_order(doc, excluded, manifest, api):
    """Restore source-witnessed heading order and nonoverlapping wide-table intervals.

    Lists retain their internal structure. Their one-page descendant bounds
    allow a list definition to move before a later table without flattening it
    or globally sorting columns and unrelated paragraphs.
    """
    from docling_core.types.doc import ContentLayer

    followers = _heading_followers(manifest.get('ocr_blocks', []))
    repairs = []
    for parent in [doc.body, *doc.groups]:
        regions = {ref.cref: _flow_region(ref.resolve(doc), doc, excluded, api) for ref in parent.children}
        wide_pages = set()
        for ref, region in regions.items():
            if region and ref.startswith("#/tables/"):
                page, box = region
                if box[2] - box[0] >= .7 * doc.pages[page].size.width:
                    wide_pages.add(page)
        page_positions = {page: [] for page in wide_pages}
        for index, ref in enumerate(parent.children):
            region = regions[ref.cref]
            if region and region[0] in wide_pages:
                page_positions[region[0]].append(index)
        changed = bool(wide_pages)
        while changed:
            changed = False
            children = [ref.resolve(doc) for ref in parent.children]
            for index, item in enumerate(children):
                reg = regions[item.self_ref]
                if not reg:
                    continue
                page_no, box = reg
                for other_index in page_positions.get(page_no, ()):
                    if other_index <= index:
                        continue
                    other_reg = regions[children[other_index].self_ref]
                    if not other_reg or other_reg[0] != page_no or box[1] < other_reg[1][3] - 2:
                        continue
                    window = children[index:other_index + 1]
                    visible = [(node, regions[node.self_ref]) for node in window
                               if node.self_ref not in excluded
                               and getattr(node, "content_layer", None) != ContentLayer.FURNITURE]
                    if (any(not loc or loc[0] != page_no for _, loc in visible)
                            or not any(node.self_ref.startswith("#/tables/")
                                       and loc[1][2] - loc[1][0] >= .7 * doc.pages[page_no].size.width
                                       for node, loc in visible)):
                        continue
                    ordered = sorted(visible, key=lambda pair: pair[1][1][1])
                    if any(a[1][1][3] > b[1][1][1] + 2 for a, b in zip(ordered, ordered[1:])):
                        continue
                    if [node.self_ref for node, _ in visible] == [node.self_ref for node, _ in ordered]:
                        continue
                    refs = iter(node.get_ref() for node, _ in ordered)
                    visible_refs = {node.self_ref for node, _ in visible}
                    for offset, node in enumerate(window):
                        if node.self_ref in visible_refs:
                            parent.children[index + offset] = next(refs)
                    repairs.append({"page": page_no, "parent": parent.self_ref,
                                    "before": [node.self_ref for node, _ in visible],
                                    "after": [node.self_ref for node, _ in ordered]})
                    changed = True
                    break
                if changed:
                    break
        _restore_heading_order(parent, doc, regions, followers, excluded, manifest)
    manifest["table_reading_order_repairs"] = repairs


def restore_list_example_order(doc, excluded, manifest, reader, api):
    """Place a detached example before a numbered clause that the list absorbed."""
    from docling_core.types.doc import ListGroup, ListItem
    from docling_pdf_annotations import annotation_kind

    if reader is None:
        return
    for parent in [doc.body, *doc.groups]:
        siblings = [ref.resolve(doc) for ref in parent.children if ref.cref not in excluded]
        for group, example in zip(siblings, siblings[1:]):
            if (not isinstance(group, ListGroup) or len(group.children) < 2
                    or annotation_kind(getattr(example, 'text', '')) != 'example'
                    or example.children or len(example.prov) != 1):
                continue
            items = [ref.resolve(doc) for ref in group.children]
            if any(not isinstance(x, ListItem) or x.children or len(x.prov) != 1
                   or x.self_ref in excluded for x in items):
                continue
            tail = items[-1]
            if not re.fullmatch(r'\d+(?:\.\d+)+', tail.marker or ''):
                continue
            page, line = api.item_region(example, doc)
            if any(x.prov[0].page_no != page for x in items):
                continue
            boxes = [api.item_region(x, doc)[1] for x in items]
            above, below = boxes[-2:]
            height = line[3]-line[1]
            tolerance = height * .2
            if (height <= 0 or not -tolerance <= line[1]-above[3] <= height
                    or not -tolerance <= below[1]-line[3] <= height
                    or any(b[3] > line[1]+tolerance for b in boxes[:-1])
                    or any(b[2]-b[0] < .6*doc.pages[page].size.width
                           or not b[0] <= line[0] < line[2] <= b[2] for b in (above, below))):
                continue
            # A second column or another object beside the example invalidates
            # this local reading order, even if the preceding paragraph is wide.
            own = {example.self_ref, *(x.self_ref for x in items)}
            if any(x.self_ref not in own and x.self_ref not in excluded
                   and (loc := api.item_region(x, doc)) and loc[0] == page
                   and loc[1][1] < line[3]-tolerance and loc[1][3] > line[1]+tolerance
                   for x in [*doc.texts, *doc.tables, *doc.pictures]):
                continue
            example_source = _source_text(reader, page, line, api) or ''
            tail_source = _source_text(reader, page, below, api) or ''
            prefix = ' '.join((tail.marker, *tail.text.split()[:3]))
            if _key(example_source) != _key(example.text) or not _key(tail_source).startswith(_key(prefix)):
                continue
            new_group = doc.add_list_group(parent=parent, name=group.name, content_layer=group.content_layer)
            new_group.children = [group.children.pop()]
            tail.parent = new_group.get_ref()
            parent.children.remove(new_group.get_ref())
            parent.children.insert(parent.children.index(example.get_ref())+1, new_group.get_ref())
            manifest.setdefault('list_example_order_repairs', []).append(dict(
                page=page, example=example.self_ref, following_clause=tail.self_ref, marker=tail.marker))


def _integer_keys(profile):
    return [int(match[1]) for c in profile.get("cells", [])
            if c["column"] == 0 and c["column_span"] == 1
            and (match := re.match(r"^(\d{1,6})(?:[.)]?\s|$)", c["text"].strip()))]


def join_same_page_sections(doc, routes, excluded, note_routes, manifest, api):
    """Rejoin ruled sections separated by a source-verified spanning heading.

    Require a retained OCR table enclosing both physical grids, an intervening
    source-glyph heading recovered from its full-width OCR row, identical
    physical columns and consecutive printed row identifiers. Independent
    neighboring tables cannot satisfy these conditions merely by width.
    """
    from docling_core.types.doc import TableCell, TableData
    from docling_pdf_table_layout import _table_rows

    entries = {e["ref"]: e for e in manifest.get("tables", [])}
    joins = manifest.setdefault("joined_tables", [])
    owner = {e["from"]: e["into"] for e in joins}
    def root(ref):
        seen = set()
        while ref in owner and ref not in seen:
            seen.add(ref); ref = owner[ref]
        return ref
    merged = []
    for heading in manifest.get("recovered_ocr_table_blocks", []):
        above, below = heading.get("after_table"), heading.get("before_table")
        if not above or not below or above not in entries or below not in entries:
            continue
        a, b = entries[above], entries[below]
        ga, gb = a.get("join_geometry"), b.get("join_geometry")
        if a["page"] != b["page"] or a["page"] != heading["page"] or not ga or not gb:
            continue
        source = heading.get("source_record_bbox")
        if (not source or not ga["bbox"][3] <= heading["bbox"][1] <= heading["bbox"][3] <= gb["bbox"][1]
                or any(box[0] < source[0] - 4 or box[1] < source[1] - 4
                       or box[2] > source[2] + 4 or box[3] > source[3] + 4 for box in (ga["bbox"], gb["bbox"]))
                or len(ga["x_grid"]) != len(gb["x_grid"])
                or any(abs(x - y) > 2 for x, y in zip(ga["x_grid"], gb["x_grid"]))):
            continue
        target, current = root(above), root(below)
        if target == current or current != below:
            continue
        left = BeautifulSoup(routes.get(target, ""), "html.parser")
        right = BeautifulSoup(routes.get(current, ""), "html.parser")
        if not left.table or not right.table:
            continue
        lp, rp = api.table_profile(str(left.table)), api.table_profile(str(right.table))
        keys_left, keys_right = _integer_keys(lp), _integer_keys(rp)
        if (lp["issues"] or rp["issues"] or lp["columns"] != rp["columns"]
                or not keys_left or not keys_right or keys_right[0] != keys_left[-1] + 1):
            continue
        fragment = note_routes.get(heading["ref"]) or heading.get("html", "")
        content = BeautifulSoup(fragment, "html.parser")
        if content.find(["table", "img", "math"]) or _key(content.get_text()) != _key(heading["text"]):
            continue
        body = left.table.find_all("tbody", recursive=False)
        body = body[-1] if body else left.table
        row = left.new_tag("tr")
        cell = left.new_tag("th", colspan=str(lp["columns"]), scope="colgroup")
        for node in list(content.contents):
            cell.append(node.extract())
        row.append(cell); body.append(row)
        for row in _table_rows(right.table):
            for cell in row.find_all("th", recursive=False):
                cell.name = "td"
            body.append(row.extract())
        api.normalize_joined_table_body(left.table)
        combined = str(left)
        profile = api.table_profile(str(left.table), lp["columns"])
        if profile["issues"]:
            continue
        routes[target], routes[current] = combined, ""
        target_item = doc.tables[int(target.rsplit("/", 1)[1])]
        current_item = doc.tables[int(current.rsplit("/", 1)[1])]
        target_item.data = TableData(num_rows=profile["rows"], num_cols=profile["columns"], table_cells=[
            TableCell(text=c["text"], row_span=c["row_span"], col_span=c["column_span"],
                      start_row_offset_idx=c["row"], end_row_offset_idx=c["row"] + c["row_span"],
                      start_col_offset_idx=c["column"], end_col_offset_idx=c["column"] + c["column_span"],
                      column_header=c["header"]) for c in profile["cells"]])
        current_item.data = TableData(num_rows=0, num_cols=0, table_cells=[])
        target_item.prov.extend(prov.model_copy(deep=True) for prov in current_item.prov)
        entries[target]["selected"], entries[target]["html"] = profile, combined
        entries[target]["last_join_geometry"] = gb
        entries[current]["joined_into"] = target
        excluded.add(heading["ref"])
        owner[current] = target
        event = {"into": target, "from": current, "page": b["page"],
                 "basis": "same_page_grids_shared_ocr_spanning_heading_and_consecutive_row_keys",
                 "heading": heading["ref"], "rows": profile["rows"], "columns": profile["columns"]}
        joins.append(event); merged.append(event)
    manifest["same_page_table_sections"] = merged
