"""Extract the embedded image stream and render its visible placement when needed."""
from io import BytesIO
import logging
import math
from pathlib import Path

from local_web.fast_resources import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
from local_web.fast_pdf_geometry import _page_box


def crop_png(page, box, scale=2.0):
    """Render a rectangle in displayed, top-left page coordinates directly to PNG."""
    pw, ph = page.get_size()
    if len(box) != 4 or not all(math.isfinite(v) for v in (*box, scale)) or scale <= 0:
        raise ValueError("Invalid image crop")
    l, t, r, b = box
    l, t, r, b = max(0, l), max(0, t), min(pw, r), min(ph, b)
    if r <= l or b <= t:
        raise ValueError("Empty image crop")
    scale = min(scale, 1800 / max(r-l, b-t))
    # Round the crop margins down so a nonempty sliver has at least one pixel.
    margins = tuple(math.floor(v*scale) / scale for v in (l, ph-b, pw-r, t))
    bitmap = page.render(scale=scale, crop=margins, rev_byteorder=True)
    try:
        with bitmap.to_pil() as image:
            out = BytesIO()
            image.save(out, format='PNG', compress_level=1)
            return out.getvalue()
    finally:
        bitmap.close()


def _stream_bytes(stream):
    data = stream.get_rawdata()
    if data is None or len(data) > MAX_IMAGE_BYTES or 'F' in stream:
        raise ValueError("Embedded stream unavailable")
    if stream.decipher:
        data = stream.decipher(stream.objid, stream.genno, data, stream.attrs)
    return data


def _page_resources(page):
    """Read resource dictionaries lazily. The independent reader closes with the PDF."""
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    doc = page.pdf
    index = getattr(page, '_fast_image_page_index', None)
    state = getattr(doc, '_fast_image_reader', None)
    if state is None:
        source = getattr(doc, '_input', None)
        if isinstance(source, (str, Path)):
            handle = open(source, 'rb')
        elif isinstance(source, bytes):
            handle = BytesIO(source)
        elif isinstance(source, BytesIO):
            handle = BytesIO(source.getvalue())
        else:
            return ()
        try:
            parsed = PDFDocument(PDFParser(handle), password=doc._password or '', caching=False)
        except Exception:
            handle.close()
            raise
        doc._data_closer.append(handle)
        state = doc._fast_image_reader = (PDFPage.create_pages(parsed), [])
    pages, cache = state
    count = len(doc) if index is None else index+1
    while len(cache) < count:
        p = next(pages, None)
        if p is None:
            break
        cache.append(p.resources)
    return cache if index is None else cache[index:index+1]


def _find_image(page, data, size):
    """Index source XObjects once per open page, retaining references, not raster bytes."""
    from hashlib import sha256
    from pdfminer.pdftypes import PDFStream, resolve1
    from pdfminer.psparser import LIT

    index = getattr(page, '_fast_image_streams', None)
    if index is None:
        index = {}
        def walk(resources, forms):
            for ref in resolve1(resources.get('XObject', {})).values():
                stream = resolve1(ref)
                if not isinstance(stream, PDFStream):
                    continue
                if stream.get('Subtype') is LIT('Form'):
                    if stream.objid not in forms and len(forms) < 32:
                        walk(resolve1(stream.get('Resources', resources)), forms | {stream.objid})
                    continue
                if stream.get('Subtype') is not LIT('Image'):
                    continue
                try:
                    content = _stream_bytes(stream)
                except ValueError:
                    continue
                key = (resolve1(stream.get('Width')), resolve1(stream.get('Height')),
                       len(content), sha256(content).digest())
                # The same encoded pixels can be painted with different Decode/Mask/colour.
                # Treat duplicate references and equivalent image dictionaries as one match.
                fields = tuple(sorted((k, repr(v)) for k, v in stream.attrs.items() if k in _IMAGE_KEYS))
                spaces = repr(resolve1(resources.get('ColorSpace', {})))
                index.setdefault(key, {}).setdefault((fields, spaces), (ref, resources))
        for resources in _page_resources(page):
            walk(resources, set())
        page._fast_image_streams = index
    matches = index.get((*size, len(data), sha256(data).digest()), {})
    if len(matches) == 1:
        ref, resources = next(iter(matches.values()))
        return resolve1(ref), resources
    return None, {}


_IMAGE_KEYS = frozenset("""
    Type Subtype Width Height BitsPerComponent ColorSpace Filter DecodeParms Decode
    ImageMask Mask SMask SMaskInData Matte Interpolate Intent
""".split())


def _image_pdf(stream, resources, width, height):
    """Wrap only this XObject and its imaging dependencies; copy every stream verbatim."""
    from PIL import PdfParser as writer
    from pdfminer.pdftypes import PDFStream, resolve1
    from pdfminer.psparser import PSLiteral, LIT

    out = BytesIO()
    pdf = writer.PdfParser(f=out)
    pdf.start_writing()
    # A mask or colour space may require a feature introduced after PDF 1.4.
    out.write(b'%PDF-1.7\n')
    page_id = pdf.next_object_id(0)
    pdf.pages.append(page_id)
    pdf.write_catalog()
    seen = {}
    budget = 0
    active = set()

    def copy(value, depth=0):
        nonlocal budget
        if depth > 25:
            raise ValueError("Invalid image dependency graph")
        value = resolve1(value)
        if isinstance(value, PDFStream):
            identity = value.objid if value.objid is not None else id(value)
            if identity in active:
                raise ValueError("Cyclic image dependency")
            if identity in seen:
                return seen[identity]
            active.add(identity)
            data = _stream_bytes(value)
            budget += len(data)
            if budget > MAX_IMAGE_BYTES:
                raise ValueError("Image dependencies exceed size limit")
            if value.get('Subtype') is LIT('Image'):
                keys = _IMAGE_KEYS
            else:
                keys = {'Filter','DecodeParms','N','Alternate','Range','FunctionType',
                        'Domain','Size','BitsPerSample','Order','Encode','Decode'}
            fields = {k: copy(v, depth+1) for k, v in value.attrs.items() if k in keys}
            ref = pdf.write_obj(None, stream=data, **fields)
            active.remove(identity)
            seen[identity] = ref
            return ref
        if isinstance(value, PSLiteral):
            return writer.PdfName(value.name)
        if isinstance(value, list):
            return [copy(v, depth+1) for v in value]
        if isinstance(value, dict):
            return {k: copy(v, depth+1) for k, v in value.items()}
        if isinstance(value, bytes):
            return writer.PdfBinary(value)
        if value is None or isinstance(value, (float, int, bool)):
            return value
        raise ValueError("Unsupported image dependency")

    image_ref = copy(stream)
    page_res = {'XObject': {'Image': image_ref}}
    # A named colour space in a mask may also refer to a resource alias.
    spaces = resolve1(resources.get('ColorSpace', {}))
    named = {}
    def include_space(space):
        space = resolve1(space)
        if isinstance(space, PSLiteral) and space.name in spaces and space.name not in named:
            named[space.name] = copy(spaces[space.name])
            include_space(spaces[space.name])
        elif isinstance(space, list):
            for part in space[1:]:
                include_space(part)
    for dev in ('DefaultRGB','DefaultGray','DefaultCMYK'):
        include_space(LIT(dev))
    for item in (stream, resolve1(stream.get('SMask')), resolve1(stream.get('Mask'))):
        if isinstance(item, PDFStream):
            include_space(item.get('ColorSpace'))
    if named:
        page_res['ColorSpace'] = named
    contents = pdf.write_obj(None, stream=f'q {width} 0 0 {height} 0 0 cm /Image Do Q'.encode('ascii'))
    pdf.write_page(page_id, MediaBox=[0, 0, width, height], Resources=page_res, Contents=contents)
    pdf.write_xref_and_trailer()
    return out.getvalue()


def _clip_contains_image(obj):
    """Accept absent clips and straight, closed rectangles enclosing the whole image."""
    import ctypes
    from pypdfium2 import raw

    clip = raw.FPDFPageObj_GetClipPath(obj)
    count = raw.FPDFClipPath_CountPaths(clip) if clip else 0
    bounds = obj.get_bounds()
    for i in range(max(0, count)):
        n = raw.FPDFClipPath_CountPathSegments(clip, i)
        if n not in (4, 5):
            return False
        points = []
        for j in range(n):
            seg = raw.FPDFClipPath_GetPathSegment(clip, i, j)
            if not seg or raw.FPDFPathSegment_GetType(seg) != (raw.FPDF_SEGMENT_MOVETO if j == 0 else raw.FPDF_SEGMENT_LINETO):
                return False
            x, y = ctypes.c_float(), ctypes.c_float()
            if not raw.FPDFPathSegment_GetPoint(seg, x, y):
                return False
            points.append((x.value, y.value))
        if (not raw.FPDFPathSegment_GetClose(seg) or (n == 5 and points[-1] != points[0])
                or len(set(points[:4])) != 4):
            return False
        for a, b in zip(points[:4], points[1:4] + points[:1]):
            if a[0] != b[0] and a[1] != b[1]:
                return False
        xs, ys = zip(*points)
        if (min(xs) > bounds[0]+.001 or min(ys) > bounds[1]+.001
                or max(xs) < bounds[2]-.001 or max(ys) < bounds[3]-.001):
            return False
    return True


def _plain_placement(page, obj, box, stream):
    from pypdfium2 import raw

    if stream is None or obj.level or page.get_rotation() or raw.FPDFPageObj_HasTransparency(obj):
        return False
    if any(k in stream for k in ('Mask', 'SMask', 'SMaskInData')) or stream.get('ImageMask', False):
        return False
    mat = obj.get_matrix()
    if mat.a <= 0 or mat.d <= 0 or mat.b != 0 or mat.c != 0:
        return False
    width, height = obj.get_px_size()
    if abs(mat.a*height - mat.d*width) > max(mat.a, mat.d):
        return False
    bounds = _page_box(obj.get_bounds(), page.get_bbox(), 0)
    if any(abs(a-b) > .01 for a, b in zip(bounds, box)):
        return False
    pw, ph = page.get_size()
    if bounds[0] < -.01 or bounds[1] < -.01 or bounds[2] > pw+.01 or bounds[3] > ph+.01:
        return False
    return _clip_contains_image(obj)


def _jpeg_matches_colors(image, stream, resources):
    from pdfminer.pdftypes import resolve1
    from pdfminer.psparser import LIT

    if 'Decode' in stream or image.mode not in ('L', 'RGB') or image.getexif().get(274, 1) != 1:
        return False
    params = resolve1(stream.get('DecodeParms'))
    if any(resolve1(x) for x in (params if isinstance(params, list) else [params])):
        return False
    spaces = resolve1(resources.get('ColorSpace', {}))
    space = resolve1(stream.get('ColorSpace'))
    if getattr(space, 'name', None) in spaces:
        space = resolve1(spaces[space.name])
    if isinstance(space, list) and len(space) == 2 and space[0] is LIT('ICCBased'):
        icc = resolve1(space[1])
        profile = image.info.get('icc_profile')
        return bool(profile) and icc.get_data() == profile
    expected = 'Gray' if image.mode == 'L' else 'RGB'
    return space is LIT('Device'+expected) and ('Default'+expected) not in spaces


def _object_png(obj, size):
    from PIL import Image

    bitmap = obj.get_bitmap(render=False)
    try:
        with bitmap.to_pil() as image:
            if image.size != size or image.mode not in ('1', 'L', 'LA', 'RGB', 'RGBA'):
                raise ValueError("Unsupported PDF image bitmap")
            # Preview bounds match the crop route. The original retains all pixels.
            if max(size) > 1800:
                image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            out = BytesIO()
            image.save(out, format='PNG', compress_level=1)
            return out.getvalue()
    finally:
        bitmap.close()


def make_image_asset(page, obj, box):
    """Return (preview MIME, bytes, optional original MIME, optional original bytes)."""
    from PIL import Image
    from pypdfium2 import raw
    from pypdfium2 import PdfiumError
    from pdfminer.psparser import PSException

    size = obj.get_px_size()
    filters = obj.get_filters()
    length = raw.FPDFImageObj_GetImageDataRaw(obj, None, 0)
    original_mime = original = None
    if 0 < length <= MAX_IMAGE_BYTES and 0 < size[0]*size[1] <= MAX_IMAGE_PIXELS:
        try:
            data = bytes(obj.get_data())
            direct = filters[-1:] in (['DCTDecode'], ['JPXDecode']) and all(
                f in obj.SIMPLE_FILTERS for f in filters[:-1])
            if direct:
                encoded = data
                if len(filters) > 1:
                    n = raw.FPDFImageObj_GetImageDataDecoded(obj, None, 0)
                    if not 0 < n <= MAX_IMAGE_BYTES:
                        raise ValueError("Embedded image exceeds size limit")
                    encoded = bytes(obj.get_data(decode_simple=True))
                with Image.open(BytesIO(encoded)) as image:
                    if image.size == size and image.format in ('JPEG','JPEG2000'):
                        if image.format == 'JPEG':
                            original_mime = 'image/jpeg'
                        else:
                            original_mime = 'image/j2k' if encoded.startswith(b'\xff\x4f') else 'image/jp2'
                        original = encoded
            stream, resources = _find_image(page, data, size)
            plain = _plain_placement(page, obj, box, stream)
            if original_mime == 'image/jpeg' and plain:
                with Image.open(BytesIO(original)) as image:
                    if _jpeg_matches_colors(image, stream, resources):
                        return original_mime, original, None, None
            if original_mime in ('image/jp2', 'image/j2k') and plain and max(size) > 1800:
                with Image.open(BytesIO(original)) as image:
                    if _jpeg_matches_colors(image, stream, resources):
                        image.reduce = int(math.log2(max(size) / 1800))
                        image.load()
                        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
                        out = BytesIO()
                        image.save(out, format='PNG', compress_level=1)
                        return 'image/png', out.getvalue(), original_mime, original
            if not direct and stream is not None:
                original = _image_pdf(stream, resources, *size)
                original_mime = 'application/pdf'
            if plain:
                return 'image/png', _object_png(obj, size), original_mime, original
        except (ValueError, KeyError, TypeError, AttributeError, OSError, PdfiumError, PSException, Image.DecompressionBombError) as exc:
            logging.getLogger(__name__).warning("Embedded image extraction: %s", exc)
    return 'image/png', crop_png(page, box), original_mime, original
