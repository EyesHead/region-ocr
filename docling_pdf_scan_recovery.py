"""Read source scan rails with their full stroke width before cell recovery."""


def join_broken_scan_rules(edges):
    """Join near-complete horizontal rails between measured vertical sides.

    Scans can erase a small interior section of a separator. Requiring a
    continuous line then invents a rowspan. Both surviving ends must meet
    vertical sides that continue above and below the separator, and measured
    fragments must cover at least 94 percent of that column. Missing rules,
    isolated underlines and large gaps therefore remain unjoined.
    """
    from statistics import median

    groups = []
    for edge in sorted((e for e in edges if e['orientation'] == 'h'), key=lambda e: e['top']):
        if groups and edge['top'] - groups[-1][0]['top'] <= 2:
            groups[-1].append(edge)
        else:
            groups.append([edge])
    result, proofs = list(edges), []
    for group in groups:
        y = median(e['top'] for e in group)
        sides = sorted({e['x0'] for e in edges if e['orientation'] == 'v'
                        and e['top'] <= y - 3 and e['bottom'] >= y + 3})
        for left, right in zip(sides, sides[1:]):
            if right - left <= 6:
                continue
            intervals = sorted((max(left, e['x0']), min(right, e['x1'])) for e in group
                               if e['x1'] > left and e['x0'] < right)
            if not intervals or intervals[0][0] > left + 3 or intervals[-1][1] < right - 3:
                continue
            merged = []
            for start, end in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            gaps = [b[0] - a[1] for a, b in zip(merged, merged[1:])]
            coverage = sum(end - start for start, end in merged) / (right - left)
            if not gaps or max(gaps) <= 3 or coverage < .94:
                continue
            start, end = merged[0][0], merged[-1][1]
            result.append(dict(x0=start, x1=end, top=y, bottom=y, width=end-start,
                               height=0, orientation='h'))
            proofs.append(dict(y=y, column_bbox=[left, right], coverage=coverage,
                               source_fragments=merged, maximum_gap=max(gaps)))
    return result, proofs


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
