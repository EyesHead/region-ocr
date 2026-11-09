"""Recover image equations in a paragraph from complete source text ownership."""
from io import BytesIO
import re
from statistics import median
import unicodedata


def _words(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text) if c.isalnum())


def source_asset_paragraph(item, doc, reader, regions, recognizer, picture_routes,
                           manifest, api, verify, budget, excluded=()):
    """Restore all source assets when *all* nonimage words match the OCR.

    One damaged OCR slot may cover several images on different source lines.
    Image count, dimensions and presence of a separately assembled PictureItem
    are not content evidence. Exact paragraph words and physical ownership are.
    Every image still passes the existing independent formula verification.
    """
    region = api.item_region(item, doc)
    if not region or reader is None:
        return None
    page, box = region
    reader.load_page(page)
    glyphs = [g for g in reader.chars if g.get('text') and
              box[0] <= (g['x0']+g['x1'])/2 <= box[2] and
              box[1] <= (g['top']+g['bottom'])/2 <= box[3]]
    if not glyphs or any('(cid:' in g['text'] or not g.get('upright', True) for g in glyphs):
        return None
    size = median(g['size'] for g in glyphs)
    assets = [o for o in reader.page.images if
              box[0]-size/2 <= o['x0'] < o['x1'] <= box[2]+size/2 and
              box[1]-size/2 <= o['top'] < o['bottom'] <= box[3]+size/2]
    # Keep the established single-asset route and its byte-exact reviewed
    # crop identity. This recovery handles the distinct multi-asset case.
    if len(assets) < 2:
        return None
    # A formula inside a separately owned table is not part of this prose slot.
    for table in doc.tables:
        if table.self_ref in excluded:
            continue
        table_region = api.item_region(table, doc)
        if table_region and table_region[0] == page and any(
                api.overlap_ratio([o['x0'],o['top'],o['x1'],o['bottom']],table_region[1]) > .5
                for o in assets):
            return None
    marker = getattr(item, 'marker', '') or ''
    def unmark(value):
        return re.sub(r'^\s*'+re.escape(marker)+r'\s*', '', value) if marker else value
    source = ' '.join(line['text'] for line in api.native_cell_lines(glyphs))
    expected = re.sub(r'[█▓■]{2,}', '', item.text)
    if _words(unmark(source)) != _words(unmark(expected)):
        return None
    # Record the actual failing item before any recognizer can raise.
    if hasattr(recognizer, 'args'):
        recognizer.args.pdf_export_context = dict(stage='inline_formula', page=page,
                                                  element=item.self_ref, bbox=box)
    rendered_assets = []
    for obj in assets:
        bounds = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
        image = regions.crop(page, bounds)
        try:
            stream = BytesIO(); image.save(stream, 'PNG')
            with budget(manifest, page, api, bounds) as allowed:
                if not allowed:
                    return None
                rendered = verify(stream.getvalue(), dict(width=image.width, height=image.height,
                    inline=True, paragraph_text=item.text, source_page=page, source_bbox=bounds,
                    element=item.self_ref, media_name=f'PDF inline source {page}, {item.self_ref}, {bounds}'),
                    recognizer, manifest, api)
        finally:
            image.close()
        rendered_assets.append(dict(obj, text='\ufffc', source_html=rendered, size=size))
    html = unmark(' '.join(line['html'] for line in api.native_cell_lines(glyphs+rendered_assets)))
    pictures = []
    for picture in doc.pictures:
        reg = api.item_region(picture, doc)
        if reg and reg[0] == page and not picture.children and not picture.captions and any(
                api.overlap_ratio(reg[1],[o['x0'],o['top'],o['x1'],o['bottom']]) > .8 for o in assets):
            picture_routes[picture.self_ref] = ''; pictures.append(picture.self_ref)
    proof = dict(page=page, ref=item.self_ref, bboxes=[
        [o['x0'],o['top'],o['x1'],o['bottom']] for o in assets], pictures=pictures,
        method='complete_source_paragraph_words_and_positioned_verified_assets')
    api.diagnostic_event('PDF_FORMULA_PARAGRAPH_RESTORED', **proof)
    return [('source_line', (item.text, html))], proof
