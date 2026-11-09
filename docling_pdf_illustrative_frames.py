"""Separate a drawing's title block from its semantic example data table.

Only an explicitly captioned source illustration with an empty physical
separator and a genuine title-block grid qualifies. A picture of an actual
data table is never approved merely because its caption says 'example'.
"""
import copy
import math
import re
from bs4 import BeautifulSoup


def split_drawing_frame_footer(fragment,geometry,source_image,region,page,doc,manifest,api):
    """Return (main HTML, main geometry, frame HTML, frame bbox, proof), or None."""
    if not geometry or source_image is None:return None
    xs,ys=geometry.get('x_grid',[]),geometry.get('y_grid',[])
    if len(xs)<3 or len(ys)<7:return None
    soup=BeautifulSoup(fragment,'html.parser');table=soup.table
    if table is None:return None
    profile=api.table_profile(str(table));rows=table.find_all('tr')
    if len(rows)!=len(ys)-1 or profile['columns']!=len(xs)-1:return None
    caption=None
    for item in doc.texts:
        text=getattr(item,'text','');location=api.item_region(item,doc)
        if (not location or location[0]!=page or not re.match(r'^\s*Рисунок\b',text,re.I)
                or not re.search(r'\b(?:Пример|Образец|Форма)\b',text,re.I)):continue
        box=location[1]
        if -3<=box[1]-region[3]<=80:
            caption=dict(ref=item.self_ref,text=text,bbox=box);break
    if caption is None:return None
    from statistics import median
    import cv2
    import numpy as np
    sx=source_image.width/(region[2]-region[0]);sy=source_image.height/(region[3]-region[1])
    heights=[ys[i+1]-ys[i] for i in range(len(ys)-1)]
    ordinary_height=median(heights)
    def crop(box):
        pixels=[max(0,math.floor((box[0]-region[0])*sx)),max(0,math.floor((box[1]-region[1])*sy)),
                min(source_image.width,math.ceil((box[2]-region[0])*sx)),min(source_image.height,math.ceil((box[3]-region[1])*sy))]
        return source_image.crop(pixels)
    def ink(box):
        image=crop(box)
        try:gray=np.asarray(image.convert('L'))
        finally:image.close()
        return cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    candidate=None
    for cell in geometry.get('cells',[]):
        row=cell['row'];end=row+cell['row_span'];box=cell['bbox']
        if (cell['column']!=0 or cell['column_span']!=profile['columns'] or row<4 or end>=len(rows)
                or box[3]-box[1]<3*ordinary_height):continue
        node=rows[row]
        if node.get_text(strip=True) or node.find(['img','math','svg','table']):continue
        mask=ink(box);margin=max(2,round(max(sx,sy)))
        interior=mask[margin:-margin,margin:-margin]
        if interior.size==0 or np.any(interior):continue
        footer=BeautifulSoup(''.join(str(r) for r in rows[end:]),'html.parser')
        native_labels=' '.join(BeautifulSoup(c.get('html',''),'html.parser').get_text(' ',strip=True)
            +' '+' '.join(line.get('text','') for line in c.get('lines',[]))
            for c in geometry['cells'] if c['row']>=end)
        fields=re.sub(r'[^A-Za-zА-Яа-яЁё]','',footer.get_text(' ',strip=True)+' '+native_labels).casefold()
        names=[name for name,pattern in (('change','изм'),('signature','подпис'),('date','дат[аы]'),
            ('stage','стади'),('sheet','лист'),('designation','наименован')) if re.search(pattern,fields)]
        if len(names)<3:continue
        footer_mask=ink([region[0],ys[end],region[2],region[3]])
        if footer_mask.size==0:continue
        h,w=footer_mask.shape
        horizontal=cv2.morphologyEx(footer_mask,cv2.MORPH_OPEN,np.ones((1,max(18,round(w*.12))),np.uint8))
        vertical=cv2.morphologyEx(footer_mask,cv2.MORPH_OPEN,np.ones((max(12,round(h*.08)),1),np.uint8))
        h_count=cv2.connectedComponents(horizontal,8)[0]-1;v_count=cv2.connectedComponents(vertical,8)[0]-1
        if h_count<3 or v_count<3:continue
        # A source data cell crossing this separator would make the split
        # ambiguous. Keep the original candidate rather than deleting it.
        if any(c['row']<row<c['row']+c['row_span'] for c in geometry['cells']):continue
        candidate=(row,end,box,names,int(h_count),int(v_count));break
    if candidate is None:return None
    row,end,blank_box,fields,h_count,v_count=candidate
    before=[n.decode_contents() for r in rows[:row] for n in r.find_all(['td','th'],recursive=False)]
    for old in rows[row:]:old.decompose()
    for group in list(table.find_all(['thead','tbody','tfoot'],recursive=False)):
        if not group.find('tr'):group.decompose()
    main=str(table);after_profile=api.table_profile(main)
    after=[n.decode_contents() for n in BeautifulSoup(main,'html.parser').table.find_all(['td','th'])]
    if before!=after or after_profile['issues'] or after_profile['rows']!=row:return None
    new_geometry=copy.deepcopy(geometry)
    new_geometry.setdefault('physical_frame_cells',copy.deepcopy(geometry['cells']))
    new_geometry.setdefault('physical_frame_y_grid',list(ys))
    new_geometry['cells']=[c for c in new_geometry['cells'] if c['row']+c['row_span']<=row]
    new_geometry['y_grid']=ys[:row+1]
    new_geometry['bbox']=[xs[0],ys[0],xs[-1],ys[row]]
    for key in ('retained_cell_images','restored_cell_math','restored_cell_text','pending_cell_ocr'):
        if key in new_geometry:new_geometry[key]=[c for c in new_geometry[key] if c.get('row',row)<row]
    # Neither crop contains the material data grid: the first is its narrow
    # side frame, the second is the empty band and lower title-block area.
    boxes=[]
    if xs[0]-region[0]>3:boxes.append([region[0],region[1],xs[0],ys[row]])
    boxes.append([region[0],ys[row],region[2],region[3]])
    graphics=[]
    for box in boxes:
        image=crop(box)
        try:html=api.image_html(image,box[2]-box[0],'picture')
        finally:image.close()
        graphics.append(dict(page=page,bbox=box,html=html,method='captioned_example_drawing_frame_outside_data_grid'))
    new_geometry.setdefault('source_illustrations',[]).extend(dict(page=page,bbox=g['bbox'],
        method=g['method']) for g in graphics)
    proof=dict(page=page,caption=caption,method='source_figure_caption_empty_band_and_titleblock_grid',
        source_bbox=list(region),data_bbox=new_geometry['bbox'],empty_separator_bbox=blank_box,
        source_rows=profile['rows'],data_rows=row,source_cells=len(profile['cells']),
        data_cells=len(after_profile['cells']),data_payloads_unchanged=True,titleblock_fields=fields,
        titleblock_horizontal_components=h_count,titleblock_vertical_components=v_count,
        source_illustrations=[{k:v for k,v in g.items() if k!='html'} for g in graphics])
    manifest.setdefault('illustrative_drawing_frames_separated',[]).append(proof)
    api.diagnostic_event('TABLE_EXAMPLE_DRAWING_FRAME_SEPARATED',**proof)
    envelope=[min(g['bbox'][0] for g in graphics),min(g['bbox'][1] for g in graphics),
              max(g['bbox'][2] for g in graphics),max(g['bbox'][3] for g in graphics)]
    return main,new_geometry,''.join(g['html'] for g in graphics),envelope,proof
