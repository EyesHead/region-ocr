"""Read bounded atlases of source text crops with Surya's positioned OCR."""

from dataclasses import dataclass
from math import ceil, floor, isfinite
from operator import index


ATLAS_WIDTH = 1536
ATLAS_HEIGHT = 1200
ATLAS_REGIONS = 24
ATLAS_GUTTER = 72
ATLAS_TOKEN_LIMIT = 4096


@dataclass(frozen=True)
class _Placement:
    region: int
    source: tuple[int, int, int, int]
    atlas: tuple[int, int, int, int]


def _intersect(left, right):
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def _pixels(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _plan_atlases(boxes, image_size):
    border = ATLAS_GUTTER // 2
    width, height = image_size
    sources = []
    for box in boxes:
        try:
            left, top, right, bottom = source = tuple(map(index, box))
        except (TypeError, ValueError):
            return None
        if (not (0 <= left < right <= width and 0 <= top < bottom <= height)
                or right - left + 2 * border > ATLAS_WIDTH
                or bottom - top + 2 * border > ATLAS_HEIGHT
                or any(_intersect(source, previous) for previous in sources)):
            return None
        sources.append(source)

    # Surya can join crops on a shared baseline into one paragraph. Use a
    # vertical strip so every source region has a separate reading band.
    plans, slots = [], []
    y = border
    for region, source in enumerate(sources):
        crop_width, crop_height = source[2] - source[0], source[3] - source[1]
        if y + crop_height + border > ATLAS_HEIGHT or len(slots) == ATLAS_REGIONS:
            plans.append(slots)
            slots = []
            y = border
        slots.append(_Placement(region, source, (border, y, border + crop_width, y + crop_height)))
        y += crop_height + ATLAS_GUTTER
    if slots:
        plans.append(slots)
    return plans


def _map_owner(box, placements):
    hits = [(slot, clipped) for slot in placements if (clipped := _intersect(box, slot.atlas))]
    if len(hits) != 1:
        return None
    slot, clipped = hits[0]
    if any(abs(raw - inside) > ATLAS_GUTTER // 2 for raw, inside in zip(box, clipped)):
        return None
    return slot, clipped


def _project_blocks(output, atlas, placements, bbox_scale):
    import numpy as np
    from docling_surya_ocr.html_text import html_to_text
    from docling_surya_ocr.normalization import NormalizedOcrBlock
    from surya.inference.parsers import denorm_bbox, parse_full_page_html
    from surya.layout.label import LAYOUT_PRED_RELABEL, TEXT_LABELS
    from surya.recognition import _detect_repeat_loop

    if not output.raw or _detect_repeat_loop(output.raw):
        return None
    try:
        parsed = parse_full_page_html(output.raw)
    except (TypeError, ValueError, KeyError):
        return None
    if not parsed or any(item.truncated for item in parsed):
        return None

    readings = {slot.region: [] for slot in placements}
    positioned = {slot.region: [] for slot in placements}
    confidence = output.mean_token_prob
    if confidence is None:
        confidence = 1.0
    elif not 0 <= confidence <= 1:
        return None
    allowed = TEXT_LABELS | {"ListGroup", "Equation", "ChemicalBlock"}
    for item in parsed:
        a, b, c, d = item.bbox
        if not (all(isfinite(value) for value in item.bbox)
                and 0 <= a < c <= bbox_scale and 0 <= b < d <= bbox_scale):
            return None
        box = denorm_bbox(item.bbox, atlas.width, atlas.height, bbox_scale)
        owner = _map_owner(box, placements)
        label = LAYOUT_PRED_RELABEL.get(item.label, item.label)
        content = html_to_text(item.html)
        if owner is None or label not in allowed or not content:
            return None
        slot, clipped = owner
        previous_boxes = positioned[slot.region]
        if any((overlap := _intersect(clipped, earlier))
               and _pixels(overlap) > .8 * min(_pixels(clipped), _pixels(earlier))
               for earlier in previous_boxes):
            return None
        previous_boxes.append(clipped)
        dx = slot.source[0] - slot.atlas[0]
        dy = slot.source[1] - slot.atlas[1]
        left, top, right, bottom = mapped = (clipped[0] + dx, clipped[1] + dy,
                                             clipped[2] + dx, clipped[3] + dy)
        polygon = ((left, top), (right, top), (right, bottom), (left, bottom))
        blocks = readings[slot.region]
        blocks.append(NormalizedOcrBlock(text=content, bbox=mapped, html=item.html,
                                        polygon=polygon, confidence=confidence, label=label,
                                        raw_label=label, reading_order=len(blocks)))

    with atlas.convert("L") as gray:
        pixels = np.asarray(gray)
    for slot in placements:
        left, top, right, bottom = slot.atlas
        if not _ink_is_covered(pixels[top:bottom, left:right], slot.atlas,
                               positioned[slot.region]):
            return None
    return readings


def _ink_is_covered(pixels, box, blocks):
    import numpy as np

    if not blocks:
        return False
    # A small tolerance absorbs coordinate quantization and tight OCR boxes.
    ink = pixels < min(220, float(np.median(pixels)) - 20)
    covered = np.zeros(ink.shape, dtype=bool)
    margin = 4
    for left, top, right, bottom in blocks:
        x0, y0 = max(0, floor(left - box[0]) - margin), max(0, floor(top - box[1]) - margin)
        x1, y1 = min(ink.shape[1], ceil(right - box[0]) + margin), min(ink.shape[0], ceil(bottom - box[1]) + margin)
        if not ink[y0:y1, x0:x1].any():
            return False
        covered[y0:y1, x0:x1] = True

    missing = ink & ~covered
    if np.count_nonzero(missing) > max(3, .04 * np.count_nonzero(ink)):
        return False
    row_ink = ink.sum(axis=1)
    unassigned = (row_ink >= 3) & (missing.sum(axis=1) == row_ink)
    return not np.any(unassigned[2:] & unassigned[1:-1] & unassigned[:-2])


def recognize_cell_regions(image, boxes, backend, *, page_no, diagnostic):
    """Return positioned blocks per source box, or None for whole-route fallback."""
    from PIL import Image
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_HIGH_ACCURACY_BBOX
    from surya.settings import settings

    plans = _plan_atlases(boxes, image.size)
    if plans is None:
        diagnostic("OCR_CELL_ATLAS_FALLBACK", page=page_no, reason="invalid_source_regions")
        return None

    border = ATLAS_GUTTER // 2
    token_limit = min(ATLAS_TOKEN_LIMIT, settings.SURYA_MAX_TOKENS_FULL_PAGE)
    result = []
    for atlas_index, placements in enumerate(plans, 1):
        size = (max(slot.atlas[2] for slot in placements) + border,
                max(slot.atlas[3] for slot in placements) + border)
        with Image.new("RGB", size, "white") as atlas:
            for slot in placements:
                with image.crop(slot.source) as crop:
                    atlas.paste(crop, slot.atlas[:2])
            request = BatchInputItem(image=atlas, prompt_type=PROMPT_TYPE_HIGH_ACCURACY_BBOX,
                                     max_tokens=token_limit, metadata=dict(page=page_no,
                                         purpose="physical_cell_atlas", atlas=atlas_index))
            outputs = backend.generate([request])
            output = outputs[0] if len(outputs) == 1 else None
            readings = None
            if (output is not None and not output.error and output.finish_reason != "length"
                    and 0 <= output.token_count < token_limit):
                readings = _project_blocks(output, atlas, placements, settings.BBOX_SCALE)
        if readings is None:
            diagnostic("OCR_CELL_ATLAS_FALLBACK", page=page_no, atlas=atlas_index,
                       reason="unconfirmed_source_coverage", regions=len(placements))
            return None
        result.extend(tuple(readings[slot.region]) for slot in placements)

    diagnostic("OCR_CELL_ATLAS_COMPLETE", page=page_no, atlases=len(plans), regions=len(result))
    return tuple(result)
