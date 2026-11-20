"""Read source scan rails with their full stroke width before cell recovery."""


def join_broken_scan_rules(edges):
    """Join near-complete horizontal rails between measured vertical sides.

    Scans can erase a small interior section of a separator. Requiring a
    continuous line then invents a rowspan. Both surviving ends must meet
    vertical sides that continue above and below the separator, and measured
    fragments must cover at least 94 percent of that column. Missing rules,
    isolated underlines and large gaps therefore remain unjoined.
    """
    from statistics import median

    groups = []
    for edge in sorted((e for e in edges if e['orientation'] == 'h'), key=lambda e: e['top']):
        if groups and edge['top'] - groups[-1][0]['top'] <= 2:
            groups[-1].append(edge)
        else:
            groups.append([edge])
    result, proofs = list(edges), []
    for group in groups:
        y = median(e['top'] for e in group)
        sides = sorted({e['x0'] for e in edges if e['orientation'] == 'v'
                        and e['top'] <= y - 3 and e['bottom'] >= y + 3})
        for left, right in zip(sides, sides[1:]):
            if right - left <= 6:
                continue
            intervals = sorted((max(left, e['x0']), min(right, e['x1'])) for e in group
                               if e['x1'] > left and e['x0'] < right)
            if not intervals or intervals[0][0] > left + 3 or intervals[-1][1] < right - 3:
                continue
            merged = []
            for start, end in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            gaps = [b[0] - a[1] for a, b in zip(merged, merged[1:])]
            coverage = sum(end - start for start, end in merged) / (right - left)
            if not gaps or max(gaps) <= 3 or coverage < .94:
                continue
            start, end = merged[0][0], merged[-1][1]
            result.append(dict(x0=start, x1=end, top=y, bottom=y, width=end-start,
                               height=0, orientation='h'))
            proofs.append(dict(y=y, column_bbox=[left, right], coverage=coverage,
                               source_fragments=merged, maximum_gap=max(gaps)))
    return result, proofs


def read_source_scan_grid(reader, regions, page, bounds, reference_fragments=(), api=None):
    """Return a source grid and its exact rendered ROI, owned by the caller.

    Scanner boxes run through rule centres. Cropping on those centres can cut
    away the outer vertical border and make a complete table appear open.
    A small source margin preserves those measured strokes; no rules are
    invented. The precise short-rule pass must prove complete source cells
    before it supersedes the ordinary drawing-preserving route.

    The return value has ``native``, ``image`` and ``region`` keys. Close its
    image after use; pass this same image/region pair to regional text OCR.
    """
    native_page = reader.load_page(page)
    if native_page is None or not bounds or len(bounds) != 4:
        return None
    margin = 2.0
    roi = [max(0., bounds[0] - margin), max(0., bounds[1] - margin),
           min(float(native_page.width), bounds[2] + margin),
           min(float(native_page.height), bounds[3] + margin)]
    if roi[2] <= roi[0] or roi[3] <= roi[1]:
        return None
    image = regions.crop(page, roi)
    try:
        detailed = reader.read_raster_table(page, roi, image, reference_fragments,
                                            _short_rules=True)
        native = None
        if detailed and detailed[1].get('source_grid_verified'):
            profile = api.table_profile(detailed[0]) if api is not None else None
            if profile is None or not profile.get('issues'):
                native = detailed
        if native is None:
            native = reader.read_raster_table(page, roi, image, reference_fragments)
        if native is None:
            image.close()
            return None
        native[1]['source_render_bbox'] = list(roi)
        native[1]['source_grid_render_padding'] = margin
        if api is not None:
            api.diagnostic_event('SOURCE_SCAN_GRID_READ', page=page,
                requested_bbox=list(bounds), render_bbox=roi,
                measured_bbox=native[1].get('bbox'),
                verified_short_rules=bool(native[1].get('source_grid_verified')),
                rows=len(native[1].get('y_grid', [])) - 1,
                columns=len(native[1].get('x_grid', [])) - 1)
        return dict(native=native, image=image, region=roi)
    except BaseException:
        image.close()
        raise


def _scan_literal_candidates(table, profile, geometry, page, region):
    """Locate a unique cell token and nearby literal in the scan's positioned OCR layer."""
    import re
    from collections import Counter
    from difflib import SequenceMatcher

    pattern = re.compile(r'[А-Яа-яЁёA-Za-z]+|[0-9]+')
    def contains(box, other):
        return box[0] <= other[0] and box[1] <= other[1] and other[2] <= box[2] and other[3] <= box[3]
    words = []
    for word in page.extract_words(return_chars=True):
        chars = word['chars']
        if len(chars) != len(word['text']):
            continue
        for match in pattern.finditer(word['text']):
            part = chars[match.start():match.end()]
            box = [min(c['x0'] for c in part), min(c['top'] for c in part),
                   max(c['x1'] for c in part), max(c['bottom'] for c in part)]
            if contains(region, box):
                words.append((match[0], box))
    nodes = [node for node in table.find_all(['td','th']) if node.find_parent('table') is table]
    if len(nodes) != len(profile['cells']):
        return
    geometry = geometry or {}
    physical = {(cell['row'],cell['column']):cell for cell in geometry.get('cells', [])}
    for node, position in zip(nodes, profile['cells']):
        if node.find(['math','table','sup','sub','details']) or node.select_one('[data-ocr-review]'):
            continue
        cell = physical.get((position['row'], position['column'])) or {}
        source = [(text, box) for text, box in words if contains(cell.get('bbox', region), box)]
        tokens = []
        for string in node.find_all(string=True):
            tokens.extend((match[0], string) for match in pattern.finditer(str(string)))
        originals = [value for value, _ in source]
        published = [value for value, _ in tokens]
        counts = Counter(published)
        for i, (value, string) in enumerate(tokens):
            if (len(value)<5 or counts[value]!=1 or value in originals
                    or not (value.isdecimal() or re.fullmatch(r'[А-Яа-яЁё]+',value))):
                continue
            matches = []
            for j, (alternative, box) in enumerate(source):
                if (len(alternative)<5 or abs(len(value)-len(alternative))>2
                        or alternative in published or value.isdecimal()!=alternative.isdecimal()
                        or (not value.isdecimal() and not re.fullmatch(r'[А-Яа-яЁё]+', alternative))):
                    continue
                diffs = [op for op in SequenceMatcher(None,value,alternative,autojunk=False).get_opcodes()
                         if op[0] != 'equal']
                if len(diffs) != 1:
                    continue
                kind, start, end, left, right = diffs[0]
                if max(end-start, right-left) > 2:
                    continue
                if kind == 'delete' and (start == 0 or end == len(value)):
                    continue  # A word continued across a printed line break can match one part.
                # At least one unchanged neighbor associates both readings.
                # Without a measured grid require two consecutive context tokens.
                context = max((n for n in (0,1,2) if n == 0
                    or (i>=n and j>=n and published[i-n:i]==originals[j-n:j])
                    or (i+n<len(published) and j+n<len(originals) and published[i+1:i+n+1]==originals[j+1:j+n+1])))
                if context >= (1 if cell else 2):
                    matches.append((alternative, box))
            if len(matches) == 1:
                yield string, value, matches[0][0], matches[0][1], position


def _confirmed_scan_glyph(table_profile, geometry, image, region, page, box, primary, native):
    """Check a single disputed letter against printed examples owned by this table."""
    import re
    import cv2
    import numpy as np

    if not geometry or not geometry.get('source_grid_verified') or len(native) != len(primary):
        return None
    differences = [i for i,(p,n) in enumerate(zip(primary,native)) if p != n]
    if len(differences) != 1 or not native.isalpha():
        return None
    index = differences[0]
    words = page.extract_words(return_chars=True)
    owners = [w for w in words if w['text'] == native and all(abs(w[k]-v)<.1
              for k,v in zip(('x0','top','x1','bottom'),box))]
    if len(owners) != 1 or len(owners[0]['chars']) != len(native):
        return None
    target = owners[0]['chars'][index]
    cells = {(c['row'],c['column']):set(re.findall(r'[^\W\d_]+',c['text'])) for c in table_profile['cells']}
    gray = np.asarray(image.convert('L'))
    sx,sy = image.width/(region[2]-region[0]), image.height/(region[3]-region[1])
    def pixels(c):
        return (round((c['x0']-region[0])*sx),round((c['top']-region[1])*sy),
                round((c['x1']-region[0])*sx),round((c['bottom']-region[1])*sy))
    x,y,x1,y1 = pixels(target)
    glyph = gray[y:y1,x:x1]
    ink = np.nonzero(glyph < 160)[0]
    if glyph.shape[1] < 8 or len(ink) < 10:
        return None
    top,stop = int(ink.min()), int(ink.max())+1
    patch = glyph[top:stop]
    if patch.shape[0] < 8:
        return None
    shift = max(1,round(.8*min(sx,sy)))
    samples = []
    for word in words:
        literal = word['text'].strip('.,:;()—-')
        owner = next((c for c in geometry['cells'] if c['bbox'][0]<=word['x0'] and word['x1']<=c['bbox'][2]
                    and c['bbox'][1]<=word['top'] and word['bottom']<=c['bbox'][3]),None)
        if not owner or literal not in cells.get((owner['row'],owner['column']),set()):
            continue
        for char in word['chars']:
            if (not char['text'].isalpha() or char.get('fontname')!=target.get('fontname')
                    or abs(char['size']-target['size'])>.15):
                continue
            left,base,_,_ = pixels(char)
            x0,y0 = left-shift,base+top-shift
            width,height = patch.shape[1]+2*shift,patch.shape[0]+2*shift
            if x0<0 or y0<0 or x0+width>gray.shape[1] or y0+height>gray.shape[0]:
                continue
            sample = gray[y0:y0+height,x0:x0+width]
            def similarity(needle,haystack):
                return float(cv2.minMaxLoc(cv2.matchTemplate(haystack,needle,cv2.TM_CCOEFF_NORMED))[1])
            samples.append(dict(letter=char['text'],word=literal,bbox=[char[k] for k in ('x0','top','x1','bottom')],
                grayscale=similarity(patch,sample),
                binary=similarity((patch<160).astype('uint8'),(sample<160).astype('uint8'))))
    positive = sorted((s for s in samples if s['letter']==native[index]),key=lambda s:s['grayscale'],reverse=True)
    negative = [s for s in samples if s['letter']!=native[index]]
    if not positive or not any(s['letter']==primary[index] for s in negative):
        return None
    best=positive[0]
    strongest_other=max(s['grayscale'] for s in negative)
    other_ink=max(s['binary'] for s in negative)
    second = next((s for s in positive if s['word'] != best['word']),None)
    if (best['grayscale'] < .94 or best['grayscale']-strongest_other < .14 or best['binary']-other_ink < .18
            or not second or second['grayscale'] < max(.85,strongest_other+.05)):
        return None
    return dict(index=index,source_bbox=box,source_letter=native[index],primary_letter=primary[index],
                reference=best,second_reference=second,
                alternative=max(negative,key=lambda s:s['grayscale']))


def _read_scan_word_context(image, region, box, page, args, api, page_no):
    """Read the physical word line with its closest printed neighbors."""
    import re
    from PIL import Image, ImageOps
    from bs4 import BeautifulSoup
    from surya.inference.schema import BatchInputItem
    from docling_pdf_table_content import _source_crop

    x0,y0,x1,y1 = box
    height = y1-y0
    neighbors = []
    for word in page.extract_words():
        if (abs(word['top']-y0) > .35*height or abs(word['bottom']-y1) > .35*height
                or word['x0'] < region[0] or word['x1'] > region[2]):
            continue
        if 0 <= x0-word['x1'] <= 1.5*height or 0 <= word['x0']-x1 <= 1.5*height:
            neighbors.append(word)
    if not neighbors:
        return None
    # Adjacent numbers/letters on the same baseline must be recognized too.
    words = sorted([dict(x0=x0, x1=x1, top=y0, bottom=y1, text=None), *neighbors],
                   key=lambda word:word['x0'])
    expected = []
    for word in words:
        if word['text'] is None:
            expected.append(None)
        else:
            expected.extend(re.findall(r'[А-Яа-яЁёA-Za-z]+|[0-9]+', word['text']))
    if len(expected) < 2:
        return None
    bounds = [max(region[0],min(w['x0'] for w in words)-1), max(region[1],min(w['top'] for w in words)-2),
              min(region[2],max(w['x1'] for w in words)+1), min(region[3],max(w['bottom'] for w in words)+2)]
    readings = []
    with _source_crop(image,region,bounds) as crop:
        for h in (48,72):
            w = round(crop.width*h/crop.height)
            if not 1 <= w <= 1800:
                return None
            with crop.resize((w,h), Image.Resampling.LANCZOS) as scaled, ImageOps.expand(scaled,border=16,fill='white') as view:
                limit = max(96,4*sum(len(s or '') for s in expected))
                output = api.local_surya_backend(args.surya_ocr_model).generate([
                    BatchInputItem(image=view,prompt_type='block',max_tokens=limit,
                        metadata={'purpose':'positioned_scan_context_counterread','page':page_no})])
            if len(output) != 1 or output[0].error or not output[0].raw or output[0].token_count >= limit:
                return None
            raw = output[0].raw
            parsed = BeautifulSoup(raw,'html.parser')
            if parsed.find(['img','svg','math','table']) or re.search(r'<\|[^>]+\|>',raw):
                return None
            readings.append(re.findall(r'[А-Яа-яЁёA-Za-z]+|[0-9]+',parsed.get_text(' ',strip=True)))
    return bounds, expected, readings


def verify_scan_cell_literals(table, profile, geometry, image, region, page_no, reader, args, manifest, api, ref):
    """Correct a disputed table token only when the source pixels confirm the other reading."""
    from base64 import b64decode
    import re
    from PIL import Image, ImageOps
    from bs4 import BeautifulSoup
    from docling_pdf_table_content import _source_crop

    if reader is None or image is None or getattr(args, 'surya_ocr_model', None) is None:
        return
    page = reader.load_page(page_no)
    if page is None or not any(reader.is_page_scan(obj, region) for obj in page.images):
        return
    alternatives = list(_scan_literal_candidates(table, profile, geometry, page, region))
    if not alternatives:
        return
    from surya.inference.schema import BatchInputItem
    from collections import defaultdict
    from hashlib import sha256
    replacements = defaultdict(dict)
    strings = {}
    for string, primary, native, box, position in alternatives:
        # Include the white word margins in the scan, never a neighboring glyph.
        bounds = [max(region[0],box[0]-.75), max(region[1],box[1]-1.5),
                  min(region[2],box[2]+.75), min(region[3],box[3]+1.5)]
        views, results = [], []
        failure, engine = None, None
        try:
            with _source_crop(image, region, bounds) as original:
                for height in (48,72):
                    width = max(1,round(original.width*height/original.height))
                    if width > 1800:
                        break
                    with original.resize((width,height), Image.Resampling.LANCZOS) as scaled:
                        views.append(ImageOps.expand(scaled,border=16,fill='white'))
            if len(views) != 2:
                continue
            engine = 'surya'
            if native.isdecimal():
                # Reuse the bundled independent decimal recognizer when both
                # renderings give the complete positioned digit sequence.
                import numpy as np
                from docling_pdf_table_content import _decimal_line_recognizer
                try:
                    digits = _decimal_line_recognizer().recognize_txt([
                        np.asarray(view.convert('RGB'))[:,:,::-1].copy() for view in views])
                    if all(score >= .99 for score in digits.scores) and list(digits.txts) == [native,native]:
                        results = [[native],[native]]
                        engine = 'rapidocr'
                except (ImportError, OSError, RuntimeError, ValueError):
                    pass
            if not results:
                backend = api.local_surya_backend(args.surya_ocr_model)
                limit = max(64,4*len(native))
                responses = backend.generate([BatchInputItem(image=view,prompt_type='block',max_tokens=limit,
                    metadata={'purpose':'positioned_scan_literal_counterread','page':page_no}) for view in views])
                for response in responses:
                    if response.error or not response.raw or response.token_count >= limit:
                        break
                    content = BeautifulSoup(response.raw, 'html.parser')
                    if content.find(['img','svg','math','table']) or re.search(r'<\|[^>]+\|>',response.raw):
                        break
                    results.append(re.findall(r'[А-Яа-яЁёA-Za-z]+|[0-9]+',content.get_text(' ',strip=True)))
        except (OSError, RuntimeError, ValueError) as error:
            failure = type(error).__name__
        finally:
            for view in views:
                view.close()
        record = dict(page=page_no,ref=ref,row=position['row'],column=position['column'],
                      source_bbox=list(box),primary_literal=primary,positioned_literal=native,
                      regional_readings=results, verification_engine=engine)
        if failure:
            record['counterread_error'] = failure
        confirmed = results == [[native],[native]]
        if not confirmed:
            try:
                context = _read_scan_word_context(image,region,box,page,args,api,page_no)
            except (OSError,RuntimeError,ValueError) as error:
                context = None
                record['context_error'] = type(error).__name__
            if context:
                context_box, expected, readings = context
                exact = [native if word is None else word for word in expected]
                confirmed = readings == [exact,exact]
                record.update(context_bbox=context_box,expected_context=exact,context_readings=readings)
        if not confirmed and results != [[primary],[primary]]:
            glyph = _confirmed_scan_glyph(profile,geometry,image,region,page,box,primary,native)
            if glyph:
                record['printed_glyph_proof'] = glyph
                confirmed = True
        if confirmed:
            replacements[id(string)][primary] = native
            strings[id(string)] = string
            record['method'] = ('positioned_scan_word_verified_by_printed_reference_letters'
                                if record.get('printed_glyph_proof') else 'positioned_scan_word_verified_by_two_source_renders')
            manifest.setdefault('source_scan_literal_repairs', []).append(record)
            api.diagnostic_event('SOURCE_SCAN_LITERAL_VERIFIED', **record)
        else:
            # Preserve the original glyphs when neither regional reading
            # confirms the disputed token. Keep both editable alternatives
            # as metadata, without printing either as the source value.
            with _source_crop(image,region,bounds) as chip:
                preview = BeautifulSoup(api.image_html(chip,bounds[2]-bounds[0],'cell-review'),'html.parser').img
            preview['alt'] = native
            preview['title'] = f'Текстовый слой: {native}. Первичное OCR: {primary}.'
            preview['data-table-content'] = 'picture'
            preview['data-ocr-literal-review'] = 'true'
            replacements[id(string)][primary] = preview
            strings[id(string)] = string
            manifest.setdefault('typed_table_picture_assets',[]).append(dict(
                page=page_no,source_ref=ref,cell={k:position[k] for k in ('row','column','row_span','column_span')},
                source_bbox=bounds,source_sha256=sha256(preview['src'].encode()).hexdigest(),
                source_image_sha256=sha256(b64decode(preview['src'].split(',', 1)[1])).hexdigest(),
                method='positioned_scan_word_source_preview'))
            record['reason'] = 'scan_literal_disagreement_unconfirmed'
            record['message'] = 'Сохранено изображение спорного слова или числа из исходной таблицы.'
            manifest.setdefault('table_assembly_reviews', []).append(record)
            api.diagnostic_event('SOURCE_SCAN_LITERAL_REVIEW', **record)
    for key, mapping in replacements.items():
        string = strings[key]
        text = str(string)
        position = 0
        for word in re.finditer(r'[А-Яа-яЁёA-Za-z]+|[0-9]+',text):
            if word[0] not in mapping:
                continue
            if position < word.start():
                string.insert_before(text[position:word.start()])
            string.insert_before(mapping[word[0]])
            position = word.end()
        if position < len(text):
            string.insert_before(text[position:])
        string.extract()
