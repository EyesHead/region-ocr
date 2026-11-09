"""Export-only ownership of cached tokens from an unreliable PDF text layer."""
from bs4 import BeautifulSoup


def _contains(outer,inner,tolerance=0.):
    return (len(outer)==len(inner)==4 and outer[0]-tolerance<=inner[0] and outer[1]-tolerance<=inner[1]
            and inner[2]<=outer[2]+tolerance and inner[3]<=outer[3]+tolerance)


def pixel_cell_token_owner(record,entry,fragment,api):
    """Prove that an obsolete hidden-layer token is owned by a fresh cell read.

    The original OCR/layout snapshot is never edited. Unknown/outside tokens
    remain eligible for the existing loss-prevention recovery path.
    """
    if record.get('kind')!='table_unassigned_token' or entry.get('page')!=record.get('page_no'):return None
    geometry=entry.get('pdf_geometry') or {}
    if (not geometry.get('source_grid_verified') or geometry.get('glyph_coverage')!=1.
            or not geometry.get('hidden_ocr_direction_conflict')):return None
    box=record.get('bbox')
    if not box or len(box)!=4 or box[0]>=box[2] or box[1]>=box[3]:return None
    soup=BeautifulSoup(fragment,'html.parser');table=soup.table
    if table is None:return None
    profile=api.table_profile(str(table));nodes=table.find_all(['td','th'])
    published={(c['row'],c['column'],c['row_span'],c['column_span']):(c,n)
               for c,n in zip(profile.get('cells',[]),nodes)}
    owners=[]
    for cell in geometry.get('cells',[]):
        bounds=cell.get('bbox');read=cell.get('raster_cell_ocr_source') or {};pixels=read.get('bbox')
        if (not bounds or not pixels or not cell.get('source_text_unreliable') or not cell.get('raster_cell_ocr')
                or read.get('coverage',0)<.99 or read.get('confidence',0)<.75):continue
        if not _contains(bounds,box,.01) or not _contains(pixels,box,.01):continue
        # The proof must describe essentially the WHOLE physical cell,
        # allowing only the deliberately removed outer table-rule margin.
        if (not _contains(bounds,pixels,.01) or any(abs(a-b)>2.01 for a,b in zip(bounds,pixels))):continue
        key=tuple(cell.get(k) for k in ('row','column','row_span','column_span'))
        item=published.get(key)
        if item is None:continue
        content,node=item
        if node.find(['img','svg','table']):continue
        expected=api.cell_text_key(BeautifulSoup(cell.get('html',''),'html.parser').get_text(' ',strip=True))
        if not expected or expected!=api.cell_text_key(node.get_text(' ',strip=True)):continue
        owners.append(dict(page=record['page_no'],bbox=box,text=record.get('text',''),table=entry['ref'],
            row=cell['row'],column=cell['column'],source_cell_bbox=bounds,source_pixel_bbox=pixels,
            source_ocr_coverage=read['coverage'],source_ocr_confidence=read['confidence'],
            published_cell_text=node.get_text(' ',strip=True),
            method='unreliable_cached_token_superseded_by_complete_pixel_transcribed_source_cell',
            reason='source_text_layer_quarantined_and_token_inside_fully_read_physical_cell'))
    return owners[0] if len(owners)==1 else None
