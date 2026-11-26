"""Read long detector crops in shorter pieces without changing the page layout."""

from bisect import bisect_left
from math import ceil

import cv2
import numpy as np
from rapidocr import RapidOCR
from rapidocr.ch_ppocr_rec.typings import WordInfo


def _word_boundaries(image, normalized_height, normalized_width):
    height, width = image.shape[:2]
    if width * normalized_height <= height * normalized_width:
        return ()

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    occupied = ink.any(axis=0)
    runs = np.flatnonzero(np.diff(np.r_[True, occupied, True])).reshape(-1, 2)
    gaps = [(left + right) // 2 for left, right in runs
            if left and right < width and right - left >= max(2, .20 * height)]
    # In tracked headings every letter can look like a word. Keep the edges
    # of such a run available, without inserting a space in its middle.
    gaps = [gap for i, gap in enumerate(gaps)
            if i == 0 or i == len(gaps)-1 or gap-gaps[i-1] >= height or gaps[i+1]-gap >= height]
    if not gaps:
        return ()

    # Aim for 256 input pixels per piece at the recognizer's line height.
    count = ceil(width * normalized_height / (256 * height))
    cuts = set()
    for i in range(1, count):
        target = width * i / count
        pos = bisect_left(gaps, target)
        nearby = gaps[max(0, pos - 1):pos + 1]
        cuts.add(min(nearby, key=lambda x: abs(x - target)))
    return (0, *sorted(cuts), width)


def _line_word_info(width, spans, results):
    # RapidOCR stores character centers in CTC columns relative to each crop.
    # Use pixels as the shared column unit before it projects onto the page.
    merged = WordInfo(line_txt_len=width)
    for (left, right), info in zip(spans, results, strict=True):
        scale = (right - left) / info.line_txt_len
        merged.words.extend(info.words)
        merged.word_types.extend(info.word_types)
        merged.confs.extend(info.confs)
        for cols in info.word_cols:
            merged.word_cols.append([left + (col + .5) * scale - .5 for col in cols])
    return merged


class LineAwareRapidOCR(RapidOCR):
    def recognize_txt(self, img):
        original = super().recognize_txt(img)
        _, height, width = self.text_rec.rec_image_shape
        pieces, groups = [], []
        for line, image in enumerate(img):
            cuts = _word_boundaries(image, height, width)
            if cuts:
                spans = list(zip(cuts, cuts[1:]))
                groups.append((line, len(pieces), spans))
                pieces.extend(image[:, left:right] for left, right in spans)
        if not pieces:
            return original

        retry = super().recognize_txt(pieces)
        texts, scores = list(original.txts), list(original.scores)
        words = list(original.word_results)
        for line, start, spans in groups:
            end = start + len(spans)
            parts = [text.strip() for text in retry.txts[start:end]]
            if not all(parts):
                continue
            text = ' '.join(parts)
            weights = [len(part) for part in parts]
            score = sum(n * conf for n, conf in zip(weights, retry.scores[start:end], strict=True)) / sum(weights)
            if text == texts[line] or score <= scores[line]:
                continue
            if sum(ch.isalnum() for ch in text) < sum(ch.isalnum() for ch in texts[line]):
                continue
            if self.return_word_box:
                words[line] = _line_word_info(img[line].shape[1], spans, retry.word_results[start:end])
            texts[line], scores[line] = text, score
        original.txts, original.scores, original.word_results = tuple(texts), tuple(scores), tuple(words)
        original.elapse += retry.elapse
        return original
