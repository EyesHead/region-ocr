"""Classify source image assets through the already running Surya backend.

Layout results are transient Python objects. This module neither starts its own
model nor writes recognition sidecars. Formula recognition keeps the complete
source asset; the layout boxes are evidence, not instructions to trim indices.
"""

import math

from PIL import Image


MATH_LABELS = frozenset({'Equation', 'Equation-Block', 'Formula'})
GRAPHIC_LABELS = frozenset({'Figure', 'Picture', 'Image', 'Diagram', 'Complex-Block'})


def padded_bbox(bbox, size, padding=4):
    """Expand a pixel bbox on every side and clamp it to the source image.

    Return None for empty or invalid boxes. Rounding outward preserves partly
    covered edge pixels, including small upper and lower mathematical indices.
    """
    try:
        width, height = size
        x0, y0, x1, y1 = map(float, bbox)
        padding = float(padding)
        if (width <= 0 or height <= 0 or padding < 0
                or not all(math.isfinite(value) for value in (x0, y0, x1, y1, padding))
                or x1 <= x0 or y1 <= y0):
            return None
        box = (max(0, math.floor(x0 - padding)), max(0, math.floor(y0 - padding)),
               min(width, math.ceil(x1 + padding)), min(height, math.ceil(y1 + padding)))
        return box if box[2] > box[0] and box[3] > box[1] else None
    except (TypeError, ValueError, OverflowError):
        return None


def opaque_image(image):
    """Flatten transparency onto white without changing source pixels or size."""
    rgba = image.convert('RGBA')
    white = Image.new('RGBA', rgba.size, 'white')
    white.alpha_composite(rgba)
    return white.convert('RGB')


def prepare_cell_text_image(image):
    """Fit a source-proven text cell to all its ink, preserving every pixel.

    Only exact white paper is trimmed. If background noise extends to an edge,
    that edge remains. Padding scales with the retained text rather than the
    physical cell dimensions, which may include a large blank field.
    """
    from PIL import ImageChops
    source = opaque_image(image)
    paper = Image.new('RGB',source.size,'white')
    difference = ImageChops.difference(source,paper)
    bounds = difference.getbbox()
    difference.close(); paper.close()
    if bounds is None:
        return source, dict(method='source_cell_exact_white_fit',blank=True,
                            original_size=list(image.size),ink_bbox=None)
    crop=source.crop(bounds);source.close()
    padding=max(1,math.ceil(min(crop.size)/6))
    prepared=Image.new('RGB',(crop.width+2*padding,crop.height+2*padding),'white')
    prepared.paste(crop,(padding,padding));crop.close()
    return prepared,dict(method='source_cell_exact_white_fit',blank=False,
        original_size=list(image.size),ink_bbox=list(bounds),padding=padding,
        prepared_size=list(prepared.size))


def source_colored_field(image, metadata):
    """Prove a large filled triangular/circular graphic from source pixels.

    A saturated convex field with a separate dark outline is a graphic
    background. Thin colored formula glyphs, square text highlights and
    small mathematical symbols do not satisfy these source conditions.
    """
    box = metadata.get('source_bbox')
    if not box or min(box[2]-box[0],box[3]-box[1]) < 20:
        return None
    import cv2
    import numpy as np
    probe=image.convert('RGB')
    probe.thumbnail((512,512),Image.Resampling.LANCZOS)
    array=np.asarray(probe).astype(np.int16)
    probe.close()
    chroma=array.max(axis=2)-array.min(axis=2)
    mask=(chroma>70)&(array.max(axis=2)>140)
    y,x=np.where(mask)
    if len(x)<mask.size*.20:
        return None
    hull=cv2.convexHull(np.column_stack((x,y)).astype(np.int32))
    area=cv2.contourArea(hull)
    if not area or len(x)/area < .60:
        return None
    perimeter=cv2.arcLength(hull,True)
    vertices=len(cv2.approxPolyDP(hull,.025*perimeter,True))
    circularity=4*math.pi*area/max(perimeter*perimeter,1)
    shape='triangle' if vertices==3 else 'circle' if vertices>=7 and circularity>.88 else None
    if not shape:
        return None
    field=np.zeros(mask.shape,np.uint8)
    cv2.fillConvexPoly(field,hull,1)
    border_radius=max(4,round(min(mask.shape)*.035))
    kernel=np.ones((2*border_radius+1,2*border_radius+1),np.uint8)
    ring=cv2.morphologyEx(field,cv2.MORPH_GRADIENT,kernel)>0
    dark=(chroma<40)&(array.max(axis=2)<170)
    outline=float((dark&ring).sum())/max(1,int(ring.sum()))
    if outline < .015:
        return None
    return dict(method='source_filled_color_field_with_dark_outline',shape=shape,
                colored_fraction=round(len(x)/mask.size,4),convex_fill=round(len(x)/area,4),
                outline_fraction=round(outline,4),source_bbox=[float(v) for v in box])


def _layout_predictor(manager):
    # Import only after backend_getter has configured Surya's environment.
    from surya.layout import LayoutPredictor
    return LayoutPredictor(manager)


def _generate(batch, backend, timeout, diagnostic=None):
    from surya.inference.backends.openai_client import chat_completions_batch
    from docling_backend_health import request_with_backend_recovery
    if backend.handle is None or backend._client is None:
        backend.start()
    from docling_pdf_ocr import recovering_generate
    def request(items):
        from surya.settings import settings
        tokens = max(item.max_tokens or settings.SURYA_MAX_TOKENS_LAYOUT for item in items)
        seconds = max(90, timeout or 0, math.ceil(tokens/80)+15)
        return request_with_backend_recovery(backend, lambda: chat_completions_batch(
            items, client=backend._client.with_options(max_retries=0, timeout=seconds),
            model_name=backend.handle.model_name, timeout=seconds, max_workers=1,
            max_retries=0, request_logprobs_default=False), diagnostic)
    return recovering_generate(batch, request, diagnostic or (lambda *a,**kw: None))


class _BoundedManager:
    """The small manager interface LayoutPredictor needs, without model ownership."""

    def __init__(self, backend, timeout, diagnostic=None):
        self.backend = backend
        self.timeout = timeout
        self.diagnostic = diagnostic

    def generate(self, batch):
        return _generate(batch, self.backend, self.timeout, self.diagnostic)


class SuryaAssetLayout:
    """Classify one complete source image, reusing the caller's local backend.

    The returned bboxes use ORIGINAL image coordinates. ``math`` and ``graphics``
    may both be true for mixed content; callers must not discard a figure just
    because one of its labels is a mathematical expression. An error is unknown
    classification and leaves the caller's existing recognition path available.
    """

    def __init__(self, backend_getter, diagnostic=None, timeout=90):
        self.backend_getter = backend_getter
        self.diagnostic = diagnostic or (lambda *args, **kwargs: None)
        self.timeout = timeout

    def __call__(self, image, metadata=None):
        metadata = metadata or {}
        result = {'labels': [], 'bboxes': [], 'math': False, 'graphics': False,
                  'error': False, 'image_size': list(image.size)}
        try:
            original = opaque_image(image)
            proof=source_colored_field(original,metadata)
            if proof:
                result.update(labels=['Figure'],bboxes=[dict(label='Figure',raw_label='source_geometric_field',
                    bbox=[0,0,original.width,original.height])],graphics=True,source_proof=proof)
                self.diagnostic('ASSET_LAYOUT_SOURCE_COLORED_FIELD',media=metadata.get('media_name'),**proof)
                return result
            # Context outside the original asset protects glyphs touching its
            # edges. Never crop the original down to a model's estimated box.
            margin = max(4, min(16, round(original.height * .04)))
            context = Image.new('RGB', (original.width + 2 * margin,
                                        original.height + 2 * margin), 'white')
            context.paste(original, (margin, margin))
            self.diagnostic('ASSET_LAYOUT_REQUEST', media=metadata.get('media_name'),
                            image_size=list(original.size), padding=margin, timeout=self.timeout)
            backend = self.backend_getter()
            predictor = _layout_predictor(_BoundedManager(backend, self.timeout, self.diagnostic))
            predictions = predictor([context])
            if not predictions or getattr(predictions[0], 'error', False):
                raise RuntimeError('Surya returned no usable asset layout')
            for box in predictions[0].bboxes:
                label = str(box.label)
                raw_label = str(getattr(box, 'raw_label', label))
                coordinates = getattr(box, 'bbox', None)
                if coordinates is None or len(coordinates) != 4:
                    continue
                source_box = padded_bbox([value - margin for value in coordinates], original.size, 0)
                if source_box is None:
                    continue
                canonical = ('Equation' if label in MATH_LABELS or raw_label in MATH_LABELS else
                             'Figure' if label in GRAPHIC_LABELS or raw_label in GRAPHIC_LABELS else label)
                result['bboxes'].append({'label': canonical, 'raw_label': raw_label,
                                         'bbox': list(source_box)})
                if canonical not in result['labels']:
                    result['labels'].append(canonical)
            result['math'] = 'Equation' in result['labels']
            result['graphics'] = 'Figure' in result['labels']
            self.diagnostic('ASSET_LAYOUT_RESULT', media=metadata.get('media_name'), **result)
        except Exception as exc:
            result['error'] = True
            result['reason'] = str(exc)[:400]
            self.diagnostic('ASSET_LAYOUT_ERROR', media=metadata.get('media_name'), error=result['reason'])
        return result
