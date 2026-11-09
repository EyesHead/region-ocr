"""Bind table captions to the following source grid, using page coordinates.

Layout ownership is a proposal. A caption below one table and above another
belongs to the latter; cross-references in prose are never caption candidates.
"""
import re


def caption_prefix(text):
    return bool(re.match(r'^\s*(?:№\s*)?(?:(?:Продолжение|Окончание)\s+)?'
        r'(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|табл\.?)\s*'
        r'(?:[А-ЯЁA-Z]\s*\.?\s*)?\d',text,re.I))


def attach_source_captions(doc,excluded,manifest,api):
    from docling_core.types.doc import ContentLayer,DocItemLabel
    tables=[]
    for table in doc.tables:
        region=api.item_region(table,doc)
        if region and table.self_ref not in excluded:tables.append((table,*region))
    repairs=[]
    for item in doc.texts:
        if item.self_ref in excluded or not caption_prefix(item.text):continue
        caption=api.extract_table_caption(item.text);region=api.item_region(item,doc)
        if not caption or not region:continue
        page,box=region;choices=[]
        height=max(1,box[3]-box[1])
        for table,table_page,bounds in tables:
            if table_page!=page:continue
            gap=bounds[1]-box[3]
            overlap=max(0,min(box[2],bounds[2])-max(box[0],bounds[0]))
            width=min(max(1,box[2]-box[0]),max(1,bounds[2]-bounds[0]))
            if -.35*height<=gap<=max(64,3*height) and overlap/width>=.6:
                choices.append((max(0,gap),table,bounds))
        choices.sort(key=lambda choice:choice[0])
        if not choices:continue
        # Overlapping layout fragments are not enough to select an owner.
        if len(choices)>1 and abs(choices[0][0]-choices[1][0])<height*.5:continue
        _,target,bounds=choices[0]
        previous=[t.self_ref for t,_,_ in tables if any(c.cref==item.self_ref for c in t.captions)]
        if previous==[target.self_ref] and item.parent and item.parent.cref==target.self_ref:continue
        for table,_,_ in tables:
            table.captions=[c for c in table.captions if c.cref!=item.self_ref]
        if item.parent and item.parent.cref!=target.self_ref:
            parent=item.parent.resolve(doc)
            parent.children=[c for c in parent.children if c.cref!=item.self_ref]
        item.parent=target.get_ref()
        if not any(c.cref==item.self_ref for c in target.children):target.children.append(item.get_ref())
        target.captions.append(item.get_ref())
        item.label=DocItemLabel.CAPTION;item.content_layer=ContentLayer.BODY
        proof=dict(page=page,caption_ref=item.self_ref,caption=caption,caption_bbox=box,
            previous_tables=previous,table=target.self_ref,table_bbox=bounds,
            method='nearest_following_same_page_grid_without_intervening_grid')
        repairs.append(proof);api.diagnostic_event('TABLE_SOURCE_CAPTION_OWNER_REPAIRED',**proof)
    manifest['source_caption_ownership_repairs']=repairs
    return repairs
