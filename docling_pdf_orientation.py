"""Classify printed page orientation with the shared four-angle CPU model."""

import hashlib
import os
from pathlib import Path
import time
from uuid import uuid4


ORIENTATION_CONFIDENCE = .90
ORIENTATION_MODEL_REVISION = "7330ab7039123e46af2dc03154b9969aa412c61d"
ORIENTATION_MODEL_SHA256 = "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92"


class PageOrientationClassifier:
    """Official PP-LCNet four-angle model using the existing CPU ONNX runtime.

    Preprocessing follows the pinned model's inference.yml: RGB, short side
    256, center crop 224, ImageNet normalization, NCHW. No Paddle dependency.
    https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx
    """

    @staticmethod
    def model_path(*, allow_download=True):
        from platformdirs import user_cache_path

        bundled = Path(__file__).resolve().parent / 'models' / 'orientation' / 'PP-LCNet_x1_0_doc_ori.onnx'
        path = bundled if bundled.is_file() else user_cache_path("docling-surya", appauthor=False) / "orientation" / "PP-LCNet_x1_0_doc_ori.onnx"
        if path.is_file():
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == ORIENTATION_MODEL_SHA256:
                    return path
        if not allow_download:
            raise OSError("Локальная модель ориентации отсутствует или не прошла проверку SHA-256")

        from urllib.request import Request, urlopen

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        url = ("https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx/resolve/"
               f"{ORIENTATION_MODEL_REVISION}/inference.onnx")
        print("Downloading page orientation model PP-LCNet (6.8 MB)...", flush=True)
        deadline, size, digest = time.monotonic() + 30, 0, hashlib.sha256()
        try:
            request = Request(url, headers={"User-Agent": "convert-docling-orientation"})
            with urlopen(request, timeout=10) as response, temporary.open("wb") as output:
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > 8 * 1024**2 or time.monotonic() > deadline:
                        raise RuntimeError("Page orientation model download exceeded its time/size limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != ORIENTATION_MODEL_SHA256:
                raise RuntimeError("Page orientation model checksum mismatch")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def __init__(self, *, allow_download=True):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = min(2, os.cpu_count() or 1)
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Reserve the GPU for the document models; this 7 MB classifier is
        # inexpensive on CPU and works with the same backend on every platform.
        self.session = ort.InferenceSession(str(self.model_path(allow_download=allow_download)), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if len(inputs) != 1 or inputs[0].shape[1:] != [3, 224, 224] or outputs[0].shape[-1] != 4:
            raise RuntimeError("Unexpected page orientation model input/output shape")
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name

    def predict(self, image):
        import cv2
        import numpy as np

        array = np.asarray(image.convert("RGB"))
        height, width = array.shape[:2]
        # A very long receipt/PDF must not allocate an enormous resized strip.
        # Only its center reaches the official 224px crop in either case.
        if height > width * 4:
            top = (height - width * 4) // 2
            array = array[top:top + width * 4]
        elif width > height * 4:
            left = (width - height * 4) // 2
            array = array[:, left:left + height * 4]
        height, width = array.shape[:2]
        scale = 256 / min(height, width)
        array = cv2.resize(array, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_LINEAR)
        height, width = array.shape[:2]
        top, left = (height - 224) // 2, (width - 224) // 2
        array = array[top:top + 224, left:left + 224].astype(np.float32)
        array *= np.float32(1 / 255)
        array -= np.array([.485, .456, .406], dtype=np.float32)
        array /= np.array([.229, .224, .225], dtype=np.float32)
        batch = np.ascontiguousarray(array.transpose(2, 0, 1)[None])
        scores = np.asarray(self.session.run([self.output_name], {self.input_name: batch})[0])
        # The pinned ONNX graph already contains softmax; applying it again
        # would destroy the confidence threshold.
        if (scores.shape != (1, 4) or not np.isfinite(scores).all()
                or np.any(scores < 0) or np.any(scores > 1) or abs(float(scores.sum()) - 1) > .001):
            raise RuntimeError("Invalid page orientation probabilities")
        index = int(scores[0].argmax())
        label = (0, 90, 180, 270)[index]
        # Paddle corrects this label counterclockwise. PDF /Rotate is clockwise.
        return (-label) % 360, {"classification_label": label, "confidence": round(float(scores[0, index]), 6),
                               "probabilities": {str(angle): round(float(score), 6)
                                                 for angle, score in zip((0, 90, 180, 270), scores[0])}}
