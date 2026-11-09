"""Verify natural-language tails of a simple mathematical lower index.

An algebra decoder often transliterates Cyrillic prose (e.g. a word label)
into Latin letters or digits. Partition actual source pixels instead: the
mathematical prefix keeps strict Surya/two-CodeFormula agreement; the literal
tail must independently agree with installed Russian OCR at both scales.
No mathematical character, number or operator is normalized or substituted.
"""
import hashlib
from io import BytesIO
import re

from bs4 import BeautifulSoup
import cv2
import numpy as np
from PIL import Image,ImageChops,ImageOps


def verify_text_index(rendered,metadata,recognizer,device,readings,signature,latex_math):
    full=BeautifulSoup(rendered,'html.parser')
    math=full.math
    whole=signature(rendered)
    if (not whole or whole[0]!='script' or whole[3] is not None
            or whole[1][0]!='text' or len(whole[1][1])!=1 or not whole[1][1].isalpha()
            or whole[2][0]!='text'):
        return None
    scripts=math.find_all('msub')
    words=math.find_all('mtext')
    if len(scripts)!=1 or len(words)!=1 or not re.fullmatch('[А-Яа-яЁё]{2,}',words[0].get_text()):
        return None
    children=scripts[0].find_all(True,recursive=False)
    if len(children)!=2 or not (words[0] is children[1] or words[0] in children[1].descendants):
        return None
    expected=words[0].get_text()
    if not children[1].get_text('',strip=True).endswith(expected):
        return None
    words[0].decompose()
    prefix_text=children[1].get_text('',strip=True)
    if not re.fullmatch('[A-Za-zΑ-Ωα-ω0-9,]*',prefix_text):
        return None
    if not prefix_text:
        scripts[0].replace_with(children[0].extract())
    math.attrs.pop('data-latex',None)
    prefix=str(math)
    expected_signature=signature(prefix)
    from docling_pdf_formula_retry import native_image
    image=native_image(recognizer,metadata)
    if image is None:
        return None
    try:
        gray=np.asarray(image.convert('L'))
        mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]>0
        yy,xx=np.where(mask)
        if not len(xx):return None
        projection=mask.sum(axis=0)
        # Test valleys between real source strokes. Every original pixel is
        # allocated to exactly one side of the cut, including connected kerning.
        cuts=[x for x in range(int(xx.min())+1,int(xx.max()))
              if projection[x]<=projection[x-1] and projection[x]<=projection[x+1]]
        cuts.sort(key=lambda x:(projection[x],x))
        from docling_source_text import verify_literal
        for cut in cuts:
            right=image.crop((cut,0,image.width,image.height))
            paper=Image.new('RGB',right.size,'white')
            box=ImageChops.difference(right,paper).getbbox();paper.close()
            if box is None:
                right.close();continue
            fitted=right.crop(box);right.close()
            right_stream=BytesIO();fitted.save(right_stream,'PNG')
            literal=verify_literal(right_stream.getvalue(),expected)
            fitted.close()
            if not literal:continue
            # The word must actually occupy the lower part of the source.
            if box[1] <= int(yy.min()) or (box[1]+box[3])/2 <= (yy.min()+yy.max())/2:
                continue
            left=image.copy();left.paste('white',(cut,0,left.width,left.height))
            paper=Image.new('RGB',left.size,'white')
            left_box=ImageChops.difference(left,paper).getbbox();paper.close()
            if left_box is None:
                left.close();continue
            fitted=left.crop(left_box);left.close()
            padded=ImageOps.expand(fitted,border=12,fill='white');fitted.close()
            stream=BytesIO();padded.save(stream,'PNG');padded.close()
            raw=readings(stream.getvalue(),device)
            parsed=[latex_math(value,'inline') for value in raw]
            if len(parsed)!=2 or not all(value and signature(value)==expected_signature for value in parsed):
                continue
            original=BytesIO();image.save(original,'PNG')
            return dict(method='source_math_and_cyrillic_text_partition',source_size=list(image.size),
                native_asset_sha256=hashlib.sha256(original.getvalue()).hexdigest(),
                cut_x=cut,source_ink_pixels=int(mask.sum()),
                math_ink_pixels=int(mask[:,:cut].sum()),literal_ink_pixels=int(mask[:,cut:].sum()),
                unassigned_ink_pixels=0,math_prefix=prefix,codeformula=list(raw),literal=literal)
        return None
    finally:
        image.close()
