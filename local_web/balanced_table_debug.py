"""Optional per-page grid overlays in the displayed page coordinate system."""

from dataclasses import asdict
from pathlib import Path
import json


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def write_page(directory, number, page, grids, scale):
    from PIL import ImageDraw

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / f'page-{number:04d}'
    sx, sy = scale
    with page.image().convert('RGB') as overlay:
        draw = ImageDraw.Draw(overlay)
        px, py = sx * overlay.width / page.width, sy * overlay.height / page.height
        records = []
        for grid in grids:
            record = {'bbox': grid.bbox, 'x_grid': grid.x_grid, 'y_grid': grid.y_grid,
                      'double_x': grid.double_x, 'double_y': grid.double_y,
                      'open_top': grid.open_top, 'open_bottom': grid.open_bottom, 'cells': []}
            for cell in grid.cells:
                l, t, r, b = cell.bbox
                draw.rectangle((l*px, t*py, r*px, b*py), outline=(15, 107, 168), width=2)
                for flag, y in ((cell.synthetic_top, t), (cell.synthetic_bottom, b)):
                    if flag:
                        draw.line((l*px, y*py, r*px, y*py), fill=(218, 72, 10), width=4)
                record['cells'].append(asdict(cell))
            records.append(record)
        overlay.save(prefix.with_suffix('.png'), format='PNG')
    _write_json(prefix.with_suffix('.json'), {'page': number, 'source': page.source or 'none',
                'coordinate_units': 'points' if page.source == 'vector' else 'pixels',
                'scale_to_pdf_points': scale, 'page_size_pt': (page.width, page.height),
                'tables': records, 'fallback_reasons': list(dict.fromkeys(page.reasons))})


def write_joins(directory, decisions):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    _write_json(path / 'stitch.json', decisions)
