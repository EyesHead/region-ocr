"""Offline Russian OCR for the fast converter. One inference pass per raster."""
from __future__ import annotations

import ctypes
import hashlib
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

from PIL import Image


_ROOT = Path(__file__).resolve().parents[1]
_REC_MODEL = _ROOT / "models" / "rapidocr" / "eslav_PP-OCRv5_rec_mobile.onnx"
_REC_SHA256 = "08705d6721849b1347d26187f15a5e362c431963a2a62bfff4feac578c489aab"
_LOCK = Lock()
_LOG = logging.getLogger(__name__)
_cuda_failed = False


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
        "Rec.lang_type": LangRec.ESLAV,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Global.use_cls": False,
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
        raise RuntimeError(f"Не удалось распознать страницу {page_no} локальной моделью RapidOCR") from exc


def _plain(lines):
    return "\n".join(line.strip() for line in lines if line.strip())


def recognize_page(image: Image.Image, page_no: int) -> str:
    """Return OCR lines without computing word polygons."""
    return _plain(_read(image, page_no).txts or ())


def _line_boxes(result, lines, size):
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
    return boxes


def recognize_page_with_layout(image: Image.Image, page_no: int) -> dict:
    """Use one page-level OCR pass, requesting word polygons only for ruled grids."""
    if __package__:
        from .fast_scan_tables import find_grids, table_blocks
    else:
        from fast_scan_tables import find_grids, table_blocks

    grids = find_grids(image)
    result = _read(image, page_no, word_boxes=bool(grids))
    lines = result.txts or ()
    positioned = _line_boxes(result, lines, image.size)
    plain = {"blocks": [{"text": _plain(lines)}], "words": positioned}
    if grids and result.word_results and len(lines) == len(result.word_results):
        words = []
        for row in result.word_results:
            if not row:
                return plain
            converted = []
            for text, _, polygon in row:
                x, y = zip(*polygon)
                converted.append((text, min(x), min(y), max(x)-min(x), max(y)-min(y)))
            words.append(converted)
        blocks = table_blocks(lines, words, grids)
        if blocks:
            blocks["words"] = positioned
            return blocks
    return plain
