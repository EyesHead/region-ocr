"""Estimate small page rotations and correct the PDF before layout and OCR."""

import math
from collections import Counter
from io import BytesIO


DESKEW_STAGE = "Оцениваем угол наклона каждой страницы"
MAX_DESKEW_DEGREES = 5


class PageDeskew:
    """Avoid editing structures that PDFium may share with another page."""

    def __init__(self, source, page_count):
        self.source = source
        self.page_count = page_count
        self.unsupported_pages = None

    def apply(self, page, index, angle):
        if not angle:
            return 0.0
        if self.unsupported_pages is None:
            self.unsupported_pages = _unsafe_pages(self.source, self.page_count)
        reason = self.unsupported_pages.get(index)
        if reason:
            print(f"Deskew skipped: page {index + 1}; {reason}", flush=True)
            return 0.0
        deskew_page(page, angle)
        return angle


def _unsafe_pages(source, page_count):
    from pdfminer.psparser import PSException

    try:
        if isinstance(source, (bytes, bytearray)):
            with BytesIO(source) as stream:
                return _shared_page_objects(stream, page_count)
        if hasattr(source, "read"):
            position = source.tell()
            try:
                return _shared_page_objects(source, page_count)
            finally:
                source.seek(position)
        with open(source, "rb") as stream:
            return _shared_page_objects(stream, page_count)
    except (OSError, PSException, ValueError, KeyError, TypeError, IndexError, RecursionError) as error:
        return dict.fromkeys(range(page_count), f"could not verify PDF resource ownership ({type(error).__name__})")


def _shared_page_objects(stream, page_count):
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import PDFObjRef, dict_value, list_value, resolve1

    class PageAttributes(PDFPage):
        def __init__(self, _document, _page_id, attrs, _label):
            # PDFPage normally resolves and loads every page's content streams.
            self.attrs = attrs

    def identity(value):
        return ("ref", value.objid) if isinstance(value, PDFObjRef) else ("direct", id(value))

    document = PDFDocument(PDFParser(stream), fallback=False)
    pages = list(PageAttributes.create_pages(document))
    if len(pages) != page_count:
        raise ValueError("The PDF page tree differs between readers.")
    references = [page.attrs.get("Contents") for page in pages]
    content_counts = Counter(identity(ref) for ref in references if isinstance(ref, PDFObjRef))
    resources = []
    for page, contents in zip(pages, references):
        owned = []
        if content_counts[identity(contents)] > 1 and isinstance(resolve1(contents), list):
            owned.append(("content array", identity(contents)))
        patterns = dict_value(dict_value(page.attrs.get("Resources")).get("Pattern"))
        owned.extend(("pattern", identity(pattern)) for pattern in patterns.values())
        owned.extend(("annotation", identity(annot)) for annot in list_value(page.attrs.get("Annots")))
        resources.append(owned)
    ownership = Counter(resource for page in resources for resource in page)
    return {index: f"shared {resource[0]}" for index, page in enumerate(resources)
            for resource in page if ownership[resource] > 1}


def estimate_skew(image):
    """Return the counterclockwise correction in display coordinates."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    if min(height, width) < 40:
        return 0.0
    scale = min(1.0, 850 / max(height, width))
    if scale < 1:
        gray = cv2.resize(gray, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    height, width = gray.shape
    top, left = max(1, height // 25), max(1, width // 25)
    interior = gray[top:height - top, left:width - left]
    threshold, _ = cv2.threshold(interior, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    y, x = np.nonzero(interior <= min(threshold, 205))
    if x.size < 150 or x.size > interior.size * .35:
        return 0.0
    if np.ptp(x) < width * .18 or np.ptp(y) < height * .12:
        return 0.0

    # Score projections of ink points, without resampling the page for every angle.
    step = max(1, math.ceil(x.size / 60_000))
    x = (x[::step] - (width - 2 * left) / 2).astype(np.float32)
    y = (y[::step] - (height - 2 * top) / 2).astype(np.float32)
    radius = math.ceil(math.hypot(width, height) / 2) + 1

    def score(angle):
        radians = math.radians(angle)
        cosine, sine = math.cos(radians), math.sin(radians)
        rows = np.bincount((y * cosine - x * sine + radius + .5).astype(np.intp))
        columns = np.bincount((x * cosine + y * sine + radius + .5).astype(np.intp))
        return max(height * (rows @ rows), width * (columns @ columns))

    baseline = score(0)
    coarse = range(-2 * MAX_DESKEW_DEGREES, 2 * MAX_DESKEW_DEGREES + 1)
    best_coarse = max(coarse, key=lambda tick: score(tick / 2)) / 2
    center = round(best_coarse * 20)
    fine = range(max(-20 * MAX_DESKEW_DEGREES, center - 10), min(20 * MAX_DESKEW_DEGREES, center + 10) + 1)
    best = max(fine, key=lambda tick: score(tick / 20)) / 20
    if abs(best) < .20 or score(best) < baseline * 1.08:
        return 0.0
    return best


def page_skew(page):
    """Use a bounded PDFium preview to estimate the angle for the fast route."""
    width, height = page.get_size()
    if min(width, height) <= 0 or not math.isfinite(width + height):
        return 0.0
    bitmap = page.render(scale=min(1.35, 850 / max(width, height)), grayscale=True)
    try:
        with bitmap.to_pil() as preview:
            return estimate_skew(preview)
    finally:
        bitmap.close()


def deskew_page(page, angle):
    """Transform existing PDF content so its next render resamples source pixels once."""
    from ctypes import byref
    from pypdfium2 import PdfiumError, raw

    if not angle:
        return
    left, bottom, right, top = page.get_bbox()
    center_x, center_y = (left + right) / 2, (bottom + top) / 2
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    matrix = raw.FS_MATRIX(cosine, sine, -sine, cosine,
                           center_x * (1 - cosine) + center_y * sine,
                           center_y * (1 - cosine) - center_x * sine)
    # Preserve the old visible area, including ink at the paper edge.
    clip = raw.FPDF_CreateClipPath(left, bottom, right, top)
    if not clip:
        raise PdfiumError("Failed to create the original page clip for deskew.")
    try:
        raw.FPDFPage_InsertClipPath(page, clip)
    finally:
        raw.FPDF_DestroyClipPath(clip)
    if not raw.FPDFPage_TransFormWithClip(page, byref(matrix), None):
        raise PdfiumError("Failed to deskew the PDF page.")
    raw.FPDFPage_TransformAnnots(page, matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f)
    half_width = (cosine * (right - left) + abs(sine) * (top - bottom)) / 2
    half_height = (cosine * (top - bottom) + abs(sine) * (right - left)) / 2
    bounds = center_x - half_width, center_y - half_height, center_x + half_width, center_y + half_height
    page.set_mediabox(*bounds)
    page.set_cropbox(*bounds)


def source_bbox(bbox, page_info):
    """Map normalized display coordinates back to the source display page."""
    left, top, right, bottom = bbox
    width, height = page_info["original_size"]
    aligned_width, aligned_height = page_info.get("deskewed_size", (width, height))
    angle = math.radians(page_info.get("deskew_degrees", 0))
    cosine, sine = math.cos(angle), math.sin(angle)
    rotation = page_info["clockwise"]
    points = []
    for x, y in ((left, top), (right, top), (right, bottom), (left, bottom)):
        if rotation == 90:
            x, y = y, aligned_height - x
        elif rotation == 180:
            x, y = aligned_width - x, aligned_height - y
        elif rotation == 270:
            x, y = aligned_width - y, x
        if angle:
            x, y = x - aligned_width / 2, y - aligned_height / 2
            x, y = cosine * x - sine * y + width / 2, sine * x + cosine * y + height / 2
        points.append((x, y))
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]
