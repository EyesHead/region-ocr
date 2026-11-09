"""Read source scan rails with their full stroke width before cell recovery."""


def read_source_scan_grid(reader, regions, page, bounds, reference_fragments=(), api=None):
    """Return a source grid and its exact rendered ROI, owned by the caller.

    Scanner boxes run through rule centres. Cropping on those centres can cut
    away the outer vertical border and make a complete table appear open.
    A small source margin preserves those measured strokes; no rules are
    invented. The precise short-rule pass must prove complete source cells
    before it supersedes the ordinary drawing-preserving route.

    The return value has ``native``, ``image`` and ``region`` keys. Close its
    image after use; pass this same image/region pair to regional text OCR.
    """
    native_page = reader.load_page(page)
    if native_page is None or not bounds or len(bounds) != 4:
        return None
    margin = 2.0
    roi = [max(0., bounds[0] - margin), max(0., bounds[1] - margin),
           min(float(native_page.width), bounds[2] + margin),
           min(float(native_page.height), bounds[3] + margin)]
    if roi[2] <= roi[0] or roi[3] <= roi[1]:
        return None
    image = regions.crop(page, roi)
    try:
        detailed = reader.read_raster_table(page, roi, image, reference_fragments,
                                            _short_rules=True)
        native = None
        if detailed and detailed[1].get('source_grid_verified'):
            profile = api.table_profile(detailed[0]) if api is not None else None
            if profile is None or not profile.get('issues'):
                native = detailed
        if native is None:
            native = reader.read_raster_table(page, roi, image, reference_fragments)
        if native is None:
            image.close()
            return None
        native[1]['source_render_bbox'] = list(roi)
        native[1]['source_grid_render_padding'] = margin
        if api is not None:
            api.diagnostic_event('SOURCE_SCAN_GRID_READ', page=page,
                requested_bbox=list(bounds), render_bbox=roi,
                measured_bbox=native[1].get('bbox'),
                verified_short_rules=bool(native[1].get('source_grid_verified')),
                rows=len(native[1].get('y_grid', [])) - 1,
                columns=len(native[1].get('x_grid', [])) - 1)
        return dict(native=native, image=image, region=roi)
    except BaseException:
        image.close()
        raise
