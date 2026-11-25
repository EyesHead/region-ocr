"""Offline Russian OCR and positioned line and word readings for PDF scans."""
from __future__ import annotations

import ctypes
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

from PIL import Image

from docling_pdf_orientation import ORIENTATION_CONFIDENCE, PageOrientationClassifier
from docling_pdf_runtime import FAST_OCR_MAX_SIDE


_ROOT = Path(__file__).resolve().parents[1]
_REC_MODEL = _ROOT / "models" / "rapidocr" / "PP-OCRv6_medium_rec_cyrillic.onnx"
_REC_SHA256 = "997e543cc73c2f99935fce99a79a74a5165602c816e15cf2b3e923e5a3420ac0"
_LOCK = Lock()
_LOG = logging.getLogger(__name__)
_cuda_failed = False
_PAGE_READS = OrderedDict()
_PAGE_CACHE_SIZE = 256
_UNREAD = object()
_OCR_ERROR = "Не удалось распознать страницу {} локальной моделью RapidOCR"
_MIN_PAGE_CONFIDENCE = .85


@lru_cache(maxsize=1)
def _cuda_libraries():
    """Use the optional CUDA 12.8 distribution's DLLs without importing torch."""
    import onnxruntime as ort

    if os.name != "nt" or "CUDAExecutionProvider" not in ort.get_available_providers():
        return ()
    folder = Path(ort.__file__).resolve().parent.parent / "torch" / "lib"
    runtime = [folder / name for name in ("nvrtc-builtins64_128.dll", "nvrtc64_120_0.dll")]
    if not all(path.is_file() for path in (*runtime, folder / "cudnn64_9.dll")):
        return ()
    try:
        os.environ["CUDA_CACHE_DISABLE"] = "1"
        search = os.add_dll_directory(str(folder))
        ort.preload_dlls(directory=str(folder))
        # cuDNN loads these through the Windows loader on the first inference.
        return (search, *(ctypes.WinDLL(str(path)) for path in runtime))
    except (OSError, RuntimeError) as exc:
        _LOG.info("CUDA недоступна для быстрого OCR: %s", exc)
        return ()


@lru_cache(maxsize=2)
def _rapid_reader(use_cuda=False):
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    import rapidocr

    detector = Path(rapidocr.__file__).parent / "models" / "PP-OCRv6_det_small.onnx"
    if not detector.is_file():
        raise FileNotFoundError(detector)
    with _REC_MODEL.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != _REC_SHA256:
            raise ValueError("Контрольная сумма русской модели OCR не совпадает")

    # Explicit paths and the ONNX model's embedded alphabet keep inference offline.
    return RapidOCR(params={
        "Det.model_path": str(detector),
        "Rec.model_path": str(_REC_MODEL),
        "Rec.lang_type": LangRec.CYRILLIC,
        "Rec.model_type": ModelType.MEDIUM,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.rec_img_shape": [3, 48, 640],
        "Global.use_cls": False,
        "Global.max_side_len": FAST_OCR_MAX_SIDE,
        "Global.log_level": "warning",
        "EngineConfig.onnxruntime.use_cuda": use_cuda,
        "EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search": "HEURISTIC",
        "EngineConfig.onnxruntime.cuda_ep_cfg.arena_extend_strategy": "kSameAsRequested",
        "EngineConfig.onnxruntime.intra_op_num_threads": min(4, os.cpu_count() or 1),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })


def _infer(image, word_boxes):
    global _cuda_failed
    # RapidOCR mutates per-call settings. The lock also protects first use and
    # the switch to CPU if the optional accelerator fails within a document.
    with _LOCK:
        if not _cuda_failed and _cuda_libraries():
            try:
                return _rapid_reader(True)(image, return_word_box=word_boxes)
            except Exception as exc:
                _cuda_failed = True
                _LOG.warning("Быстрый OCR переключён на CPU: %s", exc)
        return _rapid_reader(False)(image, return_word_box=word_boxes)


def _read(image, page_no, word_boxes=False):
    try:
        return _infer(image, word_boxes)
    except Exception as exc:
        # ORT provider errors derive directly from Exception.
        raise RuntimeError(_OCR_ERROR.format(page_no)) from exc


def _plain(lines):
    return "\n".join(line.strip() for line in lines if line.strip())


def recognize_page(image: Image.Image, page_no: int) -> str:
    """Return OCR lines without computing word polygons."""
    return _plain(getattr(_read(image, page_no), 'txts', None) or ())


def _line_boxes(result, lines, size, line_ids=None):
    """RapidOCR reports line quadrilaterals even with word-level recognition disabled."""
    polygons = getattr(result, 'boxes', None)
    scores = getattr(result, 'scores', None)
    if polygons is None or len(polygons) != len(lines):
        return []
    width, height = size
    boxes = []
    for index, (text, polygon) in enumerate(zip(lines, polygons)):
        if not text.strip() or polygon is None or len(polygon) < 3:
            continue
        xs, ys = zip(*polygon)
        left, top = max(0., float(min(xs))), max(0., float(min(ys)))
        right, bottom = min(width, float(max(xs))), min(height, float(max(ys)))
        confidence = float(scores[index]) if scores is not None and index < len(scores) else 1.
        if right > left and bottom > top and math.isfinite(confidence):
            boxes.append((left, top, right, bottom, text, confidence))
            if line_ids is not None:
                line_ids.append(index)
    return boxes


def _positioned_read(image, page_no, key):
    cached = _PAGE_READS.get(key)
    if cached is not None:
        _PAGE_READS.move_to_end(key)
        return cached
    result = _read(image, page_no, word_boxes=True)
    lines = tuple(getattr(result, 'txts', None) or ())
    line_ids = []
    positioned = tuple(_line_boxes(result, lines, image.size, line_ids))
    words = []
    word_rows = getattr(result, 'word_results', None)
    if word_rows is not None and len(lines) == len(word_rows):
        for row in word_rows:
            converted = []
            for text, _, polygon in row or ():
                if not text or polygon is None or len(polygon) < 2:
                    continue
                x, y = zip(*polygon)
                left, top = float(min(x)), float(min(y))
                converted.append((text, left, top, float(max(x))-left, float(max(y))-top))
            words.append(tuple(converted))
    reading = lines, positioned, tuple(words), tuple(line_ids), {}
    if lines:
        _PAGE_READS[key] = reading
        if len(_PAGE_READS) > _PAGE_CACHE_SIZE:
            _PAGE_READS.popitem(last=False)
    return reading


def _image_key(image):
    return image.mode, image.size, hashlib.sha256(image.tobytes()).digest()


@dataclass(frozen=True)
class _TextEvidence:
    confidence: float
    characters: int
    horizontal: int
    vertical: int
    vertical_lines: int
    plausible: bool

    @property
    def sideways(self):
        return (self.vertical >= 8 and self.vertical > 2 * self.horizontal
                and (self.vertical_lines >= 2 or self.horizontal < 3))

    def improves(self, original, *, trust_axis):
        original_text = original.characters if original.plausible else min(16, original.characters)
        if (not self.plausible or self.confidence < .65 or self.horizontal < 3
                or self.horizontal <= self.vertical or self.characters < .65 * original_text):
            return False
        if trust_axis and original.sideways:
            # RapidOCR already turns individual tall strips; their score can be
            # high while the page's table rows and reading order are sideways.
            return self.confidence >= original.confidence - .08
        return (not original.plausible or self.confidence >= original.confidence + .05
                or (self.characters >= max(original.characters + 4, 1.25 * original.characters)
                    and self.confidence >= original.confidence - .02))


def _text_evidence(reading):
    from local_web.fast_pdf_text import usable_text

    lines, boxes = reading[:2]
    text = ' '.join(lines)
    horizontal = vertical = vertical_lines = 0
    for left, top, right, bottom, line, _ in boxes:
        # Bound each label's vote so one long rotated margin caption cannot
        # outweigh an entire upright table of short fields.
        weight = min(40, sum(ch.isalnum() for ch in line))
        width, height = right - left, bottom - top
        if width > 1.5 * height:
            horizontal += weight
        elif height > 1.5 * width:
            vertical += weight
            if weight:
                vertical_lines += 1
    confidence = sum(box[5] for box in boxes) / len(boxes) if boxes else 0.
    return _TextEvidence(confidence, sum(ch.isalnum() for ch in text), horizontal, vertical, vertical_lines,
                         usable_text(text, min_text_length=1))


@lru_cache(maxsize=1)
def _balanced_classifier():
    try:
        return PageOrientationClassifier(allow_download=False)
    except Exception as error:
        _LOG.info('Модель ориентации недоступна (%s)', error)
        return None


def _orientation_hint(image):
    classifier = _balanced_classifier()
    if classifier is not None:
        try:
            angle, prediction = classifier.predict(image)
            return angle, prediction['confidence']
        except Exception as error:
            _LOG.info('Не удалось определить ориентацию страницы (%s)', error)
    return None, 0.


def balanced_page_orientation(image: Image.Image, page_no: int, *, check_cancel=None):
    """Return a clockwise page correction and its OCR read in the corrected pixel space."""
    if check_cancel:
        check_cancel()
    key = _image_key(image)
    try:
        initial = _positioned_read(image, page_no, key)
    except RuntimeError:
        initial = None
    if check_cancel:
        check_cancel()
    if initial is None:
        return 0, None
    base = _text_evidence(initial)
    if base.plausible and base.confidence >= _MIN_PAGE_CONFIDENCE and not base.sideways:
        return 0, initial

    hint, probability = _orientation_hint(image)
    trusted = probability >= ORIENTATION_CONFIDENCE
    original_axis = trusted and hint in (0, 180)
    # The page classifier sees the center crop. A table near an edge can put
    # several consistently vertical lines outside that crop.
    trust_axis = not original_axis or (base.vertical_lines >= 4 and base.vertical >= 24)
    angles = (270, 90) if hint == 270 else (90, 270)
    candidates = []
    for angle in angles:
        if check_cancel:
            check_cancel()
        operation = Image.Transpose.ROTATE_270 if angle == 90 else Image.Transpose.ROTATE_90
        with image.transpose(operation) as rotated:
            try:
                reading = _positioned_read(rotated, page_no, (key, angle))
            except RuntimeError:
                continue
        if check_cancel:
            check_cancel()
        evidence = _text_evidence(reading)
        if original_axis and base.horizontal > 2 * base.vertical and evidence.sideways:
            return 0, initial
        if not evidence.improves(base, trust_axis=trust_axis):
            continue
        if angle == hint and trusted and evidence.confidence >= _MIN_PAGE_CONFIDENCE:
            if (not base.plausible or (evidence.confidence >= base.confidence - .02
                                       and evidence.characters >= .85 * base.characters)):
                return angle, reading
        candidates.append((angle, reading, evidence))

    if check_cancel:
        check_cancel()
    if len(candidates) == 2:
        # Opposite turns both produce horizontal strips. The Cyrillic recognizer
        # can also read upside-down print, so retain the original on a close tie.
        most_text = max(item[2].characters for item in candidates)

        def strength(item):
            evidence = item[2]
            return evidence.confidence + .20 * evidence.characters / most_text

        candidates.sort(key=strength, reverse=True)
        if strength(candidates[0]) - strength(candidates[1]) < .02:
            for angle, reading, _ in candidates:
                if angle == hint and probability >= .70:
                    return angle, reading
            return 0, initial
    if candidates:
        angle, reading, _ = candidates[0]
        return angle, reading
    return 0, initial


def recognize_page_with_layout(image: Image.Image, page_no: int, *, recover_merged_tables=False,
                               balanced_grid=True, physical_grids=None, physical_grid_reasons=(),
                               positioned_read=_UNREAD) -> dict:
    """Retain the word polygons and line IDs of the OCR pass used for layout."""
    if positioned_read is _UNREAD:
        positioned_read = _positioned_read(image, page_no, _image_key(image))
    elif positioned_read is None:
        raise RuntimeError(_OCR_ERROR.format(page_no))
    lines, positioned, words, line_ids, layouts = positioned_read
    use_physical = recover_merged_tables and balanced_grid
    layout_key = bool(recover_merged_tables), bool(use_physical)
    # An explicit set of grids belongs to this call. Keep sharing the OCR read
    # even if another caller supplies different geometry for the same pixels.
    cache_layout = not physical_grids and (not use_physical or (physical_grids is None and not physical_grid_reasons))
    if cache_layout and layout_key in layouts:
        return layouts[layout_key]
    plain = {'blocks': [{'text': line, 'line_id': index} for index,line in enumerate(lines) if line.strip()],
             'words': positioned, 'line_ids': line_ids, 'source_lines': lines}
    if words:
        plain['word_rows'] = tuple(words[index] for index in line_ids)
    line_boxes = {index: box[:4] for index, box in zip(line_ids, positioned, strict=True)}
    grids = ()
    reasons = list(physical_grid_reasons) if use_physical else []
    if use_physical:
        try:
            if physical_grids is None:
                from local_web.balanced_table_grid import raster_grids

                grids = raster_grids(image, diagnostics=reasons)
            else:
                grids = physical_grids
            if not grids and not reasons:
                reasons.append('no_physical_grid')
        except Exception as error:
            cache_layout = False
            reasons.append(f'physical_grid_error({type(error).__name__})')
            _LOG.warning('Не удалось определить сетку таблицы на странице %s (%s)', page_no, error)
    try:
        if recover_merged_tables:
            from local_web.fast_ruled_tables import legacy_scan_grids, scan_grid_blocks

            legacy = ()
            if not grids or reasons:
                try:
                    legacy = legacy_scan_grids(image, covered=[grid.bbox for grid in grids])
                except Exception as error:
                    cache_layout = False
                    reasons.append(f'legacy_grid_error({type(error).__name__})')
                    _LOG.warning('Резервный поиск таблиц на странице %s завершился ошибкой (%s)', page_no, error)
            if grids or legacy:
                plain['blocks'] = scan_grid_blocks(lines, words, line_boxes, grids, legacy)
        else:
            from local_web.fast_scan_tables import find_grids, table_blocks

            blocks = table_blocks(lines, words, find_grids(image), line_boxes=line_boxes, physical_grids=physical_grids)
            if blocks:
                plain['blocks'] = blocks['blocks']
    except Exception as error:
        cache_layout = False
        reasons.append(f'table_layout_error({type(error).__name__})')
        _LOG.warning('Не удалось собрать строки OCR в таблицу на странице %s (%s)', page_no, error)
    if use_physical and reasons:
        plain['balanced_grid_reason'] = ', '.join(dict.fromkeys(reasons))
    if cache_layout:
        layouts[layout_key] = plain
    return plain
