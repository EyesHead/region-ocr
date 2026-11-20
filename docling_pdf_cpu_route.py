"""Conservative CPU routing for the existing PDF assembly pipeline."""

from functools import lru_cache
from hashlib import sha256
from html import escape
from itertools import groupby
from math import ceil, floor, isfinite
import os
from pathlib import Path
import re
from statistics import median
from types import SimpleNamespace


OCCULAR_SNAPSHOT = '4848df59dd8f10bee7360366e8f2f067678bcda8'
OCCULAR_FILES = (
    ('recognizer_svtr_lcnet_fp32.onnx', 30_493_783,
     '3de3ad346181af4e4446e6c365d5d9a8bfb187ad803d6f419dd61ad3509a65f2'),
    ('recognizer_charset.txt', 472,
     'b4943823e5f46eb162fb1bce8ba341da89729cb62ed0617ee1c1f27ea0497303'),
)
TEXT_LABELS = {
    'text': 'Text',
    'title': 'Title',
    'section_header': 'SectionHeader',
    'list_item': 'ListItem',
    'page_header': 'PageHeader',
    'page_footer': 'PageFooter',
    'footnote': 'Footnote',
}
POSSIBLE_MATH = re.compile(r'[=<>±×÷√∑∫≤≥≠≈^²³Øø⌀₀-₉α-ωΑ-Ω]|\b[A-Za-zА-Яа-яЁё]\s*[/⁄]\s*\d')
BROKEN_PDF_TEXT = re.compile(r'[\u0000-\u0008\u000e-\u001f\u007f\ufffd\ue000-\uf8ff]|GLYPH<[0-9A-Fa-f]+>|(?:/G\d+){2,}')


def adaptive_enabled(device):
    return device == 'cpu' and os.environ.get('REGION_OCR_CPU_PDF_MODE', 'adaptive') == 'adaptive'


def simple_regions(page):
    layout = page.predictions.layout
    if layout is None or not layout.clusters:
        return None
    if any(cluster.label not in TEXT_LABELS for cluster in layout.clusters):
        return None
    regions = [cluster for cluster in layout.clusters if cluster.confidence >= .5]
    for index, region in enumerate(regions):
        box = region.bbox
        for other in regions[:index]:
            prior = other.bbox
            if _parallel((box.l, box.t, box.r, box.b), (prior.l, prior.t, prior.r, prior.b), 3, 3):
                return None
    return regions or None


def _ink(image):
    import cv2
    import numpy as np

    with image.convert('L') as gray:
        pixels = np.asarray(gray)
        return cv2.threshold(pixels, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]


def _covers_page(ink, covered, line_height):
    import cv2
    import numpy as np

    covered = cv2.dilate(covered, np.ones((5, 5), np.uint8))
    missing = cv2.bitwise_and(ink, cv2.bitwise_not(covered))
    ink_count = cv2.countNonZero(ink)
    if not ink_count or cv2.countNonZero(missing) > .02 * ink_count:
        return False
    _, _, components, _ = cv2.connectedComponentsWithStats(missing)
    return not any(max(w, h) >= .2 * line_height and area >= max(3, .03 * line_height ** 2)
                   for _, _, w, h, area in components[1:])


def has_visible_pdf_text(source, page_no):
    """Reject image pages and invisible PDF text before materializing cells."""
    from ctypes import byref, c_uint
    import pypdfium2 as pdfium
    from pypdfium2 import raw
    from docling.utils.locks import pypdfium2_lock

    with pypdfium2_lock:
        document = pdfium.PdfDocument(str(source))
        try:
            page = document[page_no - 1]
            try:
                visible = False
                for item in page.get_objects(filter=[raw.FPDF_PAGEOBJ_TEXT, raw.FPDF_PAGEOBJ_IMAGE]):
                    if item.type == raw.FPDF_PAGEOBJ_IMAGE or raw.FPDFTextObj_GetTextRenderMode(item.raw) != 0:
                        return False
                    channels = [c_uint() for _ in range(4)]
                    if (not raw.FPDFPageObj_GetFillColor(item.raw, *(byref(c) for c in channels))
                            or channels[-1].value != 255 or min(c.value for c in channels[:3]) >= 245):
                        return False
                    visible = True
                return visible
            finally:
                page.close()
        finally:
            document.close()


def native_text_page(page):
    """Return the original Docling cells after checking the visible page."""
    import numpy as np

    segmented = page._backend.get_segmented_page()
    if (segmented is None or segmented.bitmap_resources or segmented.widgets
            or not segmented.textline_cells or not segmented.word_cells):
        return None

    for cell in (*segmented.textline_cells, *segmented.word_cells):
        # PDF text rendering modes 3 and 7 paint no ink. A scanned image
        # with a text layer was excluded above before checking the text.
        if (getattr(cell, 'rendering_mode', None) in (None, 3, 7)
                or BROKEN_PDF_TEXT.search(cell.text) or POSSIBLE_MATH.search(cell.text)):
            return None

    image = page.get_image(scale=1.5)
    if image is None or page.size is None:
        return None
    ink = _ink(image)
    covered = np.zeros_like(ink)
    width, height = image.size
    sx, sy = width / page.size.width, height / page.size.height
    heights = []
    visited = set()
    for cell in segmented.word_cells:
        if not cell.text.strip():
            continue
        box = cell.to_bounding_box()
        if not all(isfinite(coord) for coord in (box.l, box.t, box.r, box.b)):
            return None
        left, top = max(0, floor(box.l * sx)), max(0, floor(box.t * sy))
        right, bottom = min(width, ceil(box.r * sx)), min(height, ceil(box.b * sy))
        bounds = (left, top, right, bottom)
        if (left >= right or top >= bottom or bounds in visited
                or not ink[top:bottom, left:right].any()):
            return None
        visited.add(bounds)
        covered[top:bottom, left:right] = 255
        heights.append(bottom - top)

    if not heights or not _covers_page(ink, covered, median(heights)):
        return None
    return segmented


def _model_directory():
    configured = os.environ.get('REGION_OCR_OCCULAR_MODEL_DIR')
    if configured:
        return Path(configured).resolve()
    packaged = Path(__file__).resolve().parent / 'models' / 'occular'
    if packaged.is_dir():
        return packaged
    return (Path.home() / '.cache' / 'huggingface' / 'hub'
            / 'models--Shivin11--occular-ocr' / 'snapshots' / OCCULAR_SNAPSHOT)


def occular_reader():
    return _load_occular(_model_directory())


@lru_cache(maxsize=1)
def _load_occular(directory):
    import onnxruntime as ort
    import rapidocr

    assets = []
    for name, size, checksum in OCCULAR_FILES:
        with (directory / name).open('rb') as stream:
            data = stream.read(size + 1)
        if sha256(data).hexdigest() != checksum:
            raise ValueError(f'Invalid Occular asset checksum: {name}')
        assets.append(data)

    model, charset = assets
    detector = Path(rapidocr.__file__).parent / 'models' / 'PP-OCRv6_det_small.onnx'
    if not detector.is_file():
        raise FileNotFoundError(detector)
    line_detector = rapidocr.RapidOCR(params={
        'Global.log_level': 'warning',
        'Global.use_cls': False,
        'Global.use_rec': False,
        'Det.model_path': str(detector),
        'Det.limit_type': 'max',
        'Det.limit_side_len': 960,
        'EngineConfig.onnxruntime.intra_op_num_threads': 4,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
    })
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(model, sess_options=options, providers=['CPUExecutionProvider'])
    return OccularReader(line_detector, session, charset.decode('utf-8').split('\n'))


class OccularReader:
    """Run the Occular SVTR-LCNet ONNX recognizer on detected lines."""

    def __init__(self, detector, session, alphabet):
        self.detector = detector
        self.session = session
        self.input_name = session.get_inputs()[0].name
        self.alphabet = alphabet

    def __call__(self, image):
        import numpy as np

        boxes = self.detector(image).boxes
        empty = SimpleNamespace(boxes=boxes, txts=(), scores=())
        if boxes is None or len(boxes) == 0:
            return empty

        with image.convert('RGB') as rgb:
            pixels = np.asarray(rgb)
            lines = []
            for points in boxes:
                if not np.isfinite(points).all():
                    return empty
                left, top = np.maximum(0, points.min(axis=0).astype(int))
                right, bottom = np.minimum(image.size, points.max(axis=0).astype(int))
                if left >= right or top >= bottom:
                    return empty
                lines.append(pixels[top:bottom, left:right])
            texts, scores = self.recognize_lines(lines)
        return SimpleNamespace(boxes=boxes, txts=texts, scores=scores)

    def recognize_lines(self, crops):
        """Read positioned RGB line crops using the existing width buckets."""
        import cv2

        lines = []
        for crop in crops:
            height, width = crop.shape[:2]
            target = min(640, max(8, round(48 * width / height)))
            lines.append(cv2.resize(crop[:, :, ::-1], (target, 48)))
        return self._recognize(lines)

    def _recognize(self, lines):
        import numpy as np

        order = sorted(range(len(lines)), key=lambda index: lines[index].shape[1])
        texts, scores = [''] * len(lines), [0.0] * len(lines)
        start = 0
        while start < len(order):
            end = start + 1
            while (end < min(start + 8, len(order))
                   and lines[order[end]].shape[1] <= 1.2 * lines[order[start]].shape[1]):
                end += 1
            group = order[start:end]
            batch = np.ones((len(group), 3, 48, lines[group[-1]].shape[1]), dtype=np.float32)
            for row, index in enumerate(group):
                line = lines[index]
                batch[row, :, :, :line.shape[1]] = line.transpose(2, 0, 1) / 127.5 - 1.0
            logits = self.session.run(None, {self.input_name: batch})[0]
            if logits.shape[-1] != len(self.alphabet) + 1:
                raise ValueError('Occular output does not match the CTC alphabet')
            for index, values in zip(group, logits, strict=True):
                ids = values.argmax(axis=-1)
                texts[index] = ''.join(self.alphabet[token - 1] for token, _ in groupby(ids) if token)
                nonblank = values[ids != 0]
                if len(nonblank):
                    centered = nonblank - nonblank.max(axis=-1, keepdims=True)
                    scores[index] = float(np.exp(-np.log(np.exp(centered).sum(axis=-1)).mean()))
            start = end
        return texts, scores


def _intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _parallel(a, b, minimum_overlap, minimum_gap):
    return (min(a[3], b[3]) - max(a[1], b[1]) > minimum_overlap
            and max(a[0] - b[2], b[0] - a[2]) > minimum_gap)


def recognize_prose(image, rect, scale, clusters, reader):
    """Return a complete OCR result, or leave the page for Surya."""
    import cv2
    import numpy as np
    from docling_surya_ocr.normalization import NormalizedOcrBlock, NormalizedOcrPage

    width, height = image.size
    regions = []
    for cluster in sorted(clusters, key=lambda region: region.confidence, reverse=True):
        box = cluster.bbox
        bounds = (max(0, floor((box.l - rect.l) * scale)),
                  max(0, floor((box.t - rect.t) * scale)),
                  min(width, ceil((box.r - rect.l) * scale)),
                  min(height, ceil((box.b - rect.t) * scale)))
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            return None
        for _, accepted in regions:
            overlap = _intersection(bounds, accepted)
            if overlap >= .9 * _area(bounds):
                break  # Heron may emit a second detection of the same block.
            if overlap > .1 * min(_area(bounds), _area(accepted)):
                return None
        else:
            regions.append((TEXT_LABELS[cluster.label], bounds))
    regions.sort(key=lambda region: (region[1][1], region[1][0]))

    ink = _ink(image)
    covered = np.zeros_like(ink)
    blocks = []
    pad = ceil(3 * scale)
    for label, (left, top, right, bottom) in regions:
        left, top = max(0, left - pad), max(0, top - pad)
        right, bottom = min(width, right + pad), min(height, bottom + pad)
        with image.crop((left, top, right, bottom)) as patch:
            result = reader(patch)
        boxes, texts, scores = result.boxes, result.txts, result.scores
        if boxes is None or not texts or len(boxes) != len(texts) or len(texts) != len(scores):
            return None
        for text, score, points in zip(texts, scores, boxes, strict=True):
            text = text.strip()
            if not text or not isfinite(score) or score < .8 or POSSIBLE_MATH.search(text):
                return None
            points = np.asarray(points, dtype=float)
            if points.shape != (4, 2) or not np.isfinite(points).all():
                return None
            points = points.clip((0, 0), (right - left - 1, bottom - top - 1)) + (left, top)
            x0, y0 = points.min(axis=0)
            x1, y1 = points.max(axis=0)
            bounds = tuple(map(float, (x0, y0, x1, y1)))
            if x0 >= x1 or y0 >= y1 or any(
                    _intersection(bounds, prior.bbox) > .5 * min(_area(bounds), _area(prior.bbox))
                    for prior in blocks):
                return None
            cv2.fillConvexPoly(covered, np.rint(points).astype(np.int32), 255)
            blocks.append(NormalizedOcrBlock(
                text=text, bbox=bounds, html=escape(text), confidence=float(score),
                label=label, reading_order=len(blocks),
            ))

    if not blocks:
        return None
    line_height = median(b.bbox[3] - b.bbox[1] for b in blocks)
    # Parallel lines may be columns or a borderless table.
    for index, current in enumerate(blocks):
        if any(_parallel(current.bbox, prior.bbox, .5 * line_height, line_height)
               for prior in blocks[:index]):
            return None
    if not _covers_page(ink, covered, line_height):
        return None
    return NormalizedOcrPage(tuple(blocks), image_bbox=(0, 0, width, height))


def restore_prose_order(doc, page_routes):
    """Respect physical line order inside single-column text sibling runs."""
    from docling_core.types.doc import TextItem

    pages = {number for number, route in page_routes.items() if route['engine'] == 'occular'}
    if not pages:
        return 0

    def positioned(ref):
        item = ref.resolve(doc)
        if not isinstance(item, TextItem) or item.label not in TEXT_LABELS or len(item.prov) != 1:
            return None
        origin = item.prov[0]
        if origin.page_no not in pages:
            return None
        box = origin.bbox.to_top_left_origin(doc.pages[origin.page_no].size.height)
        return origin.page_no, box.t, box.l

    changed = 0
    for parent in (doc.body, *doc.groups):
        positions = [(ref, positioned(ref)) for ref in parent.children]
        ordered = []
        for page_no, chunk in groupby(positions, key=lambda entry: entry[1][0] if entry[1] else None):
            run = list(chunk)
            if page_no is not None:
                physical = sorted(run, key=lambda entry: entry[1][1:])
                if physical != run:
                    changed += 1
                run = physical
            ordered.extend(ref for ref, _ in run)
        if ordered != parent.children:
            parent.children[:] = ordered
    return changed
