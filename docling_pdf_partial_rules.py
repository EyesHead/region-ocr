"""Refine native table cells using omitted partial vertical source dividers.

The source supplies both the new X boundary and its endpoint Y boundaries.
Endpoint cuts must lie in common glyph whitespace in multiple old columns.
This is deliberately restricted to reliable native text and verified rule
rasters; semantic graphics, existing mathematics and ambiguous glyphs stay
with the original reader.
"""
from collections import Counter
from copy import deepcopy
from statistics import median
import math
import re

from bs4 import BeautifulSoup


def _box(glyph):
    return glyph.get('bbox') or [glyph.get('x0'),glyph.get('top'),glyph.get('x1'),glyph.get('bottom')]


def _cluster(values,tolerance=1.):
    groups=[]
    for value in sorted(values):
        if groups and value-groups[-1][-1]<=tolerance:groups[-1].append(value)
        else:groups.append([value])
    return [float(median(group)) for group in groups]


def _native_glyph(glyph):
    box=_box(glyph)
    return dict(text=glyph['text'],x0=box[0],top=box[1],x1=box[2],bottom=box[3],
                size=glyph.get('size') or box[3]-box[1],fontname=glyph.get('font',''),upright=True)


def _glyph_key(glyph):
    return glyph.get('text',''),tuple(_box(glyph)),glyph.get('font'),glyph.get('size')


def refine_partial_dividers(fragment,geometry,reader,page,image,region,api):
    """Return ``(HTML, geometry, proof)``; leave unsupported cases untouched."""
    if not geometry or image is None or not getattr(reader,'page',None):return fragment,geometry,None
    soup=BeautifulSoup(fragment,'html.parser');table=soup.table
    if table is None or table.find(['math','svg','table']):return fragment,geometry,None
    profile=api.table_profile(str(table));source=geometry.get('cells',[])
    if profile.get('issues') or not source or len(source)!=len(profile['cells']):return fragment,geometry,None
    if any(c.get('source_text_unreliable') or c.get('restored_math') or c.get('mapped_ocr_html') for c in source):return fragment,geometry,None
    if any(any(v is None for v in _box(g)) for c in source for g in c.get('glyphs',[])):return fragment,geometry,None
    xs=list(geometry.get('x_grid',[]));ys=list(geometry.get('y_grid',[]))
    if len(xs)<3 or len(ys)<2:return fragment,geometry,None
    original_shape=sorted((c['row'],c['column'],c['row_span'],c['column_span']) for c in source)
    if original_shape!=sorted((c['row'],c['column'],c['row_span'],c['column_span']) for c in profile['cells']):
        return fragment,geometry,None  # never flatten an existing logical-subrow plan
    entries=geometry.get('retained_cell_images',[])
    if not entries or len(entries)!=len(table.find_all('img')):return fragment,geometry,None
    import cv2
    import numpy as np
    glyphs=[g for c in source for g in c.get('glyphs',[])]
    if not glyphs:return fragment,geometry,None
    sx=image.width/(region[2]-region[0]);sy=image.height/(region[3]-region[1])
    gray=np.asarray(image.convert('L'))
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    dividers=[]
    for entry in entries:
        bounds=entry.get('bbox')
        if not bounds:return fragment,geometry,None
        width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
        x=(bounds[0]+bounds[2])/2
        if width<=0 or width>2.5 or height<12*width:return fragment,geometry,None
        if min(abs(x-edge) for edge in xs)<=max(1.,width):return fragment,geometry,None
        owner=next((c for c in source if c['row']==entry.get('row') and c['column']==entry.get('column')),None)
        if owner is None:return fragment,geometry,None
        box=owner['bbox']
        if not box[0]+3*width<x<box[2]-3*width:return fragment,geometry,None
        pixels=[max(0,math.floor((bounds[0]-region[0])*sx)),max(0,math.floor((bounds[1]-region[1])*sy)),
                min(image.width,math.ceil((bounds[2]-region[0])*sx)),min(image.height,math.ceil((bounds[3]-region[1])*sy))]
        patch=ink[pixels[1]:pixels[3],pixels[0]:pixels[2]]
        if not patch.size or np.count_nonzero(np.any(patch,axis=1))<.9*patch.shape[0]:return fragment,geometry,None
        edges=[]
        for edge in reader.page.edges:
            if abs(edge.get('x1',0)-edge.get('x0',0))>.1 or edge.get('bottom',0)-edge.get('top',0)<max(6.,3*width):continue
            if abs(edge['x0']-x)>max(1.25,width):continue
            if edge['bottom']<bounds[1]-1 or edge['top']>bounds[3]+1:continue
            color=edge.get('non_stroking_color') if edge.get('fill') else edge.get('stroking_color')
            if isinstance(color,(int,float)):color=(color,)
            if color is not None and color and all(float(c)>=.9 for c in color):continue
            lo=max(box[1],edge['top']);hi=min(box[3],edge['bottom'])
            if hi>lo:edges.append([float(edge['x0']),float(lo),float(hi)])
        if not edges:return fragment,geometry,None
        starts=_cluster([e[1] for e in edges]);ends=_cluster([e[2] for e in edges])
        low,high=min(starts),max(ends)
        if abs(low-bounds[1])>2 or abs(high-bounds[3])>2:return fragment,geometry,None
        # A continuous source rule is required; an arrow or separate marks
        # near the same X coordinate cannot invent a table boundary.
        intervals=sorted((e[1],e[2]) for e in edges);end=intervals[0][1]
        for lo,hi in intervals[1:]:
            if lo>end+1:return fragment,geometry,None
            end=max(end,hi)
        dividers.append(dict(x=float(median(e[0] for e in edges)),top=low,bottom=high,
            endpoints=_cluster([v for e in edges for v in e[1:]]),source_bbox=list(bounds),source_vector_edges=edges))
    if not dividers:return fragment,geometry,None
    new_xs=_cluster(xs+[d['x'] for d in dividers]);cuts=_cluster([v for d in dividers for v in d['endpoints']
                         if ys[0]+1<v<ys[-1]-1 and min(abs(v-y) for y in ys)>1])
    if not cuts:return fragment,geometry,None
    cut_proofs=[]
    for cut in cuts:
        if any(_box(g)[1]+.25<cut<_box(g)[3]-.25 for g in glyphs if g.get('text','').strip()):return fragment,geometry,None
        supported=[]
        for cell in source:
            box=cell['bbox']
            if not box[1]<cut<box[3]:continue
            gs=[g for g in cell.get('glyphs',[]) if g.get('text','').strip()]
            if any(_box(g)[3]<=cut for g in gs) and any(_box(g)[1]>=cut for g in gs):supported.append(cell['column'])
        if len(set(supported))<2:return fragment,geometry,None
        cut_proofs.append(dict(y=cut,source_columns_with_text_above_and_below=sorted(set(supported)),crossed_glyphs=0))
    for divider in dividers:
        if any(_box(g)[0]+.25<divider['x']<_box(g)[2]-.25 and
               divider['top']<(_box(g)[1]+_box(g)[3])/2<divider['bottom'] for g in glyphs if g.get('text','').strip()):
            return fragment,geometry,None
    new_ys=_cluster(ys+cuts);created=[]
    for old in source:
        box=old['bbox'];first_y=min(range(len(new_ys)),key=lambda i:abs(new_ys[i]-box[1]));last_y=min(range(len(new_ys)),key=lambda i:abs(new_ys[i]-box[3]))
        local_dividers=[d for d in dividers if box[0]<d['x']<box[2] and d['top']<box[3] and box[1]<d['bottom']]
        preserve_vertical=old['row_span']>1 and not local_dividers
        starts=[first_y]
        for boundary in range(first_y+1,last_y):
            if preserve_vertical:continue
            cut=new_ys[boundary]
            local=[g for g in old.get('glyphs',[]) if g.get('text','').strip()]
            texts_on_both_sides=any(_box(g)[3]<=cut for g in local) and any(_box(g)[1]>=cut for g in local)
            divider_endpoint=any(min(abs(cut-y) for y in d['endpoints'])<=1 for d in local_dividers)
            if texts_on_both_sides or divider_endpoint:starts.append(boundary)
        row_spans=list(zip(starts,[*starts[1:],last_y]))
        for y,end_y in row_spans:
            top,bottom=new_ys[y],new_ys[end_y];middle=(top+bottom)/2
            active=[d['x'] for d in dividers if box[0]<d['x']<box[2] and d['top']-1<=middle<=d['bottom']+1]
            edges=[box[0],*sorted(active),box[2]]
            for left,right in zip(edges,edges[1:]):
                start_x=min(range(len(new_xs)),key=lambda i:abs(new_xs[i]-left));end_x=min(range(len(new_xs)),key=lambda i:abs(new_xs[i]-right))
                local=[g for g in old.get('glyphs',[]) if left<=(_box(g)[0]+_box(g)[2])/2<right and top<=(_box(g)[1]+_box(g)[3])/2<bottom]
                lines=api.native_cell_lines([_native_glyph(g) for g in local])
                created.append(dict(row=y,column=start_x,row_span=end_y-y,column_span=end_x-start_x,bbox=[left,top,right,bottom],
                    glyphs=deepcopy(local),lines=lines,source_images=0,restored_math=0,mapped_ocr_html=None))
    if Counter(_glyph_key(g) for g in glyphs)!=Counter(_glyph_key(g) for c in created for g in c['glyphs']):return fragment,geometry,None
    header_rows=geometry.get('header_rows',0)
    numbered=[]
    for cell in source:
        if cell['column']==0:
            numbered.extend(l for l in cell.get('lines',[]) if re.match(r'^\s*\d+[.)]?\s+[^\W\d_]',l.get('text','')))
    demoted=header_rows==len(ys)-1 and len(numbered)>=2
    new_header=0 if demoted else sum(y<ys[min(header_rows,len(ys)-1)] for y in new_ys[:-1])
    out=BeautifulSoup('<table><thead></thead><tbody></tbody></table>','html.parser')
    if table.caption:out.table.insert(0,deepcopy(table.caption))
    for y in range(len(new_ys)-1):
        row=out.new_tag('tr');(out.thead if y<new_header else out.tbody).append(row)
        for cell in sorted((c for c in created if c['row']==y),key=lambda c:c['column']):
            node=out.new_tag('th' if y<new_header else 'td')
            if cell['row_span']>1:node['rowspan']=str(cell['row_span'])
            if cell['column_span']>1:node['colspan']=str(cell['column_span'])
            node.append(BeautifulSoup('<br/>'.join(l['html'] for l in cell['lines']),'html.parser'));row.append(node)
    if not new_header:out.thead.decompose()
    result=api.clean_table_html(str(out.table));checked=api.table_profile(result,len(new_xs)-1)
    if checked['issues']:return fragment,geometry,None
    proof=dict(method='native_partial_vertical_rules_and_common_glyph_whitespace',page=page,dividers=dividers,
        row_cuts=cut_proofs,source_glyphs_preserved=len(glyphs),old_columns=profile['columns'],new_columns=checked['columns'],
        old_rows=profile['rows'],new_rows=checked['rows'],demoted_false_whole_table_header=demoted)
    updated=deepcopy(geometry);updated.update(x_grid=new_xs,y_grid=new_ys,cells=created,header_rows=new_header,
        retained_cell_images=[],source_grid_verified=True,glyph_coverage=1.,partial_source_divider_repair=proof)
    api.diagnostic_event('TABLE_NATIVE_PARTIAL_SOURCE_DIVIDER_REPAIRED',**proof)
    return result,updated,proof
