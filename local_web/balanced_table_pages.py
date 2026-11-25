"""Prepare bounded, ordered page geometry for the balanced reader."""

from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from functools import cached_property
import os

from docling_pdf_deskew import page_skew, source_bbox
from docling_pdf_runtime import FAST_OCR_MAX_SIDE, PDF_OCR_SCALE
from local_web.fast_pdf_geometry import _page_geometry, _render, _vector_segments
from local_web.fast_pdf_text import covers_scanned_body, page_text, raster_table_hint


def grid_enabled():
    return os.environ.get('REGION_OCR_BALANCED_TABLES', '1').strip().lower() not in {'0', 'false', 'off', 'no'}


@dataclass
class PageContent:
    page: object
    width: float
    height: float
    objects: dict
    paths: list | None
    rules: object
    text_layer: dict | None = None
    residual: float = 0.
    raster: object = None
    blank: bool = False
    source: str = ''
    grids: tuple = ()
    horizontal_rules: tuple = ()
    reasons: list = field(default_factory=list)
    turn: int = 0
    source_view: dict | None = None
    positioned_read: object = None
    route_reason: str = 'text_reader_unavailable'

    def image(self):
        if self.raster is None:
            self.raster = _render(self.page, FAST_OCR_MAX_SIDE, PDF_OCR_SCALE)
        return self.raster

    @cached_property
    def possible_raster_table(self):
        return self.text_layer is not None and raster_table_hint(self, self.text_layer)

    def measure(self, detailed_tables):
        self.width, self.height = self.page.get_size()
        self.objects = {}
        self.paths, self.rules = _page_geometry(self.page, self.width, self.height, self.objects,
                                                path_limit=3000 if detailed_tables else 360)

    @property
    def pixel_scale(self):
        return self.width / self.raster.width, self.height / self.raster.height

    @property
    def grid_scale(self):
        return self.pixel_scale if self.source == 'raster' else (1., 1.)

    def grids_for_text(self, *, native):
        if not self.grids or (self.source == 'vector') == native:
            return self.grids
        sx, sy = self.pixel_scale
        if not native:
            sx, sy = 1/sx, 1/sy
        return tuple(grid.scaled(sx, sy) for grid in self.grids)

    def orient_scan(self, page_no, detailed_tables, cancelled):
        from local_web.fast_ocr import balanced_page_orientation

        turn, self.positioned_read = balanced_page_orientation(self.raster, page_no, check_cancel=cancelled)
        if turn:
            self.rotate(turn, detailed_tables)

    def orient_text_layer(self, detailed_tables):
        from docling_pdf_orientation import ORIENTATION_CONFIDENCE
        from local_web.fast_ocr import _orientation_hint

        area = self.width * self.height
        if not any((r-l) * (b-t) >= .80 * area for l, t, r, b in self.objects):
            return
        # PP-LCNet takes a 224 px center crop. Leave room for its resize on a portrait scan.
        with _render(self.page, 640, PDF_OCR_SCALE) as preview:
            turn, confidence = _orientation_hint(preview)
        if turn and confidence >= ORIENTATION_CONFIDENCE:
            self.rotate(turn, detailed_tables)
            inverse = dict(original_size=(self.width, self.height), clockwise=(-turn) % 360)
            reading = self.text_layer
            reading['words'] = [(*source_bbox(box, inverse), text, score)
                                for *box, text, score in reading['words']]

            def word_in_reading_space(word):
                text, x, y, width, height = word
                l, t, r, b = source_bbox((x, y, x + width, y + height), inverse)
                return text, l, t, r - l, b - t

            reading['word_rows'] = [[word_in_reading_space(word) for word in row]
                                    for row in reading['word_rows']]

    def rotate(self, turn, detailed_tables):
        from PIL import Image

        self.turn = turn
        if self.source_view is None:
            self.source_view = dict(original_size=(self.width, self.height))
        self.source_view['clockwise'] = turn
        if self.raster is not None:
            transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                         270: Image.Transpose.ROTATE_90}
            upright = self.raster.transpose(transpose[turn])
            self.raster.close()
            self.raster = upright
        self.page.set_rotation((self.page.get_rotation() + turn) % 360)
        self.measure(detailed_tables)

    def close(self):
        if self.raster is not None:
            self.raster.close()
        self.page.close()


def read_page(document, page_no, improved, deskew, text_document, on_page=None, *, orient_scans=False):
    page = document[page_no - 1]
    try:
        page._fast_image_page_index = page_no - 1
        width, height = page.get_size()
        current = PageContent(page, width, height, {}, None, ())
        current.measure(improved)
        if text_document is not None:
            reading = page_text(text_document, page_no, (width, height))
            current.route_reason = 'insufficient_text_coverage' if reading else 'unusable_text_layer'
            if reading and covers_scanned_body(reading, current.objects, width, height):
                if on_page:
                    on_page(page_no, True)
                current.text_layer = reading
                current.route_reason = 'covered_text_layer'
                if orient_scans:
                    current.orient_text_layer(improved)
                return current
        if on_page:
            on_page(page_no, False)
        measured = page_skew(page)
        angle = deskew.apply(page, page_no - 1, measured)
        current.residual = measured - angle
        if angle:
            current.measure(improved)
            current.source_view = dict(original_size=(width, height), deskewed_size=(current.width, current.height),
                                       deskew_degrees=angle, clockwise=0)
            print(f'Deskew: page {page_no}; {angle:+.2f} degrees', flush=True)
        return current
    except BaseException:
        page.close()
        raise


def _detect(source, payload):
    from local_web.balanced_table_grid import raster_grids, vector_grids

    reasons = []
    try:
        if source == 'vector':
            grids = vector_grids(*payload, diagnostics=reasons)
        else:
            grids = raster_grids(payload, diagnostics=reasons)
        return tuple(grids), reasons
    except Exception as error:
        # An unavailable detector must not prevent the text pass.
        return (), [f'{source}_detector_error ({type(error).__name__})']


def _single_core():
    import cv2
    cv2.setNumThreads(1)


def _submit(pool, page, source, payload):
    page.source = source
    if pool is not None:
        try:
            return pool.submit(_detect, source, payload)
        except (RuntimeError, OSError):
            pass
    return _detect(source, payload)


def _start(page, pool, residual, raster_tables):
    if page.blank:
        return (), ['blank']
    if abs(residual) > 2:
        return (), ['residual_skew']
    if page.paths:
        try:
            axes, _ = _vector_segments(page.paths, 6, thin_rules=True)
        except (RuntimeError, ValueError, OSError) as error:
            page.reasons.append(f'vector_paths ({type(error).__name__})')
        else:
            if page.text_layer is not None:
                page.horizontal_rules = tuple(axes[0])
            if len(axes[0]) >= 1 and len(axes[1]) >= 2:
                return _submit(pool, page, 'vector', axes)
    if not raster_tables or not (page.objects or page.rules or page.paths is None or page.reasons):
        return (), ['no_ruled_grid']
    return _submit(pool, page, 'raster', page.image())


def _finish(page, pending, raster_tables):
    try:
        grids, reasons = pending.result() if hasattr(pending, 'result') else pending
    except (OSError, RuntimeError) as error:
        # A pool worker may have exited. The current bitmap/paths still belong to the parent.
        if page.source == 'raster':
            grids, reasons = _detect('raster', page.image())
        else:
            grids, reasons = (), [f'grid_worker_error ({type(error).__name__})']
    page.reasons.extend(reasons)
    if raster_tables and page.source == 'vector' and not grids:
        page.source = 'raster'
        grids, reasons = _detect('raster', page.image())
        page.reasons.extend(reasons)
    page.grids = grids


_PARALLEL_MIN_PAGES = 6


def read_pages(document, first, last, *, improved, deskew, text_document=None, simple_vectors=False,
               orient_scans=False, cancelled=None, on_page=None):
    """PDFium stays in the parent. Workers receive only axis segments or grayscale pixels."""
    worker_count = min(2, os.cpu_count() or 1)
    parallel = improved and last - first + 1 >= _PARALLEL_MIN_PAGES and worker_count > 1
    queue = deque()
    with ExitStack() as scope:
        pool = None
        if parallel:
            try:
                pool = scope.enter_context(ProcessPoolExecutor(worker_count, initializer=_single_core))
            except (OSError, RuntimeError):
                pass
        window = worker_count if pool is not None else 1
        try:
            for number in range(first, last + window + 1):
                if cancelled:
                    cancelled()
                if number <= last:
                    page = read_page(document, number, improved, deskew, text_document, on_page,
                                     orient_scans=orient_scans)
                    try:
                        if page.text_layer is None:
                            low, high = page.image().getextrema()
                            page.blank = low >= 254 and high - low <= 1
                            if page.blank:
                                page.route_reason = 'blank_raster'
                            if orient_scans and not page.blank:
                                page.orient_scan(number, improved, cancelled)
                        raster_tables = improved and (page.text_layer is None or page.possible_raster_table)
                        task = (_start(page, pool, page.residual, raster_tables)
                                if improved or simple_vectors or page.text_layer is not None else None)
                    except BaseException:
                        page.close()
                        raise
                    queue.append((number, page, task, raster_tables))
                if queue and (len(queue) >= window or number > last):
                    page_no, current, task, raster_tables = queue.popleft()
                    with closing(current):
                        if task is not None:
                            _finish(current, task, raster_tables)
                        if simple_vectors:
                            current.grids = tuple(g for g in current.grids if len(g.cells) == g.rows * g.columns)
                        yield page_no, current
        finally:
            for _, _, task, _ in queue:
                if hasattr(task, 'cancel'):
                    task.cancel()
            if pool is not None and queue:
                # The queue feeder can still be serializing a PIL bitmap.
                pool.shutdown(wait=True, cancel_futures=True)
            while queue:
                _, page, _, _ = queue.popleft()
                page.close()


def grid_attributes(grid, scale, page_size):
    sx, sy = scale
    coords = lambda values: ','.join(f'{v:.3f}' for v in values)
    l, t, r, b = grid.bbox
    double_rules = ''.join(
        f' data-grid-double-{axis}="{coords(value * factor for value in values)}"'
        for axis, values, factor in (('x', grid.double_x, sx), ('y', grid.double_y, sy))
        if values)
    if double_rules:
        double_rules = ' data-grid-double="1"' + double_rules
    return (f' data-balanced-grid="1" data-grid-evidence="{grid.evidence}"'
            f' data-source-bbox="{coords((l*sx,t*sy,r*sx,b*sy))}"'
            f' data-grid-x="{coords(x*sx for x in grid.x_grid)}"'
            f' data-grid-y="{coords(y*sy for y in grid.y_grid)}"'
            f'{double_rules}'
            f' data-page-width="{page_size[0]:.3f}" data-page-height="{page_size[1]:.3f}"'
            f' data-open-top="{int(grid.open_top)}" data-open-bottom="{int(grid.open_bottom)}"')
