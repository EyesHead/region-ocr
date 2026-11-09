"""Conservative source-pixel proof for dimensioned engineering schematics.

This recognises drawing geometry, not the text written next to it.  Callers
must additionally establish that the bytes are an original positioned PDF
image; a generated table, word or formula crop never supplies that proof.
"""
from io import BytesIO


def dimensioned_schematic(data):
    import cv2
    import numpy as np
    from PIL import Image
    try:
        with Image.open(BytesIO(data)) as image:
            gray=np.asarray(image.convert('RGB').convert('L'))
    except (OSError,ValueError):return None
    threshold,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    mask=(gray<(threshold+255)/2).astype('uint8')*255
    yy,xx=np.where(mask)
    if not len(xx):return None
    mask=mask[yy.min():yy.max()+1,xx.min():xx.max()+1]
    height,width=mask.shape
    if min(width,height)<32:return None
    # These are long physical strokes measured against the complete drawing,
    # not the short stems, serifs and fraction bars of neighbouring labels.
    hs=cv2.connectedComponentsWithStats(cv2.morphologyEx(mask,cv2.MORPH_OPEN,
        np.ones((1,max(12,round(width*.30))),np.uint8)),8)[2][1:]
    vs=cv2.connectedComponentsWithStats(cv2.morphologyEx(mask,cv2.MORPH_OPEN,
        np.ones((max(12,round(height*.25)),1),np.uint8)),8)[2][1:]
    if len(hs)<3 or len(vs)<2:return None
    if (max(y+h for x,y,w,h,a in hs)-min(y for x,y,w,h,a in hs)<height*.3
        or max(x+w for x,y,w,h,a in vs)-min(x for x,y,w,h,a in vs)<width*.3):return None
    # A section hatch contains repeated, distinct parallel diagonal strokes.
    # A slash or the two diagonals of an ordinary mathematical glyph cannot
    # meet this repeated-geometry requirement.
    lines=cv2.HoughLinesP(mask,1,np.pi/180,threshold=max(12,round(min(width,height)*.10)),
        minLineLength=max(12,min(width,height)*.13),maxLineGap=2)
    diagonal=[]
    for raw in [] if lines is None else lines:
        x0,y0,x1,y1=map(int,raw[0]);dx,dy=x1-x0,y1-y0
        if dx==0:continue
        angle=np.degrees(np.arctan2(dy,dx))%180
        if not (25<=angle<=65 or 115<=angle<=155):continue
        length=float(np.hypot(dx,dy));a=np.radians(angle)
        diagonal.append((float(angle),float(-x0*np.sin(a)+y0*np.cos(a)),length))
    hatch=[]
    for angle,offset,length in diagonal:
        offsets=sorted(o for a,o,l in diagonal if abs(a-angle)<=4)
        distinct=[]
        for value in offsets:
            if not distinct or value-distinct[-1]>=max(3,min(width,height)*.012):distinct.append(value)
        if len(distinct)>=5 and distinct[-1]-distinct[0]>=min(width,height)*.25:
            hatch=dict(angle=round(angle,2),parallel_strokes=len(distinct),normal_span=round(distinct[-1]-distinct[0],2))
            break
    # Alternatively prove opposed filled arrowheads on a long dimension
    # line.  Their cross-sections taper from tip to base at BOTH ends; a
    # closed rectangular frame or a grid intersection has no such ramps.
    arrows=[]
    def opposed_heads(view,start,end,axis,stroke):
        length=end-start
        if length<max(24,view.shape[0]*.35):return None
        radius=max(5,min(16,round(length*.04)))
        widths=[];balanced=[]
        for along in range(start,end):
            if not view[along,axis]:widths.append(0);balanced.append(False);continue
            left=axis;right=axis
            while left>max(0,axis-radius) and view[along,left-1]:left-=1
            while right<min(view.shape[1]-1,axis+radius) and view[along,right+1]:right+=1
            widths.append(right-left+1);balanced.append(abs((axis-left)-(right-axis))<=max(2,stroke))
        widths=np.asarray(widths);balanced=np.asarray(balanced)
        body=float(np.median(widths[round(length*.2):round(length*.8)]))
        if body<=0 or body>max(5,length*.025):return None
        window=min(round(length*.13),max(12,round(body*18)))
        proofs=[]
        for values,symmetry in ((widths[:window],balanced[:window]),(widths[-window:][::-1],balanced[-window:][::-1])):
            found=None
            # Ignore the crossing extension line at the exact tip.
            for skip in range(min(7,max(2,round(body*3)))):
                for size in range(max(7,round(body*4)),len(values)-skip+1):
                    v=values[skip:skip+size];s=symmetry[skip:skip+size]
                    if v[0]>body*2 or v[-1]<max(body*3,body+4) or np.mean(s)<.85:continue
                    if np.mean(np.diff(v)>=-1)<.9 or np.corrcoef(np.arange(len(v)),v)[0,1]<.92:continue
                    found=dict(tip_width=int(v[0]),base_width=int(v[-1]),length=len(v));break
                if found:break
            if found is None:return None
            proofs.append(found)
        return dict(body_stroke=body,heads=proofs)
    for vertical,stats in ((True,vs),(False,hs)):
        view=mask if vertical else mask.T
        for x,y,w,h,area in stats:
            start,end,axis,stroke=(int(y),int(y+h),int(x+w//2),int(w)) if vertical else (int(x),int(x+w),int(y+h//2),int(h))
            proof=opposed_heads(view,start,end,axis,stroke)
            if proof:arrows.append(dict(axis='vertical' if vertical else 'horizontal',**proof))
    if not hatch and not arrows:return None
    return dict(method='source_orthogonal_outline_with_repeated_section_hatch_or_opposed_dimension_arrows',
        source_ink_size=[width,height],horizontal_rules=len(hs),vertical_rules=len(vs),
        section_hatch=hatch or None,dimension_axes=arrows)
