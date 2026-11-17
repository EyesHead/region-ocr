"""Recover editable source fractions misclassified as layout pictures."""
from hashlib import sha256
import math
import re

from bs4 import BeautifulSoup



def fraction_layout(image):
    """Measure separate fraction rules and complete glyph bands in source pixels."""
    import cv2
    import numpy as np

    with image.convert('L') as gray:
        values = np.asarray(gray)
        ink = cv2.threshold(values, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    _, labels, stats, centers = cv2.connectedComponentsWithStats(ink, 8)
    opened = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        np.ones((1, max(12, round(image.width*.18))), np.uint8))
    _, _, rules, _ = cv2.connectedComponentsWithStats(opened, 8)
    bars = {}
    for x, y, w, h, area in rules[1:]:
        if w < 6*h or h > max(6, image.height*.06):
            continue
        ids, counts = np.unique(labels[y:y+h, x:x+w], return_counts=True)
        candidates = [(int(count), int(index)) for index, count in zip(ids, counts) if index]
        if not candidates:
            continue
        index = max(candidates)[1]
        a, b, c, d, count = map(int, stats[index])
        component = labels[b:b+d, a:a+c] == index
        if (c < .8*w or d > max(6, image.height*.06)
                or component.sum(axis=1).max() < .8*c):
            continue
        bars[index] = [a, b, c, d, count]
    if not bars:
        return None
    groups = []
    for index, bar in sorted(bars.items(), key=lambda pair: pair[1][0]):
        left, right = bar[0], bar[0]+bar[2]
        matches = [g for g in groups if min(g['right'], right)-max(g['left'], left)
                   >= .7*min(g['right']-g['left'], right-left)]
        if len(matches) > 1:
            return None
        if matches:
            group = matches[0]
            group['left'], group['right'] = min(group['left'], left), max(group['right'], right)
            group['bars'].append(index)
        else:
            groups.append(dict(left=left, right=right, bars=[index], glyphs=[]))
    leftovers = []
    for index, stat in enumerate(stats[1:], start=1):
        if index in bars or stat[4] < 3:
            continue
        owners = [g for g in groups if g['left'] <= centers[index, 0] < g['right']]
        if len(owners) > 1:
            return None
        if owners:
            owners[0]['glyphs'].append(index)
        else:
            leftovers.append(index)
    for group in groups:
        ordered = sorted(group['bars'], key=lambda index: stats[index, 1])
        leaves = []
        for band in range(len(ordered)+1):
            top = 0 if band == 0 else int(stats[ordered[band-1], 1]+stats[ordered[band-1], 3])
            bottom = image.height if band == len(ordered) else int(stats[ordered[band], 1])
            owned = [index for index in group['glyphs'] if top <= stats[index, 1]
                     and stats[index, 1]+stats[index, 3] <= bottom]
            if not owned:
                return None
            leaves.append([min(int(stats[i, 0]) for i in owned), min(int(stats[i, 1]) for i in owned),
                max(int(stats[i, 0]+stats[i, 2]) for i in owned), max(int(stats[i, 1]+stats[i, 3]) for i in owned)])
        if sum(sum(top <= stats[i, 1] and stats[i, 1]+stats[i, 3] <= bottom
                   for top, bottom in zip([0]+[int(stats[j, 1]+stats[j, 3]) for j in ordered],
                                         [int(stats[j, 1]) for j in ordered]+[image.height]))
               for i in group['glyphs']) != len(group['glyphs']):
            return None
        group['leaves'] = leaves
        group['rules'] = [list(map(int, stats[index][:4])) for index in ordered]
    # Only small punctuation may remain outside the measured fractions.
    typical = float(np.median([stats[i, 3] for g in groups for i in g['glyphs']]))
    punctuation = []
    clusters = []
    for index in sorted(leftovers, key=lambda i: centers[i, 0]):
        x, y, w, h, area = map(int, stats[index])
        if h > .65*typical or w > .5*typical:
            return None
        if clusters and abs(centers[index, 0]-centers[clusters[-1][-1], 0]) <= max(w, int(stats[clusters[-1][-1], 2])):
            clusters[-1].append(index)
        else:
            clusters.append([index])
    for cluster in clusters:
        ordered = sorted(cluster, key=lambda i: stats[i, 1])
        if len(ordered) == 2:
            first, last = stats[ordered[0]], stats[ordered[1]]
            if first[1]+first[3] >= last[1] or last[1]-(first[1]+first[3]) > typical:
                return None
            x, y, w, h, area = map(int, last)
            component = labels[y:y+h, x:x+w] == ordered[1]
            upper_y, upper_x = np.where(component[:max(1, h//2)])
            lower_y, lower_x = np.where(component[max(1, h*2//3):])
            widths = component.sum(axis=1)
            comma_tail = bool(len(upper_x) and len(lower_x) and h >= 1.3*first[3]
                and upper_x.mean()-lower_x.mean() >= max(.75, w*.08)
                and widths[-max(2, h//4):].mean() < .8*max(widths[:max(1, h//2)]))
            compact = all(.5 <= stat[2]/stat[3] <= 2 and stat[4] >= .6*stat[2]*stat[3] for stat in (first, last))
            if comma_tail:
                value = ';'
            elif compact:
                value = ':'
            else:
                return None
        elif len(ordered) == 1:
            _, _, w, h, area = map(int, stats[ordered[0]])
            if .35 <= w/h <= 2.5 and h <= .35*typical and area >= .35*w*h:
                value = '.'
            else:
                return None
        else:
            return None
        punctuation.append(dict(left=min(int(stats[i, 0]) for i in ordered), value=value))
    return dict(groups=groups, punctuation=punctuation, source_ink_pixels=int(np.count_nonzero(ink)))


def _atom_latex(raw, api):
    from surya.inference.parsers import clean_block_html
    from docling_pdf_table_content import _safe_markup

    parsed = BeautifulSoup(clean_block_html(raw), 'html.parser')
    if parsed.find(['img', 'svg', 'table', 'input']):
        return None
    maths = parsed.find_all('math')
    if len(maths) == 1:
        if maths[0].find(True):
            from local_web.structured_export import math_to_latex
            latex = math_to_latex(maths[0])
        else:
            latex = maths[0].get('data-latex') or maths[0].get_text()
        maths[0].decompose()
        if parsed.get_text(strip=True):
            return None
    elif not maths:
        def latex_text(node):
            if not getattr(node, 'name', None):
                return str(node)
            value = ''.join(latex_text(child) for child in node.children)
            return ('^{'+value+'}' if node.name == 'sup' else '_{'+value+'}' if node.name == 'sub' else value)
        value = latex_text(parsed).strip()
        if not value or not re.fullmatch(r'[\w\s⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺^{}]+', value):
            return None
        superscripts = dict(zip('⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺', '0123456789-+'))
        latex = re.sub('[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+', lambda m: '^{'+''.join(superscripts[c] for c in m[0])+'}', value)
    else:
        return None
    rendered = api.latex_math(latex, 'inline')
    if not rendered:
        return None
    content = BeautifulSoup(_safe_markup(rendered, api), 'html.parser')
    if content.find(['mfrac', 'mtable', 'msqrt', 'mroot']):
        return None  # This glyph band has no independent fraction/root rule.
    return latex.strip(), str(content)


def _glyph_groups(image):
    import cv2
    import numpy as np
    with image.convert('L') as gray:
        ink = cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    boxes = [[int(x), int(y), int(x+w), int(y+h)] for x, y, w, h, area in stats[1:] if area >= 3]
    # A dot above its stem is one glyph, not a superscript.
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(boxes):
            for j, b in enumerate(boxes[i+1:], i+1):
                horizontal = min(a[2], b[2])-max(a[0], b[0])
                gap = max(0, max(a[1], b[1])-min(a[3], b[3]))
                if horizontal >= .6*min(a[2]-a[0], b[2]-b[0]) and gap <= .6*max(a[3]-a[1], b[3]-b[1]):
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    boxes.pop(j); changed = True; break
            if changed:
                break
    if not boxes:
        return []
    baseline = max(b[3] for b in boxes)
    typical = float(np.median([b[3]-b[1] for b in boxes]))
    result = []
    for box in boxes:
        raised = box[3] < baseline-max(3, typical*.22) and any(
            other[0] < box[0] and other[3] >= baseline-3
            and box[0]-other[2] <= 1.2*(other[3]-other[1]) for other in boxes)
        result.append((box, raised))
    return result


def _atom_source_supported(latex, image, api):
    rendered = api.latex_math(latex, 'inline')
    if not rendered:
        return False
    parsed = BeautifulSoup(rendered, 'html.parser')
    glyphs = _glyph_groups(image)
    visible = ''.join(parsed.get_text().split())
    return bool(glyphs and len(visible) >= len(glyphs)
                and sum(raised for _, raised in glyphs) == len(parsed.find_all(['msup', 'msubsup'])))


def _primary_atoms(content, api):
    from docling_pdf_table_content import _safe_markup
    from local_web.structured_export import math_to_latex
    parsed = BeautifulSoup(_safe_markup(content, api), 'html.parser')
    result = []
    for fraction in parsed.find_all('mfrac'):
        if fraction.find_parent('mfrac'):
            continue
        children = fraction.find_all(True, recursive=False)
        if len(children) != 2:
            return []
        result.append([math_to_latex(BeautifulSoup('<math>'+str(child)+'</math>', 'html.parser').math)
                       for child in children])
    return result


def _add_templates(image, latex, templates, api):
    from local_web.structured_export import math_to_latex
    parsed = BeautifulSoup(api.latex_math(latex, 'inline'), 'html.parser')
    glyphs = _glyph_groups(image)
    candidates = []
    for token in parsed.find_all(['mi', 'mn', 'mo', 'mtext']):
        value = token.get_text()
        if len(value) != 1:
            continue
        raised = any(ancestor.name in {'msup', 'msubsup'} and
                     ancestor.find_all(True, recursive=False)[-1] in [token, *token.parents]
                     for ancestor in token.parents if getattr(ancestor, 'name', None))
        candidates.append((math_to_latex(BeautifulSoup('<math>'+str(token)+'</math>', 'html.parser').math), raised))
    for raised in (False, True):
        boxes = sorted((box for box, role in glyphs if role == raised), key=lambda b: b[0])
        values = [value for value, role in candidates if role == raised]
        if len(boxes) == len(values):
            for box, value in zip(boxes, values):
                templates.append((image.crop(box), value))


def _template_atom(image, templates):
    import cv2
    import numpy as np
    def normalize(picture):
        values = np.asarray(picture.convert('L'))
        mask = cv2.threshold(values, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
        yy, xx = np.where(mask)
        if not len(xx):
            return None
        mask = mask[yy.min():yy.max()+1, xx.min():xx.max()+1]
        h, w = mask.shape
        scale = 48/max(h, w)
        resized = cv2.resize(mask, (max(1, round(w*scale)), max(1, round(h*scale))), interpolation=cv2.INTER_NEAREST)
        output = np.zeros((64, 64), np.uint8)
        y, x = (64-resized.shape[0])//2, (64-resized.shape[1])//2
        output[y:y+resized.shape[0], x:x+resized.shape[1]] = resized
        return output, w/h
    source = normalize(image)
    if source is None:
        return None
    scores = {}
    for template, value in templates:
        reference = normalize(template)
        if reference is None or abs(math.log(reference[1]/source[1])) > .2:
            continue
        a, b = source[0], reference[0]
        dilated_a, dilated_b = cv2.dilate(a, np.ones((3, 3), np.uint8)), cv2.dilate(b, np.ones((3, 3), np.uint8))
        score = min(np.count_nonzero((a > 0)&(dilated_b > 0))/np.count_nonzero(a),
                    np.count_nonzero((b > 0)&(dilated_a > 0))/np.count_nonzero(b))
        scores[value] = max(scores.get(value, 0), score)
    selected = sorted(((score, value) for value, score in scores.items() if score >= .9), reverse=True)
    if not selected or (len(selected) > 1 and selected[0][0]-selected[1][0] < .02):
        return None
    return selected[0][1]


def _read_atom(image, args, page, picture_ref, api):
    from PIL import ImageOps, Image
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
    from docling_pdf_table_content import _typed_payload_key

    model = getattr(args, 'surya_ocr_model', None)
    if model is None:
        return None
    backend = api.local_surya_backend(model)
    replies = []
    for scale in (2, 3):
        enlarged = image.resize((image.width*scale, image.height*scale), Image.Resampling.NEAREST)
        view = ImageOps.expand(enlarged, border=max(12, round(image.height*.4)), fill='white')
        enlarged.close()
        try:
            outputs = backend.generate([BatchInputItem(image=view, prompt_type=PROMPT_TYPE_BLOCK,
                max_tokens=512, metadata=dict(page=page, element=picture_ref, reading='source_fraction_atom', scale=scale))])
            if len(outputs) != 1 or outputs[0].error or outputs[0].token_count >= 512:
                return None
            replies.append(outputs[0].raw)
        finally:
            view.close()
    values = [_atom_latex(raw, api) for raw in replies]
    if not all(values) or _typed_payload_key(values[0][1]) != _typed_payload_key(values[1][1]):
        return None
    return values[0][0]


def reconstruct_fractions(image, args, page, picture_ref, api, *, primary_html='', templates=None):
    layout = fraction_layout(image)
    if layout is None:
        return None
    fragments = []
    ambiguous = False
    templates = [] if templates is None else templates
    hypotheses = _primary_atoms(primary_html, api) if primary_html else []
    if len(hypotheses) != len(layout['groups']):
        hypotheses = []
    for group_index, group in enumerate(layout['groups']):
        atoms = []
        methods = []
        for leaf_index, bounds in enumerate(group['leaves']):
            crop = image.crop(bounds)
            try:
                choices = hypotheses[group_index] if hypotheses else []
                atom = choices[leaf_index] if leaf_index < len(choices) else None
                method = 'primary_equation_atom'
                if atom and not _atom_source_supported(atom, crop, api):
                    atom = None
                if atom is None:
                    atom = _template_atom(crop, templates)
                    method = 'same_page_source_glyph_template'
                if atom is None:
                    atom = _read_atom(crop, args, page, picture_ref, api)
                    method = 'agreeing_source_atom_ocr_views'
                if atom and not _atom_source_supported(atom, crop, api):
                    atom = None
                if atom:
                    _add_templates(crop, atom, templates, api)
            finally:
                crop.close()
            if atom is None:
                return None
            atoms.append(atom)
            methods.append(method)
        group['atom_latex'] = atoms
        group['atom_methods'] = methods
        if len(group['rules']) == 1:
            latex = r'\frac{'+atoms[0]+'}{'+atoms[1]+'}'
        else:
            # A printed stack of several bars need not define associativity.
            # Preserve its literal rows and rules rather than invent a tree.
            latex = r'\begin{array}{c}'+r'\\\hline '.join(atoms)+r'\end{array}'
            ambiguous = True
        rendered = api.latex_math(latex, 'inline')
        if not rendered:
            return None
        fragments.append((group['left'], rendered))
    fragments += [(p['left'], p['value']) for p in layout['punctuation']]
    return ''.join(value for _, value in sorted(fragments)), dict(
        method='source_fraction_rules_primary_atoms_and_pixel_templates',
        source_image_sha256=sha256(image.convert('RGB').tobytes()).hexdigest(),
        layout=layout, literal_stack=ambiguous)


def recover_picture_math(doc, records, regions, args, picture_routes, excluded, manifest, api, *, protected=()):
    templates = {}
    try:
        return _recover_picture_math(doc, records, regions, args, picture_routes, excluded, manifest, api,
                                     protected=protected, templates=templates)
    finally:
        for values in templates.values():
            for image, _ in values:
                image.close()


def _recover_picture_math(doc, records, regions, args, picture_routes, excluded, manifest, api, *, protected, templates):
    """Replace only source-proved equation/picture duplicates with editable math."""
    from docling_core.types.doc import DocItemLabel
    from docling_pdf_table_content import _typed_payload_digest

    table_refs = {table.self_ref for table in doc.tables}
    protected_refs = set(protected)
    for entry in manifest.get('source_illustration_picture_refs', []):
        value = entry.get('ref') if isinstance(entry, dict) else entry
        if value:
            protected_refs.add(value)
    def priority(picture):
        region = api.item_region(picture, doc)
        if (not region or len(picture.prov) != 1 or picture.self_ref in protected_refs
                or picture.self_ref in excluded or picture.captions
                or getattr(picture.parent, 'cref', None) in table_refs):
            return 999
        if not any(record.get('page_no') == region[0]
                   and str(record.get('label', '')).casefold() in {'equation', 'formula'}
                   and api.overlap_ratio(region[1], record.get('bbox', [0, 0, 0, 0])) > .85 for record in records):
            return 999
        box = region[1]
        image = None
        try:
            image = regions.crop(region[0], [box[0]-1, box[1]-1, box[2]+1, box[3]+1], scale=4.2)
            layout = fraction_layout(image)
            return max(len(group['rules']) for group in layout['groups']) if layout else 999
        except Exception:
            return 999
        finally:
            if image is not None:
                image.close()
    for picture in sorted(doc.pictures, key=priority):
        if (len(picture.prov) != 1 or picture.self_ref in excluded or picture.self_ref in protected_refs or picture.captions
                or getattr(picture.parent, 'cref', None) in table_refs):
            continue
        region = api.item_region(picture, doc)
        if not region:
            continue
        page, box = region
        area = max(1, (box[2]-box[0])*(box[3]-box[1]))
        candidates = []
        for record in records:
            bounds = record.get('bbox')
            if record.get('page_no') != page or str(record.get('label', '')).casefold() not in {'equation', 'formula'} or not bounds:
                continue
            other_area = max(1, (bounds[2]-bounds[0])*(bounds[3]-bounds[1]))
            overlap = max(0, min(box[2], bounds[2])-max(box[0], bounds[0]))*max(0, min(box[3], bounds[3])-max(box[1], bounds[1]))
            if overlap >= .85*area and overlap >= .85*other_area:
                candidates.append(record)
        if len(candidates) != 1:
            continue
        record = candidates[0]
        targets = []
        for item in doc.texts:
            where = api.item_region(item, doc)
            if (item.self_ref in excluded or item.label != DocItemLabel.FORMULA or len(item.prov) != 1
                    or item.children or not where or where[0] != page):
                continue
            a, b = where[1], record['bbox']
            overlap = max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))
            if (overlap >= .85*max(1, (a[2]-a[0])*(a[3]-a[1]))
                    and overlap >= .85*max(1, (b[2]-b[0])*(b[3]-b[1]))):
                targets.append(item)
        if len(targets) != 1:
            continue
        target = targets[0]
        duplicate_children = []
        children_proved = True
        for ref in picture.children:
            if ref.cref == target.self_ref:
                continue
            child = ref.resolve(doc)
            where = api.item_region(child, doc) if getattr(child, 'prov', None) else None
            if (not where or len(child.prov) != 1 or where[0] != page
                    or not hasattr(child, 'orig') or getattr(child, 'children', None)
                    or _typed_payload_digest(child.orig or '') != _typed_payload_digest(record.get('html', ''))
                    or any(abs(a-b) > 1 for a, b in zip(where[1], record['bbox']))):
                children_proved = False
                break
            duplicate_children.append(child.self_ref)
        if not children_proved:
            continue
        ancestor, seen = target, set()
        live = True
        while getattr(ancestor, 'parent', None):
            reference = ancestor.parent.cref
            if reference in seen or (reference in excluded and reference != picture.self_ref):
                live = False
                break
            seen.add(reference)
            ancestor = ancestor.parent.resolve(doc)
        if not live:
            continue
        reparent = getattr(target.parent, 'cref', None) == picture.self_ref
        if reparent:
            if picture.parent is None:
                continue
            parent = picture.parent.resolve(doc)
            index = next((i for i, ref in enumerate(parent.children) if ref.cref == picture.self_ref), None)
            if index is None:
                continue
        bounds = [min(box[0], record['bbox'][0])-1, min(box[1], record['bbox'][1])-1,
                  max(box[2], record['bbox'][2])+1, max(box[3], record['bbox'][3])+1]
        image = None
        try:
            image = regions.crop(page, bounds, scale=4.2)
            result = reconstruct_fractions(image, args, page, picture.self_ref, api,
                                           primary_html=record.get('html', ''), templates=templates.setdefault(page, []))
        except Exception as error:
            api.diagnostic_event('SOURCE_PICTURE_MATH_RECOVERY_UNAVAILABLE', page=page,
                                 ref=picture.self_ref, reason=str(error))
            result = None
        finally:
            if image is not None:
                image.close()
        if result is None:
            manifest.setdefault('primary_cell_reconciliation_reviews', []).append(dict(
                page=page, bbox=bounds, reason='unresolved_source_math_picture',
                message='Дробная запись требует проверки: исходное изображение сохранено рядом с первичным OCR.'))
            continue  # An unresolved source picture remains visible.
        rendered, proof = result
        if reparent:
            picture.children = [ref for ref in picture.children if ref.cref != target.self_ref]
            target.parent = parent.get_ref()
            parent.children.insert(index, target.get_ref())
        target.text = target.orig = rendered
        for provenance in target.prov:
            provenance.charspan = (0, len(rendered))
        picture_routes[picture.self_ref] = ''
        excluded.add(picture.self_ref)
        excluded.update(duplicate_children)
        record = dict(page=page, picture_ref=picture.self_ref, ref=target.self_ref, bbox=bounds,
                      duplicate_children=duplicate_children, **proof)
        manifest.setdefault('source_picture_math_recoveries', []).append(record)
        if proof['literal_stack']:
            manifest.setdefault('primary_cell_reconciliation_reviews', []).append(dict(
                page=page, bbox=bounds, reason='literal_source_fraction_stack',
                message='Многоэтажная дробная запись сохранена буквально. Алфавит обозначений взят '
                        'из первичного OCR; возможна путаница кириллицы и латиницы.'))
        api.diagnostic_event('SOURCE_PICTURE_MATH_RECOVERED', page=page, picture=picture.self_ref,
                             ref=target.self_ref, literal_stack=proof['literal_stack'])
