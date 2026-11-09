"""Rejoin raster accents with their actual, positioned PDF text glyphs.

An alpha image's transparent canvas is not its visible content. In particular,
an accent may have a formula-sized canvas over native Q, its index and number.
Rendering that canvas as a separate formula clips and duplicates native text.
"""
from io import BytesIO
import math
import re

import cv2
import numpy as np
from PIL import Image


def tilde_ink(image):
    """Prove one thin wavy stroke, declining bars, hats and multi-part ink."""
    gray = np.asarray(image.convert('L'))
    mask = (gray < 160).astype('uint8')
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    parts = [s for s in stats[1:] if s[4] > 1]
    if len(parts) != 1:
        return None
    x,y,w,h,area = map(int,parts[0])
    if h < 3 or not 1.7 <= w/h <= 8 or area/(w*h) > .75:
        return None
    ink = mask[y:y+h,x:x+w]
    if not np.all(ink.sum(axis=0)):
        return None
    center = np.array([np.where(ink[:,i])[0].mean() for i in range(w)])
    left, right = np.mean(center[:max(1,w//8)]), np.mean(center[-max(1,w//8):])
    start, stop = math.ceil(.15*w),math.floor(.85*w)
    interior = center[start:stop]
    peak, trough = start+int(np.argmin(interior)), start+int(np.argmax(interior))
    if abs(peak-trough) < .2*w or abs(center[peak]-center[trough]) < .30*h:
        return None
    if peak < trough:
        wave = left-center[peak] > .25*h and center[trough]-right > .25*h
    else:
        wave = center[trough]-left > .25*h and right-center[peak] > .25*h
    if not wave:
        return None
    return dict(ink_bbox=[x,y,x+w,y+h], method='single_connected_wavy_accent',
                source_size=list(image.size), foreground_pixels=area)


def source_layer(reader, page_no, bounds, *, alpha_only=True, native=False):
    """Decode only one PDF image with its own alpha mask, never other objects."""
    import pypdfium2 as pdfium
    if reader.page.rotation:
        return None
    if reader.pdfium is None:
        reader.pdfium = pdfium.PdfDocument(str(reader.path))
    page = reader.pdfium[page_no-1]
    try:
        height = page.get_height()
        matches = []
        for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
            l,b,r,t = obj.get_bounds()
            box = [l,height-t,r,height-b]
            if max(abs(a-b) for a,b in zip(box,bounds)) <= .1:
                matches.append(obj)
        if len(matches) != 1:
            return None
        bitmap = matches[0].get_bitmap(render=not native)
        try:
            rgba = bitmap.to_pil().convert('RGBA')
            # An opaque formula bitmap needs the ordinary equation reader.
            if alpha_only and rgba.getchannel('A').getextrema()[0] == 255:
                rgba.close()
                return None
            white = Image.new('RGBA',rgba.size,'white')
            white.alpha_composite(rgba); rgba.close()
            result = white.convert('RGB'); white.close()
            return result
        finally:
            bitmap.close()
    finally:
        page.close()


def restore_raster_accents(reader, page_no, glyphs, objects, api):
    """Consume only a proven accent and its one native base/index allocation."""
    proofs = []
    for obj in objects:
        if obj.get('source_type') != 'image' or '<img' not in obj.get('source_html',''):
            continue
        bounds = [obj['x0'],obj['image_top'],obj['x1'],obj['image_bottom']]
        transparent = [o for o in reader.page.images if
            max(abs(a-b) for a,b in zip([o['x0'],o['top'],o['x1'],o['bottom']],bounds))<=.1
            and o['stream'].attrs.get('SMask') is not None]
        if len(transparent)!=1:
            continue
        layer = source_layer(reader,page_no,bounds)
        if layer is None:
            continue
        try:
            proof = tilde_ink(layer)
        finally:
            layer.close()
        if proof is None:
            continue
        pixels = proof['ink_bbox']; width,height = proof['source_size']
        ink = [bounds[0]+pixels[0]*(bounds[2]-bounds[0])/width,
               bounds[1]+pixels[1]*(bounds[3]-bounds[1])/height,
               bounds[0]+pixels[2]*(bounds[2]-bounds[0])/width,
               bounds[1]+pixels[3]*(bounds[3]-bounds[1])/height]
        center = (ink[0]+ink[2])/2
        bases = [g for g in glyphs if re.fullmatch(r'[A-Za-zА-Яа-яЁёΑ-Ωα-ω]',g['text'])
                 and g.get('upright',True) and g['x0'] <= center <= g['x1']
                 and g['top']-.5*g['size'] <= ink[1] < ink[3] <= g['top']+.2*g['size']
                 and ink[2]-ink[0] <= g['x1']-g['x0']]
        if len(bases) != 1:
            continue
        base = bases[0]
        indices = [g for g in glyphs if g is not base and g.get('upright',True)
            and re.fullmatch(r'[A-Za-zА-Яа-яЁёΑ-Ωα-ω0-9]',g['text'])
            and g['size'] < .85*base['size']
            and base['x0']+.5*(base['x1']-base['x0']) <= g['x0'] <= base['x1']+.25*base['size']
            and base['bottom']+.12*base['size'] < g['bottom'] < base['bottom']+.6*base['size']]
        if len(indices) > 1 or base.get('source_html'):
            continue
        latex = r'\tilde{'+base['text']+'}'
        if indices:
            latex += '_{'+indices[0]['text']+'}'
        rendered = api.latex_math(latex,'inline')
        if rendered is None:
            continue
        base['source_html'] = rendered
        if indices:
            index=indices[0]
            base['text']+=index['text']
            glyphs.remove(index)
            base['x1']=max(base['x1'],index['x1'])
        obj['source_html']=''
        obj['text']=''
        entry=dict(page=page_no,bbox=bounds,visible_accent_bbox=ink,latex=latex,**proof)
        proofs.append(entry)
        api.diagnostic_event('PDF_NATIVE_RASTER_ACCENT_RESTORED',**entry)
    return proofs


def decode_symbol_glyphs(chars):
    """Decode Microsoft's F000 Symbol cmap only for a declared Symbol font.

    The low byte is the standard Adobe Symbol encoding, not Unicode Latin.
    Unknown slots remain unchanged; unrelated fonts and ordinary Greek never
    pass through this legacy-font mapping.
    """
    upper = dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ'))
    lower = dict(zip('abcdefghijklmnopqrstuvwxyz', 'αβχδεφγηιϕκλμνοπθρστυϖωξψζ'))
    ordinary = {c:c for c in ' !#%&()+,./0123456789:;<=>?[]{}|'}
    encoding = dict(ordinary,**upper,**lower)
    encoding['-']='−'
    result=[]
    for glyph in chars:
        text=glyph.get('text','')
        family=re.sub(r'^[A-Z]{6}\+','',glyph.get('fontname',''))
        if (re.fullmatch(r'Symbol(?:MT)?(?:-(?:Regular|Bold|Italic|BoldItalic))?',family) and len(text)==1
                and 0xF020 <= ord(text) <= 0xF07E and chr(ord(text)-0xF000) in encoding):
            glyph=dict(glyph,text=encoding[chr(ord(text)-0xF000)],source_unicode=text,
                       unicode_method='declared_legacy_symbol_encoding')
        result.append(glyph)
    return result
