"""Additional recognition views grounded in the original PDF image objects.

These are retries, not text corrections. A view is accepted only by the same
Surya plus two independent CodeFormula comparisons as the initial reading.
"""
from io import BytesIO

from PIL import Image, ImageOps


def native_image(recognizer,metadata):
    reader=getattr(recognizer,'pdf_source_reader',None)
    page,bounds=metadata.get('source_page'),metadata.get('source_bbox')
    if reader is None or not page or not bounds:
        return None
    reader.load_page(page)
    # Visible native text inside the same canvas is a composite source, not
    # a standalone embedded formula. It must keep its native glyph route.
    if any(g['text'].strip() and bounds[0] <= (g['x0']+g['x1'])/2 <= bounds[2]
           and bounds[1] <= (g['top']+g['bottom'])/2 <= bounds[3] for g in reader.chars):
        return None
    objects=[o for o in reader.page.images if max(abs(a-b) for a,b in zip(
        [o['x0'],o['top'],o['x1'],o['bottom']],bounds)) <= .1]
    if (len(objects)!=1 or objects[0]['stream'].attrs.get('SMask') is not None
            or objects[0]['stream'].attrs.get('Mask') is not None):
        return None
    from docling_pdf_accents import source_layer
    return source_layer(reader,page,bounds,alpha_only=False,native=True)


def embedded_image_views(recognizer, metadata):
    source=native_image(recognizer,metadata)
    if source is None:
        return
    try:
        # Exact integer pixel replication retains tiny serifs/crossbars that
        # page antialiasing and interpolated resizing otherwise merge.
        enlarged=source.resize((source.width*2,source.height*2),Image.Resampling.NEAREST)
        image=ImageOps.expand(enlarged,border=24,fill='white')
        enlarged.close()
        buffer=BytesIO();image.save(buffer,'PNG')
        proof=dict(method='native_embedded_image_integer_pixels',native_size=list(source.size),
                   recognition_size=list(image.size),scale=2,interpolation='nearest',padding=24)
        image.close()
        yield buffer.getvalue(),proof
    finally:
        source.close()
