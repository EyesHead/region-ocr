"""PDFium geometry and rendering in displayed, top-left page coordinates."""
import math


def _page_box(box, crop, rotation):
    """Convert original PDF coordinates into displayed top-left coordinates."""
    l, b, r, t = box
    x0, y0, x1, y1 = crop
    if rotation == 90:
        return b-y0, l-x0, t-y0, r-x0
    if rotation == 180:
        return x1-r, b-y0, x1-l, t-y0
    if rotation == 270:
        return y1-t, x1-r, y1-b, x1-l
    return l-x0, y1-t, r-x0, y1-b


def _page_geometry(page, width, height, image_objects, *, path_limit):
    """One walk for images and small vector table candidates, including nested forms."""
    from ctypes import c_int, byref
    from pypdfium2 import PdfiumError, raw

    crop,rotation = page.get_bbox(),page.get_rotation()
    parents = {0:None}
    active_parent = {0:True}
    paths, rules = [], set()
    types = [raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_FORM, raw.FPDF_PAGEOBJ_PATH]
    for obj in page.get_objects(filter=types):
        parent = parents[obj.level]
        visible = active_parent[obj.level]
        if visible and obj.type in (raw.FPDF_PAGEOBJ_FORM,raw.FPDF_PAGEOBJ_IMAGE):
            active = c_int()
            visible = not raw.FPDFPageObj_GetIsActive(obj.raw,byref(active)) or active.value
        if obj.type == raw.FPDF_PAGEOBJ_FORM:
            active_parent[obj.level+1] = bool(visible)
        elif not visible:
            continue
        if obj.type==raw.FPDF_PAGEOBJ_FORM:
            matrix=obj.get_matrix()
            parents[obj.level+1]=matrix.multiply(parent) if parent else matrix
            continue
        try:
            bounds=obj.get_bounds()
        except PdfiumError:
            # An unmeasurable drawing cannot prove a complete ruling.
            paths=None
            continue
        box=_page_box(parent.on_rect(*bounds) if parent else bounds,crop,rotation)
        l,t,r,b=box
        if obj.type==raw.FPDF_PAGEOBJ_IMAGE:
            l,t,r,b=max(0,l),max(0,t),min(width,r),min(height,b)
            if r>l and b>t:
                box = (l,t,r,b)
                image_objects.setdefault(box,[]).append(obj)
        else:
            if paths is not None:
                if len(paths) < path_limit:
                    paths.append((obj,box,parent,crop,rotation))
                else:
                    paths = None
            if (r-l)>.18*width and b-t < .015*(r-l):
                rules.add(box)
    return paths, rules


def _vector_segments(objects, em, *, thin_rules=False):
    """Read only short, solid PDF paths; an unexplained drawing blocks its region."""
    from ctypes import byref, c_float, c_int, c_uint
    from pypdfium2 import raw

    axes = [[],[]]
    obstacles = []
    tol = .09*em
    for obj, box, parent, crop, rotation in objects:
        l,t,r,b = box
        wide, tall = r-l,b-t
        # Uniform page backgrounds and point-sized joins cannot be independent rails.
        if max(wide,tall)<.45*em:
            continue
        fill,stroke = c_int(),c_int()
        if not raw.FPDFPath_GetDrawMode(obj.raw,byref(fill),byref(stroke)):
            obstacles.append(box);continue
        rgba = [c_uint() for _ in range(4)]
        color = raw.FPDFPageObj_GetStrokeColor if stroke.value else raw.FPDFPageObj_GetFillColor
        if not color(obj.raw,*(byref(c) for c in rgba)) or not rgba[-1].value:
            continue
        if min(c.value for c in rgba[:3])>235:
            continue
        count = raw.FPDFPath_CountSegments(obj.raw)
        active = c_int()
        if raw.FPDFPageObj_GetIsActive(obj.raw,byref(active)) and not active.value:
            continue
        clip = raw.FPDFPageObj_GetClipPath(obj.raw)
        if (count<2 or count>5 or (clip and raw.FPDFClipPath_CountPaths(clip)>0)
                or (stroke.value and raw.FPDFPageObj_GetDashCount(obj.raw)>0)):
            obstacles.append(box);continue
        matrix = obj.get_matrix()
        if parent is not None:
            matrix = matrix.multiply(parent)
        points = []
        closed = False
        for i in range(count):
            seg = raw.FPDFPath_GetPathSegment(obj.raw,i)
            kind = raw.FPDFPathSegment_GetType(seg)
            if kind != (raw.FPDF_SEGMENT_MOVETO if i==0 else raw.FPDF_SEGMENT_LINETO):
                break
            x,y = c_float(),c_float()
            if not raw.FPDFPathSegment_GetPoint(seg,byref(x),byref(y)):
                break
            px,py = matrix.on_point(x.value,y.value)
            xx,yy,_,_ = _page_box((px,py,px,py),crop,rotation)
            points.append((xx,yy))
            closed = bool(raw.FPDFPathSegment_GetClose(seg))
        if len(points)!=count:
            obstacles.append(box);continue
        if closed and points[0]!=points[-1]:
            points.append(points[0])
        if any(abs(x-u)>tol and abs(y-v)>tol for (x,y),(u,v) in zip(points,points[1:])):
            obstacles.append(box);continue
        if not stroke.value:
            thickness = 1.5 if thin_rules else .35*em
            if (not fill.value or not closed or len(set(points))!=4 or min(wide,tall)>thickness
                    or max(wide,tall)<3*min(wide,tall)):
                obstacles.append(box);continue
            if wide>tall:
                axes[0].append(((t+b)/2,l,r))
            else:
                axes[1].append(((l+r)/2,t,b))
        else:
            if thin_rules:
                line_width = c_float()
                if raw.FPDFPageObj_GetStrokeWidth(obj.raw, byref(line_width)):
                    a, b_, c, d = matrix.a, matrix.b, matrix.c, matrix.d
                    if line_width.value * max(math.hypot(a,b_), math.hypot(c,d)) > 1.5:
                        continue
            for (x,y),(u,v) in zip(points,points[1:]):
                if abs(x-u)>.45*em:
                    axes[0].append(((y+v)/2,min(x,u),max(x,u)))
                elif abs(y-v)>.45*em:
                    axes[1].append(((x+u)/2,min(y,v),max(y,v)))
    return axes,obstacles


def _render(page, largest, max_scale):
    width,height = page.get_size()
    scale = min(max_scale,largest/max(width,height))
    bitmap = page.render(scale=scale,grayscale=True)
    try:
        return bitmap.to_pil().convert('L')
    finally:
        bitmap.close()
