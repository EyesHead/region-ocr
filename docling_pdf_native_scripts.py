"""Preserve indices measured from real PDF glyph sizes and text baselines."""
from collections import Counter
from html import escape
import re

from bs4 import BeautifulSoup


def _positioned(glyph):
    matrix=glyph.get('matrix')
    return (matrix and len(matrix)==6 and glyph.get('upright',True) and glyph.get('native_script_eligible',True)
            and not glyph.get('source_html') and len(glyph.get('text',''))==1
            and abs(matrix[1])+abs(matrix[2]) <= .01*max(abs(matrix[0]),abs(matrix[3]),1))


def attach_source_scripts(chars, render_math):
    """Group smaller, shifted glyphs with their actual adjacent base.

    A smaller font alone is insufficient. The source text origin AND the ink
    box centre must shift in the same direction. Operators, other lines and
    ambiguous bases block attachment. Original input dictionaries are untouched.
    """
    eligible=[i for i,c in enumerate(chars) if _positioned(c) and c['text'].strip()]
    if not eligible:return chars
    owners={}
    for i in eligible:
        child=chars[i]
        if not (child['text'].isalnum() or child['text'] in ',+−-'):continue
        choices=[]
        for j in eligible:
            base=chars[j]
            if (i==j or not base['text'].isalnum() or child['size']>=.85*base['size']
                    or child['x0']<base['x0']+.35*(base['x1']-base['x0'])
                    or child['x0']-base['x1']>.55*base['size']):continue
            centre=(child['top']+child['bottom']-base['top']-base['bottom'])/2
            rise=child['matrix'][5]-base['matrix'][5]
            if not .08*base['size']<abs(centre)<.75*base['size']:continue
            if abs(rise)<=.06*base['size'] or rise*centre>=0:continue
            overlap=min(child['bottom'],base['bottom'])-max(child['top'],base['top'])
            if overlap<.15*min(child['size'],base['size']):continue
            if any(k not in (i,j) and chars[k]['text'].strip()
                and chars[k]['x0']>=base['x1'] and chars[k]['x1']<=child['x0']
                and chars[k]['size']>=.85*base['size']
                and abs(chars[k]['matrix'][5]-base['matrix'][5])<.15*base['size'] for k in eligible):continue
            # Ordinary smaller words beside a formula can have a slightly
            # different typeface baseline. They are not mathematical indices.
            word_peer=any(k!=i and chars[k]['text'].isalpha()
                and abs(chars[k]['size']-child['size'])<.05*child['size']
                and abs(chars[k]['matrix'][5]-child['matrix'][5])<.03*child['size']
                and min(abs(chars[k]['x0']-child['x1']),abs(child['x0']-chars[k]['x1']))<.3*child['size']
                for k in eligible)
            if child['text'].isalpha() and word_peer and abs(rise)<.15*base['size']:continue
            choices.append((abs(child['x0']-base['x1']),j,'sup' if rise>0 else 'sub'))
        choices.sort()
        if choices and (len(choices)==1 or choices[1][0]-choices[0][0]>.05*chars[choices[0][1]]['size']):
            owners[i]=choices[0][1:]
    groups={}
    for child,(base,role) in owners.items():groups.setdefault(base,{}).setdefault(role,[]).append(child)
    # Continue a source index along its own baseline, rather than truncating
    # longer words at an arbitrary character count or distance from the base.
    for base,roles in groups.items():
        for role,values in roles.items():
            while True:
                last=chars[max(values,key=lambda i:chars[i]['x1'])]
                tail=[i for i in eligible if i not in owners and i!=base
                    and (chars[i]['text'].isalnum() or chars[i]['text'] in ',+−-')
                    and chars[i]['x0']>=last['x1']-.05*last['size']
                    and chars[i]['x0']-last['x1']<.35*last['size']
                    and abs(chars[i]['size']-last['size'])<.05*last['size']
                    and abs(chars[i]['matrix'][5]-last['matrix'][5])<.03*last['size']]
                if not tail:break
                i=min(tail,key=lambda i:chars[i]['x0']);values.append(i);owners[i]=(base,role)
    # Nested scripts retain the established OCR/MathML path. This simple
    # source route must not flatten one script into the text of another.
    nested={b for b in groups if b in owners}
    blocked=nested|{owners[b][0] for b in nested}
    replacements={};consumed=set()
    for base_no,roles in groups.items():
        if base_no in blocked:continue
        base=chars[base_no]
        for values in roles.values():values.sort(key=lambda i:chars[i]['x0'])
        scripts={role:''.join(chars[i]['text'] for i in values) for role,values in roles.items()}
        prefix=[]
        if base['text'].isdigit():
            last=base
            while True:
                before=[i for i in eligible if i not in owners and i!=base_no and i not in prefix
                    and chars[i]['text'].isdigit() and chars[i]['x1']<=last['x0']+.02*base['size']
                    and last['x0']-chars[i]['x1']<.2*base['size']
                    and abs(chars[i]['size']-base['size'])<.05*base['size']
                    and abs(chars[i]['matrix'][5]-base['matrix'][5])<.03*base['size']]
                if not before:break
                i=max(before,key=lambda i:chars[i]['x1']);prefix.insert(0,i);last=chars[i]
        base_text=''.join(chars[i]['text'] for i in prefix)+base['text']
        peers=any(i!=base_no and i not in owners and chars[i]['text'].isalpha()
            and abs(chars[i]['matrix'][5]-base['matrix'][5])<.05*base['size']
            and abs(chars[i]['size']-base['size'])<.1*base['size']
            and min(abs(chars[i]['x1']-base['x0']),abs(chars[i]['x0']-base['x1']))<.2*base['size']
            for i in eligible)
        expression=base_text+''.join(('_{' if role=='sub' else '^{')+scripts[role]+'}'
            for role in ('sub','sup') if role in scripts)
        if re.fullmatch('[А-Яа-яЁё]',base_text) or peers:
            html=escape(base_text)+''.join('<'+role+'>'+escape(scripts[role])+'</'+role+'>'
                for role in ('sub','sup') if role in scripts)
        else:
            html=render_math(expression,'inline')
            if not html:continue
        indices=[i for values in roles.values() for i in values]
        original=[*prefix,base_no,*indices]
        proof=dict(method='source_glyph_size_text_origin_and_adjacent_base',base=base_text,
            scripts=scripts,expression=expression,
            glyphs=[dict(text=chars[i]['text'],bbox=[chars[i]['x0'],chars[i]['top'],chars[i]['x1'],chars[i]['bottom']],
                size=chars[i]['size'],baseline=chars[i]['matrix'][5]) for i in original])
        replacements[base_no]=dict(base,text=base_text+scripts.get('sub','')+scripts.get('sup',''),
            source_html=html,source_script_proof=proof,
            x0=min(chars[i]['x0'] for i in original),x1=max(chars[i]['x1'] for i in original))
        consumed.update(prefix+indices)
    if not replacements:return chars
    result=[replacements.get(i,c) for i,c in enumerate(chars) if i not in consumed]
    # Ambiguous overlapping groups must never duplicate or consume a glyph.
    if Counter(''.join(c['text'] for c in result))!=Counter(''.join(c['text'] for c in chars)):
        return chars
    return result


def script_terms(fragment):
    """Compare explicit script roles, never merely flattened cell text."""
    soup=BeautifulSoup(fragment,'html.parser');terms=Counter()
    raised='⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾';lowered='₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎'
    digits=str.maketrans(raised+lowered,'0123456789+−=()'*2)
    raw=lambda n:n.get_text() if hasattr(n,'get_text') else str(n)
    compact=lambda n:re.sub(r'\s+','',raw(n)).translate(digits)
    for node in soup.find_all(['msub','msup','msubsup']):
        parts=node.find_all(True,recursive=False)
        if len(parts)!=(3 if node.name=='msubsup' else 2):continue
        roles=('sub','sup') if node.name=='msubsup' else ('sub' if node.name=='msub' else 'sup',)
        for role,value in zip(roles,parts[1:]):
            if compact(value):terms[compact(parts[0]),role,compact(value)]+=1
    for node in soup.find_all(['sub','sup']):
        previous=node.previous_sibling
        while previous is not None and (not str(previous).strip() or getattr(previous,'name',None) in ('sub','sup')):
            previous=previous.previous_sibling
        if previous is None:continue
        match=re.search(r'([\w]+)\s*$',raw(previous))
        if match and compact(node):terms[match[1],node.name,compact(node)]+=1
    # A Unicode square/cube is already explicit source typography. OCR must
    # preserve it just as it preserves HTML or MathML script roles.
    pattern=re.compile(r'([\w]+?)(['+raised+lowered+']+)')
    for node in soup.find_all(string=True):
        if any(p.name in ('math','sub','sup') for p in node.parents):continue
        for match in pattern.finditer(str(node)):
            for token in re.finditer('['+raised+']+|['+lowered+']+',match[2]):
                terms[match[1],'sup' if token[0][0] in raised else 'sub',token[0].translate(digits)]+=1
    return terms


def preserves_source_scripts(source,reference):
    terms=script_terms(source)
    return not terms or terms==script_terms(reference)
