#!/usr/bin/env python3
"""Cross-platform PDF and Office document -> HTML CLI using Docling.

Searches Input in the application folder recursively; all HTML files go to Output there.
Install dependencies: python -m pip install -r requirements-docling-surya.txt
NVIDIA: install CUDA PyTorch before the other requirements.
Default launch: python convert_docling.py

Modes:
  local : Surya 2 plugin with automatic CUDA / Metal / CPU setup (default)
  ai    : OpenAI vision API; prompts visibly for a missing API key

Whole-page orientation uses PP-LCNet on CPU through ONNX Runtime, with
RapidOCR for uncertain pages. The verified 6.8 MB model downloads once to
the user's cache; an unavailable classifier falls back automatically.
"""

import argparse
import ast
import base64
import dis
import hashlib
import json
import math
import marshal
import os
import platform
import re
import shutil
import sys
import time
import threading
import traceback
import tempfile
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import unicodedata
from pathlib import Path
from io import BytesIO
from uuid import uuid4
from dataclasses import replace
from functools import lru_cache
import subprocess
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
from types import SimpleNamespace

from portable_runtime import configure_runtime
configure_runtime()


LOCAL_SCHEMA = 6
TEXT_CLEANUP_SCHEMA = 6
TABLE_RECOVERY_SCHEMA = 21
TABLE_JOIN_SCHEMA = 14
TABLE_CELL_SCHEMA = 39
# Whole source-image fallbacks retain one actual rendered pixel beyond owned
# glyph extents; this is geometry only and never supplies editable PDF text.
SOURCE_IMAGE_BOUNDS_HALO_PIXELS = 1
CAPTION_FIGURE_MAX_GAP_EM = 3.0  # A geometric caption must be close below its original object.
CAPTION_FIGURE_PADDING_EM = .50  # Include antialiased outer rails without absorbing nearby prose.
CAPTION_FIGURE_CAPTION_CLEARANCE_EM = .10  # Keep the printed caption outside the source image.
CAPTION_FIGURE_X_OVERLAP = .80  # Overlap relative to the narrower caption/object width.
CAPTION_FIGURE_UNIQUE_GAP_EM = .50  # Equally close objects keep their layout without a guessed owner.
CAPTION_FIGURE_BOX_TOLERANCE_EM = .25  # Tolerate small layout-box overlap at the caption edge.
CAPTION_FIGURE_BBOX_FONT_RATIO = .75  # Scan fallback: positioned caption height estimates an em.
PRIMARY_REGION_OWNERSHIP_MARGIN_EM = .50  # Allow source-font-sized uncertainty at the measured data border.
PRIMARY_REGION_OWNERSHIP_MIN_OVERLAP = .60  # Associate a primary packet with the independently proved frame.
# BEGIN borderless_rows constants
# Source font-relative tolerances; two columns and atomic items protect ordinary prose.
BORDERLESS_Y_TOLERANCE_EM = .40
BORDERLESS_MIN_ANCHOR_GAP_EM = .60
BORDERLESS_MIN_ALIGNED_COLUMNS = 2
BORDERLESS_MIN_VALUE_ROWS = 2
BORDERLESS_MIN_ATOMIC_ITEMS = 2
# Descriptor lines aligned with distinct values are ambiguous; preserve their structure.
BORDERLESS_MIN_DESCRIPTOR_ANCHORS = 2
BORDERLESS_MAX_WRAP_GAP_EM = 1.8
BORDERLESS_RULE_COVERAGE = .80
BORDERLESS_RULE_EDGE_EPS_EM = .15
BORDERLESS_RULE_MIN_EDGE_PT = .5
BORDERLESS_RULE_MIN_COLUMN_COVERAGE = .80
BORDERLESS_RULE_RAIL_TOLERANCE_EM = .25
# END borderless_rows constants

# BEGIN source_unit_composition constants
# Pixel topology thresholds factored from source_permille_quantity unchanged.
SOURCE_UNIT_COMPOSITION = dict(
    scale=2, bbox_epsilon_pt=.05, packet_overlap=.80, line_tolerance_em=.50,
    hole_height_min=.20, hole_height_max=.65, hole_aspect_min=.35, hole_aspect_max=1.5,
    hole_fill_min=.25, ring_height_ratio=1.5, ring_area_ratio=2., lower_alignment=.30,
    rise_min=.60, rise_max=1.8, lower_gap_min=.60, lower_gap_max=2.5,
    unit_aspect_min=.80, unit_aspect_max=2., meaningful_area=.40,
    hough_min_votes=4, hough_votes_height=.30, hough_min_length=3,
    hough_length_height=.70, hough_min_gap=1, hough_gap_height=.08,
    slash_x_height=.25, slash_y_height=.65, prefix_height_min=.70, prefix_height_max=1.5,
)
_SOURCE_UNIT_TOKEN = re.compile(r'(?<![\w.,])(?P<prefix>[-+−]?\d+(?:[.,]\d+)?\s*)(?P<unit>%[oо])(?!\w)')
_SOURCE_UNIT_MARKER = '⟪SOURCE_UNIT⟫'
# END source_unit_composition constants

# Source-cell and display-row coordinates remain separate; no guessed source image ownership.
SOURCE_CELL_OWNERSHIP = dict(bbox_epsilon_pt=.05)

# BEGIN source_scripts constants
# Font-relative index fields; ink area is measured in em², frame margins in PDF points.
_SOURCE_SCRIPT_MIN_DIGITS = 2
_SOURCE_SCRIPT_MAX_LETTERS = 3
_SOURCE_SCRIPT_MIN_AREA_FONT2 = .02
_SOURCE_SCRIPT_BASELINE_CLEARANCE_HEIGHT = .10
_SOURCE_SCRIPT_MAX_DEPTH_FONT = .60
_SOURCE_SCRIPT_CELL_FRAME_MARGIN = 1.0
_SOURCE_SCRIPT_LINE_BASELINE_TOLERANCE_FONT = .40
_SOURCE_SCRIPT_DIGIT_X_MARGIN_FONT = .05
_SOURCE_SCRIPT_DIGIT_TOP_MARGIN_FONT = .10
_SOURCE_SCRIPT_IDENTIFIER_SIDE_MARGIN_FONT = .10
_SOURCE_SCRIPT_MIN_FIELD_HEIGHT_GLYPH = .20
# END source_scripts constants

# BEGIN table_notes constants
PRIMARY_NOTE_X_TOLERANCE = .02  # Relative uncertainty of matching table sides.
PRIMARY_NOTE_TOP_TOLERANCE = .04  # OCR table and measured body may differ at the top.
PRIMARY_NOTE_MAX_GAP = .08  # A separately detected note must closely follow the table.
PRIMARY_NOTE_MAX_ADJACENT_PARAGRAPHS = 20  # Bound a direct sibling run; never scan a document.
PRIMARY_NOTE_BBOX_MARGIN_EM = .50  # Source font evidence bounds table-edge uncertainty.
# END table_notes constants

ORIENTATION_SCHEMA = 4
ORIENTATION_CONFIDENCE = .90
ORIENTATION_MODEL_REVISION = "7330ab7039123e46af2dc03154b9969aa412c61d"
ORIENTATION_MODEL_SHA256 = "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92"
BLANK_PAGE_HTML = '<div data-docling-blank="true"></div>'
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR
# Set before importing torch (including imports through Docling).
if platform.system() == "Darwin":
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

SURYA_VERSIONS = {'docling-surya-ocr': '0.2.0', 'surya-ocr': '0.22.1'}
SURYA_INSTALL = 'python -m pip install -r requirements-docling-surya.txt'


IMAGE_SCALE = 4.2
RETAINED_IMAGE_SCALE = 1.5
TABLE_REGION_ATTEMPTS = 4
LOCAL_REQUEST_SECONDS = 90.0
API_IMAGE_SCALE = 2.2
API_MODEL = "gpt-6-luna"
API_URL = "https://api.openai.com/v1/chat/completions"
API_TIMEOUT = 900.0
API_MAX_TOKENS = 16384
API_RETRIES = 4
BLANK_INK_THRESHOLD = 0.00001
NUM_THREADS = min(8, os.cpu_count() or 1)
OCR_BATCH_SIZE = 1
SURYA_BACKEND = "llamacpp"  # Native CUDA / Metal / CPU; no Docker setup required.

_DIAGNOSTICS = None
_PRIMARY_OCR_ONLY = ContextVar('docling_primary_ocr_only', default=False)


class PrimaryContentOnlyRecognizer:
    """Keep source pixels when the primary Surya response has no usable math."""
    def __init__(self, args):
        self.args = args

    def __call__(self, data, metadata):
        diagnostic_event('PDF_SECONDARY_CONTENT_OCR_DISABLED',
                         reason='primary_surya_content_only', **metadata)
        return None

    def close(self):
        pass


def diagnostic_clean(value):
    try:return diagnostic_clean_value(value)
    except Exception:return '<redaction-unavailable>'


def diagnostic_clean_value(value):
    text = str(value)
    text = re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+', '<image-data-omitted>', text)
    text = re.sub(r'(?i)Bearer\s+[^\s\'"<>,}]+', 'Bearer <redacted>', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '<redacted-key>', text)
    recorder=_DIAGNOSTICS
    if recorder is not None:
        for secret in tuple(recorder.secrets):
            text = text.replace(secret, '<redacted>')
    return text


def diagnostic_summary(value, name='', depth=0):
    try:return diagnostic_summary_value(value,name,depth)
    except Exception:return {'type':type(value).__name__,'summary':'unavailable'}


def diagnostic_summary_value(value, name='', depth=0):
    if re.search(r'api.?key|password|secret|authorization|access.?token|refresh.?token|headers', name, re.I):
        return '<redacted>'
    if value is None or isinstance(value, (bool,int,float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        sample=value[:2048]
        if len(value)>600 or '<table' in sample or 'data:image/' in sample:
            return {'type':'text','length':len(value),'sample_sha256':hashlib.sha256(sample.encode('utf-8',errors='replace')).hexdigest(),
                    'sample_table_tags':sample.count('<table'),'sample_image_tags':sample.count('<img'),'sample_math_tags':sample.count('<math')}
        return diagnostic_clean(value)[:400]
    if isinstance(value, (bytes,bytearray)):
        return {'type':'bytes','length':len(value)}
    if isinstance(value, dict):
        if depth>=2:return {'type':'dict','length':len(value)}
        safe={'page','page_no','ref','method','mode','device','bbox','region','issues','warnings','score','columns','rows',
              'basis','source_pages','joined_into','numeric_disagreement','operation','compatible_boundary',
              'intervening_blocks','explicit_continuation','continued_data_head','translated_grid','row_key_continuity'}
        out={key:diagnostic_summary(value[key],key,depth+1) for key in sorted(safe) if key in value}
        return {'type':'dict','length':len(value),'fields':out,'keys':[diagnostic_clean(k)[:60] for k in islice(value,16)]}
    if isinstance(value, (list,tuple,set)):
        return {'type':type(value).__name__,'length':len(value),'sample':[
            diagnostic_summary(item,depth=depth+1) for item in islice(value,8)]} if depth<2 else {'type':type(value).__name__,'length':len(value)}
    try:data=object.__getattribute__(value,'__dict__')
    except (AttributeError,TypeError):data={}
    fields={key:diagnostic_summary(data[key],key,2) for key in (
        'self_ref','page_no','label','status','num_rows','num_cols','mode','device','pages','page_range','_size',
        'l','t','r','b','coord_origin') if key in data}
    for key in ('tables','pictures','texts','children','captions','blocks'):
        if key in data and isinstance(data[key],(list,tuple,dict)):fields[key+'_count']=len(data[key])
    return {'type':type(value).__name__,**fields}


def diagnostic_event(event, **fields):
    if _DIAGNOSTICS is not None:
        try:_DIAGNOSTICS.event(event,**fields)
        except Exception:pass


@lru_cache(maxsize=1)
def diagnostic_return_guards(source_mtime_ns):
    """Share static source analysis between independent document log sessions."""
    source = Path(__file__).read_text('utf-8')
    guards_by_line = {}
    def walk(node, guards=()):
        if isinstance(node, ast.Return):
            guards_by_line[node.lineno] = ' AND '.join(guards[-3:])[:500]
        if isinstance(node, ast.If):
            condition = ast.get_source_segment(source, node.test) or ''
            for child in node.body:
                walk(child, guards+(condition,))
            for child in node.orelse:
                walk(child, guards+('NOT ('+condition+')',))
        else:
            for child in ast.iter_child_nodes(node):
                walk(child, guards)
    walk(ast.parse(source))
    return guards_by_line


class ConversionTrace:
    """Bounded function tracing plus uncapped semantic conversion decisions.

    Never retain frames, document objects, images, credentials or raw HTML.
    Hot per-glyph helpers are counted and summarized, rather than flushed to
    disk millions of times. Decision/audit events are never sampled.
    """
    HOT={'inside','wall','key','numeric','numeric_text','separated','same_glyph_text',
         'cell_text_key','furniture_key','joined_russian_word','transform'}

    def __init__(self,report,lock,*,build_guards=True):
        self.report,self.lock=report,lock
        self.run_id=uuid4().hex[:12]
        self.filename=str(Path(__file__).resolve())
        self.sequence=0;self.calls=0;self.frames={};self.stats={};self.local=threading.local();self.secrets=set();self.errors=0
        self.image_origins={}
        self.active=True;self.tracing=True
        self.last_flush=time.monotonic()
        self.started=time.perf_counter()
        self.return_guards={}
        try:
            if not build_guards:return
            self.return_guards=diagnostic_return_guards(Path(__file__).stat().st_mtime_ns)
        except Exception:pass

    def event(self,event,**fields):
        if not self.active:return
        try:
            with self.lock:
                if self.report.closed:return
                self.sequence+=1
                details=' '.join(f'{key}={diagnostic_clean(value)!r}' for key,value in fields.items())
                self.report.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] [TRACE {self.run_id} pid={os.getpid()} '
                                  f'thread={threading.get_ident()} seq={self.sequence}] {event} {details}\n')
                now=time.monotonic()
                if event not in {'CALL','RETURN','YIELD','RESUME','FUNCTION_SUMMARY'} or now-self.last_flush>=.25:
                    self.report.flush();self.last_flush=now
        except Exception:self.errors+=1

    def trace(self,frame,event,arg):
        if not self.active or not self.tracing:return None
        try:
            code=frame.f_code;name=code.co_qualname
            if code.co_filename!=self.filename or name.startswith(('ConversionTrace.','TeeTextStream.','TeeBinaryStream.','DiagnosticReportRouter.')) \
                    or code.co_name.startswith('diagnostic_') or code.co_name=='report_event':return None
            frame.f_trace_lines=False;frame.f_trace_opcodes=False
            ident=id(frame)
            stack=getattr(self.local,'stack',None)
            if stack is None:self.local.stack=stack=[]
            key=(name,code.co_firstlineno)
            stat=self.stats.setdefault(key,{'calls':0,'returns':0,'none':0,'exceptions':0,'yields':0,'seconds':0.0,'return_lines':{}})
            if event=='call':
                state=self.frames.get(ident)
                if state is None:
                    stat['calls']+=1
                    hot=code.co_name.startswith('<') or code.co_name in self.HOT
                    detailed=stat['calls']<=(3 if hot else 20)
                    with self.lock:
                        self.calls+=1
                        call_id=self.calls
                    state={'id':call_id,'name':name,'key':key,'detailed':detailed,'elapsed':0.0,'parent':stack[-1] if stack else None}
                    self.frames[ident]=state
                    if detailed:
                        count=code.co_argcount+code.co_kwonlyargcount
                        arguments={n:diagnostic_summary(frame.f_locals.get(n),n) for n in code.co_varnames[:count]}
                        self.event('CALL',call=state['id'],parent=state['parent'],function=name,line=code.co_firstlineno,args=arguments)
                elif state['detailed']:self.event('RESUME',call=state['id'],function=name,line=frame.f_lineno)
                state['started']=time.perf_counter();stack.append(state['id'])
            elif event=='exception':
                stat['exceptions']+=1
                self.event('EXCEPTION',call=self.frames.get(ident,{}).get('id'),function=name,line=frame.f_lineno,
                           exception=type(arg[1]).__name__,message=diagnostic_clean(arg[1])[:800])
            elif event=='return':
                state=self.frames.get(ident)
                if state is None:return self.trace
                elapsed=time.perf_counter()-state['started'];state['elapsed']+=elapsed
                if stack and stack[-1]==state['id']:stack.pop()
                opcode=dis.opname[code.co_code[frame.f_lasti]] if frame.f_lasti>=0 else ''
                yielded=opcode in {'YIELD_VALUE','YIELD_FROM'}
                if yielded:stat['yields']+=1
                else:
                    stat['returns']+=1;stat['none']+=arg is None;stat['seconds']+=state['elapsed']
                    stat['return_lines'][frame.f_lineno]=stat['return_lines'].get(frame.f_lineno,0)+1
                    self.frames.pop(ident,None)
                if state['detailed']:
                    context={n:diagnostic_summary(frame.f_locals[n],n) for n in (
                        'page_no','row','column','method','kind','region','bbox','expected','reason','attempt','seconds','retries') if n in frame.f_locals}
                    self.event('YIELD' if yielded else ('RETURN' if opcode.startswith('RETURN') else 'UNWIND'),
                               call=state['id'],function=name,line=frame.f_lineno,elapsed_ms=round(state['elapsed']*1000,3),
                               result=diagnostic_summary(arg),context=context,
                               lexical_if_context=self.return_guards.get(frame.f_lineno,''))
            return self.trace
        except Exception:
            self.errors+=1
            return self.trace

    def summary(self,stage):
        for (name,line),stat in sorted(self.stats.items()):
            self.event('FUNCTION_SUMMARY',stage=stage,function=name,line=line,counts=stat)
        self.event('TRACE_SUMMARY',stage=stage,functions=len(self.stats),events=self.sequence,
                   live_frames=len(self.frames),diagnostic_errors=self.errors,
                   wall_seconds=round(time.perf_counter()-self.started,3))


@contextmanager
def diagnostic_session(report,lock,*,trace_functions=True):
    global _DIAGNOSTICS
    previous=_DIAGNOSTICS
    try:recorder=ConversionTrace(report,lock,build_guards=trace_functions)
    except Exception as exc:
        report_event(report,lock,'diagnostic error',f'Could not initialize diagnostics: {diagnostic_clean(exc)}')
        yield None
        return
    _DIAGNOSTICS=recorder
    old_sys,old_thread=sys.gettrace(),threading.gettrace()
    enabled=False
    try:
        if trace_functions:
            try:
                threading.settrace(recorder.trace);sys.settrace(recorder.trace)
                enabled=True
            except Exception as exc:
                sys.settrace(old_sys);threading.settrace(old_thread)
                recorder.event('DIAGNOSTIC_ERROR',operation='enable_function_tracing',error=diagnostic_clean(exc)[:400])
        recorder.event('SESSION_START',function_tracing=enabled,detail_limit=20,hot_detail_limit=3,
                       lexical_if_context='static source conditions; not evaluated runtime reasons')
        yield recorder
    finally:
        recorder.tracing=False
        try:
            if trace_functions:
                try:
                    sys.settrace(old_sys);threading.settrace(old_thread)
                except Exception as exc:
                    recorder.event('DIAGNOSTIC_ERROR',operation='restore_tracing',error=diagnostic_clean(exc)[:400])
            recorder.summary('session_end')
        except Exception:pass
        finally:
            recorder.active=False;recorder.frames.clear();recorder.secrets.clear();recorder.image_origins.clear()
            _DIAGNOSTICS=previous


def diagnostic_startup():
    args=[];redact_next=False
    for value in sys.argv:
        flag,equals,credential=value.partition('=')
        if redact_next:
            if _DIAGNOSTICS is not None and value:_DIAGNOSTICS.secrets.add(value)
            args.append('<redacted>');redact_next=False
        elif flag.startswith('--') and len(flag)>2 and '--api-key'.startswith(flag):
            if equals:
                if _DIAGNOSTICS is not None and credential:_DIAGNOSTICS.secrets.add(credential)
                args.append(flag+'=<redacted>')
            else:args.append(flag);redact_next=True
        else:args.append(diagnostic_clean(value))
    path=Path(__file__).resolve()
    diagnostic_event('RUNTIME',script=str(path),script_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     script_mtime_ns=path.stat().st_mtime_ns,python=sys.executable,python_version=sys.version,
                     cwd=str(Path.cwd()),argv=args,schemas=[LOCAL_SCHEMA,TABLE_RECOVERY_SCHEMA,TABLE_JOIN_SCHEMA,TABLE_CELL_SCHEMA])
    loaded=[]
    for name,obj in sorted(globals().items()):
        code=getattr(obj,'__code__',None)
        if code is not None and code.co_filename==str(path):
            digest=hashlib.sha256(marshal.dumps(code)).hexdigest();loaded.append(name+':'+digest)
            diagnostic_event('LOADED_CODE',function=name,sha256=digest,line=code.co_firstlineno)
    diagnostic_event('LOADED_MODULE',fingerprint=hashlib.sha256('\n'.join(loaded).encode()).hexdigest())
    for name in ('docling_office.py', 'docling_office_math.py', 'docling_layout.py', 'docling_symbol_evidence.py',
                 'docling_table_content.py', 'docling_pdf_repairs.py'):
        diagnostic_file(SCRIPT_DIR/name, 'converter_module')
    diagnostic_event('LOCAL_RUNTIME', binary=os.environ.get('LLAMA_CPP_BINARY'),
                     backend=SURYA_BACKEND, device=os.environ.get('TORCH_DEVICE'))
    for package in ('docling','docling-core','docling-surya-ocr','surya-ocr','torch','pdfplumber','pypdfium2',
                    'opencv-python-headless','Pillow','rapidocr','onnxruntime','pymorphy3'):
        try:installed=version(package)
        except PackageNotFoundError:installed='unavailable'
        diagnostic_event('DEPENDENCY',package=package,version=installed)


def diagnostic_file(path,stage):
    if _DIAGNOSTICS is None:return
    try:
        path=Path(path).resolve();digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        info=path.stat()
        diagnostic_event('FILE',stage=stage,path=path,size=info.st_size,mtime_ns=info.st_mtime_ns,sha256=digest.hexdigest())
    except Exception as exc:
        diagnostic_event('DIAGNOSTIC_ERROR',operation='file_fingerprint',path=path,error=diagnostic_clean(exc)[:400])


def diagnostic_model(doc,records=(),stage='model_result'):
    if _DIAGNOSTICS is None:return
    try:
        diagnostic_event('MODEL_INVENTORY',stage=stage,tables=len(doc.tables),pictures=len(doc.pictures),
                         texts=len(doc.texts),pages=sorted(doc.pages),ocr_records=len(records))
        for item,_ in doc.iterate_items():
            prov=[{'page':p.page_no,'bbox':diagnostic_summary(p.bbox)} for p in getattr(item,'prov',())]
            text=getattr(item,'text','')
            diagnostic_event('MODEL_ITEM',stage=stage,ref=item.self_ref,label=str(getattr(item,'label','')),
                             parent=getattr(getattr(item,'parent',None),'cref',None),provenance=prov,
                             children=[c.cref for c in getattr(item,'children',())],
                             captions=[c.cref for c in getattr(item,'captions',())],
                             text_length=len(text),text_excerpt=diagnostic_clean(text)[:180])
        for index,record in enumerate(records):
            diagnostic_event('OCR_BLOCK',index=index,page=record.get('page_no'),bbox=record.get('bbox'),
                             label=record.get('label'),html=diagnostic_summary(record.get('html','')))
    except Exception as exc:
        diagnostic_event('DIAGNOSTIC_ERROR',operation='model_inventory',error=diagnostic_clean(exc)[:400])


def diagnostic_html(content,stage,**origin):
    """Audit emitted markup without logging image payloads or changing the HTML."""
    if _DIAGNOSTICS is None:return {}
    try:
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(content,'html.parser') if isinstance(content,str) else content
        tables=soup.find_all('table');images=soup.find_all('img')
        counts={'tables':len(tables),'images':len(images),'images_inside_tables':sum(img.find_parent('table') is not None for img in images),
                'images_outside_tables':sum(img.find_parent('table') is None for img in images),
                'source_table_figures':len(soup.select('figure.source-table')),'math':len(soup.find_all('math'))}
        diagnostic_event('HTML_INVENTORY',stage=stage,**origin,**counts)
        for index,table in enumerate(tables):
            diagnostic_event('HTML_TABLE',stage=stage,**origin,index=index,rows=len(table.find_all('tr')),
                             cells=len(table.find_all(['td','th'])),images=len(table.find_all('img')),
                             caption=table.caption.get_text(' ',strip=True)[:200] if table.caption else '')
        for index,img in enumerate(images):
            src=img.get('src','');digest=hashlib.sha256(src.encode('utf-8')).hexdigest();dimensions=None
            # A PNG's IHDR is in the first 24 decoded bytes; do not decode the image.
            if src.startswith('data:image/png;base64,'):
                try:
                    head=base64.b64decode(src.split(',',1)[1][:44])
                    if head[:8]==b'\x89PNG\r\n\x1a\n':dimensions=[int.from_bytes(head[16:20],'big'),int.from_bytes(head[20:24],'big')]
                except (ValueError,TypeError):pass
            if origin:
                entry={key:str(value)[:200] for key,value in origin.items()}
                with _DIAGNOSTICS.lock:
                    owners=_DIAGNOSTICS.image_origins.setdefault(digest,[])
                    if entry not in owners and len(owners)<8:owners.append(entry)
            figure=img.find_parent('figure');caption=figure.find('figcaption') if figure else None
            diagnostic_event('HTML_IMAGE',stage=stage,**origin,index=index,src_sha256=digest,src_length=len(src),
                             mime=src.split(';',1)[0][:60] if src.startswith('data:') else 'external',dimensions=dimensions,
                             inside_table=img.find_parent('table') is not None,figure_class=figure.get('class',[]) if figure else [],
                             caption=caption.get_text(' ',strip=True)[:200] if caption else '',
                             alt=diagnostic_clean(img.get('alt',''))[:120],origins=_DIAGNOSTICS.image_origins.get(digest,[]))
        return counts
    except Exception as exc:
        diagnostic_event('DIAGNOSTIC_ERROR',operation='html_inventory',stage=stage,error=diagnostic_clean(exc)[:400])
        return {}


def diagnostic_report_write(report,lock,text):
    try:
        with lock:
            if not report.closed:
                report.write(diagnostic_clean(text).replace('\r','\n'))
                report.flush()
    except Exception:pass


class DiagnosticReportRouter:
    """Keep retained console logging handlers attached across document logs."""
    def __init__(self):
        self.target = None

    @property
    def closed(self):
        return self.target is None or self.target.closed

    def write(self, text):
        if not self.closed:
            return self.target.write(text)
        return 0

    def flush(self):
        if not self.closed:
            self.target.flush()


def diagnostic_log_paths(sources, directory=None):
    """Name logs by input stem; disambiguate only conflicting source names."""
    directory = Path(directory) if directory is not None else Path.cwd()/'convert_docling_log'
    sources = list(sources)
    groups = {}
    for source in sources:
        key = unicodedata.normalize('NFC', source.stem).casefold()
        groups.setdefault(key, []).append(source)
    result = {}
    for source in sources:
        key = unicodedata.normalize('NFC', source.stem).casefold()
        target = directory/f'{source.stem}_log.txt'
        conflict = len(groups[key]) > 1 or key == '_startup'
        if not conflict and target.is_file():
            try:
                with target.open('r', encoding='utf-8') as previous:
                    owner = previous.readline(65536).removeprefix('Source: ').strip()
                conflict = bool(owner and unicodedata.normalize('NFC', owner).casefold()
                                != unicodedata.normalize('NFC', str(source.resolve())).casefold())
            except (OSError, UnicodeError):
                conflict = True
        if conflict:
            identity = unicodedata.normalize('NFC', str(source.resolve())).casefold()
            digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]
            target = directory/f'{source.stem}__{source.suffix.lstrip(".").lower()}_{digest}_log.txt'
        result[source] = target
    return result


@contextmanager
def diagnostic_document_log(router, lock, path, source, args):
    """Open a fresh log for one source and always release it after the attempt."""
    report = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        report = path.open('w', encoding='utf-8', buffering=65536)
        report.write(f'Source: {source.resolve()}\n')
    except OSError as error:
        if report is not None:
            report.close()
        report = None
        print(f'WARNING: Could not create conversion log {path}: {error}', file=sys.stderr)
    if report is None:
        yield None
        return
    router.target = report
    try:
        with diagnostic_session(router, lock,
                                trace_functions=os.environ.get('DOCLING_TRACE_FUNCTIONS', '1') != '0') as recorder:
            if recorder is not None and getattr(args, 'api_key', None):
                recorder.secrets.add(args.api_key)
            report_event(router, lock, 'document log started', f'log={path}; pid={os.getpid()}')
            try:
                diagnostic_startup()
            except Exception as error:
                diagnostic_event('DIAGNOSTIC_ERROR', operation='startup', error=diagnostic_clean(error)[:400])
            diagnostic_event('DOCUMENT_LOG', source=str(source.resolve()), log=str(path.resolve()),
                             output=str(Path(f'{output_base(args, source)}.{args.format}').resolve()))
            try:
                yield router
            finally:
                report_event(router, lock, 'document log finished', f'source={source}')
    finally:
        router.target = None
        try:
            report.flush()
            report.close()
        except OSError as error:
            try:
                report.close()
            except OSError:
                pass
            print(f'WARNING: Could not finish conversion log {path}: {error}', file=sys.stderr)
        print(f'Conversion log: {path}', flush=True)


class TeeBinaryStream:
    """Forward binary writes to the terminal and the run report."""

    def __init__(self, console, report, lock, encoding, errors):
        self.console = console
        self.report = report
        self.lock = lock
        self.encoding = encoding
        self.errors = errors

    def write(self, data):
        written = self.console.write(data)
        text = data.decode(self.encoding or "utf-8", errors=self.errors or "replace")
        diagnostic_report_write(self.report,self.lock,text)
        return written

    def flush(self):
        self.console.flush()
        try:
            with self.lock:
                if not self.report.closed:self.report.flush()
        except Exception:pass

    def __getattr__(self, name):
        return getattr(self.console, name)


class TeeTextStream:
    """Mirror text output without changing terminal behavior such as isatty()."""

    def __init__(self, console, report, lock):
        self.console = console
        self.report = report
        self.lock = lock
        self._buffer = TeeBinaryStream(
            console.buffer, report, lock,
            getattr(console, "encoding", None), getattr(console, "errors", None),
        ) if hasattr(console, "buffer") else None

    @property
    def encoding(self):
        return getattr(self.console, "encoding", None)

    @property
    def errors(self):
        return getattr(self.console, "errors", None)

    @property
    def buffer(self):
        if self._buffer is None:
            raise AttributeError("buffer")
        return self._buffer

    def write(self, text):
        written = self.console.write(text)
        # Colorama/logging can retain this stream until interpreter shutdown.
        diagnostic_report_write(self.report,self.lock,text)
        return written

    def flush(self):
        self.console.flush()
        try:
            with self.lock:
                if not self.report.closed:self.report.flush()
        except Exception:pass

    def isatty(self):
        return self.console.isatty()

    def fileno(self):
        return self.console.fileno()

    def __getattr__(self, name):
        return getattr(self.console, name)


def report_event(report, lock, label, message):
    if report is None:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    diagnostic_report_write(report,lock,f"\n[{stamp}] [{label}] {message}\n")


class PageConversionProgress:
    """Report actual Docling page completions to stdout (and its log tee)."""

    def __init__(self, source):
        self.source = source
        self.successful_pages = set()
        self.failed_pages = set()

    def start(self, total_pages):
        print(f"Конвертация: {self.source} | сконвертировано страниц: 0 из {total_pages}",
              flush=True)

    def __call__(self, event):
        from docling.datamodel.progress import ConversionProgressKind

        if event.kind != ConversionProgressKind.PAGE_COMPLETED:
            return
        if event.page_no in self.successful_pages or event.page_no in self.failed_pages:
            return
        if event.success:
            self.successful_pages.add(event.page_no)
        else:
            self.failed_pages.add(event.page_no)
        details = f"страница {event.page_no}: {'готово' if event.success else 'ошибка'}"
        if self.failed_pages:
            processed = len(self.successful_pages) + len(self.failed_pages)
            details += f"; обработано: {processed} из {event.total_pages}; ошибок: {len(self.failed_pages)}"
        print(f"Конвертация: {self.source} | сконвертировано страниц: "
              f"{len(self.successful_pages)} из {event.total_pages} | {details}", flush=True)


def format_mib(value):
    return f"{value / (1024 ** 2):.1f} MiB"


def windows_memory_snapshot(include_process=True):
    """Read Windows physical/commit limits and, when needed, process memory."""
    import ctypes
    from ctypes import wintypes

    details = []

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatusEx)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        details.append(
            f"RAM available={format_mib(status.ullAvailPhys)}, total={format_mib(status.ullTotalPhys)}, "
            f"load={status.dwMemoryLoad}%"
        )
        details.append(
            f"Windows commit available={format_mib(status.ullAvailPageFile)}, "
            f"limit={format_mib(status.ullTotalPageFile)}"
        )

    if include_process:
        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_process_memory = psapi.GetProcessMemoryInfo
        get_process_memory.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCountersEx), wintypes.DWORD,
        ]
        get_process_memory.restype = wintypes.BOOL
        if get_process_memory(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            details.append(
                f"Python RSS={format_mib(counters.WorkingSetSize)}, "
                f"private={format_mib(counters.PrivateUsage)}"
            )
    return details


def memory_snapshot(device):
    """Best-effort host/process and accelerator memory diagnostics."""
    details = []
    psutil = None
    try:
        import psutil
    except ImportError:
        details.append("psutil unavailable; using operating-system memory counters")

    if psutil is not None:
        try:
            if os.name != "nt":
                host = psutil.virtual_memory()
                details.append(
                    f"RAM available={format_mib(host.available)}, total={format_mib(host.total)}, "
                    f"load={host.percent:.1f}%"
                )
                swap = psutil.swap_memory()
                details.append(f"swap/pagefile available={format_mib(swap.free)}, total={format_mib(swap.total)}")
            process = psutil.Process(os.getpid())
            current = process.memory_info()
            details.append(f"Python RSS={format_mib(current.rss)}, VMS={format_mib(current.vms)}")
            children = []
            for child in process.children(recursive=True):
                try:
                    info = child.memory_info()
                    children.append(f"{child.name()} pid={child.pid} RSS={format_mib(info.rss)}")
                except (psutil.Error, OSError):
                    continue
            if children:
                details.append("child processes: " + "; ".join(children))
        except Exception as exc:
            details.append(f"psutil memory stats unavailable: {type(exc).__name__}: {exc}")
    elif os.name != "nt":
        try:
            if hasattr(os, "sysconf"):
                page_size = os.sysconf("SC_PAGE_SIZE")
                total_pages = os.sysconf("SC_PHYS_PAGES")
                available_pages = os.sysconf("SC_AVPHYS_PAGES")
                details.append(
                    f"RAM available={format_mib(page_size * available_pages)}, "
                    f"total={format_mib(page_size * total_pages)}"
                )
                try:
                    import resource

                    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    if platform.system() != "Darwin":
                        peak_rss *= 1024
                    details.append(f"Python peak RSS={format_mib(peak_rss)}")
                except (ImportError, OSError, ValueError):
                    pass
        except Exception as exc:
            details.append(f"host RAM stats unavailable: {type(exc).__name__}: {exc}")
    if os.name == "nt":
        try:
            details.extend(windows_memory_snapshot(include_process=psutil is None))
        except Exception as exc:
            details.append(f"Windows memory stats unavailable: {type(exc).__name__}: {exc}")

    if device == "cuda":
        try:
            import torch

            free, total = torch.cuda.mem_get_info()
            details.append(f"CUDA free={format_mib(free)}, total={format_mib(total)}")
            details.append(
                f"PyTorch CUDA allocated={format_mib(torch.cuda.memory_allocated())}, "
                f"reserved={format_mib(torch.cuda.memory_reserved())}"
            )
        except Exception as exc:
            details.append(f"CUDA memory stats unavailable: {type(exc).__name__}: {exc}")
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                result = subprocess.run(
                    [nvidia_smi, "--query-gpu=name,memory.total,memory.used,memory.free",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    details.append("nvidia-smi GPU memory (MiB): " + result.stdout.strip().replace("\n", " | "))
                elif result.stderr.strip():
                    details.append("nvidia-smi: " + result.stderr.strip())
            except Exception as exc:
                details.append(f"nvidia-smi unavailable: {type(exc).__name__}: {exc}")
    elif device == "mps":
        try:
            import torch

            current = getattr(torch.mps, "current_allocated_memory", None)
            driver = getattr(torch.mps, "driver_allocated_memory", None)
            if current:
                details.append(f"MPS allocated={format_mib(current())}")
            if driver:
                details.append(f"MPS driver allocated={format_mib(driver())}")
        except Exception as exc:
            details.append(f"MPS memory stats unavailable: {type(exc).__name__}: {exc}")

    return "; ".join(details) if details else "memory stats unavailable"


def resolve_device():
    """Automatically select the local accelerator usable by Docling."""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if (platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64")
            and torch.backends.mps.is_available()):
        return "mps"
    return "cpu"


def configure_surya(args):
    """Set Surya settings BEFORE plugin discovery imports surya.settings."""
    for package, expected in SURYA_VERSIONS.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = None
        if installed != expected:
            raise ValueError(f"Requires {package}=={expected} (installed: {installed}). {SURYA_INSTALL}")
    os.environ["TORCH_DEVICE"] = args.device
    os.environ["SURYA_INFERENCE_BACKEND"] = SURYA_BACKEND
    os.environ["SURYA_INFERENCE_PARALLEL"] = str(OCR_BATCH_SIZE)
    os.environ["SURYA_INFERENCE_KEEP_ALIVE"] = "false"
    os.environ["SURYA_INFERENCE_STARTUP_TIMEOUT"] = "600"
    # httpx also reads the Windows system proxy. Loopback must reach the local
    # server directly, even when the computer uses a VPN/proxy for the internet.
    bypass = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    os.environ["NO_PROXY"] = ",".join(filter(None, (bypass, "localhost", "127.0.0.1", "::1")))
    os.environ.pop("SURYA_INFERENCE_URL", None)
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    candidates = [SCRIPT_DIR / ".runtime" / "llamacpp" / name]
    cached = runtime_directory(args.device)
    candidates.extend(cached.rglob(name) if cached.is_dir() else ())
    on_path = shutil.which(name)
    if on_path:
        candidates.append(Path(on_path))
    # CUDA release archives can also use CUDA 12 DLLs shipped with PyTorch.
    if os.name == "nt":
        import torch
        torch_lib = str(Path(torch.__file__).parent / "lib")
        os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
    def usable(candidate):
        try:
            probe = subprocess.run([str(candidate), "--list-devices"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return False
        devices = probe.stdout + probe.stderr
        return probe.returncode == 0 and (
            args.device == "cpu"
            or args.device == "cuda" and bool(re.search(r"CUDA\d*:", devices, re.I))
            or args.device == "mps" and "metal" in devices.lower()
        )

    binary = next((str(path) for path in candidates if path.is_file() and usable(path)), None)
    if binary is None:
        binary = offer_llama_install(args.device)
        if not usable(binary):
            raise ValueError("Установленная сборка llama.cpp не запускается на этой системе")
    os.environ["LLAMA_CPP_BINARY"] = str(binary)
    os.environ["LLAMA_CPP_NGL"] = "0" if args.device == "cpu" else "99"
    os.environ["LLAMA_CPP_NO_MMPROJ_OFFLOAD"] = "true" if args.device == "cpu" else "false"
    # Keep the vision encoder and KV cache within a laptop's memory budget.
    os.environ["LLAMA_CPP_EXTRA_ARGS"] = f"--threads {NUM_THREADS}"
    print(f"Surya 2 runtime: {binary}; device: {args.device}", flush=True)
    if os.name == "nt":
        from surya.inference.backends import spawn

        def stop_windows_server(pid, name):
            # Surya 0.22.1's POSIX os.kill(pid, 0) polling is not a safe
            # liveness check on Windows. Its atexit owner still decides
            # which server may be stopped; only the stop primitive differs.
            # taskkill can hang while waiting for a process tree to exit. Do
            # not let that escape Surya's atexit callback; terminate the known
            # server PID directly as a fallback, then report any cleanup issue.
            try:
                result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                        capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=15,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode == 0:
                    return
                taskkill_error = f"exit code {result.returncode}: {result.stderr.strip()}"
            except Exception as exc:
                taskkill_error = exc

            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                open_process = kernel32.OpenProcess
                open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                open_process.restype = wintypes.HANDLE
                terminate_process = kernel32.TerminateProcess
                terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
                terminate_process.restype = wintypes.BOOL
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL

                handle = open_process(0x0001, False, int(pid))  # PROCESS_TERMINATE
                if not handle:  # taskkill may have finished just before timing out
                    return
                try:
                    if not terminate_process(handle, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    close_handle(handle)
            except Exception as exc:
                print(f"WARNING: Could not stop {name} (pid {pid}); taskkill failed ({taskkill_error}), "
                      f"and direct termination failed ({exc})", file=sys.stderr)

        spawn._stop_process = stop_windows_server


def runtime_directory(device):
    from platformdirs import user_data_path
    return user_data_path("docling-surya", appauthor=False) / f"llamacpp-{device}"


def offer_llama_install(device):
    """Install an official portable release only after an explicit CLI y."""
    if not sys.stdin.isatty():
        raise ValueError("llama-server not found. Run in an interactive terminal to install (y/n). See requirements-docling-surya.txt")
    destination = runtime_directory(device)
    try:
        answer = input(f"Подходящий llama-server не найден. Установить официальную сборку llama.cpp "
                       f"({device}) в {destination}? [y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer != "y":
        raise ValueError("Установка отменена. Повторите запуск и подтвердите установку llama.cpp")
    return install_llama(device, destination)


def install_llama(device, destination):
    """Download official release assets; server lifecycle stays owned by Surya."""
    import tarfile
    import tempfile
    import zipfile
    from urllib.request import Request, urlopen

    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("amd64", "x86_64") else None
    if arch is None:
        raise ValueError(f"Automatic llama.cpp installation is unsupported on {machine}")
    if system == "Windows":
        target = f"win-cuda-12.4-{arch}.zip" if device == "cuda" else f"win-cpu-{arch}.zip"
    elif system == "Darwin":
        target = f"macos-{arch}.tar.gz"
    elif system == "Linux":
        target = f"ubuntu-cuda-12.8-{arch}.tar.gz" if device == "cuda" else f"ubuntu-{arch}.tar.gz"
    else:
        raise ValueError(f"Automatic llama.cpp installation is unsupported on {system}")

    def read_url(url):
        return urlopen(Request(url, headers={"User-Agent": "convert-docling-surya"}), timeout=120)

    with read_url("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10") as response:
        releases = json.load(response)
    assets = next((release["assets"] for release in releases if any(
        item["name"].endswith("bin-" + target) and item["name"].startswith("llama-")
        for item in release["assets"])), None)
    if assets is None:
        raise ValueError(f"No official llama.cpp archive for {target}; install manually from GitHub releases")
    selected = [next(item for item in assets if item["name"].endswith("bin-" + target)
                     and item["name"].startswith("llama-"))]
    if device == "cuda":
        selected.extend(item for item in assets
                        if item["name"].startswith("cudart-") and item["name"].endswith("bin-" + target))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="docling-llamacpp-") as temporary:
        for asset in selected:
            archive = Path(temporary) / asset["name"]
            print(f"Downloading {asset['name']} ({asset['size'] / 1024**2:.0f} MB)...", flush=True)
            with read_url(asset["browser_download_url"]) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            digest = asset.get("digest")
            if digest and digest.startswith("sha256:"):
                with archive.open("rb") as source:
                    actual = hashlib.file_digest(source, "sha256").hexdigest()
                if actual != digest.removeprefix("sha256:"):
                    raise ValueError(f"Checksum mismatch for {asset['name']}")
            if archive.suffix == ".zip":
                with zipfile.ZipFile(archive) as package:
                    if any(not (destination / name).resolve().is_relative_to(destination.resolve())
                           for name in package.namelist()):
                        raise ValueError("Unsafe path in llama.cpp archive")
                    package.extractall(destination)
            else:
                with tarfile.open(archive) as package:
                    package.extractall(destination, filter="data")
    name = "llama-server.exe" if system == "Windows" else "llama-server"
    binary = next(destination.rglob(name), None)
    if binary is None:
        raise ValueError("The downloaded archive contains no llama-server")
    if system != "Windows":
        binary.chmod(binary.stat().st_mode | 0o111)
    print(f"Installed llama.cpp: {binary}", flush=True)
    return str(binary)


class PageOrientationClassifier:
    """Official PP-LCNet four-angle model using the existing CPU ONNX runtime.

    Preprocessing follows the pinned model's inference.yml: RGB, short side
    256, center crop 224, ImageNet normalization, NCHW. No Paddle dependency.
    https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx
    """

    @staticmethod
    def model_path():
        from platformdirs import user_cache_path
        from urllib.request import Request, urlopen

        bundled = SCRIPT_DIR / 'models' / 'orientation' / 'PP-LCNet_x1_0_doc_ori.onnx'
        path = bundled if bundled.is_file() else user_cache_path("docling-surya", appauthor=False) / "orientation" / "PP-LCNet_x1_0_doc_ori.onnx"
        if path.is_file():
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == ORIENTATION_MODEL_SHA256:
                    return path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        url = ("https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx/resolve/"
               f"{ORIENTATION_MODEL_REVISION}/inference.onnx")
        print("Downloading page orientation model PP-LCNet (6.8 MB)...", flush=True)
        deadline, size, digest = time.monotonic() + 30, 0, hashlib.sha256()
        try:
            request = Request(url, headers={"User-Agent": "convert-docling-orientation"})
            with urlopen(request, timeout=10) as response, temporary.open("wb") as output:
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > 8 * 1024**2 or time.monotonic() > deadline:
                        raise RuntimeError("Page orientation model download exceeded its time/size limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != ORIENTATION_MODEL_SHA256:
                raise RuntimeError("Page orientation model checksum mismatch")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def __init__(self):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = min(2, NUM_THREADS)
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Reserve the GPU for the document models; this 7 MB classifier is
        # inexpensive on CPU and works with the same backend on every platform.
        self.session = ort.InferenceSession(str(self.model_path()), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if len(inputs) != 1 or inputs[0].shape[1:] != [3, 224, 224] or outputs[0].shape[-1] != 4:
            raise RuntimeError("Unexpected page orientation model input/output shape")
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name

    def predict(self, image):
        import cv2
        import numpy as np

        array = np.asarray(image.convert("RGB"))
        height, width = array.shape[:2]
        # A very long receipt/PDF must not allocate an enormous resized strip.
        # Only its center reaches the official 224px crop in either case.
        if height > width * 4:
            top = (height - width * 4) // 2
            array = array[top:top + width * 4]
        elif width > height * 4:
            left = (width - height * 4) // 2
            array = array[:, left:left + height * 4]
        height, width = array.shape[:2]
        scale = 256 / min(height, width)
        array = cv2.resize(array, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_LINEAR)
        height, width = array.shape[:2]
        top, left = (height - 224) // 2, (width - 224) // 2
        array = array[top:top + 224, left:left + 224].astype(np.float32)
        array *= np.float32(1 / 255)
        array -= np.array([.485, .456, .406], dtype=np.float32)
        array /= np.array([.229, .224, .225], dtype=np.float32)
        batch = np.ascontiguousarray(array.transpose(2, 0, 1)[None])
        scores = np.asarray(self.session.run([self.output_name], {self.input_name: batch})[0])
        # The pinned ONNX graph already contains softmax; applying it again
        # would destroy the confidence threshold.
        if (scores.shape != (1, 4) or not np.isfinite(scores).all()
                or np.any(scores < 0) or np.any(scores > 1) or abs(float(scores.sum()) - 1) > .001):
            raise RuntimeError("Invalid page orientation probabilities")
        index = int(scores[0].argmax())
        label = (0, 90, 180, 270)[index]
        # Paddle corrects this label counterclockwise. PDF /Rotate is clockwise.
        return (-label) % 360, {"classification_label": label, "confidence": round(float(scores[0, index]), 6),
                               "probabilities": {str(angle): round(float(score), 6)
                                                 for angle, score in zip((0, 90, 180, 270), scores[0])}}


class DetailedOrientationDetector:
    """Find text lines and vote on strip direction for ambiguous whole pages."""

    def __init__(self):
        from rapidocr import RapidOCR
        self.engine = RapidOCR(params={
            "Global.log_level": "error",
            "EngineConfig.onnxruntime.intra_op_num_threads": min(2, NUM_THREADS),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })

    def detect(self, image):
        import numpy as np
        from PIL import Image

        probe = image.convert("RGB")
        probe.thumbnail((1100, 1100), Image.Resampling.LANCZOS)
        array = np.asarray(probe)[:, :, ::-1].copy()
        result = self.engine(array, use_det=True, use_cls=False, use_rec=False)
        boxes = result.boxes
        if boxes is None or len(boxes) < 3:
            return 0, {"reason": "insufficient_text", "boxes": 0 if boxes is None else len(boxes)}
        horizontal = vertical = 0.0
        for box in boxes:
            width = float(np.linalg.norm(box[1] - box[0]))
            height = float(np.linalg.norm(box[3] - box[0]))
            if width > 1.6 * height:
                horizontal += width
            elif height > 1.6 * width:
                vertical += height
        total = horizontal + vertical
        clockwise = 90 if total and vertical / total > 0.72 else 0
        axis_confidence = max(horizontal, vertical) / total if total else 0.0
        # Upright pages and their drawings are never tested for a half-turn.
        # Direction voting is needed only to distinguish the two sideways views.
        if not clockwise:
            return 0, {"axis_confidence": round(axis_confidence, 4), "policy": "quarter_turns_only"}
        if clockwise:
            probe = probe.rotate(-clockwise, expand=True)
            array = np.asarray(probe)[:, :, ::-1].copy()
            boxes = self.engine(array, use_det=True, use_cls=False, use_rec=False).boxes
        votes = {"0": 0.0, "180": 0.0}
        crops = []
        if boxes is not None:
            for box in boxes:
                left, top = np.floor(box.min(axis=0)).astype(int)
                right, bottom = np.ceil(box.max(axis=0)).astype(int)
                left, top = max(0, left), max(0, top)
                right, bottom = min(array.shape[1], right), min(array.shape[0], bottom)
                if right - left > 2.2 * (bottom - top) and bottom - top >= 7:
                    # Short strips preserve letter shapes; squeezing a whole
                    # paragraph line into a 192px classifier destroys direction cues.
                    strip_width = min(right - left, 6 * (bottom - top))
                    for fraction in (0.0, 0.5, 1.0):
                        x = left + round(fraction * (right - left - strip_width))
                        crops.append(array[top:bottom, x:x + strip_width])
            if len(crops) > 24:
                crops = [crops[int(i)] for i in np.linspace(0, len(crops) - 1, 24)]
            loader = getattr(self.engine, "_load_cls_model", None)
            if crops and callable(loader):
                classifications = loader()(crops).cls_res or []
            else:
                classifications = [entry for crop in crops for entry in
                                   (self.engine(crop, use_det=False, use_cls=True, use_rec=False).cls_res or [])]
            for label, confidence in classifications:
                if label in votes and confidence >= 0.85:
                    votes[label] += float(confidence)
        vote_total = sum(votes.values())
        if vote_total >= 2.0 and votes["180"] / vote_total > 0.75:
            clockwise = (clockwise + 180) % 360
        return clockwise, {"axis_confidence": round(axis_confidence, 4), "direction_votes": votes}


class OrientationDetector:
    """Classify the whole page once; inspect text lines only when uncertain."""

    def __init__(self):
        self.classifier = None
        self.detailed = None
        self.classifier_error = None

    def detect(self, image):
        ink = sum(image.convert("L").histogram()[:220]) / (image.width * image.height)
        if ink < .0001:
            return 0, {"backend": "blank_page", "reason": "insufficient_ink"}
        prediction, fast_clockwise = None, None
        if self.classifier_error is None:
            try:
                if self.classifier is None:
                    self.classifier = PageOrientationClassifier()
                    print("Page orientation backend: PP-LCNet ONNX CPU; RapidOCR fallback", flush=True)
                fast_clockwise, prediction = self.classifier.predict(image)
                if fast_clockwise == 180:
                    return 0, {"backend": "pp_lcnet_onnx", **prediction,
                               "reason": "half_turn_disabled", "policy": "quarter_turns_only"}
                if prediction["confidence"] >= ORIENTATION_CONFIDENCE:
                    return fast_clockwise, {"backend": "pp_lcnet_onnx", **prediction}
            except Exception as exc:
                # A missing network/model/provider must not stop conversion.
                # Disable the failed classifier for this run, avoiding repeated
                # downloads or session construction for every page/document.
                self.classifier_error = f"{type(exc).__name__}: {exc}"
                self.classifier = None
                print(f"Page orientation classifier unavailable; using RapidOCR: {self.classifier_error}",
                      file=sys.stderr, flush=True)
        if self.detailed is None:
            self.detailed = DetailedOrientationDetector()
        clockwise, evidence = self.detailed.detect(image)
        return clockwise, {"backend": "rapidocr_fallback", **evidence,
                           "fallback_reason": "classifier_unavailable" if self.classifier_error else "low_confidence",
                           "fast_prediction": prediction, "classifier_error": self.classifier_error,
                           "policy": "quarter_turns_only"}


def get_orientation_detector(args):
    if not hasattr(args, "orientation_detector"):
        try:
            args.orientation_detector = OrientationDetector()
        except ImportError as exc:
            raise RuntimeError("Automatic orientation requires rapidocr (python -m pip install rapidocr)") from exc
    return args.orientation_detector


def source_bbox(bbox, page_info):
    """Map normalized display coordinates back to the source display page."""
    left, top, right, bottom = bbox
    width, height = page_info["original_size"]
    rotation = page_info["clockwise"]
    points = []
    for x, y in ((left, top), (right, top), (right, bottom), (left, bottom)):
        if rotation == 90:
            x, y = y, height - x
        elif rotation == 180:
            x, y = width - x, height - y
        elif rotation == 270:
            x, y = width - y, x
        points.append((x, y))
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


@contextmanager
def normalized_pdf(source, args):
    """Rotate entire pages in a temporary PDF, preserving their original contents."""
    import pypdfium2 as pdfium

    started = time.monotonic()
    with source.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    args.current_source_hash = source_hash
    detector = None
    info = {}
    pdf = pdfium.PdfDocument(str(source))
    try:
        first, last = args.page_range or (1, len(pdf))
        changed = False
        for index in range(max(first - 1, 0), min(last, len(pdf))):
            page = pdf[index]
            try:
                width, height = page.get_size()
                scale = min(1.35, 1100 / max(width, height))
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
                if detector is None:
                    detector = get_orientation_detector(args)
                clockwise, evidence = detector.detect(image)
                if clockwise not in (0, 90, 270):
                    raise RuntimeError(f'Unsupported page orientation: {clockwise}; only quarter turns are allowed')
                info[index + 1] = {"original_size": [width, height], "clockwise": clockwise,
                                   "pdf_rotation": page.get_rotation(), "evidence": evidence,
                                   "ink_fraction": sum(image.convert("L").histogram()[:220]) / (image.width * image.height)}
                if clockwise:
                    page.set_rotation((page.get_rotation() + clockwise) % 360)
                    changed = True
                evidence = info[index + 1]["evidence"]
                backend = evidence.get("backend", "rapidocr")
                confidence = evidence.get("confidence", (evidence.get("fast_prediction") or {}).get("confidence"))
                print(f"Orientation: page {index + 1}; clockwise {clockwise} degrees; {backend}"
                      + (f"; confidence {confidence:.3f}" if confidence is not None else "")
                      , flush=True)
            finally:
                page.close()
        backends = {}
        for page_info in info.values():
            name = page_info["evidence"].get("backend", "rapidocr")
            backends[name] = backends.get(name, 0) + 1
        print(f"Orientation completed: {len(info)} pages; "
              f"{time.monotonic() - started:.3f} seconds; backends {json.dumps(backends)}", flush=True)
        if changed:
            with tempfile.TemporaryDirectory(prefix="docling-normalized-") as temporary:
                normalized = Path(temporary) / "normalized.pdf"
                pdf.save(str(normalized))
                yield normalized, info
        else:
            yield source, info
    finally:
        pdf.close()


# BEGIN whole_source_image_bounds
def _source_image_bounds_box(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in value)
            and value[0] < value[2] and value[1] < value[3])


def _source_image_bounds_inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _source_image_bounds_contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _source_image_bounds_intersection_area(left, right):
    return max(0, min(left[2], right[2])-max(left[0], right[0])) * max(0, min(left[3], right[3])-max(left[1], right[1]))


def _source_image_bounds_plan(original_bbox, page_size, glyphs, owners, blockers, owner_ref, render_scale,
                *, enabled=True, method='source_image', region_kind='whole_table'):
    def result(status, reason, **proof):
        original = list(original_bbox) if isinstance(original_bbox, (list, tuple)) else original_bbox
        return dict(status=status, reason=reason, original_bbox=original,
                    source_image_bbox=original, **proof)
    if not enabled:
        return result('UNCHANGED', 'disabled')
    if method != 'source_image' or region_kind != 'whole_table':
        return result('DECLINE', 'not_whole_source_image_fallback')
    if (not _source_image_bounds_box(original_bbox) or not isinstance(page_size, (tuple, list)) or len(page_size) != 2
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in page_size)
            or not isinstance(render_scale, (int, float)) or not math.isfinite(render_scale) or render_scale <= 0):
        return result('DECLINE', 'source_render_geometry_unavailable')
    if owners is None or blockers is None or glyphs is None:
        return result('DECLINE', 'source_ownership_evidence_unavailable')
    if any(not isinstance(v, (list, tuple)) for v in [owners, blockers, glyphs]):
        return result('DECLINE', 'source_ownership_evidence_unavailable')
    if any(not isinstance(x, dict) or not _source_image_bounds_box(x.get('bbox')) or not x.get('ref') for x in [*owners, *blockers]):
        return result('DECLINE', 'owner_or_blocker_geometry_unavailable')
    selected = []
    for glyph in glyphs:
        if not isinstance(glyph, dict):
            return result('DECLINE', 'positioned_glyph_geometry_unavailable')
        box = glyph.get('bbox')
        if not _source_image_bounds_box(box):
            return result('DECLINE', 'positioned_glyph_geometry_unavailable')
        centre = ((box[0]+box[2])/2, (box[1]+box[3])/2)
        if not _source_image_bounds_inside(centre, original_bbox):
            continue
        refs = {entry['ref'] for entry in owners if _source_image_bounds_inside(centre, entry['bbox'])}
        if refs != {owner_ref}:
            return result('DECLINE', 'source_glyph_owner_not_unique', owner_refs=sorted(refs))
        selected.append(list(box))
    if not selected:
        return result('DECLINE', 'no_positioned_source_glyphs')
    page = [0, 0, *page_size]
    def clamp(box):
        return [max(0, box[0]), max(0, box[1]), min(page[2], box[2]), min(page[3], box[3])]
    visible = [clamp(box) for box in selected]
    before = sum(bool(_source_image_bounds_contains(original_bbox, box)) for box in visible)
    if before == len(visible):
        return result('UNCHANGED', 'source_glyphs_already_contained', source_glyphs=len(visible))
    halo = SOURCE_IMAGE_BOUNDS_HALO_PIXELS/render_scale
    proposed = clamp([min(original_bbox[0], min(b[0] for b in visible)-halo),
                      min(original_bbox[1], min(b[1] for b in visible)-halo),
                      max(original_bbox[2], max(b[2] for b in visible)+halo),
                      max(original_bbox[3], max(b[3] for b in visible)+halo)])
    foreign = [entry['ref'] for entry in blockers if entry['ref'] != owner_ref
               and _source_image_bounds_intersection_area(proposed, entry['bbox']) > _source_image_bounds_intersection_area(original_bbox, entry['bbox'])]
    if foreign:
        return result('DECLINE', 'expanded_source_image_intersects_unrelated_block', blocker_refs=foreign,
                      proposed_bbox=proposed, source_glyphs=len(visible), glyphs_contained_before=before)
    if not _source_image_bounds_box(proposed) or not all(_source_image_bounds_contains(proposed, box) for box in visible):
        return result('DECLINE', 'clamped_source_coverage_not_proven')
    plan = result('REPAIR_PROPOSAL', 'unique_owned_source_glyph_extents', source_glyphs=len(visible),
                  glyphs_contained_before=before, glyphs_contained_after=len(visible),
                  source_render_scale=render_scale, pixel_halo=SOURCE_IMAGE_BOUNDS_HALO_PIXELS, point_halo=halo,
                  owner_ref=owner_ref, page_size=list(page_size), glyph_bboxes=selected)
    plan['source_image_bbox'] = proposed
    return plan

def _source_image_bounds_context(doc, page_no, reader):
    """Collect coordinates only, retaining every unrelated text/picture blocker."""
    size = doc.pages[page_no].size
    owners, blockers = [], []
    for role, target in [('tables', owners), ('texts', blockers), ('pictures', blockers)]:
        for item in getattr(doc, role):
            for prov in item.prov or ():
                if prov.page_no != page_no:
                    continue
                box = prov.bbox.to_top_left_origin(size.height)
                target.append(dict(ref=item.self_ref, bbox=[box.l, box.t, box.r, box.b]))
    reader.load_page(page_no)
    glyphs = [dict(bbox=[g.get('x0'), g.get('top'), g.get('x1'), g.get('bottom')])
              if isinstance(g, dict) else None for g in reader.chars]
    return [size.width, size.height], glyphs, owners, blockers


def _source_image_bounds_warning(manifest, ref, page, plan):
    proof = dict(ref=ref, page=page, **plan)
    manifest.setdefault('source_image_bounds_warnings', []).append(proof)
    diagnostic_event('SOURCE_IMAGE_BOUNDS_UNCHANGED', ref=ref, page=page,
                     reason=plan['reason'], original_bbox=plan.get('original_bbox'),
                     blocker_refs=plan.get('blocker_refs', []))


def _source_image_bounds_crop(image, original_bbox, page_no, ref, doc, reader, regions, manifest):
    """Return a verified expanded whole-table crop or the original image."""
    replacement = None
    try:
        size, glyphs, owners, blockers = _source_image_bounds_context(doc, page_no, reader)
        # PdfRegions' direct-ROI branch currently does not publish its actual
        # render scale. A cached overview scale must not masquerade as ROI scale.
        normal_render = size[0]*size[1]*3.0**2 <= regions.pixel_budget
        scale = regions.cached_scale if normal_render and regions.cached_page == page_no else None
        plan = _source_image_bounds_plan(original_bbox, size, glyphs, owners, blockers, ref, scale)
        if plan['status'] == 'DECLINE':
            _source_image_bounds_warning(manifest, ref, page_no, plan)
            return image, list(original_bbox)
        if plan['status'] == 'UNCHANGED':
            return image, list(original_bbox)
        bounds = plan['source_image_bbox']
        replacement = regions.crop(page_no, bounds)
        dx = math.floor(original_bbox[0]*scale)-math.floor(bounds[0]*scale)
        dy = math.floor(original_bbox[1]*scale)-math.floor(bounds[1]*scale)
        retained = replacement.crop((dx, dy, dx+image.width, dy+image.height))
        try:
            pixels_preserved = (retained.size == image.size and retained.mode == image.mode
                                and retained.tobytes() == image.tobytes())
        finally:
            retained.close()
        if not pixels_preserved:
            replacement.close(); replacement = None
            plan.update(status='DECLINE', reason='original_source_pixels_not_preserved')
            _source_image_bounds_warning(manifest, ref, page_no, plan)
            return image, list(original_bbox)
        proof = dict(ref=ref, page=page_no, **plan,
            source_sha256=manifest.get('source_sha256'),
            old_rgb_sha256=hashlib.sha256(image.tobytes()).hexdigest(),
            new_rgb_sha256=hashlib.sha256(replacement.tobytes()).hexdigest(),
            old_image_size=list(image.size), new_image_size=list(replacement.size),
            original_source_pixels_preserved=True)
        manifest.setdefault('source_image_bounds_repairs', []).append(proof)
        diagnostic_event('SOURCE_IMAGE_BOUNDS_EXPANDED', ref=ref, page=page_no,
            original_bbox=list(original_bbox), actual_source_image_bbox=bounds,
            old_rgb_sha256=proof['old_rgb_sha256'], new_rgb_sha256=proof['new_rgb_sha256'],
            source_glyphs=plan['source_glyphs'], source_render_scale=scale)
        image.close()
        return replacement, bounds
    except (AttributeError, TypeError, ValueError, IndexError, OSError, RuntimeError) as error:
        if replacement is not None:
            replacement.close()
        _source_image_bounds_warning(manifest, ref, page_no,
            dict(status='DECLINE', reason='source_bounds_evidence_unavailable',
                 original_bbox=list(original_bbox), source_image_bbox=list(original_bbox),
                 error_type=type(error).__name__))
        return image, list(original_bbox)

# END whole_source_image_bounds

class PdfRegions:
    """Render one page at a time, with bounded raster size."""

    def __init__(self, path):
        import pypdfium2 as pdfium
        self.pdf = pdfium.PdfDocument(str(path))
        self.cached_page = self.cached_image = None
        self.cached_scale = None
        self.cached_rules = None
        self.pixel_budget = 16_000_000

    def close(self):
        if self.cached_image is not None:
            self.cached_image.close()
        self.pdf.close()

    def crop(self, page_no, bbox=None, scale=3.0):
        page = self.pdf[page_no - 1]
        try:
            width, height = page.get_size()
            if bbox is not None and width*height*scale*scale > self.pixel_budget:
                left,top,right,bottom=bbox
                left,top,right,bottom=max(0,left),max(0,top),min(width,right),min(height,bottom)
                if right <= left or bottom <= top:
                    raise ValueError(f'Invalid source region on page {page_no}: {bbox}')
                # A tiny formula on a large sheet must not inherit the
                # low-resolution full-page overview. PDFium renders its window
                # directly, with no full-sheet bitmap allocation.
                requested_scale=scale
                scale=min(scale,math.sqrt(self.pixel_budget/((right-left)*(bottom-top))))
                full_width,full_height=math.ceil(width*scale),math.ceil(height*scale)
                bounds=(math.floor(left*scale),math.floor(top*scale),
                        math.ceil(right*scale),math.ceil(bottom*scale))
                margins=(bounds[0],full_height-bounds[3],full_width-bounds[2],bounds[1])
                # PDFium rounds crop margins up. A quarter-pixel interior bias
                # makes that conversion exact even for fractional PDF points.
                crop=tuple(max(0,(margin-.25)/scale) for margin in margins)
                bitmap=page.render(scale=scale,crop=crop)
                try:
                    image=bitmap.to_pil().convert('RGB')
                finally:
                    bitmap.close()
                diagnostic_event('PDF_CROP_DIRECT_ROI',page=page_no,bbox=bbox,
                    requested_scale=requested_scale,scale=scale,pixel_bbox=bounds,pixels=image.size,
                    reason='source_region_independent_of_full_page_overview')
                return image
            scale = min(scale, math.sqrt(self.pixel_budget / (width * height)))
            if self.cached_page != page_no or self.cached_scale != scale:
                render_started=time.perf_counter()
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
                if self.cached_image is not None:
                    self.cached_image.close()
                self.cached_page, self.cached_scale, self.cached_image = page_no, scale, image
                self.cached_rules = None
                diagnostic_event('PDF_RENDER',page=page_no,scale=scale,pixels=image.size,
                                 elapsed_ms=round((time.perf_counter()-render_started)*1000,3))
            image = self.cached_image
            if bbox is None:
                return image.copy()
            left, top, right, bottom = bbox
            bounds = (max(0, math.floor(left * scale)), max(0, math.floor(top * scale)),
                      min(image.width, math.ceil(right * scale)), min(image.height, math.ceil(bottom * scale)))
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                raise ValueError(f"Invalid source region on page {page_no}: {bbox}")
            diagnostic_event('PDF_CROP',page=page_no,bbox=bbox,scale=scale,pixel_bbox=bounds)
            return image.crop(bounds)
        finally:
            page.close()

    def table_geometry(self, page_no, region):
        """Cheap fallback for scanned rules; cache one deskewed preview per page."""
        if self.cached_page != page_no:
            image = self.crop(page_no)
            image.close()
        if self.cached_rules is None:
            self.cached_rules = RasterTableRules(self.cached_image, self.cached_scale)
        return self.cached_rules.table(region)

    def scan_tables(self, page_no):
        if self.cached_page != page_no:
            self.crop(page_no).close()
        if self.cached_rules is None:
            self.cached_rules = RasterTableRules(self.cached_image, self.cached_scale)
        return self.cached_rules.tables()


class RasterTableRules:
    """Locate nearly parallel table rules without another OCR/model pass."""

    def __init__(self, image, pdf_scale):
        import cv2
        import numpy as np
        from statistics import median

        ratio = min(1.0, 1400 / max(image.size))
        gray = np.asarray(image.convert("L"))
        if ratio < 1:
            gray = cv2.resize(gray, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
        self.scale = pdf_scale * ratio
        mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        lines = cv2.HoughLinesP(mask, 1, np.pi / 720, threshold=50,
                                minLineLength=max(60, gray.shape[1] // 5), maxLineGap=8)
        angles = []
        if lines is not None:
            for x0, y0, x1, y1 in lines[:, 0]:
                angle = math.degrees(math.atan2(y1 - y0, x1 - x0))
                if abs(angle) <= 4:
                    angles.append(angle)
        self.angle = median(angles) if angles else 0.0
        self.matrix = cv2.getRotationMatrix2D((gray.shape[1] / 2, gray.shape[0] / 2), self.angle, 1)
        self.inverse = cv2.invertAffineTransform(self.matrix)
        mask = cv2.warpAffine(mask, self.matrix, (gray.shape[1], gray.shape[0]), flags=cv2.INTER_NEAREST)
        # Close only short scan gaps. Longer gaps between a table and following
        # prose (or another table) are never bridged by these kernels.
        vertical = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 1), np.uint8))
        vertical = cv2.morphologyEx(vertical, cv2.MORPH_OPEN, np.ones((18, 1), np.uint8))
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((1, 5), np.uint8))
        horizontal = cv2.morphologyEx(horizontal, cv2.MORPH_OPEN, np.ones((1, 30), np.uint8))

        def components(binary):
            _, _, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
            return [tuple(int(value) for value in box[:4]) for box in stats[1:]]

        self.vertical, self.horizontal = components(vertical), components(horizontal)
        self.preview_shape = gray.shape
        grid = cv2.dilate(cv2.bitwise_or(vertical, horizontal), np.ones((3, 3), np.uint8))
        self.grid_components = components(grid)
        diagnostic_event('RASTER_RULES',scale=self.scale,skew_degrees=self.angle,preview_shape=self.preview_shape,
                         vertical_components=len(self.vertical),horizontal_components=len(self.horizontal),
                         grid_components=len(self.grid_components))

    def transform(self, x, y, inverse=False):
        matrix = self.inverse if inverse else self.matrix
        return (float(matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]),
                float(matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]))

    def table(self, region):
        points = [self.transform(x * self.scale, y * self.scale)
                  for x in (region[0], region[2]) for y in (region[1], region[3])]
        left, right = min(x for x, _ in points), max(x for x, _ in points)
        top, bottom = min(y for _, y in points), max(y for _, y in points)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            diagnostic_event('RASTER_GRID_REJECT',bbox=region,reason='invalid_region',width=width,height=height)
            return None
        tolerance = max(10 * self.scale, width * .08)
        long_horizontal = [(y+h/2, x, x+w) for x,y,w,h in self.horizontal
                           if w >= width*.5 and top-5 <= y+h/2 <= bottom+5]
        groups = []
        for x,w,y0,y1 in sorted((x+w/2,w,y,y+h) for x,y,w,h in self.vertical
                                 if h >= 18 and w < max(8,width*.02)
                                 and min(y+h,bottom)>max(y,top)):
            if groups and x-groups[-1][-1][0] < 4:
                groups[-1].append((x,y0,y1))
            else:
                groups.append([(x,y0,y1)])
        rails, attached_rails = [], []
        for group in groups:
            x = sum(v[0] for v in group)/len(group)
            segments = sorted((max(top,v[1]),min(bottom,v[2])) for v in group)
            coverage,last = 0,top
            for start,stop in segments:
                coverage += max(0,stop-max(start,last));last=max(last,stop)
            contacts = {round(y) for y,a,b in long_horizontal if a-4 <= x <= b+4
                        and any(v[1]-4 <= y <= v[2]+4 for v in group)}
            if coverage >= height*.6 and len(contacts)>=2:
                rails.append((x,min(v[1] for v in group),max(v[2] for v in group)))
            elif len(contacts)>=2:
                # A table may have columns only in its header and merged rows
                # below. Both source junctions prove such short dividers; they
                # never substitute for the continuous outer side rails.
                # Test each connected stroke separately: a formula elsewhere
                # in the merged band can share its X without extending it.
                for rail_x,start,end in group:
                    if all(any(abs(y-position)<=4 and a-4<=rail_x<=b+4
                               for y,a,b in long_horizontal) for position in (start,end)):
                        attached_rails.append((rail_x,start,end))
        left_rails = [bar for bar in rails if abs(bar[0] - left) <= tolerance]
        right_rails = [bar for bar in rails if abs(bar[0] - right) <= tolerance]
        if not left_rails or not right_rails:
            diagnostic_event('RASTER_GRID_REJECT',bbox=region,reason='missing_side_rails',rails=len(rails),
                             left_rails=len(left_rails),right_rails=len(right_rails),long_horizontal=len(long_horizontal))
            return None
        a, b = min(left_rails, key=lambda bar: abs(bar[0] - left)), min(right_rails, key=lambda bar: abs(bar[0] - right))
        rule_top, rule_bottom = max(a[1], b[1]), min(a[2], b[2])
        if rule_bottom - rule_top < height * .6 or b[0] - a[0] < width * .8:
            diagnostic_event('RASTER_GRID_REJECT',bbox=region,reason='side_rails_do_not_cover_region',
                             rule_height=rule_bottom-rule_top,height=height,rule_width=b[0]-a[0],width=width)
            return None
        horizontal = [(y + h / 2, x, x + w) for x, y, w, h in self.horizontal
                      if rule_top - 4 <= y + h / 2 <= rule_bottom + 4
                      and min(x + w, b[0]) - max(x, a[0]) >= (b[0] - a[0]) * .55]
        if len(horizontal) < 2:
            diagnostic_event('RASTER_GRID_REJECT',bbox=region,reason='fewer_than_two_horizontal_rules',horizontal=len(horizontal))
            return None
        # Continuous side rails define the open page edge. They end before
        # the following prose, even if the final horizontal rule was omitted.
        corners = [self.transform(x, y, inverse=True) for x in (a[0], b[0]) for y in (rule_top, rule_bottom)]
        bounds = [min(x for x, _ in corners) / self.scale, min(y for _, y in corners) / self.scale,
                  max(x for x, _ in corners) / self.scale, max(y for _, y in corners) / self.scale]
        xs = sorted({x for x, y0, y1 in rails if a[0] - 3 <= x <= b[0] + 3
                     and min(y1, rule_bottom) - max(y0, rule_top) >= (rule_bottom - rule_top) * .6}
                    | {x for x,y0,y1 in attached_rails if a[0]+3<x<b[0]-3
                       and y0>=rule_top-4 and y1<=rule_bottom+4})
        # Group the two sides of a thick printed rule into one column boundary.
        groups = []
        for x in xs:
            if groups and x - groups[-1][-1] < 4:
                groups[-1].append(x)
            else:
                groups.append([x])
        if len(groups) < 3:
            diagnostic_event('RASTER_GRID_REJECT',bbox=region,reason='fewer_than_two_columns',boundary_groups=len(groups))
            return None
        grid = [self.transform(sum(group) / len(group), (rule_top + rule_bottom) / 2, inverse=True)[0] / self.scale
                for group in groups]
        diagnostic_event('RASTER_GRID_ACCEPT',region=region,bbox=bounds,x_grid=grid,horizontal=len(horizontal),rails=len(rails))
        return {"basis": "raster_parallel_rules", "bbox": bounds, "x_grid": grid,
                "skew_degrees": round(self.angle, 3),
                "open_top": not any(abs(y - rule_top) <= 4 for y, _, _ in horizontal),
                "open_bottom": not any(abs(y - rule_bottom) <= 4 for y, _, _ in horizontal)}

    def tables(self):
        """Find ruled scan regions before relying on layout-model table labels."""
        height,width = self.preview_shape
        found = []
        for x,y,w,h in self.grid_components:
            if w < width*.25 or h < height*.05:
                continue
            corners = [self.transform(xx,yy,inverse=True) for xx in (x-2,x+w+2) for yy in (y-2,y+h+2)]
            region = [max(0,min(a for a,b in corners)/self.scale),max(0,min(b for a,b in corners)/self.scale),
                      max(a for a,b in corners)/self.scale,max(b for a,b in corners)/self.scale]
            geometry = self.table(region)
            if geometry and not any(overlap_ratio(geometry['bbox'],old['bbox'])>.9 for old in found):
                found.append(geometry)
        return sorted(found,key=lambda g:(g['bbox'][1],g['bbox'][0]))


def image_html(image, width, kind):
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    if _DIAGNOSTICS is not None:
        diagnostic_event('SOURCE_IMAGE_CREATED',kind=kind,pixels=image.size,png_bytes=buffer.tell(),
                         display_width=width)
    return (f'<figure class="source-{kind}"><img src="{uri}" '
            f'width="{max(1, round(width * 96 / 72))}" alt="" /></figure>')


def overlap_ratio(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    minimum = min(max(0, a[2] - a[0]) * max(0, a[3] - a[1]),
                  max(0, b[2] - b[0]) * max(0, b[3] - b[1]))
    return intersection / minimum if minimum else 0.0


def item_region(item, doc):
    if not item.prov:
        return None
    prov = item.prov[0]
    height = doc.pages[prov.page_no].size.height
    box = prov.bbox.to_top_left_origin(height)
    return prov.page_no, [box.l, box.t, box.r, box.b]


def furniture_key(text):
    return re.sub(r"[\s\u00ad]+", "", text).casefold().translate(str.maketrans("‐‑−–—", "-----"))


def is_document_designation(text):
    """Standalone identifiers only; citations inside a sentence remain content."""
    return bool(re.fullmatch(
        r"(?:ГОСТ(?:\s+Р)?|СП|СНиП|СН|СТО|СТП|РД|ISO|IEC|EN|DIN|ASTM)"
        r"(?:\s+(?:Р|ИСО|МЭК|ISO|IEC|EN|DIN))*\s+[A-ZА-ЯЁ]*\s*\d[\d.\s/—–−\-*:A-ZА-ЯЁ]*",
        text.strip(), re.I))


def paragraph_continuation(left, right):
    """Join only unfinished prose followed by a lower-case continuation."""
    from bs4 import BeautifulSoup

    a = BeautifulSoup(left, "html.parser").get_text(" ", strip=True).rstrip()
    b = BeautifulSoup(right, "html.parser").get_text(" ", strip=True).lstrip()
    start = b.lstrip('«“"([')
    return bool(a and start and start[0].islower() and a[-1] not in ".!?…:;。！？")


def paragraph_separator(text):
    # Preserve visible compound-word hyphens; remove only explicit soft hyphens.
    return "" if text.endswith(("-", "\u00ad")) else " "


def clean_pdf_text_flow(doc, records, excluded, manifest, native_reader=None):
    """Suppress page furniture and reconnect prose across physical page breaks."""
    from docling_core.types.doc import ContentLayer, DocItemLabel

    recover_table_captions(doc, excluded, manifest)
    from docling_pdf_flow import recover_caption_titles
    recover_caption_titles(doc, native_reader, excluded, manifest, sys.modules[__name__])
    furniture = {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
    margins = {}
    for item in doc.texts:
        region = item_region(item, doc)
        if not region or not item.text.strip():
            continue
        page_no, box = region
        size = doc.pages[page_no].size
        # Four edges also cover running furniture on whole-page rotated sheets.
        edge = ("top" if box[3] <= size.height * .12 else
                "bottom" if box[1] >= size.height * .85 else
                "left" if box[2] <= size.width * .08 else
                "right" if box[0] >= size.width * .92 else None)
        if edge and len(item.text) <= 160:
            margins.setdefault((edge, furniture_key(item.text)), set()).add(page_no)

    removed = []
    protected_source_notes = set(manifest.get('protected_image_table_text_refs', []))
    for item in doc.texts:
        region = item_region(item, doc)
        if not region or item.self_ref in excluded:
            continue
        if item.self_ref in protected_source_notes:
            diagnostic_event('SOURCE_TABLE_NOTE_FURNITURE_PROTECTED',ref=item.self_ref,
                reason='source_insert_ink_and_published_note_ownership')
            continue
        page_no, box = region
        size = doc.pages[page_no].size
        key = furniture_key(item.text)
        edge = ("top" if box[3] <= size.height * .12 else
                "bottom" if box[1] >= size.height * .85 else
                "left" if box[2] <= size.width * .08 else
                "right" if box[0] >= size.width * .92 else None)
        reason = None
        if item.label in furniture:
            reason = "layout_furniture"
        elif item.label not in {DocItemLabel.FORMULA, DocItemLabel.CAPTION, DocItemLabel.TITLE}:
            if any(record["page_no"] == page_no and record["label"] in {"PageHeader", "PageFooter"}
                   and key == furniture_key(record["text"])
                   and overlap_ratio(box, record["bbox"]) > .7 for record in records):
                reason = "ocr_furniture"
            elif (edge and is_document_designation(item.text)
                  and len(margins.get((edge, key), ())) >= 2):
                reason = "repeated_margin_designation"
            elif (edge == "bottom" and box[3] - box[1] < size.height * .05
                  and re.fullmatch(r"(?:(?:Лист|Стр(?:аница)?\.?|Page)\s*(?:№\s*)?)?"
                                   r"(?:\d{1,5}|[IVXLCDMivxlcdm]{1,8})", item.text.strip())):
                reason = "margin_page_number"
        if reason:
            excluded.add(item.self_ref)
            item.content_layer = ContentLayer.FURNITURE
            removed.append({"ref": item.self_ref, "page": page_no, "bbox": box,
                            "text": item.text, "reason": reason})
    manifest["removed_page_furniture"] = removed
    restore_note_reading_order(doc, excluded, manifest)

    previous = None
    joined = []
    prose = {DocItemLabel.TEXT, DocItemLabel.LIST_ITEM}
    for item, _ in doc.iterate_items():
        if (not hasattr(item, "prov") or not item.prov or item.self_ref in excluded
                or getattr(item, "content_layer", None) == ContentLayer.FURNITURE):
            continue
        if previous is not None and previous.label in prose and item.label == DocItemLabel.TEXT:
            last, first = previous.prov[-1], item.prov[0]
            old_size, new_size = doc.pages[last.page_no].size, doc.pages[first.page_no].size
            old_box = last.bbox.to_top_left_origin(old_size.height)
            new_box = first.bbox.to_top_left_origin(new_size.height)
            if (first.page_no == last.page_no + 1
                    and old_box.b >= old_size.height * .65 and new_box.t <= new_size.height * .25
                    and abs(old_box.l / old_size.width - new_box.l / new_size.width) < .12
                    and paragraph_continuation(previous.text, item.text)):
                separator = paragraph_separator(previous.text)
                left = previous.text.rstrip("\u00ad")
                offset = len(left) + len(separator)
                previous.text = left + separator + item.text
                previous.orig = previous.orig.rstrip("\u00ad") + separator + item.orig
                if len(previous.prov) == 1:
                    previous.prov[0].charspan = (0, len(left))
                for prov in item.prov:
                    copied = prov.model_copy(deep=True)
                    copied.charspan = (prov.charspan[0] + offset, prov.charspan[1] + offset)
                    previous.prov.append(copied)
                excluded.add(item.self_ref)
                joined.append({"into": previous.self_ref, "from": item.self_ref,
                               "pages": [last.page_no, first.page_no]})
                continue
        previous = item
    manifest["joined_page_paragraphs"] = joined
    manifest["text_cleanup_schema"] = TEXT_CLEANUP_SCHEMA
    print(f"Text flow: removed {len(removed)} page furniture blocks; joined {len(joined)} page continuations", flush=True)


def restore_note_reading_order(doc, excluded, manifest):
    """Split a list that crosses its following note heading in source order.

    Moving the entire list would put the heading before the preceding clause.
    Only move a full-width, positioned tail starting with note 1; leave nested
    lists, columns and unpositioned content in their existing structure.
    """
    from docling_core.types.doc import ListGroup, ListItem

    repairs = []
    for parent in [doc.body, *doc.groups]:
        for heading_ref in list(parent.children):
            heading = heading_ref.resolve(doc)
            if (heading.self_ref in excluded or not re.fullmatch(r"Примечани[ея]\s*[:.]?|Notes?\s*[:.]?",
                    getattr(heading, "text", "").strip(), re.I)):
                continue
            region = item_region(heading, doc) if getattr(heading, "prov", None) else None
            if not region:
                continue
            visible = [ref for ref in parent.children if ref.cref not in excluded]
            index = next(i for i, ref in enumerate(visible) if ref.cref == heading.self_ref)
            if not index:
                continue
            group = visible[index - 1].resolve(doc)
            if not isinstance(group, ListGroup):
                continue
            page_no, box = region
            children = [ref.resolve(doc) for ref in group.children]
            regions = [item_region(child, doc) if isinstance(child, ListItem) and len(child.prov) == 1 else None
                       for child in children]
            cut = next((i for i, reg in enumerate(regions) if reg and reg[0] == page_no
                        and reg[1][1] >= box[3] - 2), None)
            if cut is None or not re.match(r"^1(?:[.)])?\s", children[cut].text.strip()):
                continue
            numbers = [re.match(r"^(\d+)(?:[.)])?\s", child.text.strip()) for child in children[cut:]]
            if any(match is None for match in numbers) or [int(match[1]) for match in numbers] != list(range(1, len(numbers) + 1)):
                continue
            width = doc.pages[page_no].size.width
            if (any(not reg or reg[0] > page_no or (reg[0] == page_no and reg[1][3] > box[1] + 2)
                    for reg in regions[:cut])
                    or any(not reg or reg[0] != page_no or reg[1][1] < box[3] - 2
                           or reg[1][2] - reg[1][0] < .6 * width
                           or not reg[1][0] <= box[0] <= reg[1][2] for reg in regions[cut:])):
                continue
            if cut:
                tail = doc.add_group(label=group.label, name=group.name, parent=parent,
                                     content_layer=group.content_layer)
                tail.children = group.children[cut:]
                group.children = group.children[:cut]
                for child in children[cut:]:
                    child.parent = tail.get_ref()
            else:
                tail = group
            parent.children = [ref for ref in parent.children if ref.cref != tail.self_ref]
            position = next(i for i, ref in enumerate(parent.children) if ref.cref == heading.self_ref)
            parent.children.insert(position + 1, tail.get_ref())
            repairs.append({"page": page_no, "heading": heading.self_ref,
                            "moved": [child.self_ref for child in children[cut:]]})
    manifest["note_reading_order_repairs"] = repairs
    if repairs:
        print(f"Reading order: repaired {len(repairs)} note headings across list boundaries", flush=True)


def semantic_ocr_text(fragment, *, outside_tables=False):
    """Keep formula markers and, when requested, only table-adjacent notes."""
    from bs4 import BeautifulSoup, NavigableString

    parsed = BeautifulSoup(fragment, "html.parser")
    if outside_tables:
        for table in parsed.find_all("table"):
            table.decompose()
    for tag in parsed.find_all("math"):
        display = ' display="block"' if tag.get("display") == "block" else ""
        tag.replace_with(NavigableString(f"<math{display}>{tag.get_text()}</math>"))
    return normalize_metric_unit_text(parsed.get_text(" ", strip=True))


def normalize_metric_unit_text(text):
    """Restore OCR-flattened Cyrillic square/cubic metre units at token edges."""
    return re.sub(r'(?<![^\W\d])м([23])(?!\w)',
                  lambda match: 'м' + {'2': '²', '3': '³'}[match[1]], text)


def normalize_metric_units(soup):
    """Change visible text only; never touch image data, links, code or scripts."""
    count = 0
    for node in list(soup.find_all(string=re.compile(r'м[23]'))):
        if any(parent.name in {'script', 'style', 'code', 'pre'} for parent in node.parents):
            continue
        original = str(node)
        if re.search(r'https?://|www\.', original):
            continue
        fixed = normalize_metric_unit_text(original)
        if fixed != original:
            count += 1
            node.replace_with(fixed)
    if count:
        diagnostic_event('OCR_METRIC_UNIT_EXPONENT_NORMALIZED', text_nodes=count)
    return count


def extract_table_caption(text):
    """Extract a caption from a misclassified/mixed page-furniture block."""
    text = semantic_ocr_text(text) if "<" in text else text
    match = re.search(r"(?<!\w)(?:(?:Продолжение|Окончание)\s+)?"
                      r"(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|табл\.?)\s*"
                      r"(?:[А-ЯЁA-Z]\s*\.?\s*)?\d+(?:\s*[.\-]\s*\d+)*", text, re.I)
    if match is None:
        return None
    caption = re.split(r"https?://|www\.", text[match.start():], maxsplit=1, flags=re.I)[0]
    caption = re.sub(r"\s+\d+\s*/\s*\d+\s*$", "", caption).strip()
    return caption


def recover_table_captions(doc, excluded, manifest):
    """Protect caption content and attach bottom-of-page captions to next-page tables."""
    from docling_core.types.doc import ContentLayer, DocItemLabel

    recovered = []
    for item in doc.texts:
        if item.label not in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER} and item.content_layer != ContentLayer.FURNITURE:
            continue
        caption = extract_table_caption(item.orig or item.text)
        region = item_region(item, doc)
        if not caption or not region:
            continue
        page_no, box = region
        size = doc.pages[page_no].size
        candidates = []
        for table in doc.tables:
            if table.self_ref in excluded:
                continue
            reg = item_region(table, doc)
            if not reg:
                continue
            if getattr(item.parent, "cref", None) == table.self_ref:
                candidates.append((-1, table))
            elif reg[0] == page_no and reg[1][1] >= box[3] - 3:
                candidates.append((reg[1][1] - box[3], table))
            elif (reg[0] == page_no + 1 and box[3] > size.height * .75
                  and reg[1][1] < doc.pages[reg[0]].size.height * .25):
                candidates.append((size.height - box[3] + reg[1][1], table))
        table = min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None
        previous_text = item.text
        item.text = caption
        item.label = DocItemLabel.CAPTION if table else DocItemLabel.TEXT
        item.content_layer = ContentLayer.BODY
        for prov in item.prov:
            prov.charspan = (0, len(caption))
        excluded.discard(item.self_ref)
        if table:
            if item.parent and item.parent.cref != table.self_ref:
                parent = item.parent.resolve(doc)
                parent.children = [ref for ref in parent.children if ref.cref != item.self_ref]
                item.parent = table.get_ref()
                if not any(ref.cref == item.self_ref for ref in table.children):
                    table.children.append(item.get_ref())
            if not any(ref.cref == item.self_ref for ref in table.captions):
                table.captions.append(item.get_ref())
        recovered.append({"ref": item.self_ref, "page": page_no, "text": caption,
                          "previous_text": previous_text, "table": table.self_ref if table else None})
    manifest["recovered_table_captions"] = recovered


def local_request_seconds(items):
    """Allow the declared output to finish before Surya can validate/fallback."""
    tokens=max((item.max_tokens or 2048 for item in items),default=2048)
    return max(LOCAL_REQUEST_SECONDS, math.ceil(tokens/80)+15)


def generate_local_ocr_requests(batch, request, diagnostic):
    """Keep Surya's full-page -> layout/block recovery instead of repeating loops."""
    from docling_pdf_ocr import recovering_generate
    from surya.inference.prompts import LAYOUT_JSON_SCHEMA
    outputs=[]
    for item in batch:
        if item.prompt_type == 'high_accuracy_bbox':
            result=request([item])
            if len(result)!=1:
                raise RuntimeError('Local full-page inference result count mismatch')
            if result[0].error:
                diagnostic('OCR_FULL_PAGE_LAYOUT_FALLBACK',reason='full_page_request_failed',
                           metadata=item.metadata,image_size=list(item.image.size))
            # RecognitionPredictor validates repetition and HTML, then uses its
            # trained layout + block path. Do not retry the identical greedy
            # full-page request or recursively repeat it on smaller pages.
            outputs.extend(result)
        else:
            if item.prompt_type == 'layout' and item.guided_json is None:
                item=replace(item,guided_json=LAYOUT_JSON_SCHEMA)
            outputs.extend(recovering_generate([item],request,diagnostic))
    return outputs


def local_surya_backend(model):
    """Bound local VLM requests, including retries hidden inside Surya's adapter."""
    from surya.inference.backends.openai_client import chat_completions_batch
    from docling_backend_health import request_with_backend_recovery

    backend = model._get_runtime()._manager.backend
    if not getattr(backend, "_convert_docling_bounded", False):
        def generate(batch):
            if backend.handle is None or backend._client is None:
                backend.start()
            def request(items):
                timeout=local_request_seconds(items)
                if timeout>LOCAL_REQUEST_SECONDS:
                    diagnostic_event('OCR_REQUEST_TOKEN_TIME_ALLOWANCE',seconds=timeout,
                        max_tokens=max(item.max_tokens or 2048 for item in items))
                return request_with_backend_recovery(backend, lambda: chat_completions_batch(
                    items, client=backend._client.with_options(max_retries=0),
                    model_name=backend.handle.model_name, timeout=timeout,
                    max_workers=1, max_retries=0, request_logprobs_default=False), diagnostic_event)
            return generate_local_ocr_requests(batch, request, diagnostic_event)
        backend.generate = generate
        backend._convert_docling_bounded = True
    return backend


def surya_pdf_pipeline(args):
    """Use the installed plugin, preserving its math HTML and render scale.

    Plugin 0.2.0 needs a signature compatibility fix for Docling 2.141.0,
    defaults to scale=1 and flattens math HTML to plain text.
    These hooks retain formulas without replacing OCR or mapping.
    """
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    from docling_surya_ocr.compat import docling as compatibility

    # 2.141 adds optional priority=None after conv_res. The plugin checks exact
    # parameter names, although its conv_res call remains valid with that default.
    # Restrict the correction to the tested versions and exact known signature.
    import inspect
    from docling.models.base_ocr_model import BaseOcrModel
    parameters = inspect.signature(BaseOcrModel.post_process_cells).parameters
    if (version("docling") == "2.141.0"
            and list(parameters) == ["self", "ocr_cells", "page", "conv_res", "priority"]
            and parameters["priority"].default is None
            and not getattr(compatibility, "_convert_docling_priority_fix", False)):
        classify = compatibility._signature_style
        compatibility._signature_style = lambda names: (
            "conv_res" if names == list(parameters) else classify(names)
        )
        compatibility._convert_docling_priority_fix = True

    class SuryaPdfPipeline(StandardPdfPipeline):
        def __init__(self, pipeline_options):
            super().__init__(pipeline_options)
            from docling_pdf_ocr import install_table_retention
            install_table_retention(self.table_model, args, diagnostic_event)

        def _make_ocr_model(self, artifacts_path):
            model = super()._make_ocr_model(artifacts_path)
            model.scale = IMAGE_SCALE
            recognize = model._recognize_docling_work_items
            run_pages = model._run_docling_pages
            args.surya_ocr_model = model

            def capture_pages(conv_res, pages):
                model._quality_page_numbers = [page.page_no for page in pages]
                return run_pages(conv_res, pages)

            def preserve_math(work_items):
                predictions = []
                for work_item in work_items:
                    local_surya_backend(model)
                    prediction = recognize([work_item])[0]
                    predictions.append(prediction)
                result = []
                for work_item, prediction in zip(work_items, predictions, strict=True):
                    page_no = model._quality_page_numbers[work_item.page_index]
                    rect = work_item.rect
                    blocks = []
                    for block in prediction.blocks:
                        args.surya_records.append({
                            "page_no": page_no,
                            "bbox": [block.bbox[0] / model.scale + rect.l,
                                     block.bbox[1] / model.scale + rect.t,
                                     block.bbox[2] / model.scale + rect.l,
                                     block.bbox[3] / model.scale + rect.t],
                            "html": block.html or "", "text": block.text,
                            "label": block.label, "confidence": block.confidence,
                        })
                        if block.html and "<math" in block.html:
                            # html_to_text would remove the preserved markers again.
                            block = replace(block, text=semantic_ocr_text(block.html))
                        blocks.append(block)
                    result.append(replace(prediction, blocks=tuple(blocks)))
                return result

            model._recognize_docling_work_items = preserve_math
            model._run_docling_pages = capture_pages
            return model

    return SuryaPdfPipeline


def render_surya_math(content):
    """Turn Surya's escaped <math>LaTeX</math> in text/table cells into MathML."""
    from bs4 import BeautifulSoup, NavigableString
    from latex2mathml.converter import convert

    soup = BeautifulSoup(content, "html.parser")
    pattern = re.compile(r"<(math|chem)\b([^>]*)>(.*?)</\1>", re.DOTALL)
    for node in list(soup.find_all('chem')):
        from docling_office_math import chemical_math
        rendered = chemical_math(node.get_text(), latex_math)
        if rendered:
            node.replace_with(BeautifulSoup(rendered, 'html.parser').math)
    for node in list(soup.find_all(string=pattern)):
        if any(parent.name in ("math", "script", "style") for parent in node.parents):
            continue
        fragments, offset = [], 0
        for match in pattern.finditer(str(node)):
            fragments.append(NavigableString(str(node)[offset:match.start()]))
            latex = match.group(3).strip()
            try:
                display = "block" if re.search(r"display\s*=\s*['\"]?block", match.group(2)) else "inline"
                if match.group(1) == 'chem':
                    from docling_office_math import chemical_math
                    rendered = chemical_math(latex, latex_math)
                    if not rendered:
                        raise ValueError('Unsupported chemical notation; source must be retained')
                    math = BeautifulSoup(rendered, 'html.parser').math
                else:
                    math = BeautifulSoup(convert(latex, display=display), "html.parser").math
                    math["data-latex"] = latex
                fragments.append(math)
            except Exception as exc:
                print(f"WARNING: Surya formula retained as LaTeX: {exc}", file=sys.stderr)
                fragments.append(NavigableString(f"\\({latex}\\)"))
            offset = match.end()
        fragments.append(NavigableString(str(node)[offset:]))
        node.replace_with(*fragments)
    return str(soup)


def normalize_surya_formulas(doc):
    """Docling's FORMULA exporter expects bare LaTeX, other cells keep markers."""
    from bs4 import BeautifulSoup
    from docling_core.types.doc import DocItemLabel, FormulaItem, TextItem
    restored = []
    for index, item in enumerate(doc.texts):
        if item.label == DocItemLabel.FORMULA or isinstance(item, FormulaItem):
            # Layout labels are approximate: an explanation containing inline
            # symbols is still prose. Its original text must survive export.
            if "<math" in item.orig:
                parsed = BeautifulSoup(item.orig, "html.parser")
                for tag in parsed.find_all("math"):
                    tag.decompose()
                if parsed.get_text(" ", strip=True):
                    payload = item.model_dump()
                    payload.update(label=DocItemLabel.TEXT, text=semantic_ocr_text(item.orig))
                    # The serializer dispatches on the Python class, not label.
                    # Keep the reference/children/provenance, replace FormulaItem.
                    item = TextItem.model_validate(payload)
                    doc.texts[index] = item
                    if len(item.prov) == 1:
                        item.prov[0].charspan = (0, len(item.text))
                    restored.append(item.self_ref)
                    continue
            raw = item.text or item.orig
            if "<math" in raw:
                parsed = BeautifulSoup(raw, "html.parser")
                item.text = " ".join(tag.get_text() for tag in parsed.find_all("math")).strip()
    return restored


def restore_ocr_formula_blocks(doc, records, picture_routes, manifest):
    """Expose OCR equations hidden as children of layout PictureItems.

    Suppress only a coincident equation-sized picture. A formula embedded in
    a larger drawing or table stays in that drawing/table.
    """
    from bs4 import BeautifulSoup
    from docling_core.types.doc import DocItemLabel

    restored = []
    for record in records:
        if "<math" not in record["html"] or "<table" in record["html"]:
            continue
        parsed = BeautifulSoup(record["html"], "html.parser")
        maths = parsed.find_all("math")
        if not any(tag.get("display") == "block" for tag in maths):
            continue
        latex = " ".join(tag.get_text() for tag in maths)
        outside = BeautifulSoup(record["html"], "html.parser")
        for tag in outside.find_all("math"):
            tag.decompose()
        if re.search(r"[^\d\s().,;:\-–—]", outside.get_text(" ", strip=True)):
            continue
        # Invalid OCR must never hide the readable source picture.
        if latex_math(latex) is None:
            continue
        matches = [item for item in doc.texts if item.label == DocItemLabel.FORMULA
                   and (reg := item_region(item, doc)) and reg[0] == record["page_no"]
                   and overlap_ratio(reg[1], record["bbox"]) > .6]
        if matches:
            item = max(matches, key=lambda candidate: overlap_ratio(item_region(candidate, doc)[1], record["bbox"]))
        else:
            item = insert_source_item(doc, record["page_no"], record["bbox"], "formula")
            item.orig, item.text = record["html"], latex
        bounds = record["bbox"]
        for picture in doc.pictures:
            region = item_region(picture, doc)
            if not region or region[0] != record["page_no"]:
                continue
            box = region[1]
            area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
            math_area = max(1, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
            intersection = max(0, min(box[2], bounds[2]) - max(box[0], bounds[0])) * max(
                0, min(box[3], bounds[3]) - max(box[1], bounds[1]))
            if intersection / area <= .85 or intersection / math_area <= .7:
                continue
            if (any(ref.cref != item.self_ref for ref in picture.children)
                    or any(re.search(r"рис(?:унок|\.)|схема|figure|diagram", ref.resolve(doc).text, re.I)
                           for ref in picture.captions)):
                continue
            if getattr(item.parent, "cref", None) == picture.self_ref and picture.parent:
                parent = picture.parent.resolve(doc)
                position = next((i for i, ref in enumerate(parent.children) if ref.cref == picture.self_ref), None)
                if position is None:
                    continue
                picture.children = [ref for ref in picture.children if ref.cref != item.self_ref]
                item.parent = parent.get_ref()
                parent.children.insert(position, item.get_ref())
                restored.append({"ref": item.self_ref, "picture": picture.self_ref, "page": record["page_no"]})
            picture_routes[picture.self_ref] = ""
    manifest["promoted_picture_formulas"] = restored
    if restored:
        print(f"Formula assembly: exposed {len(restored)} equations hidden inside layout pictures", flush=True)


def expose_picture_table_children(doc, picture_routes, manifest):
    """Expose a table hidden inside a coincident layout-picture container."""
    from docling_core.types.doc import PictureItem

    restored = []
    for item in doc.tables:
        picture = item.parent.resolve(doc) if item.parent else None
        if (not isinstance(picture, PictureItem) or picture.captions or not picture.parent
                or [ref.cref for ref in picture.children] != [item.self_ref]):
            continue
        table_reg, picture_reg = item_region(item, doc), item_region(picture, doc)
        if not table_reg or not picture_reg or table_reg[0] != picture_reg[0]:
            continue
        a, b = table_reg[1], picture_reg[1]
        area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
        overlap = max(0, min(a[2],b[2])-max(a[0],b[0])) * max(0, min(a[3],b[3])-max(a[1],b[1]))
        if min(area_a, area_b) <= 0 or overlap / max(area_a, area_b) < .95:
            continue
        parent = picture.parent.resolve(doc)
        position = next((i for i, ref in enumerate(parent.children) if ref.cref == picture.self_ref), None)
        if position is None:
            continue
        picture.children = []
        item.parent = parent.get_ref()
        parent.children.insert(position, item.get_ref())
        picture_routes[picture.self_ref] = ""
        restored.append({"ref": item.self_ref, "picture": picture.self_ref, "page": table_reg[0]})
    manifest["exposed_picture_tables"] = restored


def mathematical_cell_label(cell):
    """A standalone indexed symbol can label a row in a mostly numeric column."""
    from bs4 import BeautifulSoup

    math = cell.find_all("math")
    if not math:
        text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", cell.get_text()))
        return bool(text and text[0].isalpha() and re.fullmatch(r"[\u0370-\u03ff](?:_?\d+)?", text))
    if len(math) != 1:
        return False
    latex = math[0].get("data-latex", "")
    command = re.match(r"\\([A-Za-z]+)", latex)
    if command and command[1].lower() not in {
        "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "vartheta",
        "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "varpi", "rho", "varrho",
        "sigma", "varsigma", "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega",
    }:
        return False
    if not re.fullmatch(r"(?:\\[A-Za-z]+|[A-Za-z\u0370-\u03ff])(?:_(?:\{[A-Za-z0-9,]+\}|[A-Za-z0-9]+))?", latex):
        return False
    parsed = BeautifulSoup(str(cell), "html.parser")
    parsed.math.decompose()
    return not parsed.get_text(" ", strip=True)


def latex_math(latex, display="block"):
    """Return executable-free MathML, or None so the caller preserves the source image."""
    from bs4 import BeautifulSoup
    from latex2mathml.converter import convert

    latex = latex.strip().strip("$")
    if (not latex or latex.count("{") != latex.count("}")
            or re.search(r"formula not|not decoded|unrecognized formula|не распознан|\ufffd", latex, re.I)):
        return None
    try:
        node = BeautifulSoup(convert(latex, display=display), "html.parser").math
        if node is None:
            return None
        node["data-latex"] = latex
        return str(node)
    except Exception:
        return None


@lru_cache(maxsize=1)
def russian_word_dictionary():
    """Load the local dictionary once; no model download or OCR is involved."""
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except ImportError:
        print("Table word-wrap repair needs pymorphy3; install requirements-docling-surya.txt. "
              "Uncertain hyphens will be preserved.", file=sys.stderr)
        return None


@lru_cache(maxsize=8192)
def joined_russian_word(token, dictionary_allowed=True):
    hyphens = '-\u2010\u2011\u2012\u00ad'
    pieces = re.split('(['+hyphens+']\\s*)', token)
    letters, separators = pieces[::2], pieces[1::2]
    if not separators:
        return None
    compact = ''.join(letters[i]+separator[0] for i, separator in enumerate(separators))+letters[-1]
    joined = ''.join(letters)
    if all(separator[0] == '\u00ad' for separator in separators):
        return joined
    dictionary = russian_word_dictionary() if dictionary_allowed else None
    if dictionary is None:
        return None
    def known(value):
        lookup = value.translate(str.maketrans('\u2010\u2011\u2012', '---')).lower()
        return dictionary.word_is_known(lookup)
    compact = compact.replace('\u00ad', '')
    if known(compact):
        return compact
    has_wrap = any(len(separator) > 1 or separator[0] == '\u00ad' for separator in separators)
    # Retain unbroken separators in partial candidates. A fully joined word
    # is also eligible if the dictionary confirms it: Обо- значе-ние.
    fixed = {i for i, separator in enumerate(separators)
             if has_wrap and len(separator) == 1 and separator[0] != '\u00ad'}
    optional = [i for i, separator in enumerate(separators)
                if i not in fixed and separator[0] != '\u00ad']
    def candidate(kept):
        return ''.join(letters[i]+(separator[0] if i in kept else '')
                       for i, separator in enumerate(separators))+letters[-1]
    proposals = {joined, candidate(fixed)}
    proposals.update(candidate(fixed | {i}) for i in optional)
    confirmed = {value for value in proposals if known(value)}
    return next(iter(confirmed)) if len(confirmed) == 1 else None


def normalize_table_word_wraps(table):
    """Remove dictionary-confirmed Russian word wraps within a single cell.

    Edit text nodes, retaining inline markup, true compound hyphens, codes,
    numbers, formulas and images. Never join across cells or mathematical tags.
    """
    from bs4 import NavigableString, Tag

    for cell in table.find_all(["td", "th"]):
        text, sources = [], []
        def collect(node):
            if isinstance(node, NavigableString):
                for index, char in enumerate(str(node)):
                    text.append(char)
                    sources.append((node, index))
            elif isinstance(node, Tag):
                if node.name == "br":
                    text.append("\n")
                    sources.append((node, None))
                elif node.name in {"math", "sub", "sup", "img", "table"}:
                    text.append("\x00")
                    sources.append((None, None))
                else:
                    if node.name in {"p", "div", "ul", "ol", "li", "pre", "blockquote"}:
                        text.append("\x00")
                        sources.append((None, None))
                    for child in node.children:
                        collect(child)
                    if node.name in {"p", "div", "ul", "ol", "li", "pre", "blockquote"}:
                        text.append("\x00")
                        sources.append((None, None))
        for child in cell.children:
            collect(child)
        flat = "".join(text)
        removed = {}
        breaks = {}
        hyphens = '-\u2010\u2011\u2012\u00ad'
        for match in re.finditer('[А-Яа-яЁё]+(?:['+hyphens+']\\s*[А-Яа-яЁё]+)+', flat):
            if '\u00ad' not in match[0] and not re.search('['+hyphens+']\\s+', match[0]):
                continue
            replacement = joined_russian_word(match[0], True)
            if replacement is None:
                continue
            indices = []
            position = 0
            for i in range(match.start(), match.end()):
                char = flat[i]
                if position < len(replacement) and char == replacement[position]:
                    position += 1
                    continue
                if char not in hyphens and not char.isspace():
                    break
                indices.append(i)
            else:
                if position != len(replacement):
                    continue
                diagnostic_event('TABLE_WORD_WRAP_NORMALIZED', source=match[0], result=replacement)
                for i in indices:
                    node, index = sources[i]
                    if index is None:
                        breaks[id(node)] = node
                    else:
                        entry = removed.setdefault(id(node), (node, set()))
                        entry[1].add(index)
        for node, indices in removed.values():
            node.replace_with(NavigableString("".join(c for i, c in enumerate(str(node)) if i not in indices)))
        for node in breaks.values():
            node.decompose()


def clean_table_html(fragment):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment, "html.parser")
    for node in list(soup.find_all('chem')):
        from docling_office_math import chemical_math
        rendered = chemical_math(node.get_text(), latex_math)
        if not rendered:
            if not _PRIMARY_OCR_ONLY.get():
                raise ValueError('Table contains unsupported chemical notation')
            diagnostic_event('PRIMARY_CHEMISTRY_LITERAL_KEPT', reason='unsupported_primary_notation')
            node.unwrap()
            continue
        node.replace_with(BeautifulSoup(rendered, 'html.parser').math)
    for node in soup(["script", "style", "iframe", "object", "link"]):
        node.decompose()
    for node in soup.find_all("img"):
        if (node.get("data-source-cell") == "true" and re.fullmatch(
                r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+", node.get("src", ""))):
            node.attrs = {key: value for key, value in node.attrs.items()
                          if key in {"src", "width", "height", "alt", "data-source-cell"}}
        else:
            node.decompose()
    table = soup.find("table")
    if table is None:
        raise ValueError("No table in recognition result")
    for node in list(table.find_all(True)):
        if node.find_parent('math') is not None:
            continue
        if node.name == "math" and node.find(True):
            # Structured primary MathML already owns its indices and powers.
            # Flattening its visible text and parsing it again loses them.
            continue
        if node.name == "math":
            mathml = latex_math(node.get("data-latex") or node.get_text(), "inline")
            if mathml is None:
                if not _PRIMARY_OCR_ONLY.get():
                    raise ValueError("Table contains an undecodable mathematical expression")
                literal = soup.new_tag('math', xmlns='http://www.w3.org/1998/Math/MathML')
                literal['data-math-status'] = 'primary-literal-unparsed'
                value = soup.new_tag('mtext')
                value.string = node.get('data-latex') or node.get_text()
                literal.append(value)
                node.replace_with(literal)
                diagnostic_event('PRIMARY_FORMULA_LITERAL_KEPT', reason='primary_notation_cannot_render')
                continue
            node.replace_with(BeautifulSoup(mathml, "html.parser").math)
        elif node.name == "img":
            continue
        elif node.name not in {"thead", "tbody", "tfoot", "tr", "td", "th", "caption", "br", "sub", "sup", "p", "strong", "em"}:
            node.unwrap()
        else:
            node.attrs = {key: value for key, value in node.attrs.items() if key in {"rowspan", "colspan", "scope"}}
    table.attrs = {}
    normalize_table_word_wraps(table)
    return str(table)


def normalize_html_table_words(content):
    """Share cell-only wrap repair with Office and API exports, preserving markup."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    changed = bool(normalize_metric_units(soup))
    for table in soup.find_all("table"):
        before = str(table)
        normalize_table_word_wraps(table)
        changed = changed or str(table) != before
    return str(soup) if changed else content


def table_profile(fragment, expected_columns=None):
    """Check actual HTML occupancy, OCR noise and outliers in numeric columns."""
    from bs4 import BeautifulSoup
    from docling_pdf_sparse import SparseGrid

    table = BeautifulSoup(fragment, "html.parser").find("table")
    issues, occupied, widths, columns, cells = [], SparseGrid(), [], {}, []
    row_right = {}
    text = table.get_text(" ", strip=True) if table else ""
    if table is None:
        return {"issues": ["no_table"], "score": -100, "columns": 0, "rows": 0, "text": ""}
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    try:
        for y, row in enumerate(rows):
            x = 0
            for cell in row.find_all(["td", "th"], recursive=False):
                x = occupied.next_free(y, x)
                rs, cs = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
                if not (1 <= rs <= len(rows) - y and cs >= 1):
                    raise ValueError("invalid_span")
                for dy in range(rs):
                    try:
                        occupied.add(y+dy, x, x+cs)
                    except ValueError as error:
                        raise ValueError('overlapping_span') from error
                    row_right[y + dy] = max(row_right.get(y + dy, 0), x + cs)
                value = cell.get_text(" ", strip=True)
                cells.append({"row": y, "column": x, "row_span": rs, "column_span": cs,
                              "header": cell.name == "th", "text": value, "html": cell.decode_contents(),
                              "mathematical_label": mathematical_cell_label(cell) if cell.find("math") or re.search(r"[\u0370-\u03ff]", value) else False})
                if cell.name == "td" and cs == 1 and y > 0:
                    columns.setdefault(x, []).append(value)
                x += cs
            width = row_right.get(y, 0)
            if occupied.has_gaps(y):
                issues.append("grid_gap")
            widths.append(width)
        if len(set(widths)) != 1 or not widths or not widths[0]:
            issues.append("inconsistent_grid")
    except (ValueError, TypeError) as exc:
        issues.append(str(exc))
    width = max(widths, default=0)
    if expected_columns and width != expected_columns:
        issues.append(f"raster_columns_{expected_columns}_html_columns_{width}")
    from docling_pdf_structure import numbered_header_width
    numbered_width = numbered_header_width(table)
    if numbered_width and width != numbered_width:
        issues.append(f'numbered_header_width_{numbered_width}_html_{width}')

    def numeric(value):
        # Quantitative cells may contain explicit exemptions, including mixed
        # lines such as "Не огр. / 160 / 120". These are not OCR corruption.
        value = value.translate(str.maketrans("Heop", "Неор"))
        exemption = r"н\s*е\s*(?:о\s*г\s*р(?:анич\w*)?|н\s*о\s*р\s*м(?:\w*)?)\.?"
        has_exemption = bool(re.search(exemption, value, re.I))
        value = re.sub(exemption, "", value, flags=re.I)
        # Printed numerical ranges legitimately use "Св." (= above) and
        # ditto marks in place of a repeated range operator. Interpret these
        # only for validation; keep the source spelling and marks in HTML.
        value = re.sub(r"\bсв\.(?=\s*\d)", "", value, flags=re.I)
        value = re.sub(r"\bи\s+(?:более|менее)\b", "", value, flags=re.I)
        value = re.sub(r"\b(?:от|до|менее|более|не|свыше)\b", "", value, flags=re.I)
        value = re.sub(r'(?<=\d)\s*["»″“”„]\s*(?=[+\-−]?\d)', " ", value)
        return (has_exemption and not value.strip()) or (bool(re.search(r"\d", value)) and not re.search(r"[^\d\s.,+\-−–—±/%°×*^()[\]<>≤≥=:]", value))

    first_data_row = min((cell['row'] for cell in cells if not cell['header']
                          and numeric(cell['text'])), default=len(rows))
    columns = {}
    for cell in cells:
        if cell["row"] >= first_data_row and cell["column_span"] == 1 and not cell["header"]:
            columns.setdefault(cell["column"], []).append(cell["text"])
    numeric_outliers = []
    for column, values in columns.items():
        values = [value for value in values if value]
        numeric_count = sum(numeric(value) for value in values)
        if len(values) >= 3 and numeric_count >= max(2, len(values) * 0.55):
            labels = {c["text"] for c in cells if c["column"] == column and c.get("mathematical_label")}
            invalid = {value for value in values if value not in labels and not numeric(value)
                       and not re.search(r"не\s*норм|^[-—–]+$", value, re.I)}
            if invalid:
                issues.append("numeric_column_outlier")
                numeric_outliers.extend({"row": c["row"], "column": column, "text": c["text"]}
                                        for c in cells if c["column"] == column and c["column_span"] == 1
                                        and c["row"] >= first_data_row and not c["header"] and c["text"] in invalid)
    # Column statistics are a review hint, not proof of a broken table: mixed
    # categorical/numerical columns and full-width notes are commonplace.
    warnings = sorted({issue for issue in issues if issue == "numeric_column_outlier"
                       or issue.startswith("raster_columns_")})
    issues = sorted(set(issues) - set(warnings))
    return {"issues": issues, "score": 100 - len(issues) * 30, "columns": width,
            "rows": len(rows), "text": text, "cells": cells, "numeric_outliers": numeric_outliers,
            "math_count": len(table.find_all("math")), "warnings": warnings}


def repair_table_spans(fragment, expected_columns=None):
    """Repair cell occupancy without dropping or rewriting any cell contents."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment, "html.parser")
    table = soup.find("table")
    rows = table.find_all("tr") if table else []
    repairs = []
    if table is not None and not rows:
        orphaned = table.find_all(["td", "th"])
        if orphaned:
            row = soup.new_tag("tr")
            for cell in orphaned:
                row.append(cell.extract())
            table.append(row)
            rows = [row]
            repairs.append({"operation": "wrap_orphan_cells"})
        elif table.get_text(strip=True):
            row, cell = soup.new_tag("tr"), soup.new_tag("td")
            for child in list(table.contents):
                cell.append(child.extract())
            row.append(cell);table.append(row);rows = [row]
            repairs.append({"operation": "wrap_existing_table_text"})
    if not rows:
        return fragment, []
    for y, row in enumerate(rows):
        for cell in row.find_all(["td", "th"], recursive=False):
            for name, limit in (("rowspan", len(rows) - y), ("colspan", None)):
                raw = cell.get(name, "1")
                try:
                    value = int(raw)
                except (ValueError, TypeError):
                    value = 1
                if name == "rowspan" and value == 0:
                    group = row.find_parent(["thead", "tbody", "tfoot"])
                    group_rows = group.find_all("tr") if group else rows
                    value = len(group_rows) - next(i for i, candidate in enumerate(group_rows) if candidate is row)
                value = min(value, limit) if value > 0 and limit is not None else max(1,value)
                if str(value) != str(raw):
                    cell[name] = str(value)
                    repairs.append({"row": y, "operation": "normalize_span", "attribute": name})
    # Do not alter a valid merged table on the strength of a raster estimate.
    if not table_profile(str(table))["issues"]:
        return str(table), repairs
    nodes = table.find_all(['td','th'])
    if any(int(cell.get('colspan',1)) > len(nodes) for cell in nodes):
        # A large span is one interval, not millions of artificial positions.
        # Ordinary repairs keep their established row-merging decisions.
        from docling_pdf_sparse import repair_sparse_spans
        sparse_fragment, sparse_repairs = repair_sparse_spans(str(table), expected_columns)
        return sparse_fragment, repairs+sparse_repairs
    first = rows[0].find_all(["th", "td"], recursive=False)
    header_width = sum(int(cell.get("colspan", 1)) for cell in first)
    from docling_pdf_structure import numbered_header_width
    expected = numbered_header_width(table) or expected_columns or header_width or max(
        (sum(int(cell.get("colspan", 1)) for cell in row.find_all(["td", "th"], recursive=False)) for row in rows), default=1)
    if expected < 1:
        expected = max(1, header_width)
    active, previous = {}, {}

    def numeric_text(text):
        return bool(re.search(r"\d", text)) and not re.search(r"[A-Za-zА-Яа-я]", text)

    for y, row in enumerate(rows):
        active = {x: entry for x, entry in active.items() if entry[1] > y}
        cells = row.find_all(["td", "th"], recursive=False)
        explicit = sum(int(cell.get("colspan", 1)) for cell in cells)
        if explicit == expected and active:
            # A complete new row terminates every previous vertical merge.
            for start, end, node in {id(entry[2]): entry for entry in active.values()}.values():
                node["rowspan"] = str(y - start)
            active = {}
            repairs.append({"row": y, "operation": "terminate_overlong_rowspan"})
        if len(cells) == 1 and not active and explicit != expected:
            cells[0]["colspan"] = str(expected)
            explicit = expected
            repairs.append({"row": y, "operation": "complete_full_width_cell"})
        missing = expected - explicit - len(active)
        free = next((x for x in range(expected) if x not in active), None)
        if missing == 1 and free is not None and free in previous and cells:
            start, end, node = previous[free]
            text = node.get_text(" ", strip=True)
            current_values = [cell.get_text(" ", strip=True) for cell in cells]
            if (end == y and int(node.get("colspan", 1)) == 1 and not numeric_text(text)
                    and re.search(r"[A-Za-zА-Яа-я]{3}", text)
                    and sum(numeric_text(value) for value in current_values) >= len(current_values) * 0.7):
                node["rowspan"] = str(y - start + 1)
                active[free] = (start, y + 1, node)
                repairs.append({"row": y, "column": free, "operation": "extend_text_rowspan"})
        placed, x = {}, 0
        for cell_index, cell in enumerate(cells):
            while x in active:
                x += 1
            cs, rs = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
            run = next((dx for dx in range(cs) if x + dx in active), cs)
            if expected >= len(cells) + len(active) and x < expected:
                run = min(run, max(1, expected - x - (len(cells) - cell_index - 1)))
            if run < cs:
                cell["colspan"] = str(run)
                cs = run
                repairs.append({"row": y, "operation": "shorten_colliding_colspan"})
            entry = (y, y + rs, cell)
            for dx in range(cs):
                placed[x + dx] = entry
                if rs > 1:
                    active[x + dx] = entry
            x += cs
        previous = {**active, **placed}
    if table_profile(str(table))["issues"]:
        # HTML places cells in source order. Find a free contiguous interval
        # for each span, then fill only the remaining empty grid positions.
        # This preserves every value, formula and registered source image.
        occupied, placements, width = set(), [], 0
        for y, row in enumerate(rows):
            x = 0
            for cell in row.find_all(["td", "th"], recursive=False):
                cs, rs = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
                while any((y, x + dx) in occupied for dx in range(cs)):
                    x += 1
                placements.append((y, x, cell))
                occupied.update((yy, xx) for yy in range(y, y + rs) for xx in range(x, x + cs))
                width = max(width, x + cs)
                x += cs
        for y, row in enumerate(rows):
            by_column = {x: cell for yy, x, cell in placements if yy == y}
            for cell in row.find_all(["td", "th"], recursive=False):
                cell.extract()
            for x in range(width):
                if x in by_column:
                    row.append(by_column[x])
                elif (y, x) not in occupied:
                    row.append(soup.new_tag("th" if row.find_parent("thead") else "td"))
        repairs.append({"operation": "rebuild_rectangular_occupancy", "columns": width})
    return str(table), repairs


def repair_numbered_header_spans(fragment, geometry):
    """Match header merges to source rails, anchored by a complete 1..N row.

    Every physical row must have exactly the same number/order of cells in
    OCR and the raster grid. Change spans only; never drop a value or image.
    """
    from bs4 import BeautifulSoup
    if not geometry or geometry.get('basis') != 'raster_numbered_header_rules':
        return fragment, []
    soup = BeautifulSoup(fragment, 'html.parser')
    table = soup.find('table')
    rows = [r for r in table.find_all('tr') if r.find_parent('table') is table] if table else []
    width = len(geometry['x_grid'])-1
    if len(rows) != len(geometry['y_grid'])-1:
        return fragment, []
    numbered = any([c.get_text(' ',strip=True) for c in r.find_all(['td','th'],recursive=False)]
                   == [str(i) for i in range(1,width+1)] for r in rows)
    if width < 4 or not numbered:
        return fragment, []
    plan = []
    for y, row in enumerate(rows):
        nodes = row.find_all(['td','th'],recursive=False)
        targets = sorted((c for c in geometry['cells'] if c['row']==y),key=lambda c:c['column'])
        if len(nodes) != len(targets):
            return fragment, []
        plan.extend(zip(nodes,targets))
    repairs = []
    for node, target in plan:
        for attr, key in (('rowspan','row_span'),('colspan','column_span')):
            value = target[key]
            if str(node.get(attr,1)) != str(value):
                repairs.append(dict(operation='numbered_header_source_rules',row=target['row'],
                                    column=target['column'],attribute=attr,value=value))
                if value == 1: node.attrs.pop(attr,None)
                else: node[attr]=str(value)
    result = str(table)
    if table_profile(result,width)['issues']:
        return fragment, []
    return result, repairs


def raster_column_positions(image, *, page_edge=False):
    """Use long raster rules only when they form a sufficiently complete grid."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert("L"))
    if page_edge:
        # Continued data may occupy only a short row above full-width notes.
        gray = gray[:min(gray.shape[0], max(48, round(gray.shape[0] * .25)))]
    mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, gray.shape[0] // 3))))
    indices = np.where((vertical > 0).sum(axis=0) > gray.shape[0] * 0.45)[0]
    groups = []
    for x in indices:
        if not groups or x - groups[-1][-1] > 4:
            groups.append([int(x)])
        else:
            groups[-1].append(int(x))
    if len(groups) >= 3 and groups[-1][-1] - groups[0][0] > gray.shape[1] * 0.65:
        return [sum(group) / len(group) for group in groups]
    return []


def native_cell_lines(chars):
    """Group real PDF glyphs by baseline; retain font sizes and visible order."""
    from html import escape
    from statistics import median

    from docling_pdf_native_scripts import attach_source_scripts
    chars = attach_source_scripts(chars, latex_math)

    lines = []
    for char in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if ((not char["text"] or any(c in char["text"] for c in "\r\n"))
                and not char.get("source_html")):
            continue
        line = next((line for line in reversed(lines) if
                     abs(median(c["bottom"] for c in line) - char["bottom"])
                     <= .50 * max(char["size"], median(c["size"] for c in line))), None)
        if line is None:
            lines.append([char])
        else:
            line.append(char)
    result = []
    for line in lines:
        line.sort(key=lambda c: c["x0"])
        size = median(c["size"] for c in line)
        baseline = median(c["bottom"] for c in line if c["size"] >= size * .9)
        html, plain, previous = [], [], None
        for char in line:
            if (previous and previous["text"].strip() and char["text"].strip()
                    and char["x0"] - previous["x1"] > size * .18):
                html.append(" ")
                plain.append(" ")
            text = char.get("source_html") or escape(char["text"])
            if not char.get("source_html") and char["size"] < size * .9 and char["bottom"] < baseline - size * .12:
                text = f"<sup>{text}</sup>"
            html.append(text)
            plain.append(char["text"])
            previous = char
        record={"y": baseline, "html": "".join(html).strip(), "text": "".join(plain).strip(), "size": size,
                "x0": min(c["x0"] for c in line), "x1": max(c["x1"] for c in line)}
        proofs=[c['source_script_proof'] for c in line if c.get('source_script_proof')]
        if proofs:record['source_scripts']=proofs
        result.append(record)
    logical = []
    for line in sorted(result, key=lambda line: line["y"]):
        if logical:
            previous = logical[-1]
            roman_wrap = (previous["text"].endswith(",")
                          and re.fullmatch(r"[IVXLCDM,\s]+", previous["text"])
                          and re.fullmatch(r"[IVXLCDM,\s]+", line["text"]))
            code_wrap = (re.search(r"[A-Za-zА-Яа-яЁё0-9]-$", previous["text"])
                         and re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9-]+", previous["text"])
                         and re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9]+", line["text"]))
            probe = line["text"].translate(str.maketrans("Heop", "Неор"))
            exemption_wrap = (re.fullmatch(r"[НH][еe]", previous["text"], re.I)
                              and re.match(r"(?:огр|норм)", probe, re.I))
            exemption_letter_wrap = (previous["text"] in {"Н", "H"}
                                     and re.match(r"е\s+(?:огр|норм)", probe, re.I))
            if roman_wrap or code_wrap or exemption_wrap or exemption_letter_wrap:
                separator = " " if roman_wrap or exemption_wrap else ""
                previous["text"] += separator + line["text"]
                previous["html"] += separator + line["html"]
                previous["y"] = (previous["y"] + line["y"]) / 2
                continue
        logical.append(line)
    return logical


def cell_text_key(text):
    """Compare source text anchors, allowing only whitespace and word wraps."""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    return re.sub(r"(?<=[A-Za-zА-Яа-яЁё])[-\u00ad](?=[A-Za-zА-Яа-яЁё])", "", text)


def scan_header_direction_evidence(cells, reference_profiles):
    """Disprove a scan's old text layer on numbered, otherwise blank forms.

    This supplements the body-row check: many normative forms consist ONLY
    of multi-level headers and a row numbered 1..N. Use already available OCR,
    never another model call, and leave mixed illustration tables alone.
    """
    from bs4 import BeautifulSoup

    def text(cell):
        return cell.get('text', BeautifulSoup(cell.get('html', ''), 'html.parser').get_text(' ', strip=True))

    numbered = []
    blank_form = False
    for profile in reference_profiles:
        for y in range(profile.get('rows', 0)):
            row = sorted((c for c in profile['cells'] if c['row'] == y), key=lambda c: c['column'])
            values = [text(c).strip() for c in row if text(c).strip()]
            if (len(values) >= 4 and values == [str(i) for i in range(1, len(values) + 1)]
                    and all(c['row_span'] == 1 and c['column_span'] == 1 for c in row)):
                numbered.append(len(values))
                # Filled data below the index row is not a blank form. Some
                # templates have full-width address/floor labels underneath.
                below = [c for c in profile['cells'] if c['row'] > y and text(c).strip()]
                blank_form = blank_form or all(c['column'] == 0 and c['column_span'] >= len(values)
                                               and re.search(r'[А-Яа-яЁёA-Za-z]{3}', text(c)) for c in below)
    glyphs = [g for cell in cells for g in cell.get('glyphs', ()) if g['text'].strip()]
    def turned(g):
        matrix = g.get('matrix')
        if matrix is None or len(matrix) < 4:
            return False
        a, b, c, d = matrix[:4]
        return abs(b) > max(abs(a)*3, 1e-5) and abs(c) > max(abs(d)*3, 1e-5)
    count = sum(turned(g) for g in glyphs)
    affected = []
    for cell in cells:
        local = [g for g in cell.get('glyphs', ()) if g['text'].strip()]
        rotated = sum(turned(g) for g in local)
        if rotated >= 3:
            affected.append(cell)
    affected_glyphs = sum(sum(turned(g) for g in c.get('glyphs', ()) if g['text'].strip()) for c in affected)
    native = cell_text_key(' '.join(text(c) for c in cells))
    anchors = {cell_text_key(text(c)) for p in reference_profiles for c in p['cells']
               if len(text(c).strip()) >= 8 and re.search(r'[А-Яа-яЁёA-Za-z]{3}', text(c))}
    different = {key for key in anchors if key not in native}
    mixed = any('<img' in c.get('html', '').lower() or re.search(
        r'изображен|эскиз|схема|чертеж|drawing|sketch|diagram', text(c), re.I)
        for p in reference_profiles for c in p['cells'])
    # Ordinary filled-in tables keep the stricter body-row check.
    conflict = bool(numbered and blank_form and affected_glyphs >= 40
                    and len(affected) >= 3 and len({c['column'] for c in affected}) >= 3
                    and len(different) >= 2 and not mixed)
    return dict(conflict=conflict, numbered_columns=sorted(set(numbered)), blank_form=blank_form,
                glyphs=len(glyphs), quarter_turned_glyphs=count, affected_cells=len(affected),
                different_fresh_text_cells=len(different), mixed_reference_graphics=mixed)


def native_subrows(cells):
    """Split a shared ruled row only when several numeric columns prove its baselines."""
    from statistics import median
    cells = sorted(cells, key=lambda c: c['column'])
    cells = [dict(c, lines=[line for line in c['lines'] if line['text'].strip()]) for c in cells]
    if (len(cells) < 3 or cells[0]['column'] != 0
            or any(c['row_span'] != 1 or c['column_span'] != 1 or c.get('source_images')
                   or c.get('restored_math') or c.get('mapped_ocr_html') for c in cells)):
        return None
    numeric = cells[1:]
    count = len(numeric[0]['lines'])
    if count < 2 or any(len(c['lines']) != count for c in numeric):
        return None
    if any(not re.fullmatch(r'\d+(?:[.,]\d+)?', l['text']) for c in numeric for l in c['lines']):
        return None
    size = median(l['size'] for c in numeric for l in c['lines'])
    anchors = [median(c['lines'][i]['y'] for c in numeric) for i in range(count)]
    if any(abs(c['lines'][i]['y'] - anchors[i]) > size * .4 for c in numeric for i in range(count)):
        return None
    groups = [[] for _ in anchors]; prefix = []
    for line in cells[0]['lines']:
        if line['y'] < anchors[0] - size * .5:
            prefix.append(line)
        else:
            index = max(i for i, y in enumerate(anchors) if line['y'] >= y - size * .5)
            groups[index].append(line)
    if any(not group for group in groups):
        return None
    # A category heading at an open bottom edge belongs to the following data,
    # not to the preceding record (e.g. a heading ending with a colon).
    suffix = []
    if groups[-1][-1]['text'].rstrip().endswith(':'):
        index = len(groups[-1]) - 1
        while index > 0 and re.search(r'\b(?:для|в|на|по)\s*$', groups[-1][index-1]['text'], re.I):
            index -= 1
        # Preserve at least the description anchored to the last numeric value.
        if index > 0:
            suffix = groups[-1][index:]; groups[-1] = groups[-1][:index]
    result = []
    def row(lines, index=None):
        return [dict(c, lines=lines if col == 0 else ([] if index is None else [c['lines'][index]]))
                for col, c in enumerate(cells)]
    if prefix: result.append(row(prefix))
    result.extend(row(lines, i) for i, lines in enumerate(groups))
    if suffix: result.append(row(suffix))
    return result


def catalogue_row_key(text):
    """A printed row identifier, e.g. M02 or F01-02, rather than a heading."""
    match = re.fullmatch(r"([A-ZА-ЯЁ]{1,4})(\d{1,5}(?:[-.]\d{1,5})*)", re.sub(r"\s+", "", text))
    if match is None:
        return None
    prefix = match[1].translate(str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX"))
    return prefix, tuple(int(value) for value in re.split(r"[-.]", match[2]))


def restore_inline_cell_math(glyphs, objects, references):
    """Resolve raster math only when existing OCR matches the PDF text anchors.

    No new inference: use the OCR already obtained for this table. Geometry or
    a small bitmap alone never proves that an object is a letter or formula.
    Ambiguous, oversized and unanchored objects remain source crops.
    """
    from bs4 import BeautifulSoup
    from statistics import median

    if not glyphs or not objects:
        return []
    size = median(c["size"] for c in glyphs)
    if any(obj["x1"] - obj["x0"] > 12 * size or obj["height"] > 2.2 * size for obj in objects):
        return []
    anchor = "".join(c["text"] for c in glyphs)
    if len(re.findall(r"[A-Za-zА-Яа-яЁё]", anchor)) < 6:
        return []
    ordered = sorted(objects, key=lambda obj: (obj["bottom"], obj["x0"]))
    # The same baseline grouping is used for the final HTML. Match the entire
    # cell, including each raster's position among the real text characters.
    markers = [{**obj, "text": "\ufffc", "source_html": ""} for obj in ordered]
    key = cell_text_key("".join(line["text"] for line in native_cell_lines(glyphs + markers)))
    matches = {}
    for ref in references:
        parsed = BeautifulSoup(ref["html"], "html.parser")
        maths = parsed.find_all("math")
        if len(maths) != len(objects):
            continue
        expressions, replacements = [], []
        for node in maths:
            latex = node.get("data-latex")
            # Shapes/arrows by themselves can be real pictograms. Require a
            # mathematical identifier or number, not only an operator glyph.
            identifiers = [tag.get_text() for tag in node.find_all("mi")]
            if not latex or not (any(value.isalpha() for value in identifiers) or node.find("mn")):
                break
            rendered = latex_math(latex, "inline")
            if rendered is None:
                break
            replacements.append((rendered, node.get_text(), latex))
            expressions.append(latex)
            node.replace_with("\ufffc")
        else:
            if cell_text_key(parsed.get_text()) == key:
                matches[tuple(expressions)] = replacements
    if len(matches) != 1:
        return []
    replacements = next(iter(matches.values()))
    # Map in actual reading order, not PDF resource order or image dimensions.
    reading_order = []
    for line in native_cell_lines(glyphs + [dict(obj, text=f"\ufffc{i}\ufffc", source_html="")
                                          for i, obj in enumerate(ordered)]):
        reading_order.extend(int(value) for value in re.findall(r"\ufffc(\d+)\ufffc", line["text"]))
    if len(reading_order) != len(objects):
        return []
    restored = []
    for index, (html, text, latex) in zip(reading_order, replacements):
        obj = ordered[index]
        obj["source_html"], obj["text"] = html, text
        restored.append({"bbox": [obj["x0"], obj["image_top"], obj["x1"], obj["image_bottom"]],
                         "latex": latex, "method": "ocr_math_with_matching_pdf_text_anchors"})
    return restored


def restore_pure_fraction_cell(objects, references, row, column, row_span, col_span, columns, context):
    """Use a fraction-only OCR cell after its grid and neighboring text agree.

    This complements the inline-text route for cells made entirely of a small
    raster formula. It never interprets drawings, arrows or pictograms as math.
    """
    from bs4 import BeautifulSoup
    if len(objects) != 1:
        return []
    choices = {}
    for ref in references:
        if (ref.get('_columns') != columns or (ref['row'], ref['column'], ref['row_span'], ref['column_span'])
                != (row, column, row_span, col_span)):
            continue
        parsed = BeautifulSoup(ref['html'], 'html.parser')
        maths = parsed.find_all('math')
        if len(maths) != 1 or not maths[0].find('mfrac'):
            continue
        latex = maths[0].get('data-latex', '')
        if (not re.search(r'\\frac\{\d+\}\{\d+\}', latex)
                or re.sub(r'\\(?:frac|div)|[\d{}\s]', '', latex)):
            continue
        peers = [c for c in references if c.get('_reference') == ref.get('_reference') and c['row']==row]
        if not any(cell_text_key(c['text']) == cell_text_key(context.get((row,c['column']), ''))
                   and c['text'].strip() for c in peers if c['column'] != column):
            continue
        outside = BeautifulSoup(str(parsed), 'html.parser')
        for node in outside.find_all('math'):
            node.decompose()
        if outside.get_text(strip=True):
            continue
        choices[latex] = (str(maths[0]), maths[0].get_text())
    if len(choices) != 1:
        return []
    latex, (html, text) = next(iter(choices.items()))
    obj = objects[0]
    obj['source_html'], obj['text'] = html, text
    return [{'bbox':[obj['x0'],obj['image_top'],obj['x1'],obj['image_bottom']],
             'latex':latex,'method':'fraction_ocr_with_matching_cell_grid_and_neighbor_text'}]


def scripted_cell_math(fragment):
    """Keep OCR sub/sup structure when a source cell is recovered as MathML."""
    from bs4 import BeautifulSoup, NavigableString
    from docling_office_math import parsed_formula
    if re.search(r'<br\s*/?>', fragment, re.I):
        parts = [scripted_cell_math(part) for part in re.split(r'<br\s*/?>', fragment, flags=re.I)]
        return '<br/>'.join(parts) if all(part is not None for part in parts) else None
    soup = BeautifulSoup(fragment, 'html.parser')
    if not soup.find(['math', 'sub', 'sup']):
        return None
    if not soup.find('math') and not re.search(r'[А-Яа-яЁё]', soup.get_text()):
        rendered = parsed_formula(str(soup), latex_math)
        if rendered:
            return rendered
    for script in list(soup.find_all(['sub', 'sup'])):
        previous = script.previous_sibling
        if not isinstance(previous, NavigableString):
            return None
        match = re.search(r'([A-Za-zА-Яа-яЁёΑ-Ωα-ω]+|\d+)\s*$', str(previous))
        if not match:
            return None
        expression = match[1] + ('_{' if script.name == 'sub' else '^{') + script.get_text() + '}'
        rendered = latex_math(expression, 'inline')
        if not rendered:
            return None
        previous.replace_with(str(previous)[:match.start()])
        script.replace_with(BeautifulSoup(rendered, 'html.parser').math)
    root = soup.find(['td', 'th'])
    return root.decode_contents() if root else str(soup)


def source_anchored_cell_math(glyphs, objects, references, row, column, row_span, col_span, columns, context):
    """Reuse page OCR only after the PDF cell's text/placement or CAS proves it.

    Native geometry knows the grid but cannot read raster chemical formulae.
    A source image is not evidence that fresh OCR should be discarded. Require
    all visible native text in its original order around each image, or exact
    CAS row identifiers for an image-only formula column. Never use a drawing
    column as evidence for replacing a picture with a mathematical label.
    """
    from bs4 import BeautifulSoup
    if not objects:
        return None
    if any(obj.get('source_type') != 'image' for obj in objects):
        return None
    headings = ' '.join(ref['text'] for ref in references
                        if ref.get('_columns') == columns and ref['column'] == column
                        and (ref.get('header') or ref['row'] < row))
    if re.search(r'эскиз|схема|рисунок|drawing|sketch|diagram', headings, re.I):
        return None
    def key(text):
        text = cell_text_key(text).translate(str.maketrans({'х':'x', '×':'x', '−':'-', '’':"'", '′':"'"}))
        return re.sub(r'(?<=[A-Za-zА-Яа-яЁё\ufffc])-+(?=[A-Za-zА-Яа-яЁё\ufffc])', '', text)
    template = key(''.join(line['text'] for line in native_cell_lines(
        glyphs + [dict(obj, text='\ufffc', source_html='') for obj in objects])))
    pattern = '^' + re.escape(template).replace('\ufffc', '(.{1,100}?)') + '$'
    source_cas_order = list(dict.fromkeys(re.findall(r'\b\d{2,7}-\d{2}-\d\b', ' '.join(
        value for (r, c), value in context.items() if r == row and c != column))))
    source_cas = set(source_cas_order)
    choices = {}
    for reference_id in sorted({ref.get('_reference', 0) for ref in references}):
        group = [ref for ref in references if ref.get('_reference', 0) == reference_id
                 and ref.get('_columns') == columns]
        direct = [ref for ref in group if (ref['row'], ref['column'], ref['row_span'], ref['column_span'])
                  == (row, column, row_span, col_span)]
        # Some PDFs put several catalogue entries in one ruled row; OCR may
        # split it into separate rows. CAS proves the exact group without
        # guessing by row number or copying a neighboring substance's formula.
        cas_rows = []
        if source_cas:
            for ref in group:
                if ref['column'] != column or ref['column_span'] != col_span:
                    continue
                peers = [p['text'] for p in group if p['row'] == ref['row'] and p['column'] != column]
                peer_cas = set(re.findall(r'\b\d{2,7}-\d{2}-\d\b', ' '.join(peers)))
                if peer_cas and peer_cas <= source_cas:
                    cas_rows.append((ref, peer_cas))
        direct_verified = False
        if len(direct) == 1:
            peers = [p for p in group if p['row'] == direct[0]['row'] and p['column'] != column
                     and p['column_span'] == 1 and p['text'].strip()
                     and key(p['text']) == key(context.get((row, p['column']), ''))]
            direct_verified = (not source_cas and len(peers) >= 2
                               and any(re.search(r'[A-Za-zА-Яа-яЁё]{3}', p['text']) for p in peers))
        candidates = [(direct, direct_verified)] if len(direct) == 1 else []
        # A substance can occur again in a mixture farther down the table.
        # Treat each occurrence/group separately, preserving source CAS order;
        # never concatenate every occurrence of a repeated identifier.
        cas_rows.sort(key=lambda entry: entry[0]['row'])
        for start in range(len(cas_rows)):
            selected, ordered = [], []
            for ref, identifiers in cas_rows[start:]:
                if (set(ordered) & identifiers
                        or (selected and ref['row'] != selected[-1]['row'] + selected[-1]['row_span'])):
                    break
                selected.append(ref)
                ordered.extend(re.findall(r'\b\d{2,7}-\d{2}-\d\b', ' '.join(
                    p['text'] for p in group if p['row'] == ref['row'] and p['column'] != column)))
                ordered = list(dict.fromkeys(ordered))
                if ordered == source_cas_order:
                    candidates.append((list(selected), True))
                    break
        for selected, cas_verified in candidates:
            raw = '<br/>'.join(ref['html'] for ref in selected)
            parsed = BeautifulSoup(raw, 'html.parser')
            if parsed.find(['img', 'svg', 'table']):
                continue
            typed = scripted_cell_math(raw)
            # An isolated Greek character in a native text definition can be
            # emitted as plain text by OCR. Retain it only with exact anchors.
            if typed is None and re.search(r'[Α-Ωα-ω]', parsed.get_text()):
                typed = raw
            if typed is None:
                continue
            match = re.fullmatch(pattern, key(parsed.get_text()))
            if not match:
                continue
            if not glyphs and not cas_verified:
                continue
            # Equal flattened text is not equal mathematics: H_2 and H^2
            # have identical characters. Conflicting structures must decline.
            choices[typed] = typed
    if len(choices) != 1:
        diagnostic_event('NATIVE_CELL_MATH_KEEP', row=row, column=column,
                         reason='conflicting_ocr_math' if choices else 'source_anchors_not_verified',
                         image_count=len(objects), source_cas=source_cas_order,
                         source_template=template[:180])
        return None
    content = next(iter(choices.values()))
    for obj in objects:
        obj['source_html'] = ''
    return content


def scan_cell_graphic(mask, image_region, cell_bbox, glyphs, sx, sy, *, pure_math=False,graphic_hint=False,full_ink=False,source_image=None):
    """Locate non-text ink inside a scanned cell using positioned OCR glyphs.

    Only the detection mask is erased; returned crops use the untouched scan,
    including dimensions and labels. Grid borders and ordinary text cannot
    independently classify an entire column as illustrations.
    """
    import cv2
    import numpy as np
    from statistics import median

    x0,y0,x1,y1 = [round(v) for v in ((cell_bbox[0]-image_region[0])*sx,(cell_bbox[1]-image_region[1])*sy,
                                     (cell_bbox[2]-image_region[0])*sx,(cell_bbox[3]-image_region[1])*sy)]
    trim_x,trim_y = max(3,round(sx)),max(3,round(sy))
    x0,y0 = max(0,x0+trim_x),max(0,y0+trim_y)
    x1,y1 = min(mask.shape[1],x1-trim_x),min(mask.shape[0],y1-trim_y)
    if source_image is not None:
        from docling_pdf_cell_crop import cell_content_crop
        interior=cell_content_crop(source_image,image_region,cell_bbox)
        if interior is None:return None
        x0,y0,x1,y1=interior[0]
    residual = mask[y0:y1,x0:x1].copy()
    if not residual.size:
        diagnostic_event('CELL_GRAPHIC_REJECT',cell_bbox=cell_bbox,reason='empty_trimmed_cell')
        return None
    original = residual.copy()
    original_ink = max(1,int(np.count_nonzero(residual)))
    # Old OCR may transcribe a complete drawing as letters/slashes. Detect
    # connected source strokes BEFORE erasing those untrusted glyph boxes.
    from docling_table_content import connected_scan_graphics
    font = median(c['size'] for c in glyphs) if glyphs else 9
    connected = connected_scan_graphics(original,sx,sy,font)
    for char in glyphs:
        pad = max(.8,char['size']*.12)
        left,top = max(0,round((char['x0']-image_region[0]-pad)*sx)-x0),max(0,round((char['top']-image_region[1]-pad)*sy)-y0)
        right,bottom = min(residual.shape[1],round((char['x1']-image_region[0]+pad)*sx)-x0),min(residual.shape[0],round((char['bottom']-image_region[1]+pad)*sy)-y0)
        if right>left and bottom>top:
            residual[top:bottom,left:right]=0
    residual = cv2.morphologyEx(residual,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    _,_,stats,_ = cv2.connectedComponentsWithStats(residual,8)
    parts = [[image_region[0]+(x0+x)/sx,image_region[1]+(y0+y)/sy,
              image_region[0]+(x0+x+w)/sx,image_region[1]+(y0+y+h)/sy]
             for x,y,w,h,area in stats[1:] if w/sx>=10 and h/sy>=5 and area/(sx*sy)>=5]
    connected_boxes = [[image_region[0]+(x0+x)/sx,image_region[1]+(y0+y)/sy,
                        image_region[0]+(x0+x+w)/sx,image_region[1]+(y0+y+h)/sy]
                       for x,y,w,h in connected]
    parts.extend(connected_boxes)
    complete_unpositioned_ink = not glyphs and bool(parts)
    if complete_unpositioned_ink:
        # Component sizes may nominate a raster asset, but cannot trim its
        # content when no positioned text represents the remaining ink.
        # Include small disconnected digits, indices and complete last rows.
        ink_y, ink_x = np.where(original)
        if len(ink_x):
            parts = [[image_region[0]+(x0+int(ink_x.min()))/sx,
                      image_region[1]+(y0+int(ink_y.min()))/sy,
                      image_region[0]+(x0+int(ink_x.max())+1)/sx,
                      image_region[1]+(y0+int(ink_y.max())+1)/sy]]
            diagnostic_event('CELL_GRAPHIC_COMPLETE_UNPOSITIONED_INK',cell_bbox=cell_bbox,
                source_ink_pixels=int(np.count_nonzero(original)),bbox=parts[0])
    elif (graphic_hint or (full_ink and parts)) and not pure_math:
        _,_,raw_stats,_=cv2.connectedComponentsWithStats(original,8)
        ink=[(x,y,w,h) for x,y,w,h,area in raw_stats[1:] if area/(sx*sy)>=.25]
        if ink:
            a,b=min(v[0] for v in ink),min(v[1] for v in ink)
            z,t=max(v[0]+v[2] for v in ink),max(v[1]+v[3] for v in ink)
            w,h=(z-a)/sx,(t-b)/sy
            center=(image_region[0]+(x0+(a+z)/2)/sx,image_region[1]+(y0+(b+t)/2)/sy)
            if (graphic_hint and 3<=w<=35 and h<=2
                    and abs(center[0]-(cell_bbox[0]+cell_bbox[2])/2)<(cell_bbox[2]-cell_bbox[0])*.2):
                diagnostic_event('CELL_GRAPHIC_TEXT',cell_bbox=cell_bbox,reason='single_horizontal_scan_dash')
                return {'text':'—','basis':'single_horizontal_scan_dash'}
            if w>=3 and h>=2:
                parts=[[image_region[0]+(x0+a)/sx,image_region[1]+(y0+b)/sy,
                        image_region[0]+(x0+z)/sx,image_region[1]+(y0+t)/sy]]
    if not parts or (pure_math and not complete_unpositioned_ink and not connected
                     and np.count_nonzero(residual)<original_ink*.25):
        diagnostic_event('CELL_GRAPHIC_REJECT',cell_bbox=cell_bbox,reason='no_components' if not parts else 'pure_math_residual_too_small',
                         components=len(parts),original_ink=original_ink,residual_ink=int(np.count_nonzero(residual)),
                         pure_math=pure_math,graphic_hint=graphic_hint,full_ink=full_ink,glyphs=len(glyphs))
        return None
    ink_top,ink_bottom = min(p[1] for p in parts),max(p[3] for p in parts)
    pad = max(2,min(4,font*.35)) if connected else max(6,font*.7)
    box = [max(cell_bbox[0]+1,min(p[0] for p in parts)-pad),max(cell_bbox[1]+1,ink_top-pad),
           min(cell_bbox[2]-1,max(p[2] for p in parts)+pad),min(cell_bbox[3]-1,ink_bottom+pad)]
    # Stop padding at complete prose lines outside the actual illustration.
    # Mathematical/dimension labels remain part of the original graphic crop.
    # OCR boxes intersecting a source-proven connected drawing may be the
    # false transcription itself. They must not expand its crop to the grid.
    layout_glyphs = [c for c in glyphs if not any(
        min(c['x1'],b[2])>max(c['x0'],b[0]) and min(c['bottom'],b[3])>max(c['top'],b[1])
        for b in connected_boxes)]
    prose=[]
    for line in native_cell_lines(layout_glyphs):
        if not re.search(r'[A-Za-zА-Яа-яЁё]{4}',line['text']):
            continue
        prose.append(line)
        if line['y']<ink_top:
            box[1]=max(box[1],line['y']+1)
        if line['y']-line['size']>ink_bottom:
            box[3]=min(box[3],line['y']-line['size']-1)
    # A size label or short equation touching the padding belongs wholly to
    # the illustration. Never leave half a glyph in PNG and half in HTML.
    for _ in range(3):
        previous=tuple(box)
        for char in layout_glyphs:
            if any(abs(char['bottom']-line['y'])<line['size']*.5 for line in prose):
                continue
            if min(char['x1'],box[2])>max(char['x0'],box[0]) and min(char['bottom'],box[3])>max(char['top'],box[1]):
                box=[max(cell_bbox[0]+1,min(box[0],char['x0']-1)),max(cell_bbox[1]+1,min(box[1],char['top']-1)),
                     min(cell_bbox[2]-1,max(box[2],char['x1']+1)),min(cell_bbox[3]-1,max(box[3],char['bottom']+1))]
        if tuple(box)==previous:break
    # OCR may omit a dimension label completely. Complete any source-ink
    # component intersected by the crop edge even when it has no text glyph.
    _,_,edge_stats,_=cv2.connectedComponentsWithStats(original,8)
    for _ in range(2):
        previous=tuple(box)
        for x,y,w,h,area in edge_stats[1:]:
            if area/(sx*sy)<.2:continue
            ink=[image_region[0]+(x0+x)/sx,image_region[1]+(y0+y)/sy,
                 image_region[0]+(x0+x+w)/sx,image_region[1]+(y0+y+h)/sy]
            if min(ink[2],box[2])>max(ink[0],box[0]) and min(ink[3],box[3])>max(ink[1],box[1]):
                box=[max(cell_bbox[0]+1,min(box[0],ink[0]-.5)),max(cell_bbox[1]+1,min(box[1],ink[1]-.5)),
                     min(cell_bbox[2]-1,max(box[2],ink[2]+.5)),min(cell_bbox[3]-1,max(box[3],ink[3]+.5))]
        if tuple(box)==previous:break
    content_crop={}
    if source_image is not None:
        interior=cell_content_crop(source_image,image_region,cell_bbox,box)
        if interior is None:return None
        pixels,box,proof=interior
        content_crop=dict(source_pixel_bbox=list(pixels),content_crop=proof)
    else:
        # Even detection-only callers keep padding inside the same minimum
        # three rendered pixels; source readers also measure the actual rule.
        box=[max(box[0],image_region[0]+x0/sx),max(box[1],image_region[1]+y0/sy),
             min(box[2],image_region[0]+x1/sx),min(box[3],image_region[1]+y1/sy)]
    diagnostic_event('CELL_GRAPHIC_ACCEPT',cell_bbox=cell_bbox,bbox=box,components=len(parts),
                     original_ink=original_ink,pure_math=pure_math,graphic_hint=graphic_hint,full_ink=full_ink,glyphs=len(glyphs))
    return {'bbox':box,'components':len(parts),'basis':('connected_source_strokes_over_ocr_text' if connected
                                                     else 'scan_ink_outside_positioned_text'),
            'complete_unpositioned_ink':complete_unpositioned_ink,**content_crop}


def scan_math_header_html(native_html, reference_html):
    """Keep fresh MathML only at a header's matching literal Russian anchors."""
    from bs4 import BeautifulSoup
    native=BeautifulSoup(native_html,'html.parser')
    fresh=BeautifulSoup(reference_html,'html.parser')
    if native.find('img') or fresh.find('img') or not fresh.find('math'):
        return None
    expression_text=cell_text_key(fresh.get_text())
    for node in fresh.find_all('math'):node.decompose()
    words=re.findall(r'[А-Яа-яЁё]{2,}',fresh.get_text(' ',strip=True))
    source=cell_text_key(native.get_text(' ',strip=True))
    anchored_range=([word.lower() for word in words]==['от','до']
                    and re.findall(r'\d+',source)==re.findall(r'\d+',expression_text)
                    and len(re.findall(r'\d+',source))>=2)
    if len(words)<2 or (sum(map(len,words))<7 and not anchored_range) or any(word not in source for word in words):
        return None
    numbers=re.findall(r'\d+(?:[.,]\d+)?',fresh.get_text(' ',strip=True))
    if any(number not in source for number in numbers):return None
    return reference_html


def repair_scan_cell_spans(fragment, cells, rows, columns):
    """Reconcile OCR spans with scan rails after independent cell anchors.

    Require identical ordered cell counts in every physical row. One uniquely
    redundant empty placeholder may be removed; no visible contents are lost.
    At least four text/number anchors across rows and columns must agree.
    """
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(fragment,'html.parser');table=soup.find('table')
    source_rows=[r for r in table.find_all('tr') if r.find_parent('table') is table] if table else []
    if len(source_rows)!=rows:return fragment,[]
    plan=[];discard=[];anchors=[]
    for y,row in enumerate(source_rows):
        nodes=row.find_all(['td','th'],recursive=False)
        targets=sorted((c for c in cells if c['row']==y),key=lambda c:c['column'])
        if len(nodes)>len(targets):
            empty=[n for n in nodes if not n.get_text(strip=True) and not n.find(['math','img'])]
            if len(empty)!=len(nodes)-len(targets):return fragment,[]
            discard.extend(empty);nodes=[n for n in nodes if all(n is not e for e in empty)]
        if len(nodes)!=len(targets):return fragment,[]
        for node,target in zip(nodes,targets):
            value=cell_text_key(node.get_text(' ',strip=True))
            native=cell_text_key(BeautifulSoup(target['html'],'html.parser').get_text(' ',strip=True))
            if value==native and (re.search(r'\d',value) or re.search(r'[А-Яа-яЁёA-Za-z]{3}',value)):
                anchors.append((y,target['column']))
            plan.append((node,target))
    if len(anchors)<4 or len({y for y,x in anchors})<2 or len({x for y,x in anchors})<2:
        return fragment,[]
    repairs=[]
    for node,target in plan:
        for attr,key in (('rowspan','row_span'),('colspan','column_span')):
            value=target[key]
            if str(node.get(attr,1))!=str(value):
                repairs.append(dict(row=target['row'],column=target['column'],attribute=attr,value=value))
                if value==1:node.attrs.pop(attr,None)
                else:node[attr]=str(value)
    for node in discard:node.decompose();repairs.append(dict(operation='remove_source_covered_empty_placeholder'))
    result=str(table)
    checked=table_profile(result,columns)
    if checked['issues'] or checked['columns']!=columns or checked['rows']!=rows:return fragment,[]
    return result,repairs


def scan_numeric_text_proof(mask, region, bbox, glyphs, sx, sy, native_html, reference_html):
    """Two matching numeric reads + ink wholly inside a single text band.

    Old scan OCR can have tiny font boxes while preserving correct character
    positions. Use their advance as well as height; never hide extra ink from
    an illustration or dimension arrows merely because its label is numeric.
    """
    from bs4 import BeautifulSoup
    a,b=BeautifulSoup(native_html,'html.parser'),BeautifulSoup(reference_html,'html.parser')
    if a.find(['img','math']) or b.find(['img','math']):return False
    native=cell_text_key(a.get_text(' ',strip=True));fresh=cell_text_key(b.get_text(' ',strip=True))
    if native!=fresh or not re.fullmatch(r'[+−–-]?\d+(?:[.,]\d+)?',native):return False
    chars=sorted((g for g in glyphs if g['text'].strip()),key=lambda g:g['x0'])
    if len(chars)<2 or ''.join(g['text'] for g in chars)!=native:return False
    return scan_text_ink_band(mask,region,bbox,glyphs,sx,sy)


def scan_text_ink_band(mask, region, bbox, glyphs, sx, sy):
    """Verify that the whole source-cell ink is one glyph-aligned text strip."""
    import numpy as np
    from statistics import median
    chars=sorted((g for g in glyphs if g['text'].strip()),key=lambda g:g['x0'])
    if len(chars)<2:return False
    step=median(chars[i+1]['x0']-chars[i]['x0'] for i in range(len(chars)-1))
    glyph_height=median(g['bottom']-g['top'] for g in chars)
    if step>max(6,3*glyph_height):return False  # sparse dimension labels are not one text strip
    height=max(glyph_height,step*1.8)
    if step<=0 or max(g['bottom'] for g in chars)-min(g['bottom'] for g in chars)>height*.35:return False
    band=[chars[0]['x0']-height*.35,min(g['top'] for g in chars)-height*.65,
          chars[-1]['x1']+height*.35,max(g['bottom'] for g in chars)+height*.65]
    x0=max(0,math.ceil((bbox[0]-region[0]+1.5)*sx));y0=max(0,math.ceil((bbox[1]-region[1]+1.5)*sy))
    x1=min(mask.shape[1],math.floor((bbox[2]-region[0]-1.5)*sx));y1=min(mask.shape[0],math.floor((bbox[3]-region[1]-1.5)*sy))
    yy,xx=np.nonzero(mask[y0:y1,x0:x1])
    if len(xx)<3:return False
    left=region[0]+(xx.min()+x0)/sx;right=region[0]+(xx.max()+x0+1)/sx
    top=region[1]+(yy.min()+y0)/sy;bottom=region[1]+(yy.max()+y0+1)/sy
    return bool(band[0]<=left and right<=band[2] and band[1]<=top and bottom<=band[3])


def is_white_pdf_background(obj):
    """Unstroked white fill paints paper; it supplies neither ink nor a rule.

    Browser-printed PDFs often contain a content-sized white rectangle. Its
    synthetic pdfplumber edges must not become a table or an illustration.
    Outlined white shapes, colored fills and actual images remain untouched.
    """
    if obj.get('object_type') != 'rect' or not obj.get('fill') or obj.get('stroke'):
        return False
    color = obj.get('non_stroking_color')
    if isinstance(color, (int, float)):
        color = (color,)
    return color is not None and tuple(color) in {(1,), (1, 1, 1), (0, 0, 0, 0)}


def _native_geometry_glyph_aliases(geometry):
    """Expose compact glyph bounds to geometry consumers without changing text."""
    from copy import deepcopy
    import math
    from numbers import Real

    result = deepcopy(geometry)
    positions = ('x0', 'top', 'x1', 'bottom')
    for cell in result.get('cells', []):
        for glyph in cell.get('glyphs', []):
            if all(key in glyph for key in positions):
                continue  # Existing runtime coordinates remain authoritative.
            box = glyph.get('bbox')
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in box)
                    or box[0] > box[2] or box[1] > box[3]):
                raise ValueError('Native geometry glyph bbox must contain four finite ordered coordinates')
            if any(key in glyph and glyph[key] != value for key, value in zip(positions, box)):
                raise ValueError('Native geometry glyph has conflicting partial coordinate frames')
            for key, value in zip(positions, box):
                glyph.setdefault(key, value)
    return result


class NativeTableReader:
    """Recover ruled vector tables without OCR; hold only one parsed page."""

    def __init__(self, path):
        import pdfplumber
        self.path = Path(path)
        self.pdf = pdfplumber.open(str(path))
        self.page_no, self.page, self.tables = None, None, []
        self.region_tables = {}
        self.table_boundaries = {}
        self.pdfium = None
        self.chars = []
        self.raster_text_direction_conflicts = []
        self.formula_recognizer = None

    def close(self):
        self.pdf.close()
        if self.pdfium is not None:
            self.pdfium.close()

    def positioned_chars(self, page_no, page):
        """Repair demonstrably invalid font advances using a second PDF reader.

        Some embedded fonts have no usable widths for PDFMiner: every letter
        is assigned an entire em and successive text objects overlap. Sorting
        those boxes mixes letters and assigns word tails to neighboring cells.
        PDFium resolves the embedded font's actual metrics. This is extraction
        of source glyphs, not OCR or dictionary-based spacing inference.
        """
        from collections import Counter
        chars = page.chars
        letters = [c for c in chars if c['text'].isalpha() and c.get('upright', True)]
        em_widths = sum(abs(c['x1']-c['x0']-c['size']) < .01 for c in letters)
        if len(letters) < 20 or em_widths < .6*len(letters) or page.rotation:
            return chars
        try:
            import pypdfium2 as pdfium
            if self.pdfium is None:
                self.pdfium = pdfium.PdfDocument(str(self.path))
            p = self.pdfium[page_no-1]
            try:
                text = p.get_textpage()
                try:
                    recovered = []
                    for index in range(text.count_chars()):
                        code = pdfium.raw.FPDFText_GetUnicode(text, index)
                        value = '\u00ad' if pdfium.raw.FPDFText_IsHyphen(text, index) == 1 else chr(code)
                        if not code or any(c in value for c in '\r\n'):
                            continue
                        left, bottom, right, top = text.get_charbox(index, loose=True)
                        size = pdfium.raw.FPDFText_GetFontSize(text, index)
                        if size <= 0 or right <= left or top <= bottom:
                            continue
                        angle = pdfium.raw.FPDFText_GetCharAngle(text, index)
                        recovered.append(dict(text=value, x0=left, x1=right,
                                              top=page.height-top, bottom=page.height-bottom,
                                              size=size, fontname='PDFium embedded font metrics',
                                              upright=abs(angle) < .001))
                finally:
                    text.close()
            finally:
                p.close()
            def repertoire(items):
                # Require the same symbols as well as letters and numbers:
                # an alternate extractor must not silently change math signs.
                # PDFium marks line-end hyphens as SHY, including PDFs whose
                # first reader returns an ordinary hyphen for that same glyph.
                return Counter('-' if char == '\u00ad' else char
                               for c in items for char in c['text'] if not char.isspace())
            before, after = repertoire(chars), repertoire(recovered)
            agreement = sum((before & after).values()) / max(sum(before.values()), sum(after.values()), 1)
            proportional = sum(c['text'].isalpha() and c['x1']-c['x0'] < .85*c['size'] for c in recovered)
            if before != after or proportional < .5*len(letters):
                diagnostic_event('NATIVE_GLYPH_METRICS_KEEP', page=page_no,
                                 reason='alternate_reader_not_verified', repertoire_agreement=agreement)
                return chars
            diagnostic_event('NATIVE_GLYPH_METRICS_REPAIR', page=page_no,
                             reason='pdfminer_em_advances_disagree_with_embedded_font_metrics',
                             original_glyphs=len(chars), recovered_glyphs=len(recovered),
                             em_width_letters=em_widths, repertoire_agreement=agreement,
                             text_method='pdfium_source_glyphs')
            return recovered
        except Exception as error:
            diagnostic_event('NATIVE_GLYPH_METRICS_KEEP', page=page_no,
                             reason='alternate_reader_unavailable', error=str(error))
            return chars

    def load_page(self, page_no):
        if page_no != self.page_no:
            if self.page is not None:
                self.page.flush_cache()
                parent = getattr(self.page, 'parent_page', None)
                if parent is not None:
                    parent.flush_cache()
            self.page_no = page_no
            self.region_tables = {}
            self.table_boundaries = {}
            self.page = self.pdf.pages[page_no - 1]
            backgrounds = sum(is_white_pdf_background(obj) for obj in self.page.rects)
            if backgrounds:
                self.page = self.page.filter(lambda obj: not is_white_pdf_background(obj))
                diagnostic_event('NATIVE_BACKGROUND_FILTER', page=page_no, rectangles=backgrounds,
                                 reason='unstroked_white_fill_is_not_ink_or_table_border')
            page = self.page
            self.chars = self.positioned_chars(page_no, page)
            self.text_method = ('pdfminer_source_glyphs' if self.chars is page.chars else 'pdfium_source_glyphs')
            from docling_pdf_accents import decode_symbol_glyphs
            self.chars = decode_symbol_glyphs(self.chars)
            scan_region=[page.width*.04,page.height*.04,page.width*.96,page.height*.96]
            if any(self.is_page_scan(obj,scan_region) for obj in page.images):
                # A scan's OCR text-layer font sizes are recognition metadata,
                # not evidence of the printed document's sub/sup typography.
                self.chars=[dict(c,native_script_eligible=False) for c in self.chars]
            self.tables = page.find_tables() if len(page.edges) >= 4 else []
            diagnostic_event('NATIVE_PAGE',page=page_no,width=page.width,height=page.height,chars=len(page.chars),
                             images=len(page.images),curves=len(page.curves),lines=len(page.lines),
                             rectangles=len(page.rects),edges=len(page.edges),native_tables=len(self.tables),
                             positioned_chars=len(self.chars),text_method=self.text_method)
        return self.page

    def table_for_region(self, region):
        """Isolate a table and close open page edges using real rule endpoints."""
        from statistics import median

        key = tuple(region)
        if key in self.region_tables:
            return self.region_tables[key]

        def iou(table):
            box = table.bbox
            area = max(0, min(box[2], region[2]) - max(box[0], region[0])) * max(0, min(box[3], region[3]) - max(box[1], region[1]))
            union = (box[2] - box[0]) * (box[3] - box[1]) + (region[2] - region[0]) * (region[3] - region[1]) - area
            return area / union if union else 0

        table = max(self.tables, key=iou, default=None)
        boundary = {}
        if table is None or iou(table) < .75 or any(abs(a - b) > 3 for a, b in zip(table.bbox, region)):
            page = self.page
            bounds = (max(page.bbox[0], region[0] - 6), max(page.bbox[1], region[1] - 6),
                      min(page.bbox[2], region[2] + 6), min(page.bbox[3], region[3] + 6))
            horizontals = []
            for side, position in (("top", region[1]), ("bottom", region[3])):
                bars = [edge for edge in page.edges if edge["orientation"] == "v"
                        and region[0] - 3 <= edge["x0"] <= region[2] + 3
                        and abs(edge[side] - position) <= 6
                        and edge["bottom"] - edge["top"] >= 8]
                if (len(bars) >= 3 and max(e["x0"] for e in bars) - min(e["x0"] for e in bars)
                        >= .9 * (region[2] - region[0])):
                    position = median(edge[side] for edge in bars)
                    horizontals.append(position)
                    segments = sorted((max(e["x0"], region[0]), min(e["x1"], region[2]))
                                      for e in page.edges if e["orientation"] == "h"
                                      and abs(e["top"] - position) <= 2)
                    coverage, end = 0, region[0]
                    for start, stop in segments:
                        coverage += max(0, stop - max(start, end))
                        end = max(end, stop)
                    boundary[f"open_{side}"] = coverage < .9 * (region[2] - region[0])
            tables = page.crop(bounds).find_tables({"explicit_horizontal_lines": horizontals})
            table = max(tables, key=iou, default=None)
        if table is not None and iou(table) < .75:
            table = None
        if (table is not None and 2 < table.bbox[1] - region[1] <= 8
                and any(obj['top'] < table.bbox[1] - 2 < obj['bottom']
                        and region[0] <= (obj['x0'] + obj['x1']) / 2 <= region[2]
                        for obj in self.page.images)):
            bounds = (max(self.page.bbox[0], region[0]-6), max(self.page.bbox[1], region[1]-6),
                      min(self.page.bbox[2], region[2]+6), min(self.page.bbox[3], region[3]+6))
            candidates = self.page.crop(bounds).find_tables({'explicit_horizontal_lines': [region[1], region[3]]})
            expanded = max(candidates, key=iou, default=None)
            if expanded is not None and iou(expanded) >= .75:
                table = expanded
        if getattr(self, 'repair_boundaries', False):
            from docling_pdf_repairs import close_open_source_table
            table, closed = close_open_source_table(self, region, table, sys.modules[__name__])
            if closed:
                boundary['open_top'] = True
                boundary['open_grid_repair'] = closed
                diagnostic_event('NATIVE_OPEN_TABLE_CLOSED', page=self.page_no, region=region,
                                 basis='aligned_source_vertical_rule_endpoints')
        self.region_tables[key] = table
        self.table_boundaries[key] = boundary
        return table

    def graphics(self, page_no, region):
        """Source illustrations, excluding paths that draw the table grid."""
        from pdfplumber.table import TableSettings
        from docling_pdf_cell_rules import is_snapped_cell_border

        page = self.load_page(page_no)
        table = self.table_for_region(region)
        cells = table.cells if table is not None else [region]
        xs = {v for box in cells for v in (box[0], box[2])}
        ys = {v for box in cells for v in (box[1], box[3])}
        settings = TableSettings()
        if table is None:
            edge_grid = self.open_edge_grid(region)
            if edge_grid:
                xs.update(edge_grid['x_grid'])
                ys.update([edge_grid['bbox'][1], edge_grid['bbox'][3]])
                ys.update(e['top'] for e in page.edges if e['orientation']=='h'
                          and e['x1']-e['x0'] > .8*(region[2]-region[0])
                          and region[1]-4 <= e['top'] <= region[3]+4)

        def grid_path(obj):
            if obj["object_type"] == "image":
                return False
            path = obj.get("path") or []
            if any(part[0] not in {"m", "l", "h"} for part in path):
                return False
            if obj['object_type'] == 'rect':
                if table is not None and is_snapped_cell_border(obj, cells, x_tolerance=settings.snap_x_tolerance,
                                                               y_tolerance=settings.snap_y_tolerance):
                    return True
                # HTML table borders are often many short, filled strips, not
                # a stroked path. Their ends need not coincide with row/column
                # intersections. The fixed axis must match a real grid edge.
                points = obj.get('pts') or [(obj['x0'],obj['top']),(obj['x1'],obj['bottom'])]
                width = max(p[0] for p in points)-min(p[0] for p in points)
                height = max(p[1] for p in points)-min(p[1] for p in points)
                if (width <= 2
                        and any(abs((obj['x0'] + obj['x1']) / 2 - x) < 2 for x in xs)):
                    return True
                if (height <= 2
                        and any(abs(sum(p[1] for p in points)/len(points) - y) < 2 for y in ys)):
                    return True
            if obj["object_type"] == "rect":
                points = [(obj["x0"], obj["top"]), (obj["x1"], obj["top"]),
                          (obj["x1"], obj["bottom"]), (obj["x0"], obj["bottom"]), (obj["x0"], obj["top"])]
            else:
                points = obj.get("pts") or [(obj["x0"], obj["top"]), (obj["x1"], obj["bottom"])]
            if len(points) < 2:
                return False
            for a, b in zip(points, points[1:]):
                if abs(a[0] - b[0]) < 1 and any(abs(a[0] - x) < 2 for x in xs):
                    continue
                if abs(a[1] - b[1]) < 1 and any(abs(a[1] - y) < 2 for y in ys):
                    continue
                return False
            return True

        objects = []
        for obj in page.images + page.curves + page.lines + page.rects:
            if is_white_pdf_background(obj) or grid_path(obj):
                continue
            left, top = obj["x0"], obj["top"]
            right, bottom = obj["x1"], obj["bottom"]
            points = [point for operation in (obj.get("path") or []) for point in operation[1:]
                      if isinstance(point, (tuple, list)) and len(point) == 2
                      and all(isinstance(v, (int, float)) for v in point)]
            if points:
                left, right = min(left, *(p[0] for p in points)), max(right, *(p[0] for p in points))
                top, bottom = min(top, *(p[1] for p in points)), max(bottom, *(p[1] for p in points))
            if right - left < 1:
                left, right = left - .5, right + .5
            if bottom - top < 1:
                top, bottom = top - .5, bottom + .5
            if min(right, region[2]) <= max(left, region[0]) or min(bottom, region[3]) <= max(top, region[1]):
                continue
            objects.append(dict(obj, x0=left, x1=right, top=top, bottom=bottom,
                                width=right-left, height=bottom-top))
        # Connected vector strokes form one illustration; raster formulas stay
        # separate so text-anchor matching can still recover each formula.
        rasters = [obj for obj in objects if obj["object_type"] == "image"]
        groups = []
        for obj in (obj for obj in objects if obj["object_type"] != "image"):
            group = dict(obj)
            changed = True
            while changed:
                changed = False
                for other in list(groups):
                    if (min(group["x1"], other["x1"]) + 2 >= max(group["x0"], other["x0"])
                            and min(group["bottom"], other["bottom"]) + 2 >= max(group["top"], other["top"])):
                        group.update(x0=min(group["x0"], other["x0"]), x1=max(group["x1"], other["x1"]),
                                     top=min(group["top"], other["top"]), bottom=max(group["bottom"], other["bottom"]))
                        groups.remove(other);changed = True
            group.update(width=group["x1"]-group["x0"], height=group["bottom"]-group["top"])
            groups.append(group)
        return rasters + groups

    def is_page_scan(self, obj, region):
        """A page-sized raster is the OCR input, not a lost in-cell picture."""
        if obj.get("object_type") != "image":
            return False
        page = self.page
        box = [obj["x0"], obj["top"], obj["x1"], obj["bottom"]]
        return (box[0] <= page.width * .03 and box[1] <= page.height * .03
                and box[2] >= page.width * .97 and box[3] >= page.height * .97
                and box[0] <= region[0] and box[1] <= region[1]
                and box[2] >= region[2] and box[3] >= region[3])

    def ruled_cells(self, table):
        """Split a row above a full-width note at aligned vertical-rule ends.

        pdfplumber merges both into one spanning cell when no horizontal rule
        separates the row from its note. Require every interior rule and actual
        text below their common end; genuine merged cells remain untouched.
        """
        from statistics import median
        from docling_pdf_rule_chains import joined_vertical_rule_end

        cells, repairs = [], []
        xs = sorted({round(x, 2) for box in table.cells for x in (box[0], box[2])})
        # Collapse the two sides of a thick printed rule.
        axes = []
        for x in xs:
            if not axes or x - axes[-1] > 2:
                axes.append(x)
        for box in table.cells:
            interior = [x for x in axes if box[0] + 3 < x < box[2] - 3]
            ends = []
            for x in interior:
                end = joined_vertical_rule_end(self.page.edges, x, box)
                if end is None:
                    break
                ends.append(end)
            if not interior or len(ends) != len(interior) or max(ends) - min(ends) > 2:
                cells.append(box)
                continue
            end = median(ends)
            glyphs = [c for c in self.chars if c["text"]
                      and box[0] <= (c["x0"] + c["x1"]) / 2 < box[2]
                      and box[1] <= (c["top"] + c["bottom"]) / 2 < box[3]]
            # Reject a proposed cut through text; require both row and note.
            if (not any(c["bottom"] <= end + 1 for c in glyphs)
                    or not any(c["top"] >= end - 1 for c in glyphs)
                    or any(c["top"] + 1 < end < c["bottom"] - 1 for c in glyphs)):
                cells.append(box)
                continue
            edges = [box[0], *interior, box[2]]
            cells.extend((a, box[1], b, end) for a, b in zip(edges, edges[1:]))
            cells.append((box[0], end, box[2], box[3]))
            repairs.append({"bbox": list(box), "split_y": end,
                            "basis": "aligned_vertical_rule_ends_above_note"})
        # TableFinder can omit rectangles at open page edges or T junctions.
        # Complete only rectangular holes. Actual rules separate neighboring
        # holes; a missing rule joins them into a spanning cell.
        xs = sorted({x for box in cells for x in (box[0], box[2])})
        ys = sorted({y for box in cells for y in (box[1], box[3])})
        slots = {(y, x) for y in range(len(ys) - 1) for x in range(len(xs) - 1)}
        occupied = {(y, x) for box in cells for y in range(len(ys) - 1) for x in range(len(xs) - 1)
                    if box[0] - .01 <= xs[x] and xs[x + 1] <= box[2] + .01
                    and box[1] - .01 <= ys[y] and ys[y + 1] <= box[3] + .01}
        missing = slots - occupied
        def separated(a, b):
            y, x = a
            yy, xx = b
            vertical = x != xx
            position = xs[max(x, xx)] if vertical else ys[max(y, yy)]
            start, end = (ys[y], ys[y + 1]) if vertical else (xs[x], xs[x + 1])
            segments = sorted((max(start, e['top'] if vertical else e['x0']),
                               min(end, e['bottom'] if vertical else e['x1']))
                              for e in self.page.edges if e['orientation'] == ('v' if vertical else 'h')
                              and abs((e['x0'] if vertical else e['top']) - position) < 2)
            coverage, last = 0, start
            for left, right in segments:
                coverage += max(0, right - max(left, last))
                last = max(last, right)
            return coverage >= (end - start) * .6
        while missing:
            seed = min(missing)
            component, pending = {seed}, [seed]
            missing.remove(seed)
            while pending:
                y, x = pending.pop()
                for neighbor in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                    if neighbor in missing and not separated((y, x), neighbor):
                        missing.remove(neighbor)
                        component.add(neighbor)
                        pending.append(neighbor)
            top, bottom = min(y for y, _ in component), max(y for y, _ in component) + 1
            left, right = min(x for _, x in component), max(x for _, x in component) + 1
            if len(component) == (bottom-top) * (right-left):
                box = (xs[left], ys[top], xs[right], ys[bottom])
                cells.append(box)
                repairs.append({"bbox": list(box), "basis": "rectangular_gap_bounded_by_source_rules"})
        overlapping = any(min(a[2], b[2]) - max(a[0], b[0]) > .5
                          and min(a[3], b[3]) - max(a[1], b[1]) > .5
                          for i, a in enumerate(cells) for b in cells[i+1:])
        if overlapping:
            partition = self.partition_rule_grid(table)
            if partition:
                return partition, [{"basis": "source_rule_partition_replacing_overlapping_rectangles"}]
        return cells, repairs

    def partition_rule_grid(self, table):
        """Planar rule graph for nested/overlapping TableFinder rectangles."""
        from statistics import median
        def axis(values):
            groups = []
            for value in sorted(values):
                if groups and value - groups[-1][-1] <= 2:
                    groups[-1].append(value)
                else:
                    groups.append([value])
            return [median(g) for g in groups]
        box = table.bbox
        xs = axis([x for c in table.cells for x in (c[0], c[2])])
        vertical = [e for e in self.page.edges if e['orientation']=='v'
                    and any(abs(e['x0']-x)<2 for x in xs) and e['bottom']-e['top']>8]
        ys = axis([y for c in table.cells for y in (c[1], c[3])]
                  + [y for e in vertical for y in (e['top'], e['bottom']) if box[1]+2 < y < box[3]-2])
        note_boundaries = []
        for y in ys[1:-1]:
            ended = [x for x in xs[1:-1]
                     if any(abs(e['x0']-x)<2 and abs(e['bottom']-y)<2 for e in vertical)
                     and not any(abs(e['x0']-x)<2 and e['top']<=y+2 and e['bottom']>y+4 for e in vertical)]
            if len(ended)>=2:
                note_boundaries.append(y)
        def wall(vertical, position, start, end):
            if not vertical and any(abs(position-y)<2 for y in note_boundaries):
                return True
            parts = sorted((max(start,e['top'] if vertical else e['x0']),
                            min(end,e['bottom'] if vertical else e['x1']))
                           for e in self.page.edges if e['orientation']==('v' if vertical else 'h')
                           and abs((e['x0'] if vertical else e['top'])-position)<2)
            coverage, last = 0, start
            for a,b in parts:
                coverage += max(0,b-max(a,last)); last=max(last,b)
            return coverage >= .6*(end-start)
        remaining={(y,x) for y in range(len(ys)-1) for x in range(len(xs)-1)}
        result=[]
        while remaining:
            seed=min(remaining); group={seed}; pending=[seed]; remaining.remove(seed)
            while pending:
                y,x=pending.pop()
                for yy,xx in ((y-1,x),(y+1,x),(y,x-1),(y,x+1)):
                    if (yy,xx) not in remaining:
                        continue
                    blocked = (wall(True,xs[max(x,xx)],ys[y],ys[y+1]) if x!=xx
                               else wall(False,ys[max(y,yy)],xs[x],xs[x+1]))
                    if not blocked:
                        remaining.remove((yy,xx)); group.add((yy,xx)); pending.append((yy,xx))
            y0,y1=min(y for y,x in group),max(y for y,x in group)+1
            x0,x1=min(x for y,x in group),max(x for y,x in group)+1
            if len(group)!=(y1-y0)*(x1-x0):
                return None
            result.append((xs[x0],ys[y0],xs[x1],ys[y1]))
        return result

    def grid(self, page_no, region):
        """Read rule geometry even when cells contain rasterized mathematics."""
        from statistics import median

        self.load_page(page_no)
        def iou(box):
            area = max(0, min(box[2], region[2]) - max(box[0], region[0])) * max(0, min(box[3], region[3]) - max(box[1], region[1]))
            union = (box[2] - box[0]) * (box[3] - box[1]) + (region[2] - region[0]) * (region[3] - region[1]) - area
            return area / union if union else 0
        table = self.table_for_region(region)
        if table is None or iou(table.bbox) < .75:
            return self.open_edge_grid(region)
        def axis(values):
            groups = []
            for value in sorted(values):
                if groups and value - groups[-1][-1] <= 2:
                    groups[-1].append(value)
                else:
                    groups.append([value])
            return [median(group) for group in groups]
        source_cells, rule_repairs = self.ruled_cells(table)
        xs = axis([x for cell in source_cells for x in (cell[0], cell[2])])
        ys = axis([y for cell in source_cells for y in (cell[1], cell[3])])
        if len(xs) < 3 or len(ys) < 2:
            return None
        cells, occupied = [], set()
        for box in source_cells:
            x0, x1 = [min(range(len(xs)), key=lambda i: abs(xs[i] - x)) for x in (box[0], box[2])]
            y0, y1 = [min(range(len(ys)), key=lambda i: abs(ys[i] - y)) for y in (box[1], box[3])]
            points = {(y, x) for y in range(y0, y1) for x in range(x0, x1)}
            if not points or points & occupied:
                return None
            occupied.update(points)
            cells.append({"row": y0, "column": x0, "row_span": y1 - y0, "column_span": x1 - x0})
        # An open page boundary can leave a continued cell without a closing
        # rectangle. Complete only vertical strips at the top/bottom edge.
        for x in range(len(xs) - 1):
            y = 0
            while y < len(ys) - 1:
                if (y, x) in occupied:
                    y += 1
                    continue
                start = y
                while y < len(ys) - 1 and (y, x) not in occupied:
                    y += 1
                if start != 0 and y != len(ys) - 1:
                    return None
                cells.append({"row": start, "column": x, "row_span": y - start, "column_span": 1,
                              "open_page_edge": True})
        return {"basis": "pdf_rules", "bbox": list(table.bbox), "x_grid": xs, "y_grid": ys,
                "rule_repairs": rule_repairs,
                **self.table_boundaries.get(tuple(region), {}),
                "cells": sorted(cells, key=lambda c: (c["row"], c["column"]))}

    def open_edge_grid(self, region):
        """Use aligned vertical rules when a page break removed the top border."""
        from statistics import median

        page = self.page
        if region[1] > .15 * page.height:
            return None
        bars = [edge for edge in page.edges if edge["orientation"] == "v"
                and region[0] - 4 <= edge["x0"] <= region[2] + 4
                and region[1] - 4 <= edge["top"] <= region[1] + 10
                and edge["bottom"] - edge["top"] >= 10]
        groups = []
        for edge in sorted(bars, key=lambda edge: edge["x0"]):
            if groups and edge["x0"] - groups[-1][-1]["x0"] < 2:
                groups[-1].append(edge)
            else:
                groups.append([edge])
        if len(groups) < 3:
            return None
        xs = [median(e["x0"] for e in group) for group in groups]
        start = max(min(e["top"] for e in group) for group in groups)
        end = min(max(e["bottom"] for e in group) for group in groups)
        if end - start < 8 or xs[-1] - xs[0] < .9 * (region[2] - region[0]):
            return None
        return {"basis": "pdf_open_rules", "bbox": [xs[0], region[1], xs[-1], region[3]],
                "x_grid": xs, "open_top": True}

    def has_raster_text_direction_conflict(self, page_no, region):
        """Whether this source region has disproved the old OCR text direction.

        Keep evidence across page loads: scan discovery can inspect later pages
        before the exporter returns to the first table of the same document.
        """
        return any(entry['page']==page_no and overlap_ratio(entry['bbox'],region)>.6
                   for entry in getattr(self,'raster_text_direction_conflicts',()))

    def read_raster_table(self, page_no, region, source_image, reference_fragments=(), *, _header_rules=False, _short_rules=False):
        """Recover scan cells and illustrations without depending on headings."""
        import cv2
        import numpy as np
        from bs4 import BeautifulSoup
        from pdfplumber.table import merge_edges, edges_to_intersections, intersections_to_cells, cells_to_tables
        from statistics import median
        page = self.load_page(page_no)
        if not any(self.is_page_scan(obj, region) for obj in page.images):
            return None
        gray=np.asarray(source_image.convert('L'))
        mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
        height,width=gray.shape
        sx,sy=(region[2]-region[0])/width,(region[3]-region[1])/height
        edges=[]
        for vertical in (True,False):
            min_horizontal = 20 if _header_rules or _short_rules else max(30,int(width*.22))
            kernel=np.ones((18,1) if vertical else (1,min_horizontal),np.uint8)
            lines=cv2.morphologyEx(mask,cv2.MORPH_OPEN,kernel)
            _,_,stats,_=cv2.connectedComponentsWithStats(lines,8)
            for x,y,w,h,_ in stats[1:]:
                if (vertical and h<18) or (not vertical and w<min_horizontal):
                    continue
                left=region[0]+(x+w/2 if vertical else x)*sx
                top=region[1]+(y if vertical else y+h/2)*sy
                right=left if vertical else region[0]+(x+w)*sx
                bottom=region[1]+(y+h)*sy if vertical else top
                edges.append(dict(x0=left,x1=right,top=top,bottom=bottom,width=right-left,
                                  height=bottom-top,orientation='v' if vertical else 'h'))
        if len(edges)<6:
            return None
        vertical_edges=[e for e in edges if e['orientation']=='v']
        # Short header segments share their column position with long body
        # rails. Keep them; reject isolated diagram strokes elsewhere.
        persistent=[e for e in vertical_edges if sum(v['height'] for v in vertical_edges
                    if abs(v['x0']-e['x0'])<2) >= (region[3]-region[1])*.3]
        if _short_rules:
            horizontal_edges=[e for e in edges if e['orientation']=='h']
            junction_tolerance=max(.35,2.5*max(sx,sy))
            for edge in vertical_edges:
                # A short internal divider is real grid evidence when both
                # ends meet horizontal rails. Isolated diagram strokes do not
                # acquire cell ownership from their length alone.
                if all(any(abs(y-h['top'])<junction_tolerance
                           and h['x0']-junction_tolerance<=edge['x0']<=h['x1']+junction_tolerance
                           for h in horizontal_edges) for y in (edge['top'],edge['bottom'])):
                    if edge not in persistent:persistent.append(edge)
        edges=[e for e in edges if e['orientation']=='h']+persistent
        vertical_edges=persistent
        if vertical_edges:
            left,right=min(e['x0'] for e in vertical_edges),max(e['x0'] for e in vertical_edges)
            for position in (min(e['top'] for e in vertical_edges),max(e['bottom'] for e in vertical_edges)):
                if (min(abs(position-region[1]),abs(position-region[3]))<3
                        and not any(e['orientation']=='h' and abs(e['top']-position)<2
                                    and e['width'] >= .98*(right-left) for e in edges)):
                    edges.append(dict(x0=left,x1=right,top=position,bottom=position,
                                      width=right-left,height=0,orientation='h'))
        edges=merge_edges(edges,2,2,3,3)
        from docling_pdf_scan_recovery import join_broken_scan_rules
        edges, broken_rule_proofs = join_broken_scan_rules(edges)
        for proof in broken_rule_proofs:
            diagnostic_event('SCAN_BROKEN_RULE_JOINED', page_no=page_no, **proof)
        tables=cells_to_tables(intersections_to_cells(edges_to_intersections(edges,3,3)))
        boxes=max(tables,key=lambda t:sum((b[2]-b[0])*(b[3]-b[1]) for b in t),default=[])
        if not boxes:
            return None
        def axis(values):
            groups=[]
            for value in sorted(values):
                if groups and value-groups[-1][-1]<2:groups[-1].append(value)
                else:groups.append([value])
            return [median(group) for group in groups]
        xs=axis([x for b in boxes for x in (b[0],b[2])]);ys=axis([y for b in boxes for y in (b[1],b[3])])
        if len(xs)<3 or len(ys)<3:
            return None
        cells=[]
        for b in boxes:
            glyphs=[c for c in self.chars if c['text'] and b[0]<=(c['x0']+c['x1'])/2<b[2]
                    and b[1]<=(c['top']+c['bottom'])/2<b[3]]
            text='<br/>'.join(line['html'] for line in native_cell_lines(glyphs))
            x0,x1=[min(range(len(xs)),key=lambda i:abs(xs[i]-x)) for x in (b[0],b[2])]
            y0,y1=[min(range(len(ys)),key=lambda i:abs(ys[i]-y)) for y in (b[1],b[3])]
            cells.append(dict(row=y0,column=x0,row_span=y1-y0,column_span=x1-x0,bbox=list(b),html=text,glyphs=glyphs))
        source_slots={};source_overlap=False
        for cell in cells:
            for yy in range(cell['row'],cell['row']+cell['row_span']):
                for xx in range(cell['column'],cell['column']+cell['column_span']):
                    if (yy,xx) in source_slots:source_overlap=True
                    source_slots[yy,xx]=True
        complete_source_grid=(not source_overlap and len(source_slots)==(len(xs)-1)*(len(ys)-1))
        first=[c for c in cells if c['row']==0]
        header_rows=(max((c['row_span'] for c in first),default=1)
                     if sum(bool(re.search(r'[A-Za-zА-Яа-яЁё]{3}',BeautifulSoup(c['html'],'html.parser').get_text())) for c in first)>=2 else 0)
        # A long left header and an enclosing group above most leaf columns
        # establish a multi-level head even when a rotated hidden OCR layer
        # cannot supply readable heading words.
        if (any(c['column']==0 and c['row_span']>=3 for c in first)
                and any(c['column']>0 and c['column_span']>=max(2,(len(xs)-1)/2) for c in first)):
            header_rows=max(header_rows,max(c['row_span'] for c in first))
        def quarter_turned(glyph):
            matrix=glyph.get('matrix')
            if matrix is None or len(matrix)<4:return False
            a,b,c,d=matrix[:4]
            return abs(b)>max(abs(a)*3,1e-5) and abs(c)>max(abs(d)*3,1e-5)
        all_glyphs=[g for c in cells for g in c['glyphs'] if g['text'].strip()]
        rotated_cells=[c for c in cells if sum(quarter_turned(g) for g in c['glyphs'] if g['text'].strip())>=3]
        hidden_rotated_layer=(len(all_glyphs)>=40 and sum(map(quarter_turned,all_glyphs))>=.6*len(all_glyphs)
            and len(rotated_cells)>=4 and len({c['row'] for c in rotated_cells})>=2
            and len({c['column'] for c in rotated_cells})>=2)
        # A directionally invalid hidden layer must not nominate ink inside
        # a visibly blank double-rule band. Retain real source pixels as the
        # deciding evidence, while preserving glyph data for conflict checks.
        alignment_cells=([dict(c,glyphs=[]) for c in cells] if hidden_rotated_layer else cells)
        graphic_columns=set()
        for c in cells:
            if c['row']<header_rows and re.search(r'изображен|эскиз|схема|чертеж|drawing|sketch|diagram',BeautifulSoup(c['html'],'html.parser').get_text(),re.I):
                graphic_columns.update(range(c['column'],c['column']+c['column_span']))
        dimension_columns={c['column'] for c in cells if c['row']<header_rows and graphic_columns
                           and re.search(r'размер|dimension',BeautifulSoup(c['html'],'html.parser').get_text(),re.I)}
        reference_profiles=[]
        raw_reference_graphics=False
        for raw in reference_fragments:
            try:
                ref=clean_table_html(raw)
                parsed=BeautifulSoup(ref,'html.parser');ref_rows=parsed.table.find_all('tr')
                tails=ref_rows[len(ys)-1:]
                if tails and all(len(row.find_all(['td','th'],recursive=False))==1
                    and re.match(r'(?:Примечани|[1-9]\s+Знак)',row.get_text(' ',strip=True),re.I) for row in tails):
                    # Layout may place a table's footnote in a separate text
                    # item. Clip only the comparison reference to this source
                    # region; the original OCR/document note is not deleted.
                    for row in tails:row.decompose()
                    ref=str(parsed.table)
                from docling_pdf_structure import align_source_reference_grid
                ref, source_grid_alignment = align_source_reference_grid(ref, alignment_cells, source_image,
                    region, xs, ys, sys.modules[__name__])
                if source_grid_alignment:
                    header_rows = max(header_rows, source_grid_alignment.get('header_rows',0))
                ref,source_repairs=repair_scan_cell_spans(ref,cells,len(ys)-1,len(xs)-1)
                ref,_=repair_table_spans(ref)
                if source_repairs:
                    diagnostic_event('SCAN_OCR_SPANS_REPAIRED',page_no=page_no,rows=len(ys)-1,columns=len(xs)-1,
                                     repairs=source_repairs)
                profile=table_profile(ref)
                if not profile['issues'] and profile['rows']>=2 and profile['columns']>=2:
                    if source_grid_alignment:
                        profile['source_grid_alignment'] = source_grid_alignment
                    reference_profiles.append(profile)
                    raw_reference_graphics=raw_reference_graphics or bool(BeautifulSoup(raw,'html.parser').find('img'))
            except (ValueError,TypeError):
                pass
        # A page scan can carry a PREVIOUS OCR layer whose writing direction
        # disagrees with the now-upright raster. Its characters are real PDF
        # objects, but native_cell_lines cannot read quarter-turned glyphs as
        # horizontal prose. Do not promote that layer over a fresh OCR table.
        # This is geometric evidence, not a character/noise quality heuristic.
        ordinary=[c for c in cells if c['row']>=max(1,header_rows)
                  and c['column'] not in graphic_columns|dimension_columns]
        body_glyphs=[g for cell in ordinary for g in cell['glyphs'] if g['text'].strip()]
        turned_cells=[cell for cell in ordinary if sum(quarter_turned(g) for g in cell['glyphs']
                      if g['text'].strip())>=3]
        turned_count=sum(quarter_turned(g) for g in body_glyphs)
        native_key=cell_text_key(' '.join(BeautifulSoup(cell['html'],'html.parser').get_text(' ',strip=True)
                                         for cell in ordinary))
        fresh_text_cells=[cell for profile in reference_profiles for cell in profile['cells']
                          if cell['row']>=1 and not cell['header']
                          and re.search(r'[А-Яа-яЁёA-Za-z]{3}',cell['text']) and len(cell['text'].strip())>=8]
        different_fresh_cells=[cell for cell in fresh_text_cells
                               if cell_text_key(cell['text']) not in native_key]
        different_fresh_keys={cell_text_key(cell['text']) for cell in different_fresh_cells}
        # Declining this candidate must not discard a drawing that only the
        # scan-cell route can retain. A fresh mixed-content reference is not
        # proof that its source graphics have already been preserved elsewhere.
        mixed_reference_graphics=raw_reference_graphics or any('<img' in cell['html'].lower()
            or ((cell['header'] or cell['row']<=1) and re.search(
                r'изображен|эскиз|схема|чертеж|drawing|sketch|diagram',cell['text'],re.I))
            for profile in reference_profiles for cell in profile['cells'])
        if reference_profiles and not mixed_reference_graphics and not _header_rules and not _short_rules:
            detailed=self.read_raster_table(page_no,region,source_image,reference_fragments,_short_rules=True)
            if detailed is None and self.has_raster_text_direction_conflict(page_no,region):
                return None
            if detailed:
                candidate=table_profile(detailed[0])
                precise=detailed[1]
                same_outer=all(abs(a-b)<3 for a,b in zip(precise['bbox'],[xs[0],ys[0],xs[-1],ys[-1]]))
                refines_axes=(all(any(abs(x-a)<2 for a in precise['x_grid']) for x in xs)
                    and all(any(abs(y-a)<2 for a in precise['y_grid']) for y in ys))
                source_refinement=(precise.get('source_grid_verified') and same_outer and refines_axes
                    and len(precise['cells'])>=len(cells))
                if source_refinement or any(not p['issues'] and (p['rows'],p['columns'])==(candidate['rows'],candidate['columns'])
                                           for p in reference_profiles):
                    diagnostic_event('NATIVE_SCAN_SHORT_RULES',page_no=page_no,bbox=list(region),
                        rows=candidate['rows'],columns=candidate['columns'],
                        basis='complete_source_rail_refinement' if source_refinement else 'short_rails_confirmed_by_fresh_grid')
                    return detailed
        body_conflict=(len(body_glyphs)>=40 and turned_count>=.75*len(body_glyphs)
                  and len(turned_cells)>=4 and len({c['row'] for c in turned_cells})>=2
                  and len({c['column'] for c in turned_cells})>=2
                  and len(different_fresh_keys)>=2 and not mixed_reference_graphics)
        header_evidence=scan_header_direction_evidence(cells,reference_profiles)
        diagnostic_event('NATIVE_SCAN_HEADER_DIRECTION_CHECK',page_no=page_no,bbox=list(region),**header_evidence)
        conflict=body_conflict or header_evidence['conflict'] or _header_rules
        conflict_reason=('quarter_turned_hidden_ocr_disagrees_with_fresh_table' if body_conflict
                         else 'quarter_turned_hidden_ocr_on_numbered_blank_form')
        diagnostic_event('NATIVE_SCAN_TEXT_DIRECTION_CHECK',page_no=page_no,bbox=list(region),
                         body_glyphs=len(body_glyphs),quarter_turned_glyphs=turned_count,
                         quarter_turned_fraction=round(turned_count/max(1,len(body_glyphs)),4),
                         affected_cells=len(turned_cells),fresh_candidate_count=len(reference_profiles),
                         different_fresh_text_cells=len(different_fresh_keys),
                         mixed_reference_graphics=bool(mixed_reference_graphics),conflict=conflict)
        source_text_unreliable=False
        if body_conflict:
            # Reject the hidden OCR CONTENT, not the independently measured
            # source grid. A complete non-overlapping rectangle of physical
            # cells retains its exact merges; its text is read afresh later.
            if complete_source_grid and not header_evidence['conflict'] and not _header_rules:
                source_text_unreliable=True;conflict=False
                for c in cells:
                    c.update(glyphs=[],html='',source_text_unreliable=True)
                diagnostic_event('NATIVE_SCAN_GRID_RETAINED_TEXT_REJECTED',page_no=page_no,
                    bbox=list(region),rows=len(ys)-1,columns=len(xs)-1,cells=len(cells),
                    reason='complete_pixel_grid_independent_of_rotated_hidden_ocr',
                    fresh_grid_aligned=any(p.get('source_grid_alignment') for p in reference_profiles))
        if conflict:
            # Blank forms have short separators inside multi-level headers.
            # Once independent OCR/index evidence proves this exact case, keep
            # those rails too. The ordinary drawing-preserving route retains
            # its longer-line threshold.
            if header_evidence['conflict'] and not _header_rules:
                self.read_raster_table(page_no,region,source_image,reference_fragments,_header_rules=True)
                return None
            entry=dict(page=page_no,bbox=list(region),
                       reason=conflict_reason,
                       body_glyphs=len(body_glyphs),quarter_turned_glyphs=turned_count)
            if _header_rules and len(xs)-1 in header_evidence['numbered_columns']:
                entry['geometry']=dict(basis='raster_numbered_header_rules',bbox=[xs[0],ys[0],xs[-1],ys[-1]],
                    x_grid=xs,y_grid=ys,header_rows=header_rows,
                    cells=[{k:v for k,v in cell.items() if k!='glyphs'} for cell in cells])
            conflicts=getattr(self,'raster_text_direction_conflicts',None)
            if conflicts is None:
                self.raster_text_direction_conflicts=conflicts=[]
            if not any(old['page']==page_no and all(abs(a-b)<.1 for a,b in zip(old['bbox'],region))
                       for old in conflicts):
                conflicts.append(entry)
            diagnostic_event('NATIVE_SCAN_TEXT_DIRECTION_CONFLICT',page_no=page_no,bbox=list(region),
                             reason=conflict_reason,
                             body_glyphs=len(body_glyphs),quarter_turned_glyphs=turned_count,
                             affected_rows=len({c['row'] for c in turned_cells}),
                             affected_columns=len({c['column'] for c in turned_cells}),
                             native_grid=[len(ys)-1,len(xs)-1],
                             fresh_grids=[[p['rows'],p['columns']] for p in reference_profiles])
            return None
        references=[dict(cell,_reference=i,_source_grid_aligned=bool(profile.get('source_grid_alignment')))
                    for i,profile in enumerate(reference_profiles)
                    if profile['rows']==len(ys)-1 and profile['columns']==len(xs)-1
                    for cell in profile['cells']]
        tolerance_columns=set();class_columns=set()
        for cell in cells:
            if cell['row']<header_rows:
                label=BeautifulSoup(cell['html'],'html.parser').get_text(' ',strip=True)
                if re.search(r'отклонени|допуск',label,re.I):
                    tolerance_columns.update(range(cell['column'],cell['column']+cell['column_span']))
                if re.fullmatch(r'Класс\s+проката',label,re.I):
                    class_columns.add(cell['column'])
        reference_blank_columns = {}
        for reference_profile in reference_profiles:
            if reference_profile['columns'] != len(xs)-1:
                continue
            anchors = []
            for source_header in (h for h in cells if h['row'] < header_rows):
                key = cell_text_key(BeautifulSoup(source_header['html'],'html.parser').get_text(' ',strip=True))
                if len(key) < 3:
                    continue
                if any(ref.get('header') and ref['column'] == source_header['column']
                       and ref['column_span'] == source_header['column_span']
                       and cell_text_key(ref['text']) == key for ref in reference_profile['cells']):
                    anchors.append(dict(column=source_header['column'],text=key))
            if len(anchors) < 2:
                continue
            for column in range(len(xs)-1):
                body = [ref for ref in reference_profile['cells'] if not ref.get('header')
                        and ref['column'] <= column < ref['column']+ref['column_span']]
                if body and all(not BeautifulSoup(ref['html'],'html.parser').get_text(strip=True)
                                and not BeautifulSoup(ref['html'],'html.parser').find(['math','img']) for ref in body):
                    reference_blank_columns[column] = anchors
        soup=BeautifulSoup('<table><thead></thead><tbody></tbody></table>','html.parser');retained=[];pending=[]
        source_grid_ink=None;source_grid_ink_checked=False;empty_cell_rule_proofs=[]
        for y in range(len(ys)-1):
            row=soup.new_tag('tr');(soup.thead if y<header_rows else soup.tbody).append(row)
            for c in sorted((c for c in cells if c['row']==y),key=lambda c:c['column']):
                node=soup.new_tag('th' if y<header_rows else 'td')
                if c['row_span']>1:node['rowspan']=str(c['row_span'])
                if c['column_span']>1:node['colspan']=str(c['column_span'])
                content=c['html']
                matching=[ref for ref in references if all(ref[k]==c[k] for k in ('row','column','row_span','column_span'))]
                peers=[ref for ref in matching
                       if cell_text_key(ref['text'])==cell_text_key(BeautifulSoup(content,'html.parser').get_text(' ',strip=True))]
                override=None;override_reason=None
                no_graphic=not(set(range(c['column'],c['column']+c['column_span'])) & (graphic_columns|dimension_columns))
                if no_graphic and not c['glyphs']:
                    source_plain = {ref['html'] for ref in matching if ref.get('_source_grid_aligned')
                        and BeautifulSoup(ref['html'],'html.parser').get_text(strip=True)
                        and not BeautifulSoup(ref['html'],'html.parser').find(['math','img','table'])}
                    if len(source_plain) == 1:
                        override=next(iter(source_plain))
                        override_reason='source_aligned_primary_ocr_prose_without_equations'
                if no_graphic:
                    for ref in reversed(matching):
                        if override:
                            break
                        override=scan_math_header_html(content,ref['html'])
                        if override:
                            override_reason='matching_header_words_fresh_mathml';break
                        parsed=BeautifulSoup(ref['html'],'html.parser')
                        math_nodes=parsed.find_all('math')
                        for node_math in math_nodes:node_math.decompose()
                        if not math_nodes or parsed.get_text(strip=True):continue
                        if y>=header_rows:
                            old=cell_text_key(BeautifulSoup(content,'html.parser').get_text())
                            if (not re.fullmatch(r'[\dA-Za-zА-Яа-яЁё.,_+−-]+',old)
                                or not re.search(r'\d',old) or re.findall(r'\d+',old)!=re.findall(r'\d+',ref['text'])
                                or not scan_text_ink_band(mask,region,c['bbox'],c['glyphs'],1/sx,1/sy)):
                                continue
                        # A pure symbol under a merged, text-anchored header
                        # inherits that source cell position, not an alphabet.
                        parents=[p for p in cells if p['row']<y and
                                 min(p['column']+p['column_span'],c['column']+c['column_span'])>
                                 max(p['column'],c['column'])]
                        anchor_columns=set()
                        for parent in parents:
                            anchor=cell_text_key(BeautifulSoup(parent['html'],'html.parser').get_text())
                            parent_ref=next((p for p in references if p['_reference']==ref['_reference']
                                and all(p[k]==parent[k] for k in ('row','column','row_span','column_span'))),None)
                            words=re.findall(r'[А-Яа-яЁё]{3,}',parent_ref['text']) if parent_ref else []
                            if len(words)>=2 and sum(map(len,words))>=8 and all(w in anchor for w in words):
                                anchor_columns.add(parent['column'])
                                if (parent['column']<=c['column'] and
                                    c['column']+c['column_span']<=parent['column']+parent['column_span']) or len(anchor_columns)>=2:
                                    override=ref['html'];override_reason='source_parent_header_anchors_math_cell';break
                        if override:break
                if not override and no_graphic:
                    turned=[g for g in c['glyphs'] if g['text'].strip() and len(g.get('matrix',()))>=4
                            and abs(g['matrix'][1])>max(abs(g['matrix'][0])*3,1e-5)
                            and abs(g['matrix'][2])>max(abs(g['matrix'][3])*3,1e-5)]
                    for ref in reversed(matching):
                        if (len(turned)>=2 and len(turned)>=.5*sum(bool(g['text'].strip()) for g in c['glyphs'])
                                and re.fullmatch(r'[\d\s.,±+−–—/()%\-]+',ref['text'])
                                and not BeautifulSoup(ref['html'],'html.parser').find('img')):
                            neighbors=0
                            for other in cells:
                                if other['row']!=y or other is c:continue
                                value=cell_text_key(BeautifulSoup(other['html'],'html.parser').get_text())
                                if value and any(p['_reference']==ref['_reference'] and
                                    all(p[k]==other[k] for k in ('row','column','row_span','column_span')) and
                                    cell_text_key(p['text'])==value for p in references):neighbors+=1
                            if neighbors>=2:
                                override=ref['html'];override_reason='turned_numeric_cell_two_neighbor_anchors';break
                    if not override:
                        for peer in reversed(peers):
                            if scan_numeric_text_proof(mask,region,c['bbox'],c['glyphs'],1/sx,1/sy,content,peer['html']):
                                override=cell_text_key(BeautifulSoup(peer['html'],'html.parser').get_text())
                                override_reason='matching_numeric_reads_and_source_ink_band';break
                    if not override and c['column'] in class_columns and not re.search(r'\d',
                            BeautifulSoup(content,'html.parser').get_text()):
                        for ref in reversed(matching):
                            if not re.fullmatch(r'[АA][пn]?\d{3,4}',ref['text'].strip()):continue
                            # The old OCR lost ALL digits of a class code.
                            # Copy a fresh read only with two exact neighboring
                            # source values; never change an existing class number.
                            neighbors=sum(bool(value:=cell_text_key(BeautifulSoup(other['html'],'html.parser').get_text()))
                                and any(p['_reference']==ref['_reference'] and
                                    all(p[k]==other[k] for k in ('row','column','row_span','column_span'))
                                    and cell_text_key(p['text'])==value for p in references)
                                for other in cells if other['row']==y and other is not c)
                            if neighbors>=2:
                                override=ref['html'];override_reason='class_code_lost_digits_two_neighbor_anchors';break
                needs_numeric=(not override and no_graphic and c['column'] in tolerance_columns and y>=header_rows
                    and len(turned)>=2 and len(turned)>=.5*sum(bool(g['text'].strip()) for g in c['glyphs'])
                    and not re.fullmatch(r'[\d\s.,±+−–—/()%\-]+',BeautifulSoup(content,'html.parser').get_text(' ',strip=True)))
                if needs_numeric:pending.append(dict(row=y,column=c['column'],bbox=c['bbox'],kind='numeric'))
                if override:
                    diagnostic_event('SCAN_CELL_TEXT_RESTORED',page_no=page_no,row=y,column=c['column'],
                                     reason=override_reason,html=diagnostic_summary(override))
                pure_math=False
                if peers:
                    peer=BeautifulSoup(peers[-1]['html'],'html.parser')
                    has_math=bool(peer.find('math'))
                    for tag in peer.find_all('math'):tag.decompose()
                    pure_math=has_math and not peer.get_text(strip=True)
                graphic=(scan_cell_graphic(mask,region,c['bbox'],c['glyphs'],1/sx,1/sy,pure_math=pure_math,
                         graphic_hint=c['column'] in graphic_columns,full_ink=c['column'] in dimension_columns,
                         source_image=source_image)
                         if y>=header_rows and not needs_numeric else None)
                if override and graphic:
                    if graphic.get('basis') == 'connected_source_strokes_over_ocr_text':
                        override = None
                    else:
                        graphic = None
                if override:
                    content=override
                elif graphic and 'text' in graphic:
                    content=graphic['text']
                elif graphic:
                    b=graphic['bbox'];pixels=graphic.get('source_pixel_bbox') or (
                        max(0,math.floor((b[0]-region[0])/sx)),max(0,math.floor((b[1]-region[1])/sy)),
                        min(width,math.ceil((b[2]-region[0])/sx)),min(height,math.ceil((b[3]-region[1])/sy)))
                    img=BeautifulSoup(image_html(source_image.crop(pixels),b[2]-b[0],'cell'),'html.parser').img
                    img['data-source-cell']='true'
                    remaining=[glyph for glyph in c['glyphs'] if not (b[0]<=(glyph['x0']+glyph['x1'])/2<=b[2]
                               and b[1]<=(glyph['top']+glyph['bottom'])/2<=b[3])]
                    parts=[(line['y'],line['html']) for line in native_cell_lines(remaining)]
                    parts.append((b[1],str(img)))
                    content='<br/>'.join(part for position,part in sorted(parts,key=lambda p:p[0]))
                    retained.append(dict(row=y,column=c['column'],**graphic))
                elif peers:
                    content=peers[-1]['html']
                # A scan with no positioned glyphs must not turn a real data
                # cell into whitespace merely because graphic detection did
                # not classify its small numerals. Retain the ORIGINAL cell
                # pixels; the math stage still checks the grid before OCR.
                if not c['glyphs'] and not BeautifulSoup(content,'html.parser').get_text(strip=True) and not BeautifulSoup(content,'html.parser').find(['img','math']):
                    b=[c['bbox'][0]+1,c['bbox'][1]+1,c['bbox'][2]-1,c['bbox'][3]-1]
                    pixels=(max(0,math.floor((b[0]-region[0])/sx)),max(0,math.floor((b[1]-region[1])/sy)),
                            min(width,math.ceil((b[2]-region[0])/sx)),min(height,math.ceil((b[3]-region[1])/sy)))
                    ink=mask[pixels[1]:pixels[3],pixels[0]:pixels[2]]
                    non_grid=ink
                    if not source_grid_ink_checked:
                        from docling_pdf_table_layout import grid_ink
                        source_grid_ink=grid_ink(source_image,region,xs)
                        source_grid_ink_checked=True
                    if source_grid_ink is not None:
                        # Judge the ORIGINAL table rules before cutting out a
                        # cell: a short antialiased border remnant can survive
                        # a cell-local 70%-length morphological opening.
                        non_grid=source_grid_ink['nongrid'][pixels[1]:pixels[3],pixels[0]:pixels[2]]
                        if ink.size and cv2.countNonZero(ink) and not cv2.countNonZero(non_grid):
                            proof=dict(page=page_no,row=y,column=c['column'],bbox=list(c['bbox']),
                                source_ink_pixels=cv2.countNonZero(ink),nongrid_ink_pixels=0,
                                method='only_measured_source_grid_and_connected_rule_fringe')
                            empty_cell_rule_proofs.append(proof)
                            diagnostic_event('SCAN_EMPTY_CELL_RULE_PIXELS_IGNORED',**proof)
                    elif ink.size:
                        horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(8,round(ink.shape[1]*.7))),np.uint8))
                        vertical=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(8,round(ink.shape[0]*.7)),1),np.uint8))
                        non_grid=cv2.bitwise_and(ink,cv2.bitwise_not(cv2.bitwise_or(horizontal,vertical)))
                    if non_grid.size and cv2.countNonZero(non_grid)>=8:
                        retained_image=BeautifulSoup(image_html(source_image.crop(pixels),b[2]-b[0],'cell'),'html.parser').img
                        retained_image['data-source-cell']='true'
                        content=str(retained_image)
                        retained.append(dict(row=y,column=c['column'],bbox=b,basis='nonempty_scan_cell_without_positioned_text'))
                        diagnostic_event('SCAN_NONEMPTY_CELL_RASTER_RETAINED',page=page_no,row=y,column=c['column'],bbox=b)
                if retained and retained[-1].get('row') == y and retained[-1].get('column') == c['column']:
                    cell_columns = set(range(c['column'],c['column']+c['column_span']))
                    source_headers = [BeautifulSoup(h['html'],'html.parser').get_text(' ',strip=True)
                        for h in cells if h['row'] < header_rows and
                        cell_columns.intersection(range(h['column'],h['column']+h['column_span']))]
                    retained[-1]['source_column_context'] = dict(
                        header_texts=source_headers,
                        graphic_column=bool(cell_columns and cell_columns.issubset(graphic_columns)),
                        dimension_column=bool(cell_columns and cell_columns.issubset(dimension_columns)),
                        table_has_graphic_columns=bool(graphic_columns),
                        reference_column_body_empty=bool(cell_columns) and all(
                            col in reference_blank_columns for col in cell_columns),
                        reference_column_anchors=reference_blank_columns.get(c['column'], []),
                        reference_cell_empty=bool(matching) and all(
                            not BeautifulSoup(ref['html'],'html.parser').get_text(strip=True)
                            and not BeautifulSoup(ref['html'],'html.parser').find(['math','img'])
                            for ref in matching))
                if (not c['glyphs'] and retained and retained[-1].get('row') == y
                        and retained[-1].get('column') == c['column']
                        and retained[-1].get('basis') == 'nonempty_scan_cell_without_positioned_text'):
                    proposals = {}
                    for ref in matching:
                        proposed = BeautifulSoup(ref['html'], 'html.parser')
                        if not proposed.find('math') or proposed.find(['img','table']):
                            continue
                        for math_node in proposed.find_all('math'):
                            math_node.decompose()
                        if proposed.get_text(strip=True):
                            proposals[ref['html']] = ref['_reference']
                    if len(proposals) == 1:
                        mixed_html, reference = next(iter(proposals.items()))
                        retained[-1].update(mixed_source_html=mixed_html, mixed_source_reference=reference,
                            mixed_source_cell={k:c[k] for k in ('row','column','row_span','column_span','bbox')})
                        diagnostic_event('SCAN_MIXED_CELL_PROPOSAL',page=page_no,row=y,
                            column=c['column'],bbox=c['bbox'],reference=reference,
                            reason='matching_logical_cell_and_retained_ocr_prose_math')
                # A primary table block may contain only <img/>. Do not erase
                # reliable editable PDF text merely because that block has no
                # primary cell payload. Pixel support is required per cell;
                # drawing/dimension columns retain their original crops.
                if (not source_text_unreliable and not override and not graphic
                        and (y < header_rows or c['column'] not in graphic_columns | dimension_columns)):
                    from docling_pdf_text_cells import positioned_text_proof
                    if not source_grid_ink_checked:
                        from docling_pdf_table_layout import grid_ink
                        source_grid_ink=grid_ink(source_image,region,xs)
                        source_grid_ink_checked=True
                    # Measured table rules and their fringe are not text ink.
                    # Use the same original-table mask as empty-cell detection.
                    text_ink=source_grid_ink['nongrid'] if source_grid_ink is not None else mask
                    proof=positioned_text_proof(content,c['glyphs'],text_ink,region,c['bbox'],1/sx,1/sy)
                    if proof:c['positioned_source_text']=proof
                node.append(BeautifulSoup(content,'html.parser'));row.append(node)
        fragment=clean_table_html(str(soup.table));profile=table_profile(fragment,len(xs)-1)
        if profile['issues'] or profile['columns']!=len(xs)-1 or profile['rows']!=len(ys)-1:
            return None
        from docling_pdf_roman_labels import reference_roman_rows
        return fragment,dict(basis='raster_cell_rules',bbox=[xs[0],ys[0],xs[-1],ys[-1]],x_grid=xs,y_grid=ys,
                             source_grid_verified=bool(_short_rules and complete_source_grid),
                             source_roman_references=reference_roman_rows(reference_profiles,len(xs)-1),
                             header_rows=header_rows,retained_cell_images=retained,restored_cell_math=[],
                             empty_cell_rule_proofs=empty_cell_rule_proofs,
                             pending_cell_ocr=pending,
                             hidden_ocr_direction_conflict=source_text_unreliable,
                             glyph_coverage=1.0,text_method='positioned_pdf_ocr_layer',
                             cells=[{k:v for k,v in cell.items() if k!='glyphs'} for cell in cells])

    def read(self, page_no, region, reference_fragments=(), source_image=None):
        from bs4 import BeautifulSoup
        from statistics import median

        self.load_page(page_no)
        def iou(box):
            area = max(0, min(box[2], region[2]) - max(box[0], region[0])) * max(0, min(box[3], region[3]) - max(box[1], region[1]))
            union = (box[2] - box[0]) * (box[3] - box[1]) + (region[2] - region[0]) * (region[3] - region[1]) - area
            return area / union if union else 0
        table = self.table_for_region(region)
        if table is None or iou(table.bbox) < .75:
            return None
        box = table.bbox
        def inside(obj, bounds):
            x, y = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
            return bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]
        # Retain raster objects inside their actual cells. The table stays
        # structured even when individual formulas were printed as images.
        images = self.graphics(page_no, list(box))
        if any(self.is_page_scan(obj, region) for obj in images) or (images and source_image is None):
            return None
        chars = [char for char in self.chars if char["text"] and inside(char, box)]
        if len(chars) < 12 and not reference_fragments:
            return None
        def axis(values):
            groups = []
            for value in sorted(values):
                if groups and value - groups[-1][-1] <= 2:
                    groups[-1].append(value)
                else:
                    groups.append([value])
            return [median(group) for group in groups]
        source_cells, rule_repairs = self.ruled_cells(table)
        xs = axis([x for cell in source_cells for x in (cell[0], cell[2])])
        ys = axis([y for cell in source_cells for y in (cell[1], cell[3])])
        if len(xs) < 3 or len(ys) < 2:
            return None
        references = []
        for reference_index, raw in enumerate(reference_fragments):
            try:
                reference_profile = table_profile(clean_table_html(raw))
                references.extend(dict(c, _reference=reference_index, _columns=reference_profile['columns'])
                                  for c in reference_profile.get('cells', []))
            except ValueError:
                pass
        cells, owners, image_owners, retained_images, restored_math = [], [0] * len(chars), [0] * len(images), [], []
        context = {}
        for bounds in sorted(source_cells, key=lambda c: (c[1], c[0])):
            x0, x1 = [min(range(len(xs)), key=lambda i: abs(xs[i] - x)) for x in (bounds[0], bounds[2])]
            y0, y1 = [min(range(len(ys)), key=lambda i: abs(ys[i] - y)) for y in (bounds[1], bounds[3])]
            indices = [i for i, char in enumerate(chars) if inside(char, bounds)]
            for i in indices:
                owners[i] += 1
            glyphs = [chars[i] for i in indices]
            inline_objects = []
            for image_index, obj in enumerate(images):
                clipped = (max(obj["x0"], bounds[0]), max(obj["top"], bounds[1]),
                           min(obj["x1"], bounds[2]), min(obj["bottom"], bounds[3]))
                if clipped[2] - clipped[0] < .1 or clipped[3] - clipped[1] < .1:
                    continue
                from docling_pdf_cell_crop import cell_content_crop
                interior=cell_content_crop(source_image,region,bounds,clipped)
                if interior is None:
                    return None
                pixels,clipped,crop_proof=interior
                obj = dict(obj, x0=clipped[0], top=clipped[1], x1=clipped[2], bottom=clipped[3],
                           width=clipped[2]-clipped[0], height=clipped[3]-clipped[1])
                image_owners[image_index] += 1
                img = BeautifulSoup(image_html(source_image.crop(pixels), obj["width"], "cell"), "html.parser").img
                img["data-source-cell"] = "true"
                nearby = [c for c in glyphs if obj["top"] <= (c["top"] + c["bottom"]) / 2 <= obj["bottom"]]
                size = median(c["size"] for c in (nearby or glyphs)) if glyphs else obj["height"]
                baseline = median(c["bottom"] for c in nearby) if nearby else obj["bottom"]
                inline_objects.append({"text": "", "x0": obj["x0"], "x1": obj["x1"], "top": baseline - size,
                                       "bottom": baseline, "size": size, "height": obj["height"],
                                       "image_top": obj["top"], "image_bottom": obj["bottom"],
                                       "source_type": obj['object_type'], "source_html": str(img),
                                       "content_crop": crop_proof})
                if obj["object_type"] != "image":
                    glyphs = [c for c in glyphs if not inside(c, [obj["x0"], obj["top"], obj["x1"], obj["bottom"]])]
            from docling_pdf_accents import restore_raster_accents
            raster_accents = restore_raster_accents(self, page_no, glyphs, inline_objects, sys.modules[__name__])
            mapped_ocr = None
            if any(not c.get("upright", True) or re.search(r"\(cid:|[\ufffd\x00-\x08]", c["text"]) for c in glyphs):
                matches = [ref for ref in references if (ref["row"], ref["column"], ref["row_span"], ref["column_span"], ref["_columns"])
                           == (y0, x0, y1-y0, x1-x0, len(xs)-1) and ref["text"].strip()
                           and not re.search(r"\(cid:|[\ufffd\x00-\x08]", ref["text"])]
                if not matches:
                    return None
                mapped_ocr = matches[-1]["html"]
            restored = raster_accents or restore_inline_cell_math(glyphs, inline_objects, references)
            if not glyphs and not restored:
                restored = restore_pure_fraction_cell(inline_objects, references, y0, x0, y1-y0, x1-x0,
                                                      len(xs)-1, context)
            if not restored and inline_objects:
                anchored = source_anchored_cell_math(glyphs, inline_objects, references,
                    y0, x0, y1-y0, x1-x0, len(xs)-1, context)
                if anchored is not None:
                    mapped_ocr = anchored
                    restored = [{'method': 'page_ocr_math_with_matching_source_anchors_or_cas',
                                 'bbox': list(bounds)}]
                    diagnostic_event('NATIVE_CELL_MATH_RESTORE', page=page_no, row=y0, column=x0,
                                     image_count=len(inline_objects), method=restored[0]['method'])
            # A printed "Формула" heading is explicit source evidence, unlike
            # the mere presence or size of an image. Resolve only remaining
            # raster assets in that column; ordinary illustration columns do
            # not invoke formula OCR. Page OCR is always tried first above.
            recognizer = getattr(self, 'formula_recognizer', None)
            formula_column = any(c == x0 and r < y0 and re.fullmatch(r'формула', cell_text_key(value), re.I)
                                 for (r, c), value in context.items())
            if recognizer is not None and formula_column and not restored:
                for obj in inline_objects:
                    if obj.get('source_type') != 'image' or '<img' not in obj['source_html']:
                        continue
                    sx, sy = source_image.width/(region[2]-region[0]), source_image.height/(region[3]-region[1])
                    pixels = (max(0, math.floor((obj['x0']-region[0]-2)*sx)),
                              max(0, math.floor((obj['image_top']-region[1]-2)*sy)),
                              min(source_image.width, math.ceil((obj['x1']-region[0]+2)*sx)),
                              min(source_image.height, math.ceil((obj['image_bottom']-region[1]+2)*sy)))
                    asset = source_image.crop(pixels)
                    buffer = BytesIO()
                    try:
                        asset.save(buffer, 'PNG')
                        rendered = recognizer(buffer.getvalue(), dict(width=asset.width, height=asset.height,
                            formula_item=True, chemical=True, paragraph_text='', inline=True,
                            media_name=f'PDF page {page_no}, cell {y0}:{x0}, bbox {obj["x0"]},{obj["image_top"]}'))
                        if rendered:
                            obj['source_html'] = rendered
                            obj['text'] = BeautifulSoup(rendered, 'html.parser').get_text()
                            restored.append(dict(method='source_formula_column_raster_ocr',
                                bbox=[obj['x0'],obj['image_top'],obj['x1'],obj['image_bottom']]))
                            diagnostic_event('NATIVE_CELL_MATH_RETRY', page=page_no, row=y0, column=x0,
                                             accepted=True, method='source_formula_column_raster_ocr')
                    except Exception as error:
                        diagnostic_event('NATIVE_CELL_MATH_RETRY', page=page_no, row=y0, column=x0,
                                         accepted=False, error=str(error))
                    finally:
                        asset.close()
            restored_math.extend(dict(entry, row=y0, column=x0) for entry in restored)
            retained_images.extend({"row": y0, "column": x0,
                                    "bbox": [obj["x0"], obj["image_top"], obj["x1"], obj["image_bottom"]],
                                    "content_crop": obj.get('content_crop')}
                                   for obj in inline_objects if '<img' in obj.get('source_html', ''))
            lines = native_cell_lines(glyphs + inline_objects)
            context[y0,x0] = (BeautifulSoup(mapped_ocr, 'html.parser').get_text(' ', strip=True)
                              if mapped_ocr else ' '.join(line['text'] for line in lines))
            cells.append({"row": y0, "column": x0, "row_span": y1 - y0, "column_span": x1 - x0,
                          "bbox": list(bounds), "lines": lines,
                          "source_images": sum('<img' in obj.get('source_html', '') for obj in inline_objects),
                          "restored_math": len(restored),
                          "mapped_ocr_html": mapped_ocr,
                          "glyphs": [{"text": c["text"], "bbox": [c["x0"], c["top"], c["x1"], c["bottom"]],
                                      "font": c["fontname"], "size": c["size"]} for c in glyphs]})
        if any(owner != 1 for owner in owners) or any(owner < 1 for owner in image_owners):
            return None
        first = [cell for cell in cells if cell["row"] == 0]
        header = sum(bool(re.search(r"[А-Яа-яA-Za-z]{5}", " ".join(line["text"] for line in cell["lines"]))) for cell in first) >= 2
        if not header and len(first) == 2 and max(c["row_span"] for c in first) > 1:
            # A short stub heading (e.g. "Color") can span all header rows,
            # beside a long heading over the remaining numerical columns.
            header = all(re.search(r"[А-Яа-яA-Za-z]{3}", " ".join(line["text"] for line in cell["lines"])) for cell in first)
        if first and catalogue_row_key(" ".join(line["text"] for line in first[0]["lines"])):
            # A continued catalogue starts with data even if its descriptions
            # contain long words in several columns. Never repeat it as a head.
            header = False
        header_rows = max(cell["row_span"] for cell in first) if header else 0
        wrapped = []
        original_lines = {id(cell): cell["lines"] for cell in cells}
        for cell in cells:
            lines = cell["lines"]
            if cell["row"] < header_rows or len(lines) < 2 or not all(re.fullmatch(r"\d+", line["text"]) for line in lines):
                continue
            # Adjacent cells with the same vertical extent identify logical
            # data lines. Only concatenate digit wraps around those baselines;
            # never combine independent stacked values or superscripts.
            anchors = [original_lines[id(other)] for other in cells if other is not cell and original_lines[id(other)]
                       and abs(other["bbox"][1] - cell["bbox"][1]) < 2 and abs(other["bbox"][3] - cell["bbox"][3]) < 2
                       and all(re.fullmatch(r"\d+|[A-ZА-ЯЁ]\d(?:\s*,\s*[A-ZА-ЯЁ]\d)*", line["text"]) for line in original_lines[id(other)])]
            anchors = min(anchors, key=len, default=[])
            if not anchors or len(anchors) >= len(lines):
                continue
            # A real wrap resets the horizontal position: the first part runs
            # into the right edge and its trailing digit restarts farther left.
            # Baseline counts alone cannot distinguish "100 / 70 / 50" from
            # "10 / 0 / 7050" and must never decide this by themselves.
            pairs = [i for i in range(len(lines) - 1)
                     if lines[i]["x1"] - lines[i + 1]["x1"] >= lines[i]["size"] * .5
                     and lines[i + 1]["x0"] < lines[i]["x0"] - lines[i]["size"] * .2
                     and .6 * lines[i]["size"] <= lines[i + 1]["y"] - lines[i]["y"] <= 1.5 * lines[i]["size"]]
            if (len(pairs) != len(lines) - len(anchors) or any(i + 1 in pairs for i in pairs)
                    or max(line["size"] for line in lines) / min(line["size"] for line in lines) >= 1.1):
                continue
            joined_lines, index = [], 0
            while index < len(lines):
                if index in pairs:
                    a, b = lines[index:index + 2]
                    joined_lines.append({**a, "text": a["text"] + b["text"], "html": a["html"] + b["html"],
                                         "y": (a["y"] + b["y"]) / 2})
                    index += 2
                else:
                    joined_lines.append(lines[index])
                    index += 1
            cell["lines"] = joined_lines
            wrapped.append({"row": cell["row"], "column": cell["column"],
                            "source_lines": [line["text"] for line in lines],
                            "values": [line["text"] for line in joined_lines]})
        soup = BeautifulSoup("<table><thead></thead><tbody></tbody></table>", "html.parser")
        def same_glyph_text(a, b, header=False):
            def key(text):
                text = re.sub(r"\s+", "", text).translate(str.maketrans("АВЕКМНОРСТХаеорсху", "ABEKMHOPCTXaeopcxy")).casefold()
                return text.replace("-", "") if header else text
            return key(a) == key(b)
        physical_source_html = (clean_table_html(_source_cell_physical_html(cells, header_rows))
                                if getattr(self, 'source_cell_ownership', False) else None)
        harmonized = []
        display_cells = []
        display_header_rows = 0
        next_row = 0
        for source_row in range(len(ys) - 1):
            originals = [cell for cell in cells if cell['row'] == source_row]
            subrows = (native_subrows(originals) if source_row >= header_rows
                       and len(originals) == len(xs)-1 else None)
            for group_cells in (subrows or [originals]):
                display_cells.extend(dict(cell, row=next_row) for cell in group_cells)
                next_row += 1
                if source_row < header_rows: display_header_rows += 1
            if subrows:
                diagnostic_event('NATIVE_SUBROWS', page=page_no, source_row=source_row,
                                 records=len(subrows), basis='matching_numeric_column_baselines')
        logical_subrows = []
        if getattr(self, 'repair_subrows', False):
            from docling_pdf_repairs import split_source_subrows
            display_cells, display_header_rows, logical_subrows = split_source_subrows(
                cells, len(ys)-1, header_rows, sys.modules[__name__])
            next_row = max((c['row']+c['row_span'] for c in display_cells), default=0)
            for repair in logical_subrows:
                diagnostic_event('NATIVE_LOGICAL_SUBROWS', page=page_no, **repair)
        for y in range(next_row):
            row = soup.new_tag("tr")
            (soup.thead if y < display_header_rows else soup.tbody).append(row)
            for cell in [cell for cell in display_cells if cell["row"] == y]:
                node = soup.new_tag("th" if y < display_header_rows else "td")
                for attr, value in (("rowspan", cell["row_span"]), ("colspan", cell["column_span"])):
                    if value != 1:
                        node[attr] = str(value)
                lines = cell['lines']
                content = cell.get("mapped_ocr_html") or "<br/>".join(line["html"] for line in lines)
                if cell.get("mapped_ocr_html"):
                    content += ''.join(obj for line in lines for obj in re.findall(r'<img\b[^>]*>', line['html']))
                text = " ".join(line["text"] for line in lines)
                # Use smoother OCR wrapping only after its entire cell text
                # agrees with the actual PDF glyphs. Numeric wraps are governed
                # exclusively by glyph geometry above.
                stacked_values = (len(lines)>1 and (not re.search(r'[А-Яа-яЁё]{3}|[A-Za-z]{5}',text)
                                  or sum(bool(re.fullmatch(r'[\d\s.,+−–—\-/%]+',line['text'])) for line in lines)>1))
                if (not cell["source_images"] and not cell["restored_math"] and not stacked_values
                        and re.search(r"[A-Za-zА-Яа-я]", text)):
                    from docling_pdf_native_scripts import preserves_source_scripts
                    reference = next((ref for ref in references if ref["row"] == y
                                      and ref["column"] == cell["column"]
                                      and ref["row_span"] == cell["row_span"]
                                      and ref["column_span"] == cell["column_span"]
                                      and same_glyph_text(text, ref["text"], y < header_rows)
                                      and preserves_source_scripts(content, ref['html'])
                                      and ref["html"].count("<br") <= content.count("<br")), None)
                    if reference:
                        content = reference["html"]
                        harmonized.append({"row": y, "column": cell["column"], "basis": "matching_source_glyphs"})
                if y < header_rows:
                    content = re.sub(r"<br\s*/?>", " ", content)
                node.append(BeautifulSoup(content, "html.parser"))
                row.append(node)
        if not header_rows:
            soup.thead.decompose()
        fragment = clean_table_html(str(soup.table))
        profile = table_profile(fragment, len(xs) - 1)
        if profile["issues"]:
            return None
        script_recoveries=[dict(row=cell['row'],column=cell['column'],**proof)
            for cell in cells for line in cell['lines'] for proof in line.get('source_scripts',[])]
        for proof in script_recoveries:
            diagnostic_event('NATIVE_SOURCE_SCRIPT_RECOVERED',page=page_no,**proof)
        metadata = {"bbox": list(box), "x_grid": xs, "y_grid": ys, "header_rows": header_rows,
                          **self.table_boundaries.get(tuple(region), {}),
                          "glyph_count": len(chars), "glyph_coverage": 1.0, "text_method": self.text_method,
                          "digit_wraps": wrapped, "verified_ocr_wrapping": harmonized,
                          "source_script_recoveries":script_recoveries,
                          "logical_subrows": logical_subrows,
                          "source_physical_html": physical_source_html,
                          "rule_repairs": rule_repairs,
                          "retained_cell_images": retained_images, "restored_cell_math": restored_math, "cells": cells}
        try:
            metadata = _native_geometry_glyph_aliases(metadata)
        except ValueError as error:
            diagnostic_event('NATIVE_GLYPH_GEOMETRY_UNAVAILABLE', page=page_no,
                             severity='warning', reason=str(error))
            return None
        physical_profile = table_profile(physical_source_html) if physical_source_html else None
        metadata['positioned_source_grid_verified'] = bool(physical_profile and
            _source_cell_shape_matches(physical_profile, metadata))
        from docling_pdf_text_cells import annotate_native_text_cells
        proof_fragment = fragment if _source_cell_shape_matches(profile, metadata) else physical_source_html
        annotate_native_text_cells(metadata, source_image, region, proof_fragment)
        return fragment, metadata


def align_table_spans_to_rules(fragment, geometry):
    """Restore merges only where source rules enclose one existing value."""
    from bs4 import BeautifulSoup
    from copy import deepcopy

    if not geometry or not geometry.get("cells"):
        return fragment, []
    profile = table_profile(fragment)
    if (profile["issues"] or profile["rows"] != len(geometry["y_grid"]) - 1
            or profile["columns"] != len(geometry["x_grid"]) - 1):
        return fragment, []
    parsed = BeautifulSoup(fragment, "html.parser")
    original = parsed.table.find_all(["td", "th"])
    plan, used, repairs = [], set(), []
    for target in geometry["cells"]:
        y, x, rs, cs = (target[k] for k in ("row", "column", "row_span", "column_span"))
        indices = [i for i, cell in enumerate(profile["cells"]) if y <= cell["row"] < y + rs
                   and x <= cell["column"] < x + cs]
        if not indices or any(i in used for i in indices):
            return fragment, []
        if any(profile["cells"][i]["row"] + profile["cells"][i]["row_span"] > y + rs
               or profile["cells"][i]["column"] + profile["cells"][i]["column_span"] > x + cs for i in indices):
            return fragment, []
        values = [i for i in indices if profile["cells"][i]["text"]]
        if len(values) > 1:
            return fragment, []
        anchor = values[0] if values else indices[0]
        if profile["cells"][anchor]["row"] != y or profile["cells"][anchor]["column"] != x:
            return fragment, []
        used.update(indices)
        node = deepcopy(original[anchor])
        for attr, value in (("rowspan", rs), ("colspan", cs)):
            node.attrs.pop(attr, None)
            if value > 1:
                node[attr] = str(value)
        if len(indices) > 1 or (profile["cells"][anchor]["row_span"], profile["cells"][anchor]["column_span"]) != (rs, cs):
            repairs.append({"row": y, "column": x, "row_span": rs, "column_span": cs,
                            "removed_empty_cells": len(indices) - 1, "operation": "match_pdf_rules"})
        plan.append((y, node))
    if len(used) != len(original):
        return fragment, []
    header_rows = len(parsed.table.thead.find_all("tr")) if parsed.table.thead else 0
    for node in list(parsed.table.find_all(["thead", "tbody", "tfoot", "tr"], recursive=False)):
        node.decompose()
    head, body = parsed.new_tag("thead"), parsed.new_tag("tbody")
    if header_rows:
        parsed.table.append(head)
    parsed.table.append(body)
    for y in range(profile["rows"]):
        row = parsed.new_tag("tr")
        (head if y < header_rows else body).append(row)
        for number, node in plan:
            if number == y:
                row.append(node)
    result = str(parsed.table)
    checked = table_profile(result)
    # Neither numbers nor wording may be changed by a structural repair.
    before = [c["text"] for c in profile["cells"] if c["text"]]
    after = [c["text"] for c in checked.get("cells", []) if c["text"]]
    if checked["issues"] or before != after:
        return fragment, []
    return result, repairs


def normalize_joined_table_body(table):
    """With an explicit column header, continuation cells belong to the body."""
    if table.find("thead") is not None:
        for cell in table.find_all("th"):
            if cell.find_parent("thead") is None:
                cell.name = "td"


def suppress_table_picture_duplicates(doc, manifest, picture_routes, excluded):
    """Suppress whole-table layout crops and graphics actually retained in cells.

    Layout boxes include a few points of white padding around detected rules.
    A complete table must match all four edges and both areas; containment of
    a small, unretained drawing in a table does not establish duplication.
    """
    from docling_core.types.doc import ContentLayer, DocItemLabel

    table_items = {item.self_ref:item for item in doc.tables}
    table_entries = {entry['ref']: entry for entry in manifest['tables']}

    def caption_identity(text):
        caption=extract_table_caption(text)
        if not caption or furniture_key(text)!=furniture_key(caption):
            return None
        match=re.match(r'(?:(?:Продолжение|Окончание)\s+)?(?:т\s*а\s*б\s*л\s*и\s*ц\s*[аы]|табл\.?)\s*'
                       r'([А-ЯЁA-Z]?\s*\.?\s*\d+(?:\s*[.\-]\s*\d+)*)',caption,re.I)
        if not match:return None
        identity=furniture_key(match[1]).translate(str.maketrans('abcehkmoptxy','авсенкмортху'))
        return re.sub(r'^([^\W\d_])\.?(?=\d)',r'\1.',identity)

    def caption_compatible(captions,table_ref):
        target=table_items.get(table_ref)
        if target is None:return False
        identities={caption_identity(caption.text) for caption in captions}
        other={caption_identity(ref.resolve(doc).text) for ref in target.captions}
        return None not in identities and len(identities)==1 and not (other-identities)

    by_page = {}
    for entry in manifest["tables"]:
        geometry = entry.get("pdf_geometry") or {}
        objects = (geometry.get("retained_cell_images", []) + geometry.get("restored_cell_math", [])
                   + geometry.get("source_illustrations", [])
                   + entry.get("retained_ocr_cell_graphics", []))
        for obj in objects:
            by_page.setdefault(entry["page"], []).append((entry["ref"], obj["bbox"], "verified_cell_object"))
        if entry["method"] in {"pdf_geometry", "raster_geometry"} and geometry.get("glyph_coverage") == 1.0:
            by_page.setdefault(entry["page"], []).append((entry["ref"], geometry["bbox"], "verified_structured_table"))
        if entry["method"] == "source_image":
            by_page.setdefault(entry["page"], []).append((entry["ref"], entry["bbox"], "whole_table_crop"))
        diagnostic_event("PICTURE_DUPLICATE_TABLE", ref=entry["ref"], page=entry["page"],
                         method=entry["method"], bbox=entry.get("bbox"),
                         geometry_bbox=geometry.get("bbox"), glyph_coverage=geometry.get("glyph_coverage"),
                         retained_objects=len(objects),
                         candidate_bases=[basis for ref, _, basis in by_page.get(entry["page"], []) if ref == entry["ref"]][:16])
    diagnostic_event("PICTURE_DUPLICATE_SCAN", pictures=len(doc.pictures), tables=len(manifest["tables"]),
                     candidate_pages=sorted(by_page)[:32], candidate_page_count=len(by_page),
                     candidates=sum(len(values) for values in by_page.values()))
    duplicates = []
    for picture in doc.pictures:
        caption_refs={ref.cref for ref in picture.captions}
        captions=[ref.resolve(doc) for ref in picture.captions]
        independent=[ref.cref for ref in picture.children if ref.cref not in caption_refs and ref.cref not in excluded]
        if (picture.self_ref in picture_routes or not picture.prov or independent
                or any(not caption_identity(caption.text) or caption.children for caption in captions)):
            reason = ("already_routed" if picture.self_ref in picture_routes else "missing_provenance" if not picture.prov
                      else "independent_picture_children" if independent else "non_table_caption")
            diagnostic_event("PICTURE_DUPLICATE_SKIP", picture=picture.self_ref, reason=reason,
                             pages=[prov.page_no for prov in (picture.prov or [])][:16],
                             caption_refs=[str(getattr(caption, "cref", ""))[:160] for caption in (picture.captions or [])][:16],
                             caption_count=len(picture.captions or []),
                             routed=picture.self_ref in picture_routes, excluded=picture.self_ref in excluded,
                             independent_children=independent)
            continue
        matches = []
        keep_reason = "not_all_provenance_matched"
        for prov in picture.prov:
            box = prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
            box = [box.l, box.t, box.r, box.b]
            area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
            candidates = by_page.get(prov.page_no, [])
            diagnostic_event("PICTURE_DUPLICATE_REGION", picture=picture.self_ref, page=prov.page_no,
                             bbox=box, picture_area=area, candidate_count=len(candidates))
            if not area:
                keep_reason = "zero_picture_area"
                diagnostic_event("PICTURE_DUPLICATE_REGION_SKIP", picture=picture.self_ref, page=prov.page_no,
                                 bbox=box, reason=keep_reason)
                break
            for table_ref, source, basis in candidates:
                intersection = max(0, min(box[2], source[2]) - max(box[0], source[0])) * max(0, min(box[3], source[3]) - max(box[1], source[1]))
                source_area = max(0, source[2] - source[0]) * max(0, source[3] - source[1])
                whole = basis in {"whole_table_crop", "verified_structured_table"}
                coverage = intersection / (area if whole else max(area, source_area))
                source_coverage=intersection/source_area if source_area else 0
                margins=[float(source[0]-box[0]),float(source[1]-box[1]),float(box[2]-source[2]),float(box[3]-source[3])]
                coincident=whole and max(abs(value) for value in margins)<=6.0 and min(coverage,source_coverage)>=.97
                if basis=='verified_structured_table':
                    matched=coincident
                    policy='matching_whole_table_edges_and_areas'
                elif basis=='whole_table_crop':
                    matched=coverage>=.99 or coincident
                    policy='retained_whole_source_crop'
                else:
                    matched=coverage>=.85
                    policy='retained_cell_object'
                    if not matched and basis == 'verified_cell_object':
                        from docling_pdf_table_rasters import published_source_cell_contains
                        if published_source_cell_contains(table_entries[table_ref], source, box,
                                                          sys.modules[__name__]):
                            matched = True
                            policy = 'published_source_cell_contains_layout_crop'
                captions_ok=not captions or (coincident and caption_compatible(captions,table_ref))
                matched=matched and captions_ok
                diagnostic_event("PICTURE_DUPLICATE_PAIR", picture=picture.self_ref, table=table_ref,
                                 page=prov.page_no, bbox=box, source_bbox=source, basis=basis,
                                 picture_area=area, source_area=source_area, intersection=intersection,
                                 coverage=coverage,source_coverage=source_coverage,
                                 threshold=.97 if basis=='verified_structured_table' else .99 if whole else .85,
                                 picture_margins=margins,edge_tolerance=6.0 if whole else None,
                                 mutual_threshold=.97 if whole else None,caption_compatible=captions_ok,
                                 match_policy=policy,matched=matched)
                if matched:
                    matches.append({"picture": picture.self_ref, "table": table_ref, "page": prov.page_no,
                                    "bbox": box, "source_bbox": source, "basis": basis,
                                    "coverage": round(coverage, 4),"source_coverage":round(source_coverage,4),
                                    "match_policy":policy})
                    break
            else:
                keep_reason = "no_candidate_meets_threshold" if candidates else "no_candidates_on_page"
                diagnostic_event("PICTURE_DUPLICATE_REGION_KEEP", picture=picture.self_ref,
                                 page=prov.page_no, bbox=box, reason=keep_reason,
                                 candidate_count=len(candidates))
                break
        if len(matches) == len(picture.prov):
            if captions:
                targets={match['table'] for match in matches}
                if len(targets)!=1:
                    diagnostic_event('PICTURE_DUPLICATE_KEEP',picture=picture.self_ref,reason='ambiguous_caption_target')
                    continue
                target=table_items[next(iter(targets))]
                existing={furniture_key(ref.resolve(doc).text):ref for ref in target.captions}
                for caption in captions:
                    key=furniture_key(caption.text);canonical=existing.get(key)
                    if caption.parent:
                        parent=caption.parent.resolve(doc)
                        parent.children=[ref for ref in parent.children if ref.cref!=caption.self_ref]
                    caption.parent=target.get_ref();caption.label=DocItemLabel.CAPTION;caption.content_layer=ContentLayer.BODY
                    if not any(ref.cref==caption.self_ref for ref in target.children):target.children.append(caption.get_ref())
                    if canonical is not None and canonical.cref!=caption.self_ref:
                        excluded.add(caption.self_ref)
                        excluded.discard(canonical.cref)
                        action='equivalent_caption_already_retained'
                    else:
                        if not any(ref.cref==caption.self_ref for ref in target.captions):target.captions.append(caption.get_ref())
                        excluded.discard(caption.self_ref);existing[key]=caption.get_ref();action='caption_reparented'
                    diagnostic_event('TABLE_PICTURE_CAPTION',picture=picture.self_ref,table=target.self_ref,
                                     caption=caption.self_ref,action=action)
                picture.children=[ref for ref in picture.children if ref.cref not in caption_refs]
                picture.captions=[]
            picture_routes[picture.self_ref] = ""
            excluded.add(picture.self_ref)
            duplicates.extend(matches)
            diagnostic_event("PICTURE_DUPLICATE_SUPPRESS", picture=picture.self_ref,
                             matched_regions=len(matches), required_regions=len(picture.prov),
                             tables=[match["table"] for match in matches][:16],
                             pages=[match["page"] for match in matches][:16])
        else:
            diagnostic_event("PICTURE_DUPLICATE_KEEP", picture=picture.self_ref, reason=keep_reason,
                             matched_regions=len(matches), required_regions=len(picture.prov))
    manifest["table_picture_duplicates"] = duplicates
    diagnostic_event("PICTURE_DUPLICATE_DONE", suppressed_pictures=len({match["picture"] for match in duplicates}),
                     matched_regions=len(duplicates))


def restore_table_reading_order(doc, excluded, manifest):
    from docling_pdf_flow import restore_table_reading_order as restore_source_order
    restore_source_order(doc, excluded, manifest, sys.modules[__name__])


def prepare_pdf_inline_math(doc, reader, regions, picture_routes, recognizer):
    """Repair inline Flate/Predictor images and attach proven math slots locally."""
    from bs4 import BeautifulSoup
    targets, pages = {}, set()
    if reader is None or not getattr(reader, 'path', None):
        return targets, pages
    for picture in doc.pictures:
        if picture.self_ref in picture_routes:
            continue
        reg = item_region(picture, doc)
        if not reg:
            continue
        page_no, box = reg
        page = reader.load_page(page_no)
        matches = []
        for obj in page.images:
            attrs = obj['stream'].attrs
            parameters = attrs.get('DP') or attrs.get('DecodeParms') or {}
            predictor = parameters.get('Predictor') if isinstance(parameters, dict) else None
            bounds = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
            if predictor == 15 and obj['height'] <= 35 and obj['width'] <= 160 and overlap_ratio(bounds, box) > .65:
                matches.append((obj, bounds))
        if len(matches) != 1:
            continue
        obj, bounds = matches[0]
        nested_index = False
        try:
            from PIL import Image
            from docling_symbol_evidence import _components
            w,h = obj['srcsize']
            raw = obj['stream'].get_data()
            if obj.get('bits') == 8 and len(raw) == w*h*3:
                original = Image.frombytes('RGB',(w,h),raw)
                _, components = _components(original)
                if len(components) == 3:
                    a,b,c = components
                    nested_index = (a[3] >= 1.5*b[3] and a[1]+a[3] < b[1]+b[3]
                                    and b[1]+b[3]+1 < c[1]+c[3]
                                    and c[1] > b[1]+1 and b[1] > a[1]+a[3]*.5)
                original.close()
        except (ValueError, OSError):
            pass
        image = regions.crop(page_no, [bounds[0]-2,bounds[1]-2,bounds[2]+2,bounds[3]+2])
        # Re-rendering is safe even if no mathematical placement is proven:
        # never export the broken decoded image buffer again.
        clean = image_html(image, bounds[2]-bounds[0], 'picture')
        picture_routes[picture.self_ref] = clean
        right = [c for c in reader.chars if c['x0'] >= bounds[2]-.5
                 and bounds[1]-2 <= (c['top']+c['bottom'])/2 <= bounds[3]+2]
        source_right = ' '.join(l['text'] for l in native_cell_lines(right)).lstrip(' )-—–')
        key = cell_text_key(source_right)[:25]
        candidates = []
        if len(key) >= 10:
            for item in doc.texts:
                location = item_region(item, doc)
                if not location or location[0] != page_no or item.label.value == 'formula':
                    continue
                b = location[1]
                if (b[1] > bounds[3]+8 or b[3] < bounds[1]-4
                        or bounds[0] > b[2]+10 or bounds[2] < b[0]-30):
                    continue
                if key in cell_text_key(item.text):
                    slots = re.findall(r'\(\s*\)', item.text)
                    if len(slots) == 1:
                        kind = 'parentheses'
                    else:
                        prefix = re.sub(r'^\s*где\b\s*[-—–]?\s*', '', item.text)
                        if slots or not cell_text_key(prefix).lstrip('-—–').startswith(key):
                            continue
                        kind = 'definition'
                    candidates.append((item, kind))
        if len(candidates) == 1:
            item, kind = candidates[0]
            rendered = None
            try:
                buffer = BytesIO(); image.save(buffer,'PNG')
                rendered = recognizer(buffer.getvalue(),dict(formula_item=True,width=image.width,height=image.height,
                    paragraph_text=item.text,inline=True,media_name=picture.self_ref,
                    source_nested_index=nested_index))
            except Exception as error:
                diagnostic_event('PDF_INLINE_MATH_ERROR',page=page_no,ref=picture.self_ref,error=str(error))
            replacement = rendered or BeautifulSoup(clean,'html.parser').find('img').__str__()
            targets.setdefault(item.self_ref,[]).append((kind,replacement))
            picture_routes[picture.self_ref] = ''
            pages.add(page_no)
            diagnostic_event('PDF_INLINE_MATH',page=page_no,ref=picture.self_ref,target=item.self_ref,
                             method='mathml' if rendered else 'source_render',slot=kind)
        image.close()
    return targets, pages


def apply_inline_math(fragment, replacements):
    from bs4 import BeautifulSoup, NavigableString
    soup = BeautifulSoup(fragment,'html.parser')
    root = soup.find(['p','li'])
    if root is None:
        raise ValueError('Inline math target has no paragraph')
    for kind, raw in replacements:
        if kind == 'source_line':
            expected, rendered = raw
            text = root.find(string=lambda t: t and str(t).startswith(expected))
            if text is None: raise ValueError('Source definition line changed')
            replacement = list(BeautifulSoup(rendered, 'html.parser').contents)
            text.replace_with(*replacement, NavigableString(str(text)[len(expected):]))
            continue
        node = BeautifulSoup(raw,'html.parser').find(['math','img'])
        if kind == 'placeholder':
            text = root.find(string=lambda t: t and re.search(r'[█▓■]{2,}', t))
            if text is None: raise ValueError('Damaged formula slot changed')
            match = re.search(r'[█▓■]{2,}', str(text))
            text.replace_with(NavigableString(str(text)[:match.start()]), node,
                              NavigableString(str(text)[match.end():]))
        elif kind == 'parentheses':
            text = root.find(string=lambda t: t and re.search(r'\(\s*\)',t))
            if text is None: raise ValueError('Inline parentheses slot changed')
            match = re.search(r'\(\s*\)',str(text))
            text.replace_with(NavigableString(str(text)[:match.start()]+'('), node,
                              NavigableString(')'+str(text)[match.end():]))
        else:
            text = root.find(string=True)
            if text and re.match(r'^\s*где\b',str(text)):
                rest = re.sub(r'^\s*где\s*[-—–]?\s*','',str(text))
                text.replace_with(NavigableString('где '),node,NavigableString(' — '+rest))
            else:
                if root.name == 'li': root['style'] = 'list-style-type:none'
                root.insert(0,NavigableString(' — '));root.insert(0,node)
    return str(soup)


def source_parenthesis_continuations(doc, reader):
    """A non-bold first line can close a paragraph split by a page boundary."""
    from statistics import median
    if reader is None or not getattr(reader,'path',None): return []
    items = sorted([(item,item_region(item,doc)) for item in doc.texts if item_region(item,doc)],
                   key=lambda pair:(pair[1][0],pair[1][1][1]))
    pairs=[]
    for (left,a),(right,b) in zip(items,items[1:]):
        if (b[0] != a[0]+1 or a[1][3] < .85*doc.pages[a[0]].size.height
                or b[1][1] > .15*doc.pages[b[0]].size.height or abs(a[1][0]-b[1][0]) > 12
                or left.text.count('(')-left.text.count(')') != 1
                or right.text.count(')')-right.text.count('(') != 1):continue
        styles=[]
        for no,box in [a,b]:
            reader.load_page(no)
            chars=[c for c in reader.chars if c['text'].strip()
                   and box[0] <= (c['x0']+c['x1'])/2 <= box[2]
                   and box[1] <= (c['top']+c['bottom'])/2 <= box[3]]
            if not chars or any(re.search('bold|black',c['fontname'],re.I) for c in chars):break
            styles.append((median(c['size'] for c in chars),{c['fontname'] for c in chars}))
        if len(styles)==2 and abs(styles[0][0]-styles[1][0])<.2 and styles[0][1]&styles[1][1]:
            pairs.append((left.self_ref,right.self_ref))
    return pairs


def join_description_tail(left, right):
    """Join an edge description only when all continuation value cells are empty."""
    a = left.find_all('tr')[-1].find_all(['td','th'], recursive=False)
    first = right.find('tr')
    b = first.find_all(['td','th'], recursive=False) if first else []
    if (len(a) != len(b) or len(a) < 3
            or any(int(c.get('colspan',1)) != 1 or int(c.get('rowspan',1)) != 1 for c in a+b)
            or any(c.find(['img','math']) for c in a+b)
            or any(c.get_text(strip=True) for c in b[1:])
            or not all(re.fullmatch(r'[\d.,/\s]+', c.get_text(strip=True)) for c in a[1:])):
        return False
    tail = b[0].get_text(' ',strip=True)
    head = a[0].get_text(' ',strip=True)
    if (not re.match(r'^[а-яё]',tail) or re.match(r'^(?:примечани|таблиц|итого)',tail,re.I)
            or not head or re.search(r'[.;:!?]$',head)):
        return False
    a[0].append(' ')
    for child in list(b[0].contents): a[0].append(child.extract())
    first.decompose()
    return True


def join_page_tables(doc, routes, excluded, manifest, *, repair_boundaries=False, regions=None,
                     primary_row_boundaries=True):
    """Join neighboring HTML fragments by source grids, regardless of OCR route."""
    from bs4 import BeautifulSoup
    from docling_core.types.doc import ContentLayer, DocItemLabel, TableCell, TableData

    entries = {entry["ref"]: entry for entry in manifest["tables"]}
    def caption_text(text):
        # OCR sometimes prepends the adjacent number sign to a continuation
        # label. Recognise the label, without treating arbitrary numbered
        # prose between tables as disposable page furniture.
        return re.sub(r'^\s*(?:№\s*|N(?:o\.)?\s+)(?=(?:Продолжение|Окончание|Таблица|Таблицы|Табл\.))',
                      '',text,flags=re.I)
    def table_identity(text):
        text=caption_text(text)
        match = re.search(r"(?:т\s*а\s*б\s*л\s*и\s*ц[аы]|табл\.?)\s*(?:№\s*|N(?:o\.)?\s+)?([А-ЯЁA-Z]?\s*\.?\s*\d+(?:[.\-]\d+)*)", text, re.I)
        if not match:
            return None
        value=furniture_key(match[1]).translate(str.maketrans('abcehkmoptxy','авсенкмортху'))
        return re.sub(r'^([^\W\d_])\.?(?=\d)',r'\1.',value)

    identities, captions = {}, {}
    for text in doc.texts:
        parent = getattr(text.parent, "cref", None)
        if text.label == DocItemLabel.CAPTION and parent in entries:
            identities[parent] = table_identity(text.text)
            captions[parent] = caption_text(text.text)

    def header_signature(table, count):
        from docling_pdf_repairs import math_signature
        from docling_pdf_native_scripts import script_terms
        return [tuple((furniture_key(cell.get_text(" ", strip=True)), cell.get("rowspan", "1"), cell.get("colspan", "1"),
                       repr(sorted(script_terms(str(cell)).items())),
                       tuple(repr(math_signature(str(m))) for m in cell.find_all('math')),
                       tuple(hashlib.sha256(i.get('src','').encode()).hexdigest() for i in cell.find_all('img')))
                      for cell in row.find_all(["td", "th"], recursive=False)) for row in table.find_all("tr")[:count]]

    # Layout order can place next-section headings before the preceding table
    # fragment. Pair tables by physical source position; inspect the actual
    # source gap for prose/pictures instead of trusting that layout order.
    items_by_ref = {item.self_ref: item for item in doc.tables}
    ordered = sorted(((items_by_ref[ref], entry) for ref, entry in entries.items() if ref in items_by_ref and not entry.get('joined_into')),
                     key=lambda pair: (pair[1]["page"], pair[1]["bbox"][1], pair[1]["bbox"][0]))
    by_page = {}
    for item, entry in ordered:
        by_page.setdefault(entry["page"], []).append((item, entry))
    blocks = {}
    label_tables = {}
    for item, _ in doc.iterate_items():
        if (not hasattr(item, "prov") or not item.prov or item.self_ref in excluded
                or getattr(item, "content_layer", None) == ContentLayer.FURNITURE):
            continue
        if item.self_ref in entries:
            continue
        if item.label == DocItemLabel.CAPTION and getattr(item.parent, "cref", None) in entries:
            continue
        text = caption_text(getattr(item, "text", ""))
        for prov in item.prov:
            box = prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)
            bounds = [box.l, box.t, box.r, box.b]
            blocks.setdefault(prov.page_no, []).append((item, bounds))
            if re.match(r"^(?:(?:Продолжение|Окончание)\s+)?(?:таблица|таблицы|табл\.)(?:\s|$)", text, re.I):
                identity = table_identity(text)
                continuation_label=bool(re.match(r'^(?:Продолжение|Окончание)\s+',text,re.I))
                below = [(table, entry) for table, entry in by_page.get(prov.page_no, [])
                         if -3 <= entry["bbox"][1] - box.b <= 48
                         and min(box.r,entry['bbox'][2])-max(box.l,entry['bbox'][0])
                         >=.5*min(box.r-box.l,entry['bbox'][2]-entry['bbox'][0])]
                if (identity or continuation_label) and below:
                    table, _ = min(below, key=lambda pair: pair[1]["bbox"][1])
                    if identity:identities.setdefault(table.self_ref, identity)
                    captions.setdefault(table.self_ref, text)
                    label_tables[item.self_ref] = table.self_ref

    def row_keys(profile):
        return [key for cell in profile.get("cells", []) if cell["column"] == 0 and cell["column_span"] == 1
                and (key := catalogue_row_key(cell["text"])) is not None]

    def keys_continue(old, current):
        left, right = row_keys(old["selected"]), row_keys(current["selected"])
        if not left or not right or left[-1][0] != right[0][0]:
            return False
        a, b = left[-1][1], right[0][1]
        if not a < b:
            return False
        first_difference = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
        return first_difference is not None and b[first_difference] - a[first_difference] == 1

    def data_in_header(old, current):
        """Layout often labels the first data row on a new page as a header.

        Require values in at least two established numeric columns. A new
        heading/caption still blocks the join, as do different source grids.
        """
        numeric = r"[\d\s.,:+−–—\-/%<>≤≥=]+"
        columns = {}
        for cell in old["selected"].get("cells", []):
            if not cell["header"] and cell["column_span"] == 1 and cell["text"].strip():
                columns.setdefault(cell["column"], []).append(cell["text"].strip())
        established = {column for column, values in columns.items()
                       if len(values) >= 2 and sum(bool(re.fullmatch(numeric, v)) for v in values) >= .7 * len(values)}
        matches = {cell["column"] for cell in current["selected"].get("cells", [])
                   if cell["row"] == 0 and cell["column_span"] == 1
                   and cell["column"] in established and re.search(r"\d", cell["text"])}
        return len(matches) >= 2

    def merge_text_row_tail(left, right):
        """Join a text-only tail with blank row keys to its preceding row.

        Printed page borders can close an unfinished row. Require a complete
        one-to-one continued grid and no new numbers, images or mathematics.
        Active rowspans on the preceding page identify the actual target cells.
        """
        first = right.find("tr")
        nodes = first.find_all(["td", "th"], recursive=False) if first else []
        if (len(nodes) < 3
                or any(int(node.get(attr, 1)) != 1 for node in nodes for attr in ("colspan", "rowspan"))
                or any(node.get_text(strip=True) or node.find(["img", "math"]) for node in nodes[:2])
                or any(node.find(["img", "math"]) or re.search(r"\d", node.get_text()) for node in nodes)):
            return False
        populated = [i for i, node in enumerate(nodes) if node.get_text(strip=True)]
        if not populated:
            return False
        a = table_profile(str(left))
        if len(nodes) != a["columns"]:
            return False
        targets = {cell["column"]: node for cell, node in zip(a["cells"], left.find_all(["td", "th"]))
                   if cell["column_span"] == 1 and cell["row"] + cell["row_span"] == a["rows"]}
        if any(i not in targets or not targets[i].get_text(strip=True) or targets[i].find(["img", "math"])
               for i in populated):
            return False
        for i in populated:
            target = targets[i]
            target.append(BeautifulSoup("<br/>", "html.parser"))
            for child in list(nodes[i].contents):
                target.append(child.extract())
        first.decompose()
        return True

    def intervening_blocks(old, current, old_page, page_no, old_geo, geo, allow_page_notes=False):
        found = []
        for page in (old_page, page_no):
            for block, box in blocks.get(page, []):
                if block.self_ref in excluded:
                    continue
                height = max(1, box[3] - box[1])
                gap_height = (max(0, box[3] - max(box[1], old_geo["bbox"][3] + 2)) if page == old_page
                              else max(0, min(box[3], geo["bbox"][1] - 2) - box[1]))
                if gap_height < max(2, height * .5):
                    continue
                text = caption_text(getattr(block, "text", ""))
                if allow_page_notes and page==old_page and block.label==DocItemLabel.FOOTNOTE:
                    old.setdefault('preserved_interpage_footnotes',[]).append(block.self_ref)
                    continue
                identity = table_identity(text) if text else None
                continuation = bool(re.match(r"^(?:Продолжение|Окончание)\s+(?:таблиц[аы]|табл\.)", text, re.I))
                same_label = (label_tables.get(block.self_ref) == current["ref"] and identity
                              and identity == identities.get(old["ref"]))
                if same_label or (continuation and (not identity or not identities.get(old["ref"])
                                                    or identity == identities[old["ref"]])):
                    old.setdefault("continuation_labels", []).append(block.self_ref)
                    continue
                found.append(block.self_ref)
        return found

    joined, decisions, previous = list(manifest.get('joined_tables',[])), [], None
    for item, current in ordered:
        structured_image = (current["method"] == "source_image"
                            and "<table" in routes.get(item.self_ref, "")
                            and not table_profile(routes[item.self_ref])["issues"])
        if (current["method"] == "source_image" and not structured_image) or not current.get("join_geometry"):
            diagnostic_event('TABLE_JOIN_SKIP',ref=item.self_ref,page=current['page'],method=current['method'],
                             reason='unstructured_source_image' if current['method']=='source_image' and not structured_image else 'no_source_grid')
            previous = None
            continue
        if previous is not None:
            old_item, old = previous
            old_page = old.get("source_pages", [old["page"]])[-1]
            page_no = current["page"]
            old_size, size = doc.pages[old_page].size, doc.pages[page_no].size
            old_geo, geo = old.get("last_join_geometry", old["join_geometry"]), current["join_geometry"]
            old_soup = BeautifulSoup(routes[old_item.self_ref], "html.parser")
            soup = BeautifulSoup(routes[item.self_ref], "html.parser")
            # Source-gap checks below protect intervening prose and figures.
            head = old_soup.table.find("thead") if old_soup.table else None
            header_count = len(head.find_all("tr")) if head else 0
            new_head = soup.table.find("thead") if soup.table else None
            repeated = (header_count and soup.table is not None
                        and header_signature(old_soup.table, header_count) == header_signature(soup.table, header_count))
            numeric_head = bool(new_head and all(
                len(cells := row.find_all(["td", "th"], recursive=False)) >= 2
                and all(int(cell.get("colspan", 1)) == 1 and int(cell.get("rowspan", 1)) == 1
                        and re.fullmatch(r"[\d\s.,+−–—\-/%<>≤≥=]+", cell.get_text(" ", strip=True)) for cell in cells)
                and sum(bool(re.search(r"\d", cell.get_text())) for cell in cells) >= len(cells) / 2
                for row in new_head.find_all("tr")))
            old_identity, identity = identities.get(old_item.self_ref), identities.get(item.self_ref)
            caption = captions.get(item.self_ref, "")
            labelled_continuation = bool(re.match(r"^(?:Продолжение|Окончание)\b",caption,re.I))
            explicit_continuation = bool(labelled_continuation and
                (not identity or not old_identity or identity==old_identity))
            header_proof=None
            review_header=(explicit_continuation or (not caption and not identity) or
                           (identity and identity==old_identity))
            if repair_boundaries and review_header and not repeated and head and new_head:
                from docling_pdf_table_layout import repeated_source_header
                try:
                    replacement,header_proof=repeated_source_header(old,current,old_soup.table,soup.table,regions,sys.modules[__name__])
                except Exception as error:
                    replacement=None
                    issue=dict(left=old_item.self_ref,right=item.self_ref,page=page_no,
                        error_type=type(error).__name__,reason=str(error))
                    manifest.setdefault('table_header_review_failures',[]).append(issue)
                    diagnostic_event('TABLE_REPEATED_HEADER_REVIEW_FAILED',**issue)
                if replacement is not None:
                    head.replace_with(replacement);head=replacement
                    header_count=len(head.find_all('tr'));repeated=True
                    record=dict(left=old_item.self_ref,right=item.self_ref,**header_proof)
                    manifest.setdefault('verified_repeated_headers',[]).append(record)
                    diagnostic_event('TABLE_REPEATED_HEADER_SOURCE_VERIFIED',**record)
                diagnostic_event('TABLE_REPEATED_HEADER_SOURCE_COMPARE',left=old_item.self_ref,right=item.self_ref,
                    page=page_no,verified=bool(header_proof),reason='source_header_verified' if header_proof else 'source_pixels_or_literal_fields_not_verified')
            caption_ok = (not caption or (identity and identity == old_identity)
                          or furniture_key(caption) == furniture_key(captions.get(old_item.self_ref, ""))
                          or re.match(r"^(?:Продолжение|Окончание)\b", caption, re.I))
            key_continuity = keys_continue(old, current)
            first_head_cell = new_head.find(['td','th']) if new_head else None
            first_value = first_head_cell.get_text(' ', strip=True) if first_head_cell else ''
            header_terms = r'код|наименован|показател|значени|категор|класс|размер|степень|номер|объем|цвет|вид|тип|code|name|value|type'
            continued_data_head = bool(new_head and not repeated and not identity and not caption
                and current['method'] == 'pdf_geometry'
                and (geo.get('open_top') or not first_value or re.match(r'^\d+\s+\D', first_value)
                     or (re.fullmatch(r'[А-Яа-яЁёA-Za-z]+', first_value)
                         and not re.search(header_terms, first_value, re.I))
                     or (data_in_header(old, current) and not re.search(header_terms, first_value, re.I))))
            if (repair_boundaries and new_head and not repeated and not identity and not caption
                    and re.match(r'^[а-яё]', first_value)
                    and not re.search(header_terms, first_value, re.I)):
                continued_data_head = True
            # A header-only fragment can legitimately end above the usual
            # bottom-of-page threshold. Its whole grid must still agree, and
            # the physical gap must contain no independent content.
            header_only = bool(head and old_soup.table and not row_keys(old["selected"])
                               and all(row.find_parent("thead") is head
                                       for row in old_soup.table.find_all("tr"))
                               and old_geo["bbox"][1] > .5 * old_size.height
                               and old_geo["bbox"][3] > .65 * old_size.height)
            # A full-width source section title starts the body, rather than
            # replacing the column header stranded on the preceding page.
            first_band = [c for c in geo.get('cells', []) if c['row'] == 0]
            subsection_head = bool(header_only and new_head and len(new_head.find_all('tr')) == 1
                and len(new_head.find_all(['td', 'th'])) == 1 and len(first_band) == 1
                and first_band[0]['column'] == 0 and first_band[0]['row_span'] == 1
                and first_band[0]['column_span'] == current['selected']['columns']
                and int(new_head.find(['td', 'th']).get('colspan', 1)) == current['selected']['columns'])
            catalogue_head = bool(new_head and len(new_head.find_all("tr")) == 1 and key_continuity
                                  and catalogue_row_key(new_head.find(["td", "th"]).get_text()))
            blockers = (intervening_blocks(old, current, old_page, page_no, old_geo, geo,
                                          allow_page_notes=explicit_continuation and bool(repeated))
                        if page_no == old_page + 1 else [])
            same_grid = (len(old_geo['x_grid'])==len(geo['x_grid'])
                         and all(abs(a/old_size.width-b/size.width)<.008
                                 for a,b in zip(old_geo['x_grid'],geo['x_grid'])))
            # Alternating binding margins can translate the entire continued
            # table. Repeated headers/identity and relative column widths still
            # prove its grid; page-relative X positions need not be identical.
            a_grid,b_grid=old_geo['x_grid'],geo['x_grid']
            translated_grid = bool((repeated or (identity and identity==old_identity))
                and len(a_grid)==len(b_grid) and abs((a_grid[-1]-a_grid[0])/old_size.width
                -(b_grid[-1]-b_grid[0])/size.width)<.015
                and all(abs((a-a_grid[0])/(a_grid[-1]-a_grid[0])
                            -(b-b_grid[0])/(b_grid[-1]-b_grid[0]))<.015 for a,b in zip(a_grid,b_grid)))
            compatible = (page_no == old_page + 1
                          and (old_geo["bbox"][3] > .85 * old_size.height or key_continuity or header_only or explicit_continuation
                               or (repair_boundaries and old_geo.get('terminal_source_table')))
                          and (geo["bbox"][1] < .15 * size.height or (explicit_continuation and repeated))
                          and (new_head is None or repeated or numeric_head or catalogue_head or continued_data_head or subsection_head)
                          and (not identity or identity == old_identity or
                               (not old_identity and explicit_continuation and repeated))
                          and caption_ok
                          and (same_grid or translated_grid)
                          and old_soup.table is not None and soup.table is not None
                          and (old_soup.table.find("tfoot") is None or (explicit_continuation and repeated)) and not blockers)
            diagnostic_event('TABLE_JOIN_BOUNDARY',left=old_item.self_ref,right=item.self_ref,left_page=old_page,right_page=page_no,
                             compatible=bool(compatible),adjacent_pages=page_no==old_page+1,
                             old_bbox=old_geo['bbox'],new_bbox=geo['bbox'],same_grid=same_grid,translated_grid=translated_grid,
                             old_grid=a_grid,new_grid=b_grid,old_identity=old_identity,identity=identity,
                             caption_ok=bool(caption_ok),repeated_header=bool(repeated),numeric_head=numeric_head,
                             catalogue_head=catalogue_head,continued_data_head=continued_data_head,header_only=header_only,
                             source_fullwidth_subheading=subsection_head,
                             key_continuity=key_continuity,explicit_continuation=explicit_continuation,
                             left_has_table=old_soup.table is not None,right_has_table=soup.table is not None,
                             left_has_tfoot=bool(old_soup.table and old_soup.table.find('tfoot')),blockers=blockers)
            if page_no == old_page + 1:
                decisions.append({"left": old_item.self_ref, "right": item.self_ref,
                                  "compatible_boundary": bool(compatible), "basis": geo.get("basis", "pdf_rules"),
                                  "header_only_fragment": header_only,
                                  "source_fullwidth_subheading": subsection_head,
                                  "continued_data_head": continued_data_head,
                                  "translated_grid": translated_grid and not same_grid,
                                  "explicit_continuation": explicit_continuation,
                                  "row_key_continuity": key_continuity, "intervening_blocks": blockers})
            if compatible:
                if explicit_continuation and repeated and old_soup.table.find('tfoot'):
                    for footer in old_soup.table.find_all('tfoot'):
                        footer.name='tbody';footer['data-source-role']='continued-footer'
                if subsection_head:
                    for cell in new_head.find_all(['td', 'th']):
                        cell.name = 'td'
                if (numeric_head or catalogue_head or continued_data_head or subsection_head) and not repeated:
                    new_head.name = "tbody"
                    for cell in new_head.find_all("th"):
                        cell.name = "td"
                if repeated:
                    for row in list(soup.table.find_all("tr")[:header_count]):
                        row.decompose()
                    if new_head:
                        if new_head.find('tr'):
                            new_head.name='tbody'
                            for th in new_head.find_all('th'):th.name='td'
                        else:new_head.decompose()
                merged_tail = merge_text_row_tail(old_soup.table, soup.table) if continued_data_head else False
                if (not merged_tail and same_grid
                        and old_geo['bbox'][3] > old_size.height * .85
                        and geo['bbox'][1] < size.height * .15):
                    merged_tail = join_description_tail(old_soup.table, soup.table)
                if not merged_tail:
                    from docling_pdf_row_tails import merge_open_sparse_row_tail
                    sparse_tail = merge_open_sparse_row_tail(old_soup.table, soup.table, old_geo, geo,
                                                            same_grid=same_grid)
                    if sparse_tail:
                        merged_tail = True
                        proof = dict(left=old_item.self_ref, right=item.self_ref, page=page_no, **sparse_tail)
                        manifest.setdefault('open_source_row_tails_merged', []).append(proof)
                        diagnostic_event('TABLE_OPEN_SOURCE_ROW_TAIL_MERGED', **proof)
                if merged_tail and decisions:
                    decisions[-1]["merged_text_row_tail"] = True
                boundary_left=old_soup.table.find_all('tr')[-1].find_all(['td','th'],recursive=False)
                boundary_right=soup.table.find('tr').find_all(['td','th'],recursive=False) if soup.table.find('tr') else []
                blank_stub = (len(boundary_left)==len(boundary_right) and len(boundary_right)>=3
                    and all(not c.get_text(strip=True) and not c.find(['img','math']) for c in boundary_right[:2])
                    and all(c.get_text(strip=True) for c in boundary_left[:2])
                    and re.match(r'^[а-яё]',boundary_right[2].get_text(' ',strip=True)))
                if not merged_tail and ((old_geo.get("open_bottom") and geo.get("open_top")) or blank_stub):
                    last_row, first_row = old_soup.table.find_all("tr")[-1], soup.table.find("tr")
                    left_cells = last_row.find_all(["td", "th"], recursive=False)
                    right_cells = first_row.find_all(["td", "th"], recursive=False) if first_row else []
                    # An unfinished physical row continues across the page;
                    # join only one-to-one cells with no ambiguous merges.
                    if (len(left_cells) == len(right_cells) == len(geo["x_grid"]) - 1
                            and all(int(c.get("rowspan", 1)) == int(c.get("colspan", 1)) == 1
                                    for c in left_cells + right_cells)):
                        # Open physical rails prove a table continuation, not
                        # that independently read PRIMARY rows are one row.
                        # The positive text-tail helpers above remain active;
                        # image-only physical fragments keep their old merge.
                        protect_primary = (primary_row_boundaries and manifest.get('primary_ocr_only', False)
                            and all(any(c.get_text('', strip=True) or c.find('math') for c in cells)
                                    for cells in (left_cells, right_cells)))
                        if protect_primary:
                            proof = dict(left=old_item.self_ref, right=item.self_ref, page=page_no,
                                reason='no_positive_primary_row_continuation',
                                left_row_sha256=hashlib.sha256(str(last_row).encode()).hexdigest(),
                                right_row_sha256=hashlib.sha256(str(first_row).encode()).hexdigest())
                            manifest.setdefault('primary_row_boundaries_preserved', []).append(proof)
                            diagnostic_event('PRIMARY_ROW_BOUNDARY_PRESERVED', **proof)
                        else:
                            for left, right in zip(left_cells, right_cells):
                                if right.get_text(strip=True) or right.find(["img", "math"]):
                                    if left.contents:
                                        left.append(old_soup.new_tag("br"))
                                    for child in list(right.contents):
                                        left.append(child.extract())
                            first_row.decompose()
                if repair_boundaries and same_grid:
                    from docling_pdf_repairs import merge_source_word_tail
                    if merge_source_word_tail(old_soup.table, soup.table, sys.modules[__name__]):
                        diagnostic_event('TABLE_SOURCE_WORD_TAIL', left=old_item.self_ref, right=item.self_ref, page=page_no)
                old_profile, new_profile = table_profile(str(old_soup.table)), table_profile(str(soup.table))
                old_nodes = old_soup.table.find_all(["td", "th"])
                new_nodes = soup.table.find_all(["td", "th"])
                # The first empty cell of a continued group inherits the existing
                # vertical merge. A new nonempty cell is always kept unchanged.
                first = next((c for c in new_profile["cells"] if c["row"] == 0 and c["column"] == 0), None)
                last = next((c for c in old_profile["cells"] if c["column"] == 0
                             and c["row"] + c["row_span"] == old_profile["rows"]), None)
                if (first and last and not first["text"] and last["text"]
                        and first["column_span"] == last["column_span"] == 1
                        and not new_nodes[new_profile["cells"].index(first)].find(["img", "math"])):
                    prefix, empty_cells = 0, []
                    for cell in new_profile["cells"]:
                        if cell["column"] != 0:
                            continue
                        if cell["row"] != prefix or cell["text"] or cell["column_span"] != 1:
                            break
                        if new_nodes[new_profile["cells"].index(cell)].find(["img", "math"]):
                            break
                        prefix += cell["row_span"]
                        empty_cells.append(cell)
                    old_nodes[old_profile["cells"].index(last)]["rowspan"] = str(last["row_span"] + prefix)
                    for cell in empty_cells:
                        new_nodes[new_profile["cells"].index(cell)].decompose()
                for cell, node in zip(new_profile["cells"], new_nodes):
                    if node.parent is not None and cell["column"] > 0 and re.fullmatch(r"[\d\s.,+−–—\-/%]+", cell["text"]):
                        node.name = "td"
                bodies = old_soup.table.find_all("tbody", recursive=False)
                target = bodies[-1] if bodies else old_soup.table
                for row in list(soup.table.find_all("tr")):
                    if row.find_parent("thead") is None:
                        target.append(row.extract())
                normalize_joined_table_body(old_soup.table)
                combined = str(old_soup.table)
                profile = table_profile(combined, old_profile["columns"])
                diagnostic_event('TABLE_JOIN_RESULT',left=old_item.self_ref,right=item.self_ref,page=page_no,
                                 accepted=not profile['issues'],rows=profile['rows'],columns=profile['columns'],
                                 issues=profile['issues'],images=combined.count('<img'))
                if profile['issues'] and decisions:
                    decisions[-1]['combined_issues'] = profile['issues']
                if not profile["issues"]:
                    if old_soup.table.find("colgroup") is None:
                        xs = old_geo["x_grid"]
                        group = old_soup.new_tag("colgroup")
                        for a, b in zip(xs, xs[1:]):
                            col = old_soup.new_tag("col")
                            col["style"] = f"width:{100 * (b - a) / (xs[-1] - xs[0]):.3f}%"
                            group.append(col)
                        old_soup.table.insert(0, group)
                        old_soup.table["class"] = "pdf-native"
                        old_soup.table["style"] = f"min-width:{profile['columns'] * 6}em"
                        combined = str(old_soup.table)
                    routes[old_item.self_ref], routes[item.self_ref] = combined, ""
                    old_item.data = TableData(num_rows=profile["rows"], num_cols=profile["columns"], table_cells=[
                        TableCell(text=c["text"], row_span=c["row_span"], col_span=c["column_span"],
                                  start_row_offset_idx=c["row"], end_row_offset_idx=c["row"] + c["row_span"],
                                  start_col_offset_idx=c["column"], end_col_offset_idx=c["column"] + c["column_span"],
                                  column_header=c["header"]) for c in profile["cells"]])
                    item.data = TableData(num_rows=0, num_cols=0, table_cells=[])
                    old_item.prov.extend(prov.model_copy(deep=True) for prov in item.prov)
                    old["source_pages"] = old.get("source_pages", [old["page"]]) + [page_no]
                    if identity and not old_identity:identities[old_item.self_ref]=identity
                    old["last_join_geometry"] = geo
                    old["selected"], old["html"] = profile, combined
                    current["joined_into"] = old_item.self_ref
                    excluded.update(old.get("continuation_labels", []))
                    joined.append({"into": old_item.self_ref, "from": item.self_ref, "page": page_no,
                                   "basis": f"adjacent_source_pages_and_matching_{geo.get('basis', 'pdf_rules')}",
                                   "source_verified_repeated_header": bool(header_proof),
                                   "removed_repeated_header_rows": header_count if repeated else 0})
                    print(f"Joined table continuation: page {page_no}; columns {profile['columns']}; basis {geo.get('basis', 'pdf_rules')}", flush=True)
                    continue
        previous = (item, current)
    manifest["joined_tables"] = joined
    manifest["table_join_decisions"] = decisions


def insert_source_item(doc, page_no, bbox, kind):
    from docling_core.types.doc import BoundingBox, CoordOrigin, ProvenanceItem, TableData, DocItemLabel

    prov = ProvenanceItem(page_no=page_no,
                          bbox=BoundingBox(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3], origin=CoordOrigin.TOPLEFT),
                          charspan=(0, 0))
    neighbors = []
    for item, _ in doc.iterate_items():
        region = item_region(item, doc) if hasattr(item, "prov") else None
        if region and region[0] == page_no:
            neighbors.append((abs(region[1][1] - bbox[1]), region[1][1] <= bbox[1], item))
    kwargs = {"prov": prov}
    if kind == "table":
        kwargs["data"] = TableData(num_rows=0, num_cols=0, table_cells=[])
    elif kind == "formula":
        kwargs.update(text="", orig="")
    elif kind == "text":
        kwargs.update(label=DocItemLabel.TEXT, text="", orig="")
    if neighbors:
        _, after, sibling = min(neighbors, key=lambda entry: entry[0])
        method = {"table": doc.insert_table, "formula": doc.insert_formula, "picture": doc.insert_picture,
                  "text": doc.insert_text}[kind]
        return method(sibling=sibling, after=after, **kwargs)
    # A wholly omitted page still belongs between its neighboring source pages.
    all_neighbors = [(reg[0], item) for item, _ in doc.iterate_items() if hasattr(item, "prov")
                     and (reg := item_region(item, doc))]
    preceding = [item for number, item in all_neighbors if number < page_no]
    following = [item for number, item in all_neighbors if number > page_no]
    if preceding or following:
        after = bool(preceding)
        sibling = preceding[-1] if preceding else following[0]
        method = {"table": doc.insert_table, "formula": doc.insert_formula, "picture": doc.insert_picture,
                  "text": doc.insert_text}[kind]
        return method(sibling=sibling, after=after, **kwargs)
    method = {"table": doc.add_table, "formula": doc.add_formula, "picture": doc.add_picture,
              "text": doc.add_text}[kind]
    return method(**kwargs)


def recover_table_crop(args, image, seconds):
    """Reuse the running local server. One bounded request, without API retries."""
    if getattr(args, 'primary_ocr_only', False):
        raise RuntimeError('Auxiliary content OCR disabled: use the primary Surya response')
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
    from surya.inference.backends.openai_client import chat_completions_batch
    from surya.inference.parsers import clean_block_html
    from docling_backend_health import request_with_backend_recovery

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    key = hashlib.sha256(f'{TABLE_RECOVERY_SCHEMA}:'.encode()+buffer.getvalue()).hexdigest()
    cache = getattr(args, 'table_region_memory', None)
    if cache is None:
        args.table_region_memory = cache = {}
    if key in cache:
        return cache[key]
    backend = local_surya_backend(args.surya_ocr_model)
    if backend.handle is None:
        backend.start()
    outputs = request_with_backend_recovery(backend, lambda: chat_completions_batch(
        [BatchInputItem(image=image, prompt_type=PROMPT_TYPE_BLOCK, max_tokens=8192)],
        client=backend._client.with_options(max_retries=0, timeout=seconds),
        model_name=backend.handle.model_name, timeout=seconds, max_workers=1,
        max_retries=0, request_logprobs_default=False,
    ), diagnostic_event)
    if not outputs or outputs[0].error:
        raise RuntimeError("Regional table recognition failed")
    if outputs[0].token_count >= 8192:
        raise RuntimeError('Regional table reached model output capacity; partial table rejected')
    content = clean_table_html(clean_block_html(outputs[0].raw))
    cache[key] = content
    return content


def numeric_cells(profile):
    result = {}
    for cell in profile.get("cells", []):
        value = cell["text"].strip()
        if not cell["header"] and re.fullmatch(r"[\d\s.,+\-−–—±/%°×*^()[\]<>≤≥=:]+", value):
            value = re.sub(r"\s+", "", value).replace(".", ",").translate(str.maketrans("−–—", "---"))
            result[(cell["row"], cell["column"])] = value.replace("<=", "≤").replace(">=", "≥")
    return result


def missing_scan_header_cells(fragment, geometry, image, region):
    """Find omitted header text by ink in exact source cells, excluding rails."""
    import cv2
    import numpy as np
    if not geometry or geometry.get('basis') != 'raster_numbered_header_rules':
        return []
    profile = table_profile(fragment)
    if profile['issues'] or profile['columns'] != len(geometry['x_grid'])-1:
        return []
    index_rows = [y for y in range(profile['rows']) if [c['text'].strip() for c in profile['cells']
                  if c['row']==y] == [str(i) for i in range(1,profile['columns']+1)]]
    if not index_rows:
        return []
    sx,sy=image.width/(region[2]-region[0]),image.height/(region[3]-region[1])
    targets=[]
    for cell in profile['cells']:
        if cell['row']>=min(index_rows) or cell['text'].strip() or '<img' in cell['html'].lower():
            continue
        source=next((c for c in geometry['cells'] if all(c[k]==cell[k]
                    for k in ('row','column','row_span','column_span'))),None)
        if not source:
            continue
        a,b,d,e=source['bbox']
        bounds=(max(0,round((a-region[0]+2)*sx)),max(0,round((b-region[1]+2)*sy)),
                min(image.width,round((d-region[0]-2)*sx)),min(image.height,round((e-region[1]-2)*sy)))
        if bounds[2]-bounds[0]<8 or bounds[3]-bounds[1]<8:
            continue
        crop=image.crop(bounds)
        gray=np.asarray(crop.convert('L'))
        mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
        _,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
        letters=[(x,y,w,h,area) for x,y,w,h,area in stats[1:]
                 if h>=max(3,2*sy) and w>=2 and area>=5 and h<.8*crop.height and w<.8*crop.width]
        if len(letters)>=3:
            # A tall empty header cell can dwarf its one short word. Read
            # the text components with padding, not a mostly white rectangle.
            padding=max(4,math.ceil(2*max(sx,sy)))
            tight=(max(0,min(v[0] for v in letters)-padding),
                   max(0,min(v[1] for v in letters)-padding),
                   min(crop.width,max(v[0]+v[2] for v in letters)+padding),
                   min(crop.height,max(v[1]+v[3] for v in letters)+padding))
            crop=crop.crop(tight)
            if crop.height<40:
                crop=crop.resize((round(crop.width*40/crop.height),40))
            targets.append(dict(row=cell['row'],column=cell['column'],bbox=source['bbox'],image=crop))
    return targets


def scan_numeric_crop(image, region, target):
    """Tight, padded source ink for a damaged numeric tolerance cell."""
    import numpy as np
    sx,sy=image.width/(region[2]-region[0]),image.height/(region[3]-region[1])
    a,b,d,e=target['bbox']
    crop=image.crop((max(0,math.ceil((a-region[0]+2)*sx)),max(0,math.ceil((b-region[1]+2)*sy)),
                     min(image.width,math.floor((d-region[0]-2)*sx)),min(image.height,math.floor((e-region[1]-2)*sy))))
    gray=np.asarray(crop.convert('L'));yy,xx=np.nonzero(gray<180)
    if not len(xx):return None
    pad=max(4,math.ceil(2*max(sx,sy)))
    bounds=(max(0,int(xx.min())-pad),max(0,int(yy.min())-pad),
            min(crop.width,int(xx.max())+pad+1),min(crop.height,int(yy.max())+pad+1))
    rows=sorted(set(yy.tolist()));groups=[]
    for y in rows:
        if not groups or y-groups[-1][-1]>max(2,sy):groups.append([y])
        else:groups[-1].append(y)
    crop=crop.crop(bounds)
    if crop.height<48:crop=crop.resize((round(crop.width*48/crop.height),48))
    return dict(target,image=crop,source_lines=len(groups))


def recognize_scan_header_cells(args, targets, seconds):
    """Read only the missing labels on the already running OCR server."""
    if getattr(args, 'primary_ocr_only', False):
        diagnostic_event('TABLE_SECONDARY_CONTENT_OCR_DISABLED', stage='header_cells',
                         cells=len(targets), reason='primary_surya_content_only')
        return []
    from bs4 import BeautifulSoup
    from surya.inference.schema import BatchInputItem, PROMPT_TYPE_BLOCK
    from surya.inference.backends.openai_client import chat_completions_batch
    from surya.inference.parsers import clean_block_html
    from docling_backend_health import request_with_backend_recovery
    if not targets:
        return []
    # A later cell gets the same complete request allowance as the first one.
    # Document size and worker waves must not reduce its chance to be read.
    backend=local_surya_backend(args.surya_ocr_model)
    if backend.handle is None:
        backend.start()
    requests=[];prepared_images=[]
    try:
        for target in targets:
            image=target['image'];prompt=None
            if target.get('source_cell_interior'):
                from docling_layout import prepare_cell_text_image
                image, preparation = prepare_cell_text_image(image)
                prepared_images.append(image)
                prompt=('OCR this text block to HTML. This is one physical table header cell. '
                        'Transcribe every visible word, number and symbol in reading order as a paragraph. '
                        'Preserve indices and printed line breaks. Return the text in <p>...</p>. '
                        'Do not create a table or infer missing text.')
                diagnostic_event('TABLE_HEADER_SOURCE_INK_FIT',row=target['row'],
                    column=target['column'],bbox=target['bbox'],**preparation)
            requests.append(BatchInputItem(image=image,prompt_type=PROMPT_TYPE_BLOCK,
                                          prompt=prompt,max_tokens=2048))
        outputs=request_with_backend_recovery(backend, lambda: chat_completions_batch(requests,
            client=backend._client.with_options(max_retries=0,timeout=seconds),
            model_name=backend.handle.model_name,timeout=seconds,max_workers=min(2,len(targets)),
            max_retries=0,request_logprobs_default=False), diagnostic_event)
    finally:
        for image in prepared_images:image.close()
    restored=[]
    for target,output in zip(targets,outputs):
        if output.error:
            diagnostic_event('TABLE_HEADER_TEXT_UNREAD',row=target['row'],column=target['column'],
                             reason='ocr_request_error')
            continue
        if output.token_count >= 2048:
            diagnostic_event('TABLE_HEADER_TEXT_UNREAD',row=target['row'],column=target['column'],
                             reason='model_output_capacity_partial_cell_rejected')
            continue
        parsed=BeautifulSoup(render_surya_math(clean_block_html(output.raw)),'html.parser')
        undecoded_math=False
        for math_node in list(parsed.find_all('math')):
            if math_node.find(True):
                continue
            rendered=latex_math(math_node.get('data-latex') or math_node.get_text(),'inline')
            if rendered is None:
                undecoded_math=True
                break
            math_node.replace_with(BeautifulSoup(rendered,'html.parser').math)
        if undecoded_math:
            diagnostic_event('TABLE_HEADER_TEXT_UNREAD',row=target['row'],column=target['column'],
                reason='undecodable_cell_mathematics',response=diagnostic_summary(output.raw))
            continue
        if (target.get('source_cell_interior') and parsed.find('table')
                and not parsed.find(['img','script'])):
            # A crop proven to be one physical source cell has no inner grid.
            # OCR may nevertheless wrap its lines in a synthetic HTML table.
            # Remove that layout wrapper while preserving every text/math node.
            wrapped_cells = len(parsed.find_all(['td','th']))
            for cell in list(parsed.find_all(['td','th'])):
                cell.insert_after(' ')
                cell.unwrap()
            for row in list(parsed.find_all('tr')):
                row.insert_after(parsed.new_tag('br'))
                row.unwrap()
            for layout in list(parsed.find_all(['thead','tbody','tfoot','table'])):
                layout.unwrap()
            diagnostic_event('TABLE_HEADER_LAYOUT_WRAPPER_NORMALIZED',row=target['row'],
                column=target['column'],cells=wrapped_cells,
                reason='one_physical_source_cell_without_internal_grid')
        if parsed.find(['img','table','script']) or not parsed.get_text(strip=True):
            diagnostic_event('TABLE_HEADER_TEXT_UNREAD',row=target['row'],column=target['column'],
                             reason='empty_transcription' if not parsed.get_text(strip=True) else 'not_cell_text',
                             response=diagnostic_summary(output.raw))
            continue
        if target.get('kind')=='numeric':
            text=re.sub(r'\s+','',parsed.get_text())
            if not re.fullmatch(r'[\d.,±+−–—/()%\-]+',text):continue
            if parsed.find(['mfrac','msup','msub','msqrt','mroot']):continue
            values=re.findall(r'[±+−–—-]?\d+(?:[.,]\d+)?',text)
            if len(values)!=target.get('source_lines') or ''.join(values)!=text:continue
            restored.append(dict(row=target['row'],column=target['column'],html='<br/>'.join(values)))
            continue
        # Keep short labels inline; the original enclosing cell is unchanged.
        for tag in list(parsed.find_all(['p','div','h1','h2','h3'])):
            if tag.find_previous_sibling(['p','div','h1','h2','h3']):tag.insert_before(parsed.new_tag('br'))
            tag.unwrap()
        restored.append(dict(row=target['row'],column=target['column'],html=str(parsed).strip()))
    return restored


def retain_graphics_in_table(fragment, geometry, objects, source_image, region):
    """Project source graphics into real grid coordinates, never guessed rows."""
    from bs4 import BeautifulSoup

    profile = table_profile(fragment)
    xs, ys = (geometry or {}).get("x_grid", []), (geometry or {}).get("y_grid", [])
    if (profile["issues"] or len(xs) != profile["columns"] + 1 or len(ys) != profile["rows"] + 1):
        return fragment, [], list(objects)
    parsed = BeautifulSoup(fragment, "html.parser")
    nodes = parsed.table.find_all(["td", "th"])
    retained, unmapped = [], []
    sx, sy = source_image.width / (region[2]-region[0]), source_image.height / (region[3]-region[1])
    for obj in objects:
        mapped = False
        for cell, node in zip(profile["cells"], nodes):
            x, y, cs, rs = (cell[k] for k in ("column", "row", "column_span", "row_span"))
            box = [max(obj["x0"], xs[x]), max(obj["top"], ys[y]),
                   min(obj["x1"], xs[x+cs]), min(obj["bottom"], ys[y+rs])]
            if box[2]-box[0] < .1 or box[3]-box[1] < .1:
                continue
            pixels = (max(0, math.floor((box[0]-region[0])*sx)), max(0, math.floor((box[1]-region[1])*sy)),
                      min(source_image.width, math.ceil((box[2]-region[0])*sx)),
                      min(source_image.height, math.ceil((box[3]-region[1])*sy)))
            if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
                continue
            img = BeautifulSoup(image_html(source_image.crop(pixels), box[2]-box[0], "cell"), "html.parser").img
            img["data-source-cell"] = "true"
            node.append(img)
            retained.append({"row": y, "column": x, "bbox": box})
            mapped = True
        if not mapped:
            unmapped.append(obj)
    return str(parsed.table), retained, unmapped


def table_request_limit(table_count):
    """All source regions are eligible; retries are local to each region."""
    return None


def _captioned_figure_title(text):
    """Recognize a complete formal figure caption, never an in-prose citation."""
    return re.fullmatch(r'\s*Рисунок\s+(?P<number>(?:[A-ZА-ЯЁ]\s*\.\s*)?\d+'
                        r'(?:\s*[.\-]\s*\d+)*)\s*[—–−-]\s*(?P<title>\S[\s\S]*)', text, re.I)


def _captioned_figure_contains(outer, inner):
    return bool(outer and inner and outer[0] <= inner[0] <= inner[2] <= outer[2]
                and outer[1] <= inner[1] <= inner[3] <= outer[3])


def _captioned_figure_warning(manifest, api, reason, **fields):
    warning = dict(severity='warning', reason=reason, **fields)
    manifest.setdefault('captioned_figure_warnings', []).append(warning)
    api.diagnostic_event('CAPTIONED_FIGURE_UNCHANGED', **warning)


def capture_captioned_figures(doc, reader, records, manifest, api):
    """Snapshot source figure ownership before any table repair shrinks its bbox."""
    objects = [(item, api.item_region(item, doc)) for item in [*doc.tables, *doc.pictures]]
    objects = [(item, loc) for item, loc in objects if loc]
    texts = [(item, api.item_region(item, doc)) for item in doc.texts]
    table_refs = {item.self_ref for item in doc.tables}
    registry = {}

    def x_overlap(a, b):
        return max(0., min(a[2], b[2])-max(a[0], b[0]))/max(1e-9, min(a[2]-a[0], b[2]-b[0]))

    for caption, location in texts:
        if not location or not _captioned_figure_title(getattr(caption, 'text', '')):
            continue
        page, capbox = location
        font = _primary_note_source_font(dict(page=page, bbox=capbox, pdf_geometry={}), reader)
        font_basis = 'positioned_source_glyphs'
        if not font:
            font = (capbox[3]-capbox[1])*CAPTION_FIGURE_BBOX_FONT_RATIO
            font_basis = 'positioned_primary_caption_height'
        if not math.isfinite(font) or font <= 0:
            _captioned_figure_warning(manifest, api, 'caption_has_no_positioned_height', caption_ref=caption.self_ref, page=page)
            continue
        tolerance = font*CAPTION_FIGURE_BOX_TOLERANCE_EM
        candidates = []
        for item, loc in objects:
            if loc[0] != page:
                continue
            box = loc[1]; gap = capbox[1]-box[3]
            linked = any(ref.cref == caption.self_ref for ref in getattr(item, 'captions', []))
            if (not -tolerance <= gap <= font*CAPTION_FIGURE_MAX_GAP_EM
                    or x_overlap(box, capbox) < CAPTION_FIGURE_X_OVERLAP
                    or not box[0]-tolerance <= (capbox[0]+capbox[2])/2 <= box[2]+tolerance):
                continue
            intervening = [other.self_ref for other, pos in [*objects, *texts] if pos and pos[0] == page
                and other.self_ref not in {item.self_ref, caption.self_ref}
                and not _captioned_figure_contains(box, pos[1])
                and pos[1][1] >= box[3]-tolerance and pos[1][3] <= capbox[1]+tolerance
                and x_overlap(box, pos[1]) >= CAPTION_FIGURE_X_OVERLAP]
            if intervening:
                continue
            candidates.append(dict(item=item, box=list(box), gap=gap, linked=linked))
        linked = [candidate for candidate in candidates if candidate['linked']]
        if linked:
            candidates = linked
        # The enclosing original object owns small sketches inside its body.
        candidates = [c for c in candidates if not any(c is not outer
            and _captioned_figure_contains(outer['box'], c['box']) and outer['box'] != c['box'] for outer in candidates)]
        candidates.sort(key=lambda c: c['gap'])
        if (not candidates or len(candidates) > 1 and (linked
                or candidates[1]['gap']-candidates[0]['gap'] <= font*CAPTION_FIGURE_UNIQUE_GAP_EM)):
            # Skipping optional regrouping is not evidence of a conversion defect.
            api.diagnostic_event('CAPTIONED_FIGURE_BINDING_SKIPPED', severity='info', caption_ref=caption.self_ref,
                                 page=page, candidates=[c['item'].self_ref for c in candidates])
            continue
        selected = candidates[0]; item, box = selected['item'], selected['box']
        # A nearby footer alone cannot stand in for a fragmented whole drawing.
        fragments = []
        for other, pos in objects:
            if (pos[0] != page or other.self_ref == item.self_ref or other.self_ref not in table_refs
                    or _captioned_figure_contains(box, pos[1]) or pos[1][3] > box[1]+tolerance
                    or x_overlap(box, pos[1]) < CAPTION_FIGURE_X_OVERLAP):
                continue
            separates = any(t.self_ref != caption.self_ref and p and p[0] == page
                and p[1][1] >= pos[1][3] and p[1][3] <= box[1] and x_overlap(box, p[1]) >= CAPTION_FIGURE_X_OVERLAP
                for t, p in texts)
            if not separates:
                fragments.append(other.self_ref)
        if fragments:
            _captioned_figure_warning(manifest, api, 'fragmented_figure_container_not_proven', caption_ref=caption.self_ref,
                                     page=page, candidate=item.self_ref, upper_tables=fragments)
            continue
        if item.self_ref in registry:
            _captioned_figure_warning(manifest, api, 'figure_has_multiple_formal_captions', caption_ref=caption.self_ref,
                                     page=page, candidate=item.self_ref)
            continue
        padding = font*CAPTION_FIGURE_PADDING_EM
        expanded = [box[0]-padding, box[1]-padding, box[2]+padding, box[3]+padding]
        owned = [r for r in records if r.get('page_no') == page and r.get('bbox')
                 and _captioned_figure_contains(expanded, r['bbox'])]
        envelope = [box, *[r['bbox'] for r in owned]]
        size = doc.pages[page].size
        crop = [max(0., min(b[0] for b in envelope)-padding), max(0., min(b[1] for b in envelope)-padding),
                min(size.width, max(b[2] for b in envelope)+padding),
                min(size.height, capbox[1]-font*CAPTION_FIGURE_CAPTION_CLEARANCE_EM,
                    max(b[3] for b in envelope)+padding)]
        # Padding may not absorb an unrelated paragraph outside the source object.
        for text, pos in texts:
            if (not pos or pos[0] != page or text.self_ref == caption.self_ref
                    or _captioned_figure_contains(box, pos[1]) or not api.overlap_ratio(crop, pos[1])):
                continue
            b = pos[1]
            if b[3] <= box[1]: crop[1] = max(crop[1], b[3])
            elif b[1] >= box[3]: crop[3] = min(crop[3], b[1])
            elif b[2] <= box[0]: crop[0] = max(crop[0], b[2])
            elif b[0] >= box[2]: crop[2] = min(crop[2], b[0])
        if not _captioned_figure_contains(crop, box):
            _captioned_figure_warning(manifest, api, 'caption_overlaps_original_object', caption_ref=caption.self_ref,
                                     page=page, candidate=item.self_ref)
            continue
        entry = dict(ref=item.self_ref, original_ref=item.self_ref, page=page, original_bbox=box, bbox=crop,
            caption_ref=caption.self_ref, caption_text=caption.text, caption_bbox=list(capbox),
            caption_match='linked' if selected['linked'] else 'nearest_unique', source_font=font, font_basis=font_basis,
            source_table_refs=[item.self_ref] if item.self_ref in table_refs else [], excluded_refs=[],
            primary_records=[dict(record_sha256=hashlib.sha256(r.get('html', '').encode()).hexdigest(), bbox=list(r['bbox'])) for r in owned],
            method='explicit_source_figure_caption')
        registry[item.self_ref] = entry
        manifest.setdefault('captioned_figures', []).append(entry)
        api.diagnostic_event('CAPTIONED_FIGURE_REGISTERED', **entry)
    return registry


def reconcile_captioned_figures(doc, registry, figure_routes, routes, picture_routes, excluded, manifest, regions, api):
    """Keep whole original figures authoritative after legacy recovery inserts children."""
    from bs4 import BeautifulSoup
    from docling_core.types.doc import BoundingBox, CoordOrigin
    all_items = [*doc.tables, *doc.pictures, *doc.texts]
    table_refs = {item.self_ref for item in doc.tables}
    caption_refs = {ref for entry in registry.values()
                    for ref in entry.get('caption_refs', [entry['caption_ref']])}
    for ref, entry in registry.items():
        owner = next((item for item in all_items if item.self_ref == ref), None)
        if owner is None:
            continue
        if ref not in figure_routes:
            image = regions.crop(entry['page'], entry['bbox'])
            try:
                fragment = api.image_html(image, entry['bbox'][2]-entry['bbox'][0], 'picture')
                entry['image_pixels'] = list(image.size)
            finally:
                image.close()
            parsed = BeautifulSoup(fragment, 'html.parser')
            parsed.figure['data-pdf-figure-ref'] = ref
            src = parsed.img['src']
            entry['image_src_sha256'] = hashlib.sha256(src.encode()).hexdigest()
            entry['png_sha256'] = hashlib.sha256(base64.b64decode(src.split(',', 1)[1])).hexdigest()
            figure_routes[ref] = str(parsed)
        excluded.discard(ref)
        for prov in owner.prov:
            if prov.page_no == entry['page']:
                b = entry['bbox']; prov.bbox = BoundingBox(l=b[0], t=b[1], r=b[2], b=b[3], origin=CoordOrigin.TOPLEFT)
        (routes if ref in table_refs else picture_routes)[ref] = figure_routes[ref]
        excluded.update(entry.get('caption_refs', [entry['caption_ref']]))
        known = {p['ref'] for p in entry['excluded_refs']}
        for item in all_items:
            if item.self_ref in registry or item.self_ref in caption_refs:
                continue
            loc = api.item_region(item, doc)
            if loc and loc[0] == entry['page'] and _captioned_figure_contains(entry['bbox'], loc[1]):
                excluded.add(item.self_ref)
                if item.self_ref in table_refs:
                    routes[item.self_ref] = ''
                    if item.self_ref not in entry['source_table_refs']: entry['source_table_refs'].append(item.self_ref)
                elif item in doc.pictures:
                    picture_routes[item.self_ref] = ''
                if item.self_ref not in known:
                    entry['excluded_refs'].append(dict(ref=item.self_ref, bbox=list(loc[1]), label=str(item.label)))
                    known.add(item.self_ref)
        api.diagnostic_event('CAPTIONED_FIGURE_RECONCILED', ref=ref, page=entry['page'], bbox=entry['bbox'],
                             source_table_refs=entry['source_table_refs'], excluded_refs=entry['excluded_refs'])
    replaced = {ref for e in registry.values() for ref in e['source_table_refs']}
    manifest['tables'] = [e for e in manifest.get('tables', []) if e['ref'] not in replaced]


def serialize_captioned_figure(item, entry, fragment, doc_serializer, **kwargs):
    from bs4 import BeautifulSoup
    from docling_core.types.doc import RefItem
    if entry.get('serialized_count'):
        return ''
    caption_refs = entry.get('caption_refs', [entry['caption_ref']])
    caption_owner = item.model_copy(update={'captions': [RefItem(cref=ref) for ref in caption_refs]})
    caption_options = dict(kwargs, visited=set(kwargs.get('visited') or ()) | set(caption_refs))
    caption = doc_serializer.serialize_captions(item=caption_owner, tag='figcaption', **caption_options).text
    parsed = BeautifulSoup(fragment, 'html.parser')
    if caption:
        rendered_caption = BeautifulSoup(caption, 'html.parser')
        if entry.get('caption_position') == 'before':
            parsed.figure.insert(0, rendered_caption)
        else:
            parsed.figure.append(rendered_caption)
    entry['serialized_count'] = 1
    entry['caption_rendered'] = bool(caption)
    return str(parsed)


def _primary_region_ownership_guard(geometry, frame_proof, records, page, target, source_image,
                                    region, source_font, api):
    """Require cell ownership before applying a drawing-frame OCR packet to its inner table."""
    from bs4 import BeautifulSoup
    from docling_pdf_primary import _aligned_profile

    if (not geometry or not frame_proof or frame_proof.get('page') != page
            or frame_proof.get('method') != 'source_figure_caption_empty_band_and_titleblock_grid'
            or not frame_proof.get('caption') or not frame_proof.get('empty_separator_bbox')
            or not frame_proof.get('data_payloads_unchanged')):
        return None
    data, frame = geometry.get('bbox'), frame_proof.get('source_bbox')
    measured = frame_proof.get('data_bbox')
    bands = [p.get('bbox') for p in frame_proof.get('source_illustrations', []) if p.get('bbox')]
    if not data or not frame or not measured or not bands:
        return None
    if not source_font or not math.isfinite(source_font) or source_font <= 0:
        api.diagnostic_event('PRIMARY_REGION_OWNERSHIP_UNCHANGED', page=page, severity='warning',
                             reason='positioned_source_font_unavailable')
        return None
    margin = source_font*PRIMARY_REGION_OWNERSHIP_MARGIN_EM
    if any(abs(a-b) > margin for a, b in zip(data, measured)):
        return None
    problems = []
    for record in records:
        box = record.get('bbox')
        if (record.get('page_no') != page or not box or '<table' not in record.get('html', '').lower()
                or overlap_ratio(box, data) <= PRIMARY_REGION_OWNERSHIP_MIN_OVERLAP
                or overlap_ratio(box, frame) <= PRIMARY_REGION_OWNERSHIP_MIN_OVERLAP):
            continue
        outside = (box[0] < data[0]-margin or box[1] < data[1]-margin
                   or box[2] > data[2]+margin or box[3] > data[3]+margin)
        if not outside or not any(overlap_ratio(box, band) > 0 for band in bands):
            continue
        parsed = BeautifulSoup(record['html'], 'html.parser')
        if len(parsed.find_all('table')) != 1:
            continue
        try:
            aligned, _ = _aligned_profile(api.clean_table_html(str(parsed.table)), geometry,
                                         target, source_image, region, api)
        except (ValueError, TypeError, KeyError):
            aligned = None
        if aligned is not None:
            return None  # A larger packet bbox is harmless when its cells are proved.
        profile = api.table_profile(str(parsed.table))
        problems.append(dict(bbox=list(box), record_sha256=hashlib.sha256(record['html'].encode()).hexdigest(),
                             primary_rows=profile['rows'], primary_columns=profile['columns']))
    if not problems:
        return None
    envelopes = [frame, *[p['bbox'] for p in problems]]
    return dict(page=page, severity='warning', reason='primary_packet_crosses_proved_inner_table_region',
                method='proved_drawing_frame_and_unaligned_primary_packet',
                data_bbox=list(data), source_frame_bbox=list(frame), source_font=source_font,
                border_margin=margin, primary_records=problems,
                fallback_bbox=[min(b[0] for b in envelopes), min(b[1] for b in envelopes),
                               max(b[2] for b in envelopes), max(b[3] for b in envelopes)])


def _primary_region_exclude_owned_frame_pictures(created, bbox, picture_routes, excluded):
    """Suppress only unchanged frame pictures fully covered by their new source crop."""
    removed = []
    for picture in created:
        bounds = picture['bbox']; ref = picture['ref']; prior = picture_routes.get(ref)
        if (bbox[0] <= bounds[0] <= bounds[2] <= bbox[2]
                and bbox[1] <= bounds[1] <= bounds[3] <= bbox[3]
                and prior is not None and hashlib.sha256(prior.encode()).hexdigest() == picture['html_sha256']):
            picture_routes[ref] = ''; excluded.add(ref); removed.append(dict(picture))
    return removed


def _packet_payload_profile(fragment,api):
    from bs4 import BeautifulSoup
    from docling_pdf_review import primary_fingerprint
    if len(BeautifulSoup(fragment,'html.parser').find_all('table'))!=1:
        return None
    try:
        return primary_fingerprint(api.table_profile(api.clean_table_html(fragment)))
    except (ValueError,TypeError):
        return None

def _packet_assets(entry):
    geometry=entry.get('pdf_geometry')
    if not isinstance(geometry,dict):
        raise ValueError('Source asset geometry is unavailable')
    values=[value for key in ['source_graphic_assets','retained_cell_images','restored_cell_math']
            for value in geometry.get(key,[])]+entry.get('_additional_source_assets',[])
    return {hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            for value in values}

def plan_recovered_packet_duplicates(entries,records,original_refs,api):
    bindings={}
    identities={}
    warnings=[]
    for entry in entries:
        profile=_packet_payload_profile(entry['html'],api)
        if profile is None:continue
        matches=[]
        for index,record in enumerate(records):
            if (record.get('page_no')!=entry['page'] or '<table' not in record.get('html','').lower()
                    or api.overlap_ratio(record['bbox'],entry['bbox'])<=.6):
                continue
            if _packet_payload_profile(record['html'],api)!=profile:continue
            identity=dict(page=record['page_no'],bbox=record['bbox'],record_index=index,
                html_sha256=hashlib.sha256(record['html'].encode()).hexdigest())
            key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            identities[key]=identity
            matches.append(key)
        if len(matches)==1:bindings[entry['ref']]=matches[0]
        elif entry['ref'] not in original_refs:
            warnings.append(dict(ref=entry['ref'],reason='physical_primary_packet_not_unique',matches=len(matches)))
    plans=[]
    for recovered in entries:
        if recovered['ref'] in original_refs:
            continue
        geometry=recovered.get('pdf_geometry')
        if not isinstance(geometry,dict):
            warnings.append(dict(ref=recovered['ref'],reason='recovered_source_geometry_unavailable'))
            continue
        if not geometry.get('source_native_discovery'):
            continue
        packet=bindings.get(recovered['ref'])
        if not packet:continue
        owners=[e for e in entries if e['ref'] in original_refs and bindings.get(e['ref'])==packet]
        if not owners:
            continue
        if len(owners)!=1 or owners[0]['html']!=recovered['html']:
            warnings.append(dict(ref=recovered['ref'],reason='original_owner_or_exact_route_payload_not_unique',matches=len(owners)))
            continue
        owner=owners[0]
        try:
            assets_owned=_packet_assets(recovered)<=_packet_assets(owner)
        except (ValueError,TypeError):
            warnings.append(dict(ref=recovered['ref'],reason='source_asset_identity_unavailable'))
            continue
        if not assets_owned:
            warnings.append(dict(ref=recovered['ref'],reason='recovered_source_assets_not_owned_by_original'))
            continue
        plans.append(dict(owner_ref=owner['ref'],exclude_recovered_ref=recovered['ref'],
            packet_id=packet,source_packet=identities[packet],owner_bbox=owner['bbox'],recovered_bbox=recovered['bbox'],
            route_html_sha256=hashlib.sha256(recovered['html'].encode()).hexdigest(),removed_route_html=recovered['html'],
            action='prevent_duplicate_recovery_of_one_physical_primary_packet'))
    return plans,warnings

def apply_recovered_packet_ownership_guard(routes,excluded,manifest,records,original_refs,api,*,table_owned_refs=None):
    """Before page joins, suppress only a newly created copy of an original carrier."""
    from bs4 import BeautifulSoup
    entries=[]
    for entry in manifest.get('tables',[]):
        fragment=routes.get(entry['ref'])
        if not fragment or entry['ref'] in excluded:
            continue
        additional=[]
        for name in ['protected_source_graphic_assets','source_cell_diagrams','source_cell_image_assets',
                     'final_graphic_asset_registration']:
            values=manifest.get(name,[])
            if isinstance(values,dict):values=list(values.values())
            for asset in values if isinstance(values,list) else []:
                if isinstance(asset,dict) and (asset.get('ref')==entry['ref'] or asset.get('source_ref')==entry['ref']):
                    additional.append(asset)
        entries.append(dict(entry,html=fragment,_additional_source_assets=additional))
    plans,warnings=plan_recovered_packet_duplicates(entries,records,set(original_refs),api)
    for warning in warnings:
        warning=dict(warning,severity='warning')
        manifest.setdefault('recovered_packet_ownership_declines',[]).append(warning)
        api.diagnostic_event('RECOVERED_PACKET_OWNERSHIP_UNCHANGED',**warning)
    removed=[]
    for proof in plans:
        ref=proof['exclude_recovered_ref']
        ownership=table_owned_refs or {}
        reason=None
        if ref not in ownership or proof['owner_ref'] not in ownership:
            reason='table_caption_and_child_ownership_unavailable'
        elif any(not set(ownership[ref].get(role,[]))<=set(ownership[proof['owner_ref']].get(role,[]))
                 for role in ('captions','footnotes','children')):
            reason='recovered_table_has_unique_caption_or_child'
        if reason:
            warning=dict(ref=ref,owner_ref=proof['owner_ref'],reason=reason,severity='warning')
            if warning not in manifest.setdefault('recovered_packet_ownership_declines',[]):
                manifest['recovered_packet_ownership_declines'].append(warning)
                api.diagnostic_event('RECOVERED_PACKET_OWNERSHIP_UNCHANGED',**warning)
            continue
        # Recheck the actual route at the mutation point, preserving the exact full payload.
        fragment=routes.get(ref)
        if fragment!=proof['removed_route_html'] or routes.get(proof['owner_ref'])!=fragment:
            continue
        proof=dict(proof,phase='before_page_join',removed_text=BeautifulSoup(fragment,'html.parser').get_text(' ',strip=True),
                   owner_original=True,recovered_created=True,caption_ownership_checked=True,
                   owner_owned_refs=ownership[proof['owner_ref']],recovered_owned_refs=ownership[ref])
        manifest.setdefault('recovered_primary_packet_exclusions',[]).append(proof)
        routes[ref]='';excluded.add(ref);removed.append(ref)
        entry=next(e for e in manifest['tables'] if e['ref']==ref)
        entry['recovered_duplicate_of']=proof['owner_ref']
        entry['excluded_before_join']=True
        api.diagnostic_event('RECOVERED_PRIMARY_PACKET_COPY_EXCLUDED',ref=ref,owner_ref=proof['owner_ref'],
            page=proof['source_packet']['page'],packet_id=proof['packet_id'],html_sha256=proof['route_html_sha256'])
    return removed

def table_content_candidates(item, doc, records, page_no, bbox, *, primary_only):
    """Keep TableFormer's structure separate from its matched text content."""
    candidates = [] if primary_only else [('tableformer', item.export_to_html(doc=doc))]
    return candidates + [('surya_page', record['html']) for record in records
                         if record['page_no'] == page_no and overlap_ratio(record['bbox'], bbox) > .6]


def table_consensus_candidate(candidates):
    """Choose a complete existing reading; never synthesize numerical values."""
    numbers = [numeric_cells(candidate[3]) for candidate in candidates]
    def support(index):
        values = numbers[index]
        return sum(sum(values[key] == peer[key] for key in values.keys() & peer.keys())
                   for peer_index, peer in enumerate(numbers) if peer_index != index)
    return max(enumerate(candidates), key=lambda pair: (
               pair[1][1] in {"pdf_geometry", "raster_geometry"}, support(pair[0]), pair[1][0]))[1]


def recover_complete_table_header(content, candidates):
    """Recover a lost grouped heading when two reads agree on the data grid.

    Replace only an incomplete header plus empty padding rows. Never move,
    overwrite or infer body values from header similarity alone.
    """
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(content,'html.parser');profile=table_profile(content)
    rows=soup.table.find_all('tr')
    current_head=len(soup.table.thead.find_all('tr')) if soup.table.thead else 0
    values=numeric_cells(profile)
    for _,method,fragment,other in candidates:
        if (other['issues'] or (other['columns'],other['rows'])!=(profile['columns'],profile['rows'])):
            continue
        alternative=BeautifulSoup(fragment,'html.parser')
        header=alternative.table.thead
        count=len(header.find_all('tr')) if header else 0
        if header is None:
            prefix=[]
            for row in alternative.table.find_all('tr'):
                if not row.find('th',recursive=False):break
                prefix.append(row)
            if prefix:
                header=alternative.new_tag('thead')
                for row in prefix:header.append(row.extract())
                count=len(prefix)
        if count<=current_head or count>=len(rows):
            continue
        if any(row.get_text(strip=True) or row.find(['math','img']) for row in rows[current_head:count]):
            continue
        numeric=numeric_cells(other);common={k for k in values.keys()&numeric.keys() if k[0]>=count}
        if len(common)<5 or any(values[k]!=numeric[k] for k in common):
            continue
        # A layout parser may put the stub heading in the second header row
        # with an empty cell above it. Restore its vertical merge.
        header_rows=header.find_all('tr')
        if len(header_rows)>=2:
            upper=header_rows[0].find(['td','th'],recursive=False)
            lower=header_rows[1].find(['td','th'],recursive=False)
            if (upper and lower and not upper.get_text(strip=True) and not upper.find(['math','img'])
                    and lower.get_text(strip=True) and upper.get('colspan','1')==lower.get('colspan','1')=='1'
                    and upper.get('rowspan','1')==lower.get('rowspan','1')=='1'):
                for child in list(lower.contents):upper.append(child.extract())
                upper['rowspan']='2';upper.name='th';lower.decompose()
        for row in rows[:count]:row.decompose()
        if soup.table.thead:soup.table.thead.decompose()
        soup.table.insert(0,header.extract())
        result=str(soup.table)
        if not table_profile(result,profile['columns'])['issues']:
            return result,{'method':method,'header_rows':count,'matching_numeric_cells':len(common)}
        return content,None
    return content,None


def recover_scanned_table_regions(doc, reader, regions, page_numbers, records, routes, excluded, manifest):
    """Recover ruled scan tables even when layout emitted pictures/fragments.

    Require positioned text and either an existing table or a source table
    caption. A chart with a few parallel lines is not sufficient evidence.
    """
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, Size

    recovered, repairs = {}, []
    diagnostic_event('SCAN_RECOVERY_BEGIN', pages=diagnostic_summary(page_numbers),
                     table_count=len(doc.tables), ocr_record_count=len(records), reader_available=reader is not None)
    if reader is None or not hasattr(regions,'scan_tables'):
        diagnostic_event('SCAN_RECOVERY_SKIP', reason='reader_unavailable' if reader is None else 'scan_table_detector_unavailable')
        return recovered
    for page_no in page_numbers:
        page = reader.load_page(page_no)
        if page is None or len(page.chars)<12 or not any(reader.is_page_scan(obj,
                [page.width*.03,page.height*.03,page.width*.97,page.height*.97]) for obj in page.images):
            diagnostic_event('SCAN_RECOVERY_PAGE_SKIP', page_no=page_no,
                             reason='page_unavailable' if page is None else 'insufficient_positioned_text' if len(page.chars)<12 else 'no_scan_background',
                             char_count=len(page.chars) if page is not None else 0)
            continue
        diagnostic_event('SCAN_RECOVERY_PAGE_BEGIN', page_no=page_no, char_count=len(page.chars), image_count=len(page.images))
        region_count = 0
        for geometry in regions.scan_tables(page_no):
            region_count += 1
            box = geometry['bbox']
            from docling_pdf_forms import form_owns_region
            if form_owns_region(manifest, page_no, box):
                diagnostic_event('SCAN_RECOVERY_REGION_SKIP', page_no=page_no, bbox=box,
                                 reason='explicit_source_form_illustration')
                continue
            matched = [item for item in doc.tables if item.self_ref not in excluded
                       and (reg:=item_region(item,doc)) and reg[0]==page_no and overlap_ratio(reg[1],box)>.65]
            above = [c for c in page.chars if box[0]-6 <= (c['x0']+c['x1'])/2 <= box[2]+6
                     and box[1]-64 <= (c['top']+c['bottom'])/2 < box[1]]
            caption_line = next((line for line in reversed(native_cell_lines(above)) if extract_table_caption(line['text'])),None)
            if not matched and caption_line is None:
                diagnostic_event('SCAN_RECOVERY_REGION_SKIP', page_no=page_no, bbox=box,
                                 reason='no_existing_table_or_caption', matched_count=len(matched), caption_found=False)
                continue
            refs = [record['html'] for record in records if record['page_no']==page_no
                    and '<table' in record['html'].lower() and overlap_ratio(record['bbox'],box)>.6]
            refs += [item.export_to_html(doc=doc) for item in matched]
            diagnostic_event('SCAN_RECOVERY_REGION_READ', page_no=page_no, bbox=box,
                             matched_refs=[item.self_ref for item in matched], reference_count=len(refs),
                             caption_found=caption_line is not None, geometry=diagnostic_summary(geometry))
            from docling_pdf_scan_recovery import read_source_scan_grid
            source_grid = read_source_scan_grid(reader, regions, page_no, box, refs,
                                                sys.modules[__name__])
            native = source_grid['native'] if source_grid else None
            if source_grid:
                source_grid['image'].close()
            if not native:
                diagnostic_event('SCAN_RECOVERY_REGION_SKIP', page_no=page_no, bbox=box, reason='raster_reader_returned_no_table')
                continue
            # The render includes a rule-width margin; source ownership stays
            # attached to the actual measured table, not that helper margin.
            box = list(native[1].get('bbox', box))
            if page_no not in doc.pages:
                doc.add_page(page_no=page_no,size=Size(width=page.width,height=page.height))
            item = matched[0] if matched else insert_source_item(doc,page_no,box,'table')
            item.prov[0].bbox = BoundingBox(l=box[0],t=box[1],r=box[2],b=box[3],origin=CoordOrigin.TOPLEFT)
            for duplicate in matched[1:]:
                routes[duplicate.self_ref]='';excluded.add(duplicate.self_ref)
                for ref in duplicate.captions:
                    if not any(old.cref==ref.cref for old in item.captions):
                        item.captions.append(ref)
                    caption_item=ref.resolve(doc)
                    if getattr(caption_item.parent,'cref',None)==duplicate.self_ref:
                        duplicate.children=[child for child in duplicate.children if child.cref!=ref.cref]
                        caption_item.parent=item.get_ref()
                        if not any(child.cref==ref.cref for child in item.children):item.children.append(ref)
            if caption_line and not item.captions:
                caption = extract_table_caption(caption_line['text'])
                # Reuse an existing nearby source label; otherwise add the
                # native caption beside this table, without another OCR call.
                existing = next((text for text in doc.texts if (reg:=item_region(text,doc))
                                and reg[0]==page_no and abs(reg[1][1]-caption_line['y'])<18
                                and extract_table_caption(text.text)),None)
                if existing:
                    item.captions.append(existing.get_ref())
                else:
                    cap=doc.insert_text(sibling=item,after=False,label=DocItemLabel.CAPTION,text=caption,
                                        prov=item.prov[0].model_copy(deep=True))
                    cap.prov[0].bbox=BoundingBox(l=caption_line['x0'],t=caption_line['y']-caption_line['size'],
                        r=caption_line['x1'],b=caption_line['y'],origin=CoordOrigin.TOPLEFT)
                    item.captions.append(cap.get_ref())
            recovered[item.self_ref] = native
            repairs.append({'ref':item.self_ref,'page':page_no,'bbox':box,
                            'replaced_fragments':[other.self_ref for other in matched[1:]],
                            'cell_images':len(native[1]['retained_cell_images'])})
            diagnostic_event('SCAN_RECOVERY_REGION_ACCEPT', page_no=page_no, ref=item.self_ref, bbox=box,
                             replaced_refs=[other.self_ref for other in matched[1:]],
                             cell_image_count=len(native[1]['retained_cell_images']),
                             rows=max(0,len(native[1].get('y_grid',[]))-1), columns=max(0,len(native[1].get('x_grid',[]))-1))
        diagnostic_event('SCAN_RECOVERY_PAGE_END', page_no=page_no, region_count=region_count,
                         recovered_count=sum(entry['page']==page_no for entry in repairs))
    manifest['recovered_scan_tables'] = repairs
    if repairs:
        print(f"Scan table recovery: {len(repairs)} source grids; "
              f"{sum(entry['cell_images'] for entry in repairs)} illustration cells retained",flush=True)
    diagnostic_event('SCAN_RECOVERY_END', recovered_count=len(recovered),
                     replaced_fragment_count=sum(len(entry['replaced_fragments']) for entry in repairs),
                     cell_image_count=sum(entry['cell_images'] for entry in repairs))
    return recovered


# BEGIN borderless_rows functions
def _borderless_key(text):
    import re
    text = text.translate(str.maketrans({'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E',
        'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'Т': 'T', 'Х': 'X',
        'а': 'a', 'в': 'b', 'с': 'c', 'е': 'e', 'к': 'k', 'м': 'm', 'н': 'h',
        'о': 'o', 'р': 'p', 'т': 't', 'х': 'x',
        '−': '-', '–': '-', '—': '-', '\u00ad': ''}))
    return re.sub(r'\s+', '', text).casefold()

def _borderless_html_lines(html):
    """Return original inline fragments, breaking only at explicit BR/blocks."""
    from bs4 import BeautifulSoup, NavigableString
    from html import escape
    import copy
    import re

    def join(children):
        lines = ['']
        for child in children:
            pieces = split(child)
            lines[-1] += pieces[0]
            lines.extend(pieces[1:])
        return lines

    def split(node):
        if isinstance(node, NavigableString):
            return [escape(re.sub(r'\s+', ' ', str(node)))]
        if node.name == 'br':
            return ['', '']
        if node.name in {'p', 'div'}:
            return ['', *join(node.children), '']
        if not node.find(['br', 'p', 'div']):
            return [str(node)]
        result = []
        for text in join(node.children):
            clone = copy.copy(node)
            clone.clear()
            clone.append(BeautifulSoup(text, 'html.parser'))
            result.append(str(clone))
        return result

    parsed = BeautifulSoup(html, 'html.parser')
    return [part.strip() for part in join(parsed.children)
            if BeautifulSoup(part, 'html.parser').get_text(strip=True)
            or BeautifulSoup(part, 'html.parser').find('math')]

def _borderless_atomic(text):
    import re
    value = text.strip()
    return bool(value and (re.fullmatch(r'[\d\s.,+−–—\-<>≤≥%°/:()]+', value)
        or re.fullmatch(r'[A-Za-zА-Яа-яЁёΑ-Ωα-ω]{1,2}\s*\d*\)?', value)
        or re.fullmatch(r'[^\w\s]+', value)))

def _borderless_source_lines(reader, cell, page, api):
    """Use PDF/OCR positions as evidence, never as replacement HTML."""
    if cell.get('source_text_unreliable'):
        return []
    box = cell.get('bbox')
    if not box:
        return []
    glyphs = cell.get('glyphs')
    if not glyphs and reader is not None:
        glyphs = [g for g in reader.chars if box[0] < (g['x0']+g['x1'])/2 < box[2]
                  and box[1] < (g['top']+g['bottom'])/2 < box[3]]
    if glyphs:
        return api.native_cell_lines(glyphs)
    # Some OCR/layout providers already attach positioned line records.
    lines = cell.get('lines') or cell.get('ocr_lines') or []
    return sorted((dict(line) for line in lines if line.get('text', '').strip()
                   and line.get('y') is not None and line.get('size', 0) > 0), key=lambda l: l['y'])

def _borderless_interior_rules(reader, regions, page, box, size, rails=()):
    rules = []
    if reader is not None:
        for edge in getattr(getattr(reader, 'page', None), 'edges', ()):
            if edge.get('orientation') == 'h':
                rules.append((edge['x0'], edge['x1'], edge['top']))
    if regions is not None:
        from docling_pdf_math_grid import raster_grid
        image = regions.crop(page, box)
        try:
            rules.extend(raster_grid(image, box)['horizontal'])
        finally:
            image.close()
    if regions is None and reader is None:
        return None
    epsilon = max(BORDERLESS_RULE_MIN_EDGE_PT, size*BORDERLESS_RULE_EDGE_EPS_EM)
    width = box[2]-box[0]
    rail_tolerance = size*BORDERLESS_RULE_RAIL_TOLERANCE_EM
    smallest = min((b-a for a, b in zip(rails, rails[1:])), default=width)
    def separates_columns(left, right):
        return (right-left >= smallest*BORDERLESS_RULE_MIN_COLUMN_COVERAGE
                and all(any(abs(end-x) <= rail_tolerance for x in rails) for end in (left, right)))
    return [[left, y, right, y] for left, right, y in rules
            if box[1]+epsilon < y < box[3]-epsilon
            and (min(right, box[2])-max(left, box[0]) >= width*BORDERLESS_RULE_COVERAGE
                 or separates_columns(left, right))]

def _borderless_warning(manifest, api, page, ref, row, reason, **extra):
    warning = dict(page=page, ref=ref, row=row, reason=reason, **extra)
    manifest.setdefault('borderless_row_warnings', []).append(warning)
    api.diagnostic_event('BORDERLESS_ROW_UNCHANGED', **warning)

def _borderless_plan(lines, fragments):
    """Infer aligned value starts and preserve unmatched wrapped descriptors."""
    from statistics import median
    from bs4 import BeautifulSoup

    groups = {}
    for col, values in enumerate(lines):
        if len(values) >= BORDERLESS_MIN_VALUE_ROWS:
            groups.setdefault(len(values), []).append(col)
    options = []
    for count, columns in groups.items():
        if len(columns) < BORDERLESS_MIN_ALIGNED_COLUMNS:
            continue
        size = median(l['size'] for col in columns for l in lines[col])
        anchors = [median(lines[col][i]['y'] for col in columns) for i in range(count)]
        if (all(abs(lines[col][i]['y']-anchors[i]) <= size*BORDERLESS_Y_TOLERANCE_EM
                for col in columns for i in range(count))
                and all(b-a >= size*BORDERLESS_MIN_ANCHOR_GAP_EM for a, b in zip(anchors, anchors[1:]))):
            options.append((len(columns), count, columns, anchors, size))
    options.sort(key=lambda item: item[:2], reverse=True)
    if not options:
        return None, 'insufficient_aligned_columns'
    if len(options) > 1 and options[0][0] == options[1][0]:
        return None, 'competing_baseline_clusters'
    _, count, columns, anchors, size = options[0]
    atomics = sum(_borderless_atomic(BeautifulSoup(part, 'html.parser').get_text(' ', strip=True))
                  for values in fragments for part in values)
    if atomics < BORDERLESS_MIN_ATOMIC_ITEMS:
        return None, 'ordinary_multiline_prose_or_ambiguous_values'
    spans, assignments, extra_groups = {}, {}, []
    tolerance = size*BORDERLESS_Y_TOLERANCE_EM
    for col, values in enumerate(lines):
        if not values:
            assignments[col] = {}
            continue
        ordinary_wrap = all(0 < b['y']-a['y'] <= size*BORDERLESS_MAX_WRAP_GAP_EM
                            for a, b in zip(values, values[1:]))
        non_atomic = not any(_borderless_atomic(BeautifulSoup(p, 'html.parser').get_text(' ', strip=True))
                             for p in fragments[col])
        if len(values) < count and col < min(columns) and ordinary_wrap and non_atomic:
            matched_anchors = {i for line in values for i, y in enumerate(anchors)
                               if abs(line['y']-y) <= tolerance}
            if len(matched_anchors) >= BORDERLESS_MIN_DESCRIPTOR_ANCHORS:
                return None, 'descriptor_lines_align_to_distinct_value_rows'
            spans[col] = True
            continue
        assigned, unmatched = {}, []
        for index, line in enumerate(values):
            nearby = [i for i, y in enumerate(anchors) if abs(line['y']-y) <= tolerance]
            if len(nearby) > 1:
                return None, 'ambiguous_line_baseline'
            if nearby:
                assigned.setdefault(nearby[0], []).append(index)
            else:
                unmatched.append(index)
        assignments[col] = assigned
        current = []
        for index in unmatched:
            if current and (index != current[-1]+1 or values[index]['y']-values[current[-1]]['y'] > size*BORDERLESS_MAX_WRAP_GAP_EM):
                extra_groups.append((col, current)); current = []
            current.append(index)
        if current:
            extra_groups.append((col, current))
    extras = []
    for col, indices in extra_groups:
        top, bottom = lines[col][indices[0]]['y'], lines[col][indices[-1]]['y']
        before = [i for i, y in enumerate(anchors) if y < top-tolerance]
        after = [i for i, y in enumerate(anchors) if y > bottom+tolerance]
        final_text = BeautifulSoup(fragments[col][indices[-1]], 'html.parser').get_text(' ', strip=True)
        if not before or not after:
            # Only an explicit heading punctuation proves an unmatched edge block.
            if not (not before and after and final_text.endswith(':')):
                return None, 'unmatched_edge_lines_may_be_paragraph_wrap'
        if any(not (bottom < old['top']-tolerance or top > old['bottom']+tolerance) for old in extras):
            return None, 'overlapping_unmatched_descriptors'
        extras.append(dict(y=(top+bottom)/2, top=top, bottom=bottom, column=col, indices=indices))
    slots = sorted([dict(y=y, anchor=i) for i, y in enumerate(anchors)]
                   +[dict(y=e['y'], extra=e) for e in extras], key=lambda slot: slot['y'])
    return dict(slots=slots, spans=spans, assignments=assignments,
                value_columns=columns, anchors=anchors, font_size=size), None

def repair_borderless_table_routes(doc, routes, manifest, reader, regions, api):
    """Split proven logical rows; preserve primary text, spans and inline markup."""
    from bs4 import BeautifulSoup
    from statistics import median
    from docling_core.types.doc import TableCell, TableData
    import copy

    entries = {e['ref']: e for e in manifest.get('tables', [])}
    for item in doc.tables:
        entry = entries.get(item.self_ref)
        if not entry:
            continue
        geometry = entry.get('pdf_geometry') or {}
        original = routes.get(item.self_ref, '')
        if not geometry.get('source_grid_verified') or geometry.get('hidden_ocr_direction_conflict'):
            continue
        parsed = BeautifulSoup(original, 'html.parser')
        table = parsed.table
        if table is None:
            continue
        initial = api.table_profile(str(table))
        if initial.get('issues'):
            continue
        all_nodes = [n for n in table.find_all(['td', 'th']) if n.find_parent('table') is table]
        node_profiles = {id(node): cell for node, cell in zip(all_nodes, initial['cells'])}
        page = entry['page']
        if reader is not None:
            reader.load_page(page)
        source_bands = {}
        for cell in geometry.get('cells', []):
            if cell['row_span'] == cell['column_span'] == 1:
                source_bands.setdefault(cell['row'], []).append(cell)
        repairs = []
        for tr in list(table.find_all('tr')):
            if tr.find_parent('table') is not table:
                continue
            cells = tr.find_all(['td', 'th'], recursive=False)
            if (len(cells) < BORDERLESS_MIN_ALIGNED_COLUMNS or any(c.name != 'td' for c in cells)
                    or any(c.find(['img', 'svg', 'table']) for c in cells)):
                continue
            specs = [node_profiles[id(c)] for c in cells]
            if any(c['row_span'] != 1 or c['column_span'] != 1 for c in specs):
                continue
            fragments = [_borderless_html_lines(c.decode_contents()) for c in cells]
            if sum(len(v) >= BORDERLESS_MIN_VALUE_ROWS for v in fragments) < BORDERLESS_MIN_ALIGNED_COLUMNS:
                continue
            matches = []
            for source_row, band in source_bands.items():
                band = sorted(band, key=lambda c: c['column'])
                if len(band) != len(cells) or [c['column'] for c in band] != [c['column'] for c in specs]:
                    continue
                lines = [_borderless_source_lines(reader, c, page, api) for c in band]
                if all(len(v) == len(parts) and all(_borderless_key(line['text']) == _borderless_key(
                        BeautifulSoup(part, 'html.parser').get_text(' ', strip=True)) for line, part in zip(v, parts))
                        for v, parts in zip(lines, fragments)):
                    matches.append((source_row, band, lines))
            if len(matches) != 1:
                _borderless_warning(manifest, api, page, item.self_ref, specs[0]['row'],
                                    'source_line_mapping_not_unique', matches=len(matches))
                continue
            source_row, band, lines = matches[0]
            size = median(l['size'] for values in lines for l in values)
            box = [min(c['bbox'][0] for c in band), min(c['bbox'][1] for c in band),
                   max(c['bbox'][2] for c in band), max(c['bbox'][3] for c in band)]
            rails = sorted({x for c in band for x in (c['bbox'][0], c['bbox'][2])})
            rules = _borderless_interior_rules(reader, regions, page, box, size, rails)
            if rules is None or rules:
                _borderless_warning(manifest, api, page, item.self_ref, specs[0]['row'],
                                    'source_contains_row_rules' if rules else 'source_rules_not_available', rules=rules)
                continue
            plan, reason = _borderless_plan(lines, fragments)
            if plan is None:
                _borderless_warning(manifest, api, page, item.self_ref, specs[0]['row'], reason)
                continue
            count = len(plan['slots'])
            new_rows = []
            for index, slot in enumerate(plan['slots']):
                new_row = parsed.new_tag('tr')
                new_row.attrs = dict(tr.attrs)
                if index:
                    new_row.attrs.pop('id', None)
                for col, old in enumerate(cells):
                    if col in plan['spans']:
                        if index:
                            continue
                        node = copy.copy(old)
                        node['rowspan'] = str(count)
                    else:
                        node = parsed.new_tag(old.name)
                        node.attrs = dict(old.attrs)
                        if index:
                            node.attrs.pop('id', None)
                        node.attrs.pop('rowspan', None)
                        node.attrs.pop('colspan', None)
                        indices = (plan['assignments'].get(col, {}).get(slot['anchor'], []) if 'anchor' in slot
                                   else slot['extra']['indices'] if slot['extra']['column'] == col else [])
                        for j, line_index in enumerate(indices):
                            if j:
                                node.append(parsed.new_tag('br'))
                            node.append(BeautifulSoup(fragments[col][line_index], 'html.parser'))
                    new_row.append(node)
                new_rows.append(new_row)
            for row in new_rows:
                tr.insert_before(row)
            tr.decompose()
            repairs.append(dict(source_row=source_row, source_bbox=box, logical_rows=count,
                value_columns=[specs[i]['column'] for i in plan['value_columns']],
                spanning_columns=[specs[i]['column'] for i in plan['spans']],
                anchors=plan['anchors'], font_size=plan['font_size'],
                method='aligned_source_baselines_and_unchanged_primary_fragments'))
        if not repairs:
            continue
        html = str(table)
        checked = api.table_profile(html)
        if checked.get('issues'):
            _borderless_warning(manifest, api, page, item.self_ref, None, 'reconstructed_grid_invalid')
            continue
        # Comparison strips whitespace only; no ditto expansion or text rewriting.
        before_text = ''.join(BeautifulSoup(original, 'html.parser').stripped_strings)
        after_text = ''.join(table.stripped_strings)
        # Row-major reading order changes, so compare the per-fragment multiset.
        from collections import Counter
        def tokens(text):
            import re
            return Counter(re.sub(r'\s+', '', text))
        if tokens(before_text) != tokens(after_text):
            _borderless_warning(manifest, api, page, item.self_ref, None, 'primary_text_coverage_changed')
            continue
        routes[item.self_ref] = html
        item.data = TableData(num_rows=checked['rows'], num_cols=checked['columns'], table_cells=[
            TableCell(text=c['text'], row_span=c['row_span'], col_span=c['column_span'],
                      start_row_offset_idx=c['row'], end_row_offset_idx=c['row']+c['row_span'],
                      start_col_offset_idx=c['column'], end_col_offset_idx=c['column']+c['column_span'],
                      column_header=c['header']) for c in checked['cells']])
        entry.update(html=html, selected=checked, borderless_row_repairs=repairs)
        proof = dict(ref=item.self_ref, page=page, rows=repairs)
        manifest.setdefault('borderless_row_repairs', []).append(proof)
        api.diagnostic_event('BORDERLESS_ROWS_RESTORED', **proof)
# END borderless_rows functions


# BEGIN source_unit_composition helpers
def _source_unit_topology(image):
    """Pixel-only part of quantities.source_permille_quantity; no numeric reader."""
    import cv2
    import numpy as np
    from statistics import median
    p = SOURCE_UNIT_COMPOSITION
    gray_image = image.convert('L')
    try:
        gray = np.asarray(gray_image).copy()
    finally:
        gray_image.close()
    threshold, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    ys, xs = np.where(mask)
    if not len(xs): return None
    inkheight = int(ys.max()-ys.min()+1)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = []
    if hierarchy is not None:
        for index, contour in enumerate(contours):
            parent = int(hierarchy[0,index,3])
            if parent < 0: continue
            x,y,w,h = cv2.boundingRect(contour); area = float(cv2.contourArea(contour))
            if (p['hole_height_min']*inkheight <= h <= p['hole_height_max']*inkheight
                    and p['hole_aspect_min'] <= w/h <= p['hole_aspect_max'] and area >= w*h*p['hole_fill_min']):
                holes.append(dict(box=[x,y,x+w,y+h], area=area, parent=parent, cx=x+w/2, cy=y+h/2, height=h))
    for triple in [sorted(holes,key=lambda h:h['cx'])[-3:]] if len(holes) >= 3 else []:
        top = sorted(triple,key=lambda h:h['cy'])[0]
        bottom = sorted((h for h in triple if h is not top),key=lambda h:h['cx'])
        ringheight = median(h['height'] for h in triple)
        if (max(h['height'] for h in triple) > p['ring_height_ratio']*min(h['height'] for h in triple)
                or max(h['area'] for h in triple) > p['ring_area_ratio']*min(h['area'] for h in triple)
                or abs(bottom[0]['cy']-bottom[1]['cy']) > ringheight*p['lower_alignment']
                or not p['rise_min']*ringheight <= min(h['cy'] for h in bottom)-top['cy'] <= p['rise_max']*ringheight
                or not top['cx'] < bottom[0]['cx'] < bottom[1]['cx']
                or not p['lower_gap_min']*ringheight <= bottom[1]['cx']-bottom[0]['cx'] <= p['lower_gap_max']*ringheight):
            continue
        boxes = [cv2.boundingRect(contours[parent]) for parent in {h['parent'] for h in triple}]
        left,top_edge = min(b[0] for b in boxes),min(b[1] for b in boxes)
        right,bottom_edge = max(b[0]+b[2] for b in boxes),max(b[1]+b[3] for b in boxes)
        width,height = right-left,bottom_edge-top_edge
        if not width or not height or not p['unit_aspect_min'] <= width/height <= p['unit_aspect_max']:
            continue
        meaningful = []
        for index, contour in enumerate(contours):
            if hierarchy[0,index,3] < 0: continue
            x,y,w,h = cv2.boundingRect(contour)
            if x >= left and cv2.contourArea(contour) >= median(t['area'] for t in triple)*p['meaningful_area']:
                meaningful.append(index)
        if len(meaningful) != 3: continue
        stroke_mask = (gray < (threshold+255)/2).astype('uint8')*255
        unit = stroke_mask[top_edge:bottom_edge,left:right]
        lines = cv2.HoughLinesP(unit,1,np.pi/180,
            threshold=max(p['hough_min_votes'],round(height*p['hough_votes_height'])),
            minLineLength=max(p['hough_min_length'],round(height*p['hough_length_height'])),
            maxLineGap=max(p['hough_min_gap'],round(height*p['hough_gap_height'])))
        diagonals = []
        for line in lines if lines is not None else []:
            ax,ay,bx,by = map(int,line[0]); dx,dy = bx-ax,by-ay
            if dx*dy < 0 and abs(dx) >= height*p['slash_x_height'] and abs(dy) >= height*p['slash_y_height']:
                diagonals.append([ax+left,ay+top_edge,bx+left,by+top_edge])
        if not diagonals or np.any(mask[:,right:]): continue
        py,px = np.where(mask[:,:left])
        if not len(px) or not p['prefix_height_min']*height <= py.max()-py.min()+1 <= p['prefix_height_max']*height:
            continue
        return dict(unit_bbox=[left,top_edge,right,bottom_edge],
            ring_holes=[h['box'] for h in sorted(triple,key=lambda h:(h['cy'],h['cx']))],
            diagonal_strokes=diagonals,prefix_bbox=[int(px.min()),int(py.min()),int(px.max()+1),int(py.max()+1)])
    return None


def _source_unit_xobjects(regions, page_no, bounds=None):
    """Decode original source images, including their masks; never render neighbours."""
    import pypdfium2 as pdfium
    from PIL import Image
    assets = []
    page = regions.pdf[page_no-1]
    try:
        _,height = page.get_size()
        for index,obj in enumerate(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE])):
            left,bottom,right,top = obj.get_bounds()
            bbox = [left,height-top,right,height-bottom]
            if bounds is not None and not any(all(abs(a-b) <= SOURCE_UNIT_COMPOSITION['bbox_epsilon_pt']
                    for a,b in zip(bbox,box)) for box in bounds):
                continue
            matrix = list(obj.get_matrix().get())
            if matrix[1] or matrix[2] or matrix[0] <= 0 or matrix[3] <= 0:
                continue  # Rotated/reflected/local-form ownership requires its own proof.
            bitmap = obj.get_bitmap(render=True,scale_to_original=True)
            try:
                rgba = bitmap.to_pil().convert('RGBA')
                flattened = Image.new('RGBA',rgba.size,'white')
                try:
                    flattened.alpha_composite(rgba); original = flattened.convert('RGB')
                finally:
                    rgba.close(); flattened.close()
            finally:
                bitmap.close()
            try:
                data = BytesIO(); original.save(data,format='PNG')
                scale = SOURCE_UNIT_COMPOSITION['scale']
                normalized = original.resize((original.width*scale,original.height*scale),Image.Resampling.BICUBIC)
                try:
                    normalized_data = BytesIO(); normalized.save(normalized_data,format='PNG')
                    proof = _source_unit_topology(normalized)
                finally:
                    normalized.close()
                assets.append(dict(index=index,bbox=bbox,matrix=matrix,
                    source_dimensions=list(original.size),source_png_sha256=hashlib.sha256(data.getvalue()).hexdigest(),
                    normalized_png_sha256=hashlib.sha256(normalized_data.getvalue()).hexdigest(),
                    normalization='original_xobject_2x_bicubic_once',topology=proof))
            finally:
                original.close()
    finally:
        page.close()
    return assets


def _source_unit_key(value, *, source=False):
    if source:
        value = re.sub(r'(?<=[A-Za-zА-Яа-яЁё])[-\u00ad][ \t]*\n[ \t]*(?=[a-zа-яё])','',value)
    return re.sub(r'\s+','',value)


def _source_unit_contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _source_unit_anchor_text(reader, box, assets, api):
    from statistics import median
    from math import isfinite
    if any(any(key not in g or not isinstance(g[key],(int,float)) or not isfinite(g[key])
            for key in ('x0','x1','top','bottom','size')) or g['size'] <= 0 for g in reader.chars):
        return None
    glyphs = [dict(g) for g in reader.chars if _source_unit_contains(box,[g['x0'],g['top'],g['x1'],g['bottom']])]
    if not glyphs or any(not g.get('upright',True) for g in glyphs): return None
    for asset in assets:
        b = asset['bbox']; cy = (b[1]+b[3])/2
        near = [g for g in glyphs if abs((g['top']+g['bottom'])/2-cy) <= g['size']*SOURCE_UNIT_COMPOSITION['line_tolerance_em']]
        if not near: return None
        baseline,font = median(g['bottom'] for g in near),median(g['size'] for g in near)
        glyphs.append(dict(text=_SOURCE_UNIT_MARKER,x0=b[0],x1=b[2],top=baseline-font,bottom=baseline,
                           size=font,fontname='source-image-placeholder',upright=True))
    return '\n'.join(line['text'] for line in api.native_cell_lines(glyphs))


def _source_unit_warning(manifest, api, reason, **fields):
    entry = dict(reason=reason,severity='warning',**fields)
    manifest.setdefault('source_unit_composition_warnings',[]).append(entry)
    api.diagnostic_event('SOURCE_UNIT_COMPOSITION_UNCHANGED',**entry)


def repair_source_unit_compositions(soup, manifest, regions, reader, records, api):
    """Canonicalize a source-proved unit, preserving every primary digit and word."""
    from bs4 import BeautifulSoup, NavigableString
    from copy import deepcopy
    proofs = []
    for table in soup.select('table[data-pdf-table-ref]'):
        candidates = [node for node in table.find_all(string=True) if _SOURCE_UNIT_TOKEN.search(str(node))]
        if not candidates: continue
        ref = table['data-pdf-table-ref']
        entries = [entry for entry in manifest.get('tables',[]) if entry.get('ref') == ref]
        entry = entries[0] if len(entries) == 1 else {}
        page,geometry = entry.get('page'),entry.get('pdf_geometry') or {}
        def decline(reason, **fields): _source_unit_warning(manifest,api,reason,ref=ref,page=page,**fields)
        if (reader is None or regions is None or not page or not geometry.get('cells') or not geometry.get('bbox')
                or not geometry.get('x_grid') or not geometry.get('y_grid')
                or geometry.get('source_grid_verified') is False or geometry.get('hidden_ocr_direction_conflict')):
            decline('positioned_source_grid_unavailable'); continue
        profile = api.table_profile(str(table))
        if profile['issues'] or table.find('table'):
            decline('table_structure_not_unique'); continue
        signature = lambda profile: [(c['row'],c['column'],c['row_span'],c['column_span'],_source_unit_key(c['text'])) for c in profile['cells']]
        matching = []
        for record in records:
            if (record.get('page_no') != page or not record.get('bbox') or '<table' not in record.get('html','').lower()
                    or api.overlap_ratio(record['bbox'],geometry['bbox']) < SOURCE_UNIT_COMPOSITION['packet_overlap']):
                continue
            parsed = BeautifulSoup(record['html'],'html.parser')
            if len(parsed.find_all('table')) != 1: continue
            raw_profile = api.table_profile(str(parsed.table))
            if not raw_profile['issues'] and signature(raw_profile) == signature(profile): matching.append(record)
        if len(matching) != 1:
            decline('primary_packet_ownership_not_unique',candidates=len(matching)); continue
        record = matching[0]
        try:
            reader.load_page(page)
            source_images = [obj for obj in reader.page.images if _source_unit_contains(record['bbox'],[obj['x0'],obj['top'],obj['x1'],obj['bottom']])]
            bounds = [[obj['x0'],obj['top'],obj['x1'],obj['bottom']] for obj in source_images]
            assets = _source_unit_xobjects(regions,page,bounds)
        except Exception as error:
            decline('source_image_decode_unavailable',error=str(error)); continue
        nodes = table.find_all(['th','td'])
        for cell,node in zip(profile['cells'],nodes):
            text_nodes = [value for value in node.find_all(string=True) if _SOURCE_UNIT_TOKEN.search(str(value))]
            if not text_nodes: continue
            cell_fields = dict(row=cell['row'],column=cell['column'])
            if node.find(['math','sup','sub','code','pre','img','svg']) or any(value.find_parent(['math','sup','sub','code','pre']) for value in text_nodes):
                decline('protected_cell_markup',**cell_fields); continue
            target = [c for c in geometry['cells'] if all(c[k] == cell[k] for k in ('row','column','row_span','column_span'))]
            basis = 'unique_source_cell'
            if len(target) == 1:
                box = target[0]['bbox']
                if target[0].get('source_text_unreliable'):
                    decline('unreliable_source_cell',**cell_fields); continue
            elif (not target and cell['row'] == profile['rows']-1 and cell['column'] == 0
                    and cell['column_span'] == profile['columns'] and cell['row_span'] == 1
                    and cell['row'] == max(c['row']+c['row_span'] for c in geometry['cells'])
                    and record['bbox'][3] > geometry['bbox'][3]):
                box = [record['bbox'][0],geometry['bbox'][3],record['bbox'][2],record['bbox'][3]]
                basis = 'terminal_fullspan_after_native_grid_inside_primary_packet'
            else:
                decline('source_cell_ownership_not_unique',**cell_fields); continue
            owned_images = [obj for obj in source_images if _source_unit_contains(box,[obj['x0'],obj['top'],obj['x1'],obj['bottom']])]
            owned = []
            for obj in owned_images:
                bounds = [obj['x0'],obj['top'],obj['x1'],obj['bottom']]
                match = [asset for asset in assets if all(abs(a-b) <= SOURCE_UNIT_COMPOSITION['bbox_epsilon_pt'] for a,b in zip(asset['bbox'],bounds))]
                if len(match) != 1 or not match[0]['topology']: break
                owned.append(match[0])
            count = sum(len(list(_SOURCE_UNIT_TOKEN.finditer(str(value)))) for value in text_nodes)
            if len(owned) != len(owned_images) or len(owned) != count or not count:
                decline('source_unit_topology_or_count_not_proven',source_images=len(owned_images),positive_assets=len(owned),primary_units=count,**cell_fields); continue
            anchors = _source_unit_anchor_text(reader,box,owned,api)
            masked = _SOURCE_UNIT_TOKEN.sub(_SOURCE_UNIT_MARKER,cell['text'])
            if not anchors or _source_unit_key(anchors,source=True) != _source_unit_key(masked):
                decline('full_source_cell_anchors_differ',**cell_fields); continue
            before = str(node)
            replacements = []
            for value in text_nodes:
                after = _SOURCE_UNIT_TOKEN.sub(lambda match:match['prefix']+'‰',str(value))
                replacements.append((value,after))
            proposed = BeautifulSoup(before,'html.parser')
            for value in proposed.find_all(string=True):
                if _SOURCE_UNIT_TOKEN.search(str(value)):
                    value.replace_with(_SOURCE_UNIT_TOKEN.sub(lambda match:match['prefix']+'‰',str(value)))
            if (re.findall(r'\d',before) != re.findall(r'\d',str(proposed))
                    or _SOURCE_UNIT_TOKEN.sub(_SOURCE_UNIT_MARKER,node.get_text(' ',strip=True))
                    != re.sub(r'(?<![\w.,])[-+−]?\d+(?:[.,]\d+)?\s*‰',_SOURCE_UNIT_MARKER,proposed.get_text(' ',strip=True))):
                decline('primary_numeric_or_context_invariance_failed',**cell_fields); continue
            for value,after in replacements: value.replace_with(NavigableString(after))
            proof = dict(ref=ref,page=page,**cell_fields,bbox=list(box),basis=basis,units=count,
                primary_packet_sha256=hashlib.sha256(record['html'].encode()).hexdigest(),primary_packet_bbox=list(record['bbox']),
                source_assets=deepcopy(owned),source_anchor_sha256=hashlib.sha256(anchors.encode()).hexdigest(),
                before_html_sha256=hashlib.sha256(before.encode()).hexdigest(),after_html_sha256=hashlib.sha256(str(node).encode()).hexdigest(),
                primary_digits_unchanged=True,new_ocr_calls=0,method='source_xobject_unit_topology_and_exact_primary_cell_anchors')
            proofs.append(proof); manifest.setdefault('source_unit_compositions',[]).append(proof)
            api.diagnostic_event('SOURCE_UNIT_COMPOSITION_NORMALIZED',**proof)
    return proofs
# END source_unit_composition helpers

# BEGIN source_cell_ownership functions
def _source_cell_positions(cells):
    return {(c['row'],c['column'],c['row_span'],c['column_span']) for c in cells}

def _source_cell_shape_matches(profile,geometry):
    try:
        cells=geometry.get('cells') or [];current=profile.get('cells') or []
        physical=_source_cell_positions(cells);display=_source_cell_positions(current)
        bounds=geometry['bbox']
        valid_bounds=lambda box: (box and len(box)==4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in box)
            and box[0]<box[2] and box[1]<box[3])
        return bool(not profile['issues'] and cells and valid_bounds(bounds)
            and all(valid_bounds(c.get('bbox')) for c in cells)
            and all(bounds[0]-SOURCE_CELL_OWNERSHIP['bbox_epsilon_pt']<=c['bbox'][0]
                and bounds[1]-SOURCE_CELL_OWNERSHIP['bbox_epsilon_pt']<=c['bbox'][1]
                and c['bbox'][2]<=bounds[2]+SOURCE_CELL_OWNERSHIP['bbox_epsilon_pt']
                and c['bbox'][3]<=bounds[3]+SOURCE_CELL_OWNERSHIP['bbox_epsilon_pt'] for c in cells)
            and profile['rows']==len(geometry.get('y_grid') or [])-1
            and profile['columns']==len(geometry.get('x_grid') or [])-1
            and len(cells)==len(physical)==len(current)==len(display) and physical==display)
    except (KeyError,TypeError,ValueError,AttributeError):
        return False

def _source_cell_physical_html(cells, header_rows):
    """Capture the physical cells before any display-only subrow renumbering."""
    from bs4 import BeautifulSoup
    soup=BeautifulSoup('<table><thead></thead><tbody></tbody></table>','html.parser')
    rows=max((c['row']+c['row_span'] for c in cells),default=0)
    for y in range(rows):
        row=soup.new_tag('tr');(soup.thead if y<header_rows else soup.tbody).append(row)
        for cell in sorted((c for c in cells if c['row']==y),key=lambda c:c['column']):
            node=soup.new_tag('th' if y<header_rows else 'td')
            for attr,key in (('rowspan','row_span'),('colspan','column_span')):
                if cell[key]!=1:node[attr]=str(cell[key])
            content=cell.get('mapped_ocr_html')
            if content:
                content+=''.join(part for line in cell.get('lines',[]) for part in re.findall(r'<img\b[^>]*>',line.get('html','')))
            else:content='<br/>'.join(line.get('html','') for line in cell.get('lines',[]))
            node.append(BeautifulSoup(content,'html.parser'));row.append(node)
    if not header_rows:soup.thead.decompose()
    return str(soup.table)

def _source_cell_warning(manifest,api,reason,**fields):
    warning=dict(reason=reason,severity='warning',**fields)
    manifest.setdefault('source_cell_ownership_warnings',[]).append(warning)
    api.diagnostic_event('SOURCE_CELL_OWNERSHIP_UNCHANGED',**warning)

def prepare_source_cell_ownership(native,page,ref,manifest,api):
    """Select a coherent physical candidate before preserve_primary_cells runs."""
    from copy import deepcopy
    if not native:return native
    html,geometry=native
    profile=api.table_profile(html)
    if not geometry:
        _source_cell_warning(manifest,api,'physical_source_geometry_unavailable',page=page,ref=ref)
        return None
    physical=geometry.get('cells') or []
    valid=lambda p:_source_cell_shape_matches(p,geometry)
    if valid(profile):return native
    candidate=geometry.get('source_physical_html')
    proposed=api.table_profile(candidate) if candidate else None
    if not proposed or not valid(proposed):
        _source_cell_warning(manifest,api,'physical_source_candidate_unavailable',page=page,ref=ref,
            display_rows=profile['rows'],physical_rows=len(geometry.get('y_grid',[]))-1)
        return None  # Caller must retain primary HTML or complete source pixels, never publish hidden prose.
    proof=dict(page=page,ref=ref,display_rows=profile['rows'],physical_rows=proposed['rows'],
        display_html_sha256=hashlib.sha256(html.encode()).hexdigest(),
        physical_html_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
        reason='display_grid_differs_from_physical_source_cells',method='captured_physical_cells_before_subrow_display')
    geometry['source_display_grid']=dict(rows=profile['rows'],columns=profile['columns'],
        cell_positions=[list(tuple(c[k] for k in ('row','column','row_span','column_span'))) for c in profile['cells']],
        logical_subrows=deepcopy(geometry.get('logical_subrows') or []),html_sha256=proof['display_html_sha256'])
    geometry['logical_subrows']=[]  # Physical routes must not reuse stale display-row indices.
    geometry['source_physical_grid_selected']=True
    manifest.setdefault('source_cell_ownership_preparations',[]).append(proof)
    api.diagnostic_event('SOURCE_CELL_PHYSICAL_CANDIDATE_SELECTED',**proof)
    return candidate,geometry

def mask_source_owned_cells(fragment,geometry,source_image,region,page,manifest,api):
    """Keep complete source-cell pixels when primary payload has no owner."""
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(fragment,'html.parser')
    profile=api.table_profile(fragment)
    cells=(geometry or {}).get('cells') or []
    if not _source_cell_shape_matches(profile,geometry):
        _source_cell_warning(manifest,api,'physical_and_display_cell_identity_not_proven',page=page)
        return None,[]
    physical={(c['row'],c['column'],c['row_span'],c['column_span']):c for c in cells}
    nodes=[n for n in soup.table.find_all(['td','th']) if n.find_parent('table') is soup.table]
    proofs=[];retained=list(geometry.get('retained_cell_images',[]))
    sx,sy=source_image.width/(region[2]-region[0]),source_image.height/(region[3]-region[1])
    for node,position in zip(nodes,profile['cells']):
        key=tuple(position[k] for k in ('row','column','row_span','column_span'));source=physical[key]
        if source.get('primary_ocr'):
            continue  # Preserve primary words, numbers, MathML and images literally.
        from docling_pdf_text_cells import owns_positioned_text
        if owns_positioned_text(node,source,geometry):
            proof=dict(page=page,row=key[0],column=key[1],**source['positioned_source_text'])
            manifest.setdefault('positioned_source_text_cells_preserved',[]).append(proof)
            api.diagnostic_event('TABLE_POSITIONED_SOURCE_TEXT_PRESERVED',**proof)
            continue
        if not node.get_text(strip=True) and not node.find('math'):
            continue  # Already complete original native image, or a genuinely empty source cell.
        box=source.get('bbox')
        if not box or source_image is None:
            _source_cell_warning(manifest,api,'source_cell_pixels_unavailable',page=page,row=key[0],column=key[1])
            return None,[]
        from docling_pdf_cell_crop import cell_content_crop
        interior=cell_content_crop(source_image,region,box)
        if interior is None:
            _source_cell_warning(manifest,api,'source_cell_pixels_outside_region',page=page,row=key[0],column=key[1])
            return None,[]
        pixels,content_box,crop_proof=interior
        complete=bool(region[0]<=box[0] and region[1]<=box[1] and box[2]<=region[2] and box[3]<=region[3])
        if not complete:
            _source_cell_warning(manifest,api,'source_cell_bbox_exceeds_rendered_region',page=page,
                row=key[0],column=key[1],bbox=list(box),rendered_region_bbox=list(region))
        crop=source_image.crop(pixels)
        try:
            rendered=BeautifulSoup(api.image_html(crop,content_box[2]-content_box[0],'cell'),'html.parser').img
            rgb_sha256=hashlib.sha256(crop.convert('RGB').tobytes()).hexdigest()
        finally:crop.close()
        rendered['data-source-cell']='true'
        replaced_images=[hashlib.sha256(image.get('src','').encode()).hexdigest() for image in node.find_all('img')]
        before_sha256=hashlib.sha256(node.decode_contents().encode()).hexdigest()
        node.clear();node.append(rendered)
        source.update(html=node.decode_contents(),text='',primary_content_unmapped=True)
        retained=[entry for entry in retained if (entry.get('row'),entry.get('column'))!=key[:2]]
        retained.append(dict(row=key[0],column=key[1],bbox=list(content_box),content_crop=crop_proof,
            basis='complete_source_cell_without_mapped_primary_ocr' if complete else 'layout_limited_source_cell_pixels'))
        proof=dict(page=page,row=key[0],column=key[1],row_span=key[2],column_span=key[3],bbox=list(box),
            reason='primary_ocr_not_mapped_keep_complete_source_cell_pixels' if complete else 'source_cell_pixels_limited_by_layout_region',
            source_pixel_bbox=list(pixels),source_bbox_coverage_complete=complete,rendered_region_bbox=list(region),
            source_content_bbox=list(content_box),content_crop=crop_proof,
            source_rgb_sha256=rgb_sha256,before_html_sha256=before_sha256,
            replaced_native_image_sources_sha256=replaced_images,primary_payload_replaced=False)
        proofs.append(proof);api.diagnostic_event('SOURCE_CELL_COMPLETE_PIXELS_KEPT' if complete else 'SOURCE_CELL_LAYOUT_LIMITED_PIXELS_KEPT',**proof)
    geometry['retained_cell_images']=sorted(retained,key=lambda e:(e['row'],e['column']))
    manifest.setdefault('primary_ocr_unmapped_cells',[]).extend(proofs)
    manifest.setdefault('source_cell_complete_fallbacks',[]).extend(p for p in proofs if p['source_bbox_coverage_complete'])
    manifest.setdefault('source_cell_partial_fallbacks',[]).extend(p for p in proofs if not p['source_bbox_coverage_complete'])
    return str(soup.table),proofs

def source_owned_graphic_objects(fragment,geometry,objects,page,ref,manifest,api):
    """A source graphic needs its own cell anchors; equal dimensions prove nothing."""
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(fragment,'html.parser');profile=api.table_profile(fragment)
    source=(geometry or {}).get('cells') or []
    if not _source_cell_shape_matches(profile,geometry):
        if objects:_source_cell_warning(manifest,api,'graphic_projection_cell_identity_not_proven',page=page,ref=ref)
        return [],list(objects)
    physical={tuple(c[k] for k in ('row','column','row_span','column_span')):c for c in source}
    def text_key(text):
        text=re.sub(r'(?<=[A-Za-zА-Яа-яЁё])[-\u00ad][ \t]*\n[ \t]*(?=[a-zа-яё])','',text)
        return re.sub(r'\s+','',text)
    eligible=[];unmapped=[]
    for obj in objects:
        box=[obj['x0'],obj['top'],obj['x1'],obj['bottom']]
        owners=[cell for cell in source if cell.get('bbox') and cell['bbox'][0]<=box[0] and cell['bbox'][1]<=box[1]
            and box[2]<=cell['bbox'][2] and box[3]<=cell['bbox'][3]]
        if len(owners)==1:
            owner=owners[0];key=tuple(owner[k] for k in ('row','column','row_span','column_span'))
            primary=next(c for c in profile['cells'] if tuple(c[k] for k in ('row','column','row_span','column_span'))==key)
            source_text='\n'.join(line.get('text','') for line in owner.get('lines',[]))
            if source_text.strip() and text_key(source_text)==text_key(primary['text']):
                eligible.append(obj);continue
        unmapped.append(obj)
        _source_cell_warning(manifest,api,'graphic_source_anchors_not_unique',page=page,ref=ref,bbox=box)
    return eligible,unmapped

# END source_cell_ownership functions

# BEGIN source_scripts functions
import re

from bs4 import BeautifulSoup

def _source_script_fold(text):
    text = text.translate(str.maketrans({'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E',
        'Н': 'H', 'К': 'K', 'М': 'M', 'О': 'O', 'Р': 'P', 'Т': 'T', 'Х': 'X',
        'а': 'a', 'с': 'c', 'е': 'e', 'о': 'o', 'р': 'p', 'х': 'x'}))
    return ''.join(c for c in text.casefold() if c.isalnum())

def _source_script_line(fragment):
    parsed = BeautifulSoup(fragment, 'html.parser')
    if parsed.find(['math', 'img', 'svg', 'table']):
        return None
    subs = parsed.find_all(['sub', 'sup'])
    if len(subs) != 1 or subs[0].find(True):
        return None
    sub = subs[0]
    role = sub.name
    marker = sub.get_text('', strip=True)
    if not re.fullmatch(r'[A-Za-zА-Яа-яЁё]{1,'+str(_SOURCE_SCRIPT_MAX_LETTERS)+'}', marker):
        return None
    # A simple engineering identifier, not a chemical formula or prose.
    siblings = list(parsed.contents)
    if sub not in siblings:
        return None
    index = next(i for i, node in enumerate(siblings) if node is sub)
    before = ''.join(n.get_text() if getattr(n, 'name', None) else str(n) for n in siblings[:index]).strip()
    after = ''.join(n.get_text() if getattr(n, 'name', None) else str(n) for n in siblings[index+1:]).strip()
    if not re.fullmatch(r'[A-Za-zА-Яа-яЁё]{1,'+str(_SOURCE_SCRIPT_MAX_LETTERS)+'}', before):
        return None
    if not re.fullmatch(r'\d{'+str(_SOURCE_SCRIPT_MIN_DIGITS)+r',}[,;]?', after):
        return None
    sub.decompose()
    return dict(role=role, signature=(role, _source_script_fold(before), _source_script_fold(marker), re.sub(r'\D', '', after)),
                without_script=str(parsed), skeleton=_source_script_fold(parsed.get_text()),
                full=_source_script_fold(BeautifulSoup(fragment, 'html.parser').get_text()))

def _source_script_numeric_ink(mask, box, sx, sy, glyphs, font):
    import numpy as np
    digits = sorted((g for g in glyphs if g['text'].isdigit()), key=lambda g: g['x0'])
    if len(digits) < _SOURCE_SCRIPT_MIN_DIGITS:
        return None
    # A suffix digit away from the index and terminal punctuation anchors the
    # real raster baseline; hidden PDF font boxes alone do not prove a script.
    glyph = digits[-2]
    x0 = max(0, round((glyph['x0']-box[0]-_SOURCE_SCRIPT_DIGIT_X_MARGIN_FONT*font)*sx))
    x1 = min(mask.shape[1], round((glyph['x1']-box[0]+_SOURCE_SCRIPT_DIGIT_X_MARGIN_FONT*font)*sx))
    y0 = max(0, round((glyph['top']-box[1]-_SOURCE_SCRIPT_DIGIT_TOP_MARGIN_FONT*font)*sy))
    y1 = min(mask.shape[0], round((glyph['bottom']-box[1]+_SOURCE_SCRIPT_DIGIT_X_MARGIN_FONT*font)*sy))
    if x1 <= x0 or y1 <= y0:
        return None
    yy, xx = np.nonzero(mask[y0:y1, x0:x1])
    if len(xx) < 3:
        return None
    return dict(top=box[1]+(y0+int(yy.min()))/sy,
                bottom=box[1]+(y0+int(yy.max())+1)/sy,
                digits=digits, height=(int(yy.max())-int(yy.min())+1)/sy)

def _source_script_fields(image, box, source_lines, source_glyphs, role='sub'):
    import cv2
    import numpy as np
    from statistics import median
    gray = image.convert('L')
    try:
        mask = cv2.threshold(np.asarray(gray), 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    finally:
        gray.close()
    sx, sy = image.width/(box[2]-box[0]), image.height/(box[3]-box[1])
    anchors = []
    for line in source_lines:
        glyphs = [g for g in source_glyphs if g['text'].strip() and
                  abs(g['bottom']-line['y']) <= _SOURCE_SCRIPT_LINE_BASELINE_TOLERANCE_FONT*max(g['size'], line['size'])]
        font = median(g['size'] for g in glyphs) if glyphs else 0
        numeric = _source_script_numeric_ink(mask, box, sx, sy, glyphs, font) if font else None
        anchors.append((glyphs, font, numeric))
    fields = []
    for index, (glyphs, font, numeric) in enumerate(anchors):
        if not numeric or any(not g.get('upright', True) for g in glyphs):
            fields.append(None); continue
        bases = [g for g in glyphs if g['text'].isalpha() and g['x0'] < numeric['digits'][0]['x0']]
        if not bases:
            fields.append(None); continue
        if role == 'sub':
            lower = numeric['bottom']+max(1/sy, numeric['height']*_SOURCE_SCRIPT_BASELINE_CLEARANCE_HEIGHT)
            bottom = min(box[3]-_SOURCE_SCRIPT_CELL_FRAME_MARGIN,
                         numeric['bottom']+font*_SOURCE_SCRIPT_MAX_DEPTH_FONT)
            if index+1 < len(anchors) and anchors[index+1][2]:
                bottom = min(bottom, anchors[index+1][2]['top']-1/sy)
        else:
            bottom = numeric['top']-max(1/sy, numeric['height']*_SOURCE_SCRIPT_BASELINE_CLEARANCE_HEIGHT)
            lower = max(box[1]+_SOURCE_SCRIPT_CELL_FRAME_MARGIN,
                        numeric['top']-font*_SOURCE_SCRIPT_MAX_DEPTH_FONT)
            if index and anchors[index-1][2]:
                lower = max(lower, anchors[index-1][2]['bottom']+1/sy)
        left = max(box[0]+_SOURCE_SCRIPT_CELL_FRAME_MARGIN, min(g['x0'] for g in bases)-font*_SOURCE_SCRIPT_IDENTIFIER_SIDE_MARGIN_FONT)
        right = min(box[2]-_SOURCE_SCRIPT_CELL_FRAME_MARGIN, numeric['digits'][0]['x0']+font*_SOURCE_SCRIPT_IDENTIFIER_SIDE_MARGIN_FONT)
        x0, x1 = max(0, round((left-box[0])*sx)), min(mask.shape[1], round((right-box[0])*sx))
        y0, y1 = max(0, round((lower-box[1])*sy)), min(mask.shape[0], round((bottom-box[1])*sy))
        if x1 <= x0 or y1-y0 < max(2, numeric['height']*_SOURCE_SCRIPT_MIN_FIELD_HEIGHT_GLYPH*sy):
            fields.append(None); continue
        pixels = int(np.count_nonzero(mask[y0:y1, x0:x1]))
        required = max(3, round(font*font*sx*sy*_SOURCE_SCRIPT_MIN_AREA_FONT2))
        state = 'absent' if pixels == 0 else 'present' if pixels >= required else 'ambiguous'
        if role == 'sup' and pixels and np.count_nonzero(mask[y0, x0:x1]):
            state = 'ambiguous'  # Ink arriving from the previous line is not a raised index.
        fields.append(dict(state=state, ink_pixels=pixels, minimum_present_pixels=required,
                           role=role, baseline=numeric['bottom'], bbox=[left, lower, right, bottom]))
    return fields

def _source_script_warning(manifest, api, **record):
    record['severity'] = 'warning'
    manifest.setdefault('source_script_warnings', []).append(record)
    api.diagnostic_event('SOURCE_SCRIPT_DUPLICATE_UNCHANGED', **record)

def repair_source_script_duplicates(soup, manifest, regions, reader, api):
    """Remove only a primary duplicate index whose source lower field is empty."""
    from hashlib import sha256
    if reader is None:
        return []
    entries = {e['ref']: e for e in manifest.get('tables', [])}
    sources = {}
    for ref, entry in entries.items():
        owner, seen = ref, set()
        while entries.get(owner, {}).get('joined_into'):
            if owner in seen:
                owner = None; break
            seen.add(owner); owner = entries[owner]['joined_into']
        geometry = entry.get('pdf_geometry') or {}
        if not owner or not geometry.get('source_grid_verified') or geometry.get('hidden_ocr_direction_conflict'):
            continue
        for cell in geometry.get('cells', []):
            if not cell.get('source_text_unreliable') and cell.get('bbox'):
                sources.setdefault((owner, cell['column']), []).append((entry['page'], cell))
    proofs = manifest.setdefault('source_script_repairs', [])
    pages = {}
    for table in soup.select('table[data-pdf-table-ref]'):
        profile = api.table_profile(str(table))
        nodes = [n for n in table.find_all(['td','th']) if n.find_parent('table') is table]
        for position, cell in zip(profile.get('cells', []), nodes):
            if cell.find(['math', 'img', 'svg']) or '<br' not in cell.decode_contents():
                continue
            lines = re.split(r'<br\s*/?>', cell.decode_contents(), flags=re.I)
            terms = [_source_script_line(line) for line in lines]
            pairs = [i for i in range(len(terms)-1) if terms[i] and terms[i+1]
                     and terms[i]['signature'] == terms[i+1]['signature']]
            if not pairs:
                continue
            matches = []
            for page, source in sources.get((table['data-pdf-table-ref'], position['column']), []):
                if page not in pages:
                    reader.load_page(page); pages[page] = list(reader.chars)
                box = source['bbox']
                glyphs = [g for g in pages[page] if box[0] < (g['x0']+g['x1'])/2 < box[2]
                          and box[1] < (g['top']+g['bottom'])/2 < box[3]]
                native = api.native_cell_lines(glyphs)
                if len(native) != len(lines):
                    continue
                if not all(_source_script_fold(n['text']) in
                           ({term['skeleton'], term['full']} if term else
                            {_source_script_fold(BeautifulSoup(raw, 'html.parser').get_text())})
                           for n, raw, term in zip(native, lines, terms)):
                    continue
                image = regions.crop(page, box)
                try:
                    fields = {role: _source_script_fields(image, box, native, glyphs, role)
                              for role in {terms[i]['role'] for i in pairs}}
                finally:
                    image.close()
                matches.append((page, source, fields))
            if len(matches) != 1:
                _source_script_warning(manifest, api, ref=table['data-pdf-table-ref'],
                    row=position['row'], column=position['column'], reason='source_line_ownership_not_unique', matches=len(matches))
                continue
            page, source, fields = matches[0]
            repaired = []
            repaired_indices = set()
            for index in pairs:
                pair = fields[terms[index]['role']][index:index+2]
                if len(pair) != 2 or not all(pair) or {p['state'] for p in pair} != {'present', 'absent'}:
                    _source_script_warning(manifest, api, ref=table['data-pdf-table-ref'],
                        row=position['row'], column=position['column'], reason='lower_source_ink_not_decisive')
                    continue
                missing = index if pair[0]['state'] == 'absent' else index+1
                if missing in repaired_indices:
                    continue
                original = lines[missing]
                lines[missing] = terms[missing]['without_script']
                repaired.append(dict(line=missing, original_sha256=sha256(original.encode()).hexdigest(),
                    role=terms[missing]['role'],
                    source_page=page, source_cell_bbox=source['bbox'], source_fields=pair,
                    method='paired_primary_identifier_and_present_vs_empty_source_script_ink'))
                repaired_indices.add(missing)
            if repaired:
                cell.clear(); cell.append(BeautifulSoup('<br/>'.join(lines), 'html.parser'))
                proof = dict(ref=table['data-pdf-table-ref'], row=position['row'], column=position['column'], repairs=repaired)
                proofs.append(proof); api.diagnostic_event('SOURCE_SCRIPT_DUPLICATE_REPAIRED', **proof)
    return proofs
# END source_scripts functions


# BEGIN table_notes functions
import hashlib

import re

from bs4 import BeautifulSoup

def _primary_note_span(node):
    try:
        return int(node.get('colspan', 1))
    except (TypeError, ValueError):
        return 0

def _primary_note_key(fragment, api):
    from docling_pdf_notes import _semantic_key
    parsed = BeautifulSoup(api.render_surya_math(fragment), 'html.parser')
    images = tuple((n.name, n.get('src'), n.get('data')) for n in parsed.find_all(['img', 'object']))
    return _semantic_key(str(parsed), api), images

def _primary_note_start(node):
    text = node.get_text(' ', strip=True)
    number = re.match(r'^(\d{1,2})\)\s*(?=\S)', text)
    if number:
        return number[1]
    if re.match(r'^п\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]\b', text, re.I):
        return 'note'
    return None

def _primary_note_references(table):
    refs = set()
    for cell in table.find_all(['td', 'th']):
        if cell.find_parent('table') is not table:
            continue
        if _primary_note_span(cell) > 1 and _primary_note_start(cell):
            continue
        for marker in cell.find_all('sup'):
            if match := re.fullmatch(r'\s*(\d{1,2}(?:\s*,\s*\d{1,2})*)\)\s*', marker.get_text()):
                refs.update(re.findall(r'\d{1,2}', match[1]))
        for marker in cell.select('[data-footnote-reference]'):
            value = marker.get('data-footnote-reference', '')
            if re.fullmatch(r'\d{1,2}', value) and marker.get_text('', strip=True) == value+')':
                refs.add(value)
        text = cell.get_text(' ', strip=True)
        for match in re.finditer(r'(?:^|\s)(\d{1,2})\)\s*$', text):
            refs.add(match[1])
    return refs

def _primary_note_paragraphs(nodes, *, note_numbering=False):
    """Only explicit paragraph/line boundaries can introduce another note."""
    paragraphs = []
    note_context = False
    for source in nodes:
        content = BeautifulSoup(str(source), 'html.parser')
        root = content.find(['p', 'div']) or content
        pieces = []
        current = []
        for child in list(root.contents):
            if getattr(child, 'name', None) == 'br':
                probe = BeautifulSoup(''.join(str(n) for n in current), 'html.parser')
                pieces.append(probe)
                current = []
            else:
                current.append(child)
        pieces.append(BeautifulSoup(''.join(str(n) for n in current), 'html.parser'))
        grouped = []
        for piece in pieces:
            if not piece.get_text(strip=True) and not piece.find(['math', 'img']):
                continue
            start = _primary_note_start(piece)
            if start == 'note':
                note_context = True
            bare_number = note_numbering and note_context and re.match(r'^\d{1,2}\s+\S', piece.get_text(' ', strip=True))
            if start or bare_number or not grouped:
                grouped.append([piece])
            else:
                grouped[-1].append(piece)
        for group in grouped:
            p = content.new_tag('p')
            for index, piece in enumerate(group):
                if index:
                    p.append(content.new_tag('br'))
                for child in list(piece.contents):
                    p.append(child.extract())
            paragraphs.append(p)
    return paragraphs

def _primary_note_owner(ref, entries):
    seen = set()
    while entries.get(ref, {}).get('joined_into'):
        if ref in seen:
            return None
        seen.add(ref)
        ref = entries[ref]['joined_into']
    return ref

def _primary_note_table_match(bounds, entry, source_font=None):
    box = entry.get('bbox')
    if not box or len(bounds) != 4:
        return False
    width, height = box[2]-box[0], box[3]-box[1]
    margin = PRIMARY_NOTE_BBOX_MARGIN_EM*source_font if source_font else 4.0
    tolerance = max(3.0, PRIMARY_NOTE_X_TOLERANCE*width, margin if source_font else 0)
    return (width > 0 and height > 0 and abs(bounds[0]-box[0]) <= tolerance
            and abs(bounds[2]-box[2]) <= tolerance
            and abs(bounds[1]-box[1]) <= max(margin, PRIMARY_NOTE_TOP_TOLERANCE*height)
            and bounds[3] >= box[3]-margin)

def _primary_note_source_font(entry, reader):
    if reader is None or entry.get('pdf_geometry', {}).get('hidden_ocr_direction_conflict'):
        return None
    from statistics import median
    reader.load_page(entry['page'])
    box = entry['bbox']
    sizes = [g['size'] for g in reader.chars if g.get('size', 0) > 0 and g.get('text', '').strip()
             and box[0] < (g['x0']+g['x1'])/2 < box[2] and box[1] < (g['top']+g['bottom'])/2 < box[3]]
    return median(sizes) if sizes else None

def _primary_note_adjacent_duplicate(table, fragment, api):
    """Only an exact ordered prose/math/image duplicate may be removed."""
    target = _primary_note_key(fragment, api)
    container = table.parent if 'table-scroll' in table.parent.get('class', []) else table
    nodes, combined = [], ''
    for node in container.next_siblings:
        if not getattr(node, 'name', None):
            if str(node).strip():
                break
            continue
        if node.name not in {'p', 'div'} or node.find(['table', 'h1', 'h2', 'h3', 'h4']):
            break
        nodes.append(node)
        combined += str(node)
        if _primary_note_key(combined, api) == target:
            return nodes
        # No arbitrary document scan: only the directly following paragraph run.
        if len(nodes) >= PRIMARY_NOTE_MAX_ADJACENT_PARAGRAPHS:
            break
    return []

def _primary_note_warning(manifest, api, **record):
    record['severity'] = 'warning'
    warnings = manifest.setdefault('primary_table_note_warnings', [])
    if record not in warnings:
        warnings.append(record)
        api.diagnostic_event('PRIMARY_TABLE_NOTES_UNCHANGED', **record)

def _primary_note_resolve_footer_warning(manifest, api, ref):
    active = manifest.get('primary_table_note_warnings', [])
    for warning in list(active):
        if warning.get('ref') == ref and warning.get('reason') == 'unreferenced_or_non_note_footer_boundaries':
            active.remove(warning)
            resolved = dict(warning, resolved_by='validated_later_footer_pass')
            manifest.setdefault('primary_table_note_resolved_warnings', []).append(resolved)
            api.diagnostic_event('PRIMARY_TABLE_NOTE_WARNING_RESOLVED', **resolved)

def _primary_note_remove(nodes, table, source, manifest, api, action):
    ref = table.get('data-pdf-table-ref')
    entry = next((e for e in manifest.get('tables', []) if e.get('ref') == ref), {})
    for node in nodes:
        payload = str(node)
        proof = dict(table_ref=ref, action=action, html=payload, text=node.get_text(' ', strip=True),
            html_sha256=hashlib.sha256(payload.encode()).hexdigest(), node_attrs=dict(node.attrs),
            source_context=dict(source), table_bbox=entry.get('bbox'), page=source.get('page', entry.get('page')))
        manifest.setdefault('primary_table_note_removals', []).append(proof)
        node.decompose()
        api.diagnostic_event('PRIMARY_TABLE_NOTE_PAYLOAD_REMOVED', ref=ref, page=proof['page'],
            action=action, html_sha256=proof['html_sha256'])
    return len(nodes)

def _primary_note_render_existing_math(fragment, api, warn):
    """Serialize existing primary LaTeX after a late note ownership transfer."""
    from html import escape
    parsed = BeautifulSoup(fragment, 'html.parser')
    for node in list(parsed.find_all('math')):
        if node.find(True):
            continue
        latex = node.get_text().strip()
        if not latex:
            continue
        rendered = api.render_surya_math(escape(str(node), quote=False))
        converted = BeautifulSoup(rendered, 'html.parser').find_all('math')
        if len(converted) != 1 or not converted[0].find(True) or converted[0].get('data-latex') != latex:
            warn(reason='unrenderable_primary_note_math',
                 primary_math_sha256=hashlib.sha256(str(node).encode()).hexdigest())
            continue
        node.replace_with(converted[0])
    return str(parsed)

def _primary_note_install(table, paragraphs, source, manifest, api, *, remove_duplicates=False):
    fragment = ''.join(str(p) for p in paragraphs)
    duplicate_fragment = fragment
    target_key = _primary_note_key(fragment, api)
    profile = api.table_profile(str(table))
    width = profile.get('columns', 0)
    if profile.get('issues') or not width:
        _primary_note_warning(manifest, api, ref=table.get('data-pdf-table-ref'), reason='ambiguous_table_width')
        return False
    rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
    last = rows[-1].find_all(['td', 'th'], recursive=False) if rows else []
    footer = last[0] if len(last) == 1 and _primary_note_span(last[0]) == width and _primary_note_start(last[0]) else None
    existing = _primary_note_key(str(footer), api) if footer else None
    already_owned = footer is not None and existing == target_key
    if footer and existing == target_key and len(footer.find_all('p', recursive=False)) == len(paragraphs):
        if not remove_duplicates:
            return False
        duplicates = _primary_note_adjacent_duplicate(table, fragment, api)
        removed = _primary_note_remove(duplicates, table, source, manifest, api, 'remove_existing_duplicate')
        if removed:
            manifest.setdefault('primary_table_note_repairs', []).append(dict(ref=table.get('data-pdf-table-ref'),
                paragraphs=len(paragraphs), colspan=width, removed_exact_duplicates=removed, **source))
        return bool(duplicates)
    if footer and existing != target_key:
        prefixes = [_primary_note_key(''.join(str(p) for p in paragraphs[:count]), api)
                    for count in range(1, len(paragraphs))]
        if existing not in prefixes:
            old = _primary_note_footer_paragraphs(footer)
            before = [_primary_note_start(p) for p in old]
            after = [_primary_note_start(p) for p in paragraphs]
            if (before and after and all(v and v.isdigit() for v in before+after)
                    and int(after[0]) == int(before[-1])+1):
                paragraphs = old+paragraphs
            else:
                _primary_note_warning(manifest, api, ref=table.get('data-pdf-table-ref'),
                    reason='conflicting_existing_footer')
                return False
    duplicates = _primary_note_adjacent_duplicate(table, duplicate_fragment, api) if not already_owned or remove_duplicates else []
    if footer is None:
        parsed = BeautifulSoup('<tr><td></td></tr>', 'html.parser')
        row, footer = parsed.tr, parsed.td
        footer['colspan'] = str(width)
        body = table.find('tfoot', recursive=False) or table.find('tbody', recursive=False) or table
        body.append(row)
    else:
        footer.clear()
    footer['data-primary-table-notes'] = 'true'
    for paragraph in paragraphs:
        p = BeautifulSoup(str(paragraph), 'html.parser').p
        p['class'] = list(dict.fromkeys([*p.get('class', []), 'source-table-note']))
        p['style'] = 'text-indent:1.25em;margin:.35em 0'
        footer.append(p)
    _primary_note_remove(duplicates, table, source, manifest, api,
        'remove_existing_duplicate' if already_owned else 'ownership_transfer')
    proof = dict(ref=table.get('data-pdf-table-ref'), paragraphs=len(paragraphs), colspan=width,
                 removed_exact_duplicates=len(duplicates), **source)
    manifest.setdefault('primary_table_note_repairs', []).append(proof)
    api.diagnostic_event('PRIMARY_TABLE_NOTES_ATTACHED', **proof)
    return True

def _primary_note_footer_paragraphs(cell):
    paragraphs = cell.find_all('p', recursive=False)
    if paragraphs:
        return _primary_note_paragraphs(paragraphs, note_numbering=True)
    wrapper = BeautifulSoup('<p>'+cell.decode_contents()+'</p>', 'html.parser')
    return _primary_note_paragraphs([wrapper.p], note_numbering=True)

def _primary_note_footer_starts(paragraphs):
    starts, note_context, expected = [], False, 1
    for paragraph in paragraphs:
        start = _primary_note_start(paragraph)
        if start == 'note':
            note_context, expected = True, 1
        elif start is None and note_context:
            bare = re.match(r'^(\d{1,2})\s+\S', paragraph.get_text(' ', strip=True))
            if bare and int(bare[1]) == expected:
                start, expected = 'note', expected+1
        starts.append(start)
    return starts

def _primary_note_near_record(record, entry):
    box, bounds = entry.get('bbox'), record.get('bbox')
    if not box or not bounds or record.get('page_no') != entry.get('page'):
        return False
    width, height = box[2]-box[0], box[3]-box[1]
    return (width > 0 and height > 0 and -2 <= bounds[1]-box[3] <= max(4., PRIMARY_NOTE_MAX_GAP*height)
            and box[0]-3 <= bounds[0] <= box[0]+max(6., width*.06)
            and bounds[2] <= box[2]+max(3., width*PRIMARY_NOTE_X_TOLERANCE))

def _primary_note_standalone(soup, manifest, records, entries, api, *, remove_duplicates=False):
    for entry in entries.values():
        owner = _primary_note_owner(entry['ref'], entries)
        table = soup.find('table', attrs={'data-pdf-table-ref': owner}) if owner else None
        if table is None:
            continue
        refs = _primary_note_references(table)
        ordered = sorted((r for r in records if r.get('page_no') == entry.get('page') and r.get('bbox')
                          and r['bbox'][1] >= entry['bbox'][3]-2), key=lambda r: (r['bbox'][1], r['bbox'][0]))
        chosen, paragraphs, bottom = [], [], entry['bbox'][3]
        seen = set()
        for record in ordered:
            identity = (tuple(record['bbox']), record.get('html', ''))
            if identity in seen:
                continue
            seen.add(identity)
            if record.get('label', '').lower() not in {'text', 'footnote', 'listitem'}:
                break
            relative = dict(entry, bbox=[*entry['bbox'][:3], bottom])
            if not _primary_note_near_record(record, relative):
                break
            parsed = BeautifulSoup(record.get('html', ''), 'html.parser')
            if parsed.find(['table', 'h1', 'h2', 'h3', 'h4']):
                break
            parts = _primary_note_paragraphs(parsed.find_all(['p', 'div'], recursive=False))
            starts = [_primary_note_start(p) for p in parts]
            if not parts or any(s is None or s != 'note' and s not in refs for s in starts):
                break
            chosen.append(record)
            paragraphs.extend(parts)
            bottom = record['bbox'][3]
        if not chosen:
            continue
        numbers = [int(s) for p in paragraphs if (s := _primary_note_start(p)) != 'note']
        if numbers and numbers != list(range(numbers[0], numbers[0]+len(numbers))):
            _primary_note_warning(manifest, api, ref=owner, page=entry['page'],
                reason='nonconsecutive_numbered_primary_notes')
            continue
        if numbers and len(numbers) == 1 and chosen[0].get('label', '').lower() != 'footnote':
            _primary_note_warning(manifest, api, ref=owner, page=entry['page'],
                reason='ambiguous_single_numbered_text_note')
            continue
        candidates = {_primary_note_owner(e['ref'], entries) for e in entries.values()
                      if _primary_note_near_record(chosen[0], e)}
        candidates.discard(None)
        if candidates != {owner}:
            _primary_note_warning(manifest, api, ref=owner, reason='ambiguous_standalone_note_owner')
            continue
        fragment = ''.join(str(p) for p in paragraphs)
        if not _primary_note_adjacent_duplicate(table, fragment, api):
            _primary_note_warning(manifest, api, ref=owner, reason='standalone_notes_not_direct_dom_siblings')
            continue
        _primary_note_install(table, paragraphs, dict(page=entry['page'],
            record_sha256s=[hashlib.sha256(r.get('html', '').encode()).hexdigest() for r in chosen],
            source_bboxes=[r['bbox'] for r in chosen],
            method='referenced_positioned_primary_note_siblings'), manifest, api, remove_duplicates=remove_duplicates)

def _primary_note_normalize_footers(soup, manifest, api, *, remove_duplicates=False):
    for table in soup.select('table[data-pdf-table-ref]'):
        profile = api.table_profile(str(table))
        rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
        cells = rows[-1].find_all(['td', 'th'], recursive=False) if rows else []
        if profile.get('issues') or len(cells) != 1 or _primary_note_span(cells[0]) != profile.get('columns'):
            continue
        paragraphs = _primary_note_footer_paragraphs(cells[0])
        refs = _primary_note_references(table)
        starts = _primary_note_footer_starts(paragraphs)
        if not paragraphs or any(s is None or s != 'note' and s not in refs for s in starts):
            if starts and starts[0] is not None and ('note' in starts or any(s in refs for s in starts)):
                _primary_note_warning(manifest, api, ref=table.get('data-pdf-table-ref'),
                    reason='unreferenced_or_non_note_footer_boundaries')
            continue
        _primary_note_install(table, paragraphs, dict(method='existing_primary_note_cell_explicit_boundaries'), manifest, api,
                              remove_duplicates=remove_duplicates)
        # Standalone primary notes arrive after the general formula serializer.
        # Format their existing LaTeX only after note ownership is validated.
        previous = str(cells[0])
        formatted = _primary_note_render_existing_math(previous, api,
            lambda **details: _primary_note_warning(manifest, api, ref=table.get('data-pdf-table-ref'), **details))
        if formatted != previous:
            replacement = BeautifulSoup(formatted, 'html.parser').td
            if replacement is not None:
                cells[0].replace_with(replacement)
                proof = dict(ref=table.get('data-pdf-table-ref'),
                    before_sha256=hashlib.sha256(previous.encode()).hexdigest(),
                    after_sha256=hashlib.sha256(formatted.encode()).hexdigest(),
                    method='serialize_existing_primary_note_latex')
                manifest.setdefault('primary_table_note_math_formatting', []).append(proof)
                api.diagnostic_event('PRIMARY_TABLE_NOTE_MATH_FORMATTED', **proof)
        _primary_note_resolve_footer_warning(manifest, api, table.get('data-pdf-table-ref'))

def attach_primary_table_notes(soup, manifest, records, api, *, reader=None, remove_duplicates=False):
    """Keep table-owned primary note paragraphs in a final full-width row."""
    entries = {e['ref']: e for e in manifest.get('tables', [])}
    handled = set()
    for record in records:
        if record.get('label', '').lower() != 'table' or not record.get('bbox'):
            continue
        parsed = BeautifulSoup(record.get('html', ''), 'html.parser')
        if len(parsed.find_all('table')) != 1:
            continue
        nodes = [n for n in parsed.table.next_siblings if str(n).strip()]
        if not nodes or any(getattr(n, 'name', None) not in {'p', 'div'} for n in nodes):
            continue
        paragraphs = _primary_note_paragraphs(nodes)
        if not paragraphs:
            continue
        starts = [_primary_note_start(p) for p in paragraphs]
        owners = {_primary_note_owner(ref, entries) for ref, entry in entries.items()
                  if entry.get('page') == record.get('page_no') and _primary_note_table_match(record['bbox'], entry)}
        owners.discard(None)
        owner_evidence = {}
        raw_refs = _primary_note_references(parsed.table)
        numbered = {s for s in starts if s and s.isdigit()}
        if not owners and numbered and numbered == raw_refs and all(s == 'note' or s in raw_refs for s in starts):
            for ref, entry in entries.items():
                if entry.get('page') != record.get('page_no'):
                    continue
                font = _primary_note_source_font(entry, reader)
                owner = _primary_note_owner(ref, entries)
                target = soup.find('table', attrs={'data-pdf-table-ref': owner}) if owner else None
                if (font and target is not None and _primary_note_references(target) == raw_refs
                        and _primary_note_table_match(record['bbox'], entry, font)):
                    owners.add(owner)
                    owner_evidence[owner] = dict(bbox_match_method='source_font_containment_margin', source_font=font,
                        bbox_margin=font*PRIMARY_NOTE_BBOX_MARGIN_EM, matched_table_bbox=entry['bbox'])
        if len(owners) != 1:
            if all(s is not None for s in starts) and ('note' in starts or all(s in raw_refs for s in starts)):
                _primary_note_warning(manifest, api, page=record.get('page_no'),
                    reason='ambiguous_compound_note_owner', owners=sorted(owners))
            continue
        owner = next(iter(owners))
        table = soup.find('table', attrs={'data-pdf-table-ref': owner})
        if table is None:
            continue
        refs = _primary_note_references(parsed.table) | _primary_note_references(table)
        if any(start is None or start != 'note' and start not in refs for start in starts):
            if starts[0] is not None and ('note' in starts or any(s in refs for s in starts)):
                _primary_note_warning(manifest, api, page=record.get('page_no'),
                    reason='unreferenced_or_non_note_primary_tail')
            continue
        identity = (owner, _primary_note_key(''.join(str(p) for p in paragraphs), api))
        if identity in handled:
            continue
        handled.add(identity)
        try:
            rendered = api.render_surya_math(''.join(str(p) for p in paragraphs))
            protected = BeautifulSoup(rendered, 'html.parser')
            graphics = {}
            for image in list(protected.find_all('img')):
                if not image.get('src', '').startswith('data:image/'):
                    raise ValueError('Primary note image has no embedded source pixels')
                marker = '\ue000'+hashlib.sha256(str(image).encode()).hexdigest()+'\ue001'
                retained = BeautifulSoup(str(image), 'html.parser').img
                for attr in list(retained.attrs):
                    if attr.lower().startswith('on'):
                        del retained[attr]
                graphics[marker] = str(retained)
                image.replace_with(marker)
            clean = api.clean_table_html('<table><tr><td>'+str(protected)+'</td></tr></table>')
            for marker, image in graphics.items():
                clean = clean.replace(marker, image)
            sanitized = BeautifulSoup(clean, 'html.parser').td
            clean_paragraphs = _primary_note_paragraphs(sanitized.find_all('p', recursive=False))
        except (ValueError, TypeError) as error:
            _primary_note_warning(manifest, api, page=record.get('page_no'),
                reason='primary_note_payload_cannot_be_sanitized', detail=str(error))
            continue
        if not clean_paragraphs or _primary_note_key(str(sanitized), api) != _primary_note_key(rendered, api):
            _primary_note_warning(manifest, api, page=record.get('page_no'),
                reason='primary_note_payload_changed_during_sanitization')
            continue
        _primary_note_install(table, clean_paragraphs, dict(page=record['page_no'],
            record_sha256=hashlib.sha256(record['html'].encode()).hexdigest(),
            source_bbox=record['bbox'], **owner_evidence.get(owner, {}),
            method='referenced_compound_primary_table_tail'), manifest, api, remove_duplicates=remove_duplicates)
    _primary_note_standalone(soup, manifest, records, entries, api, remove_duplicates=remove_duplicates)
    _primary_note_normalize_footers(soup, manifest, api, remove_duplicates=remove_duplicates)
    return manifest.get('primary_table_note_repairs', [])
# END table_notes functions


LOCAL_CSS = """
html{background:#eef0f2}body{font-family:Arial,sans-serif;line-height:1.5;max-width:1180px;
margin:0 auto;padding:28px;background:white;color:#15191e}p,li{overflow-wrap:anywhere}
.table-scroll{max-width:100%;overflow-x:auto;margin:1.2rem 0}table{border-collapse:collapse;
width:max-content;min-width:100%;max-width:none}td,th{border:1px solid #999;padding:.45em .65em;
vertical-align:top;white-space:normal;min-width:5em;max-width:26em}th{background:#eef2f6}
table.pdf-native{width:100%;table-layout:fixed}table.pdf-native td,table.pdf-native th{
min-width:0;max-width:none;overflow-wrap:anywhere}
caption{text-align:left;font-weight:600;margin-bottom:.5em}figure{margin:1.2em 0}
td p.source-table-note{margin:.35em 0;text-indent:1.25em}
img{max-width:100%;height:auto}math[display=block]{display:block;text-align:center;margin:1em 0;
overflow-x:auto;padding:.4em}figure.source-formula{text-align:center}.source-table img{max-width:none}
@page{size:A4;margin:15mm}@page wide{size:A4 landscape;margin:12mm}
@media print{html,body{background:white;max-width:none;padding:0;box-shadow:none}
.table-scroll{overflow:visible}.wide{page:wide;break-before:page;break-after:page}
table{width:100%;min-width:0}table.pdf-native{min-width:0!important}td,th{font-size:9pt;min-width:0;padding:.25em}
thead{display:table-header-group}tr{break-inside:avoid}.source-table img{max-width:100%}}
"""


def export_local_html(args, result, normalized, orientation, source):
    from bs4 import BeautifulSoup
    from docling_core.transforms.serializer.html import HTMLDocSerializer, HTMLParams, HTMLTableSerializer, HTMLTextSerializer, HTMLPictureSerializer
    from docling_core.transforms.serializer.common import create_ser_result
    from docling_core.types.doc import DocItemLabel, ImageRefMode, TableCell, TableData, Size
    from docling_pdf_repairs import enabled as pdf_repair_enabled

    doc = result.document
    original_table_refs = {item.self_ref for item in doc.tables}
    regions = PdfRegions(normalized)
    args.primary_ocr_only = True
    formula_recognizer = PrimaryContentOnlyRecognizer(args)
    native_reader = None
    routes, excluded, picture_routes = {}, set(), {}
    captioned_figures, captioned_figure_routes = {}, {}
    note_routes, source_table_candidates = {}, {}
    manifest = {"schema": LOCAL_SCHEMA, "source": str(source),
                "source_size": source.stat().st_size, "source_mtime_ns": source.stat().st_mtime_ns,
                "source_sha256": getattr(args, "current_source_hash", None),
                "orientation_schema": ORIENTATION_SCHEMA,
                "orientation_classifier": {"model": "PP-LCNet_x1_0_doc_ori", "sha256": ORIENTATION_MODEL_SHA256,
                                           "confidence_threshold": ORIENTATION_CONFIDENCE},
                "pages": orientation, "tables": [], "formulas": [], "missing_pages": [],
                "table_recovery_schema": TABLE_RECOVERY_SCHEMA, "table_join_schema": TABLE_JOIN_SCHEMA,
                "table_cell_schema": TABLE_CELL_SCHEMA,
                "borderless_rows_enabled": getattr(args, 'pdf_borderless_rows', True),
                "primary_table_notes_enabled": getattr(args, 'pdf_table_notes', True),
                "remove_duplicate_table_notes": getattr(args, 'pdf_remove_duplicate_table_notes', False),
                "source_scripts_enabled": getattr(args, 'pdf_source_scripts', True),
                "source_unit_composition_enabled": getattr(args, 'pdf_source_unit_composition', True),
                "source_cell_ownership_enabled": getattr(args, 'pdf_source_cell_ownership', True),
                "source_image_bounds_enabled": getattr(args, 'pdf_source_image_bounds', True),
                "primary_row_boundaries_enabled": getattr(args, 'pdf_primary_row_boundaries', True),
                "primary_region_ownership_enabled": getattr(args, 'pdf_primary_region_ownership', True),
                "duplicate_primary_tables_enabled": getattr(args, 'pdf_duplicate_primary_tables', True),
                "captioned_figures_enabled": getattr(args, 'pdf_captioned_figures', True),
                "restored_mixed_math_text": [], "primary_ocr_only": args.primary_ocr_only,
                "formula_reader": "surya", "content_source": "primary_surya"}
    args.pdf_quality_current = manifest
    records = getattr(args, "surya_records", [])
    if pdf_repair_enabled(args, 'math'):
        manifest['image_budgets'] = {'classification': {'pages': {}}, 'formula': {'pages': {}}}
        manifest['math_asset_limits'] = dict(assets=getattr(args, 'pdf_math_max_assets', None),
                                            seconds=getattr(args, 'pdf_math_seconds', None))
        manifest['classification_limits'] = dict(assets=getattr(args, 'pdf_classification_max_assets', None),
                                            seconds=getattr(args, 'pdf_classification_seconds', None))
        manifest['formula_reader'] = getattr(args, 'pdf_formula_reader', 'surya')
        manifest['formula_verification_device'] = getattr(args, 'pdf_formula_device', 'auto')
        from docling_pdf_repairs import load_formula_corrections
        manifest['reviewed_formula_corrections'] = load_formula_corrections(
            getattr(args, 'formula_corrections', None), manifest['source_sha256'])
    manifest["ocr_blocks"] = records
    table_records = [record for record in records if "<table" in record["html"].lower()]
    diagnostic_event('LOCAL_EXPORT_BEGIN', source=str(source), table_count=len(doc.tables), picture_count=len(doc.pictures),
                     text_count=len(doc.texts), ocr_record_count=len(records), table_ocr_record_count=len(table_records))
    policy_token = _PRIMARY_OCR_ONLY.set(args.primary_ocr_only)
    prior_content_policy = os.environ.get('DOCLING_PRIMARY_OCR_ONLY')
    os.environ['DOCLING_PRIMARY_OCR_ONLY'] = '1'
    try:
        try:
            native_reader = NativeTableReader(normalized)
            native_reader.formula_recognizer = None if args.primary_ocr_only else formula_recognizer
            formula_recognizer.pdf_source_reader = native_reader
            native_reader.repair_subrows = pdf_repair_enabled(args, 'subrows')
            native_reader.repair_boundaries = pdf_repair_enabled(args, 'boundaries')
            native_reader.source_cell_ownership = (args.primary_ocr_only and getattr(args, 'pdf_source_cell_ownership', True)
                                                  and pdf_repair_enabled(args, 'boundaries'))
        except Exception as exc:
            diagnostic_event('LOCAL_NATIVE_READER_UNAVAILABLE', error_type=type(exc).__name__, error=diagnostic_summary(str(exc)))
            print(f"PDF geometry reader unavailable; retaining OCR route: {type(exc).__name__}: {exc}", file=sys.stderr)
        if getattr(args, 'pdf_captioned_figures', True) and pdf_repair_enabled(args, 'boundaries'):
            captioned_figures = capture_captioned_figures(doc, native_reader, records, manifest, sys.modules[__name__])
        from docling_pdf_forms import capture_form_figures
        captioned_figures.update(capture_form_figures(doc, records, manifest, sys.modules[__name__]))
        if captioned_figures:
            # Form source crops own their children before recovery can interpret
            # the drawing as an editable table or recreate its OCR packets.
            reconcile_captioned_figures(doc, captioned_figures, captioned_figure_routes, routes, picture_routes,
                                       excluded, manifest, regions, sys.modules[__name__])
            excluded.update(captioned_figures)
            records = [record for record in records if not any(record.get('page_no') == entry['page']
                and _captioned_figure_contains(entry['bbox'], record.get('bbox')) for entry in captioned_figures.values())]
            table_records = [record for record in records if '<table' in record.get('html', '').lower()]
        # Restore tables/formulas detected by Surya but omitted by the layout assembly.
        for record in table_records:
            if not any((reg := item_region(item, doc)) and reg[0] == record["page_no"]
                       and overlap_ratio(reg[1], record["bbox"]) > 0.6 for item in doc.tables):
                insert_source_item(doc, record["page_no"], record["bbox"], "table")
                diagnostic_event('LOCAL_TABLE_RESTORED_FROM_OCR', page_no=record['page_no'], bbox=record['bbox'])
        from docling_pdf_structure import (recover_image_backed_tables,
            reject_collapsed_table_candidates, table_owns_source_image, restore_ocr_paragraphs)
        image_backed_tables = recover_image_backed_tables(doc, native_reader, regions,
            records, routes, excluded, manifest, sys.modules[__name__])
        scan_candidates = recover_scanned_table_regions(
            doc,native_reader,regions,orientation,records,routes,excluded,manifest)
        expose_picture_table_children(doc, picture_routes, manifest)
        restore_ocr_formula_blocks(doc, records, picture_routes, manifest)
        if pdf_repair_enabled(args, 'boundaries'):
            from docling_pdf_structure import recover_unrouted_native_tables
            unrouted_native_candidates=recover_unrouted_native_tables(doc,native_reader,regions,
                routes,excluded,manifest,sys.modules[__name__],records=records)
            from docling_pdf_repairs import repair_source_tables
            note_routes, source_table_candidates = repair_source_tables(doc, native_reader, regions,
                routes, excluded, manifest, sys.modules[__name__])
            if args.primary_ocr_only:
                note_routes = {}
            source_table_candidates.update(unrouted_native_candidates)
            from docling_pdf_captions import attach_source_captions
            attach_source_captions(doc,excluded,manifest,sys.modules[__name__])

        # Include newly restored blocks, and replace their actual item class
        # before HTML dispatch can treat mixed prose as a standalone formula.
        manifest["restored_mixed_math_text"] = normalize_surya_formulas(doc)

        populated_pages = {prov.page_no for item, _ in doc.iterate_items() if hasattr(item, "prov")
                           and getattr(item, "label", None) not in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
                           for prov in item.prov}
        for page_no, page_info in orientation.items():
            if page_no in populated_pages or page_info.get("ink_fraction", 0) < 0.0001:
                diagnostic_event('LOCAL_PAGE_IMAGE_FALLBACK_SKIP', page_no=page_no,
                                 reason='page_has_structured_items' if page_no in populated_pages else 'blank_or_unknown_ink',
                                 ink_fraction=page_info.get('ink_fraction',0))
                continue
            image = regions.crop(page_no)
            page = regions.pdf[page_no - 1]
            try:
                width, height = page.get_size()
            finally:
                page.close()
            if page_no not in doc.pages:
                doc.add_page(page_no=page_no, size=Size(width=width, height=height))
            item = insert_source_item(doc, page_no, [0, 0, width, height], "picture")
            picture_routes[item.self_ref] = image_html(image, width, "page")
            manifest["missing_pages"].append({"page": page_no, "method": "source_image"})
            diagnostic_event('LOCAL_PAGE_IMAGE_FALLBACK', page_no=page_no, ref=item.self_ref,
                             reason='nonblank_page_without_structured_items', width=width, height=height)

        retry_limit = table_request_limit(len(doc.tables))
        retries = 0
        table_regions = []
        def table_source_order(item):
            region=item_region(item,doc)
            return (region[0],region[1][1],region[1][0],item.self_ref) if region else (
                float('inf'),float('inf'),float('inf'),item.self_ref)

        if captioned_figures:
            reconcile_captioned_figures(doc, captioned_figures, captioned_figure_routes, routes, picture_routes,
                                       excluded, manifest, regions, sys.modules[__name__])
            # The complete primary packets remain in diagnostics; later repairs
            # may not recreate a semantic table inside an explicitly captioned figure.
            records = [record for record in records if not any(record.get('page_no') == entry['page']
                and _captioned_figure_contains(entry['bbox'], record.get('bbox')) for entry in captioned_figures.values())]
            table_records = [record for record in records if '<table' in record.get('html', '').lower()]
        for item in sorted(doc.tables,key=table_source_order):
            if item.self_ref in captioned_figures:
                diagnostic_event('TABLE_EXPORT_AS_CAPTIONED_FIGURE', ref=item.self_ref,
                                 page=captioned_figures[item.self_ref]['page'])
                continue
            if item.self_ref in excluded:
                diagnostic_event('TABLE_EXPORT_SKIP', ref=item.self_ref, reason='excluded_fragment')
                continue
            region = item_region(item, doc)
            if region is None:
                diagnostic_event('TABLE_EXPORT_SKIP', ref=item.self_ref, reason='no_source_provenance')
                continue
            page_no, bbox = region
            diagnostic_event('TABLE_EXPORT_BEGIN', page_no=page_no, ref=item.self_ref, bbox=bbox)
            detected_rules = None
            try:
                if native_reader is not None:
                    native_reader.load_page(page_no)
                if native_reader is None or native_reader.table_for_region(bbox) is None:
                    detected_rules = regions.table_geometry(page_no, bbox)
                    if detected_rules:
                        bbox = detected_rules["bbox"]
            except Exception as exc:
                diagnostic_event('TABLE_BOUNDARY_ERROR', page_no=page_no, ref=item.self_ref,
                                 error_type=type(exc).__name__, error=diagnostic_summary(str(exc)))
                print(f"Table boundary analysis: page {page_no}; {type(exc).__name__}: {exc}", file=sys.stderr)
            image = regions.crop(page_no, bbox)
            table_source_bounds = list(bbox)
            raster_positions = raster_column_positions(image)
            expected = len(raster_positions) - 1 if raster_positions else None
            # TableFormer may match cells against an old hidden PDF OCR layer.
            # It remains a structural model, never a content authority here.
            raw_candidates = table_content_candidates(item, doc, table_records, page_no, bbox,
                                                       primary_only=args.primary_ocr_only)
            diagnostic_event('TABLE_SOURCE_CANDIDATES', page_no=page_no, ref=item.self_ref, bbox=bbox,
                             methods=[candidate[0] for candidate in raw_candidates], raster_columns=expected,
                             crop_width=image.width, crop_height=image.height)
            native, join_geometry, source_graphics, page_scans = None, None, [], []
            drawing_frame_proof, drawing_frame_pictures = None, []
            native_method = 'pdf_geometry'
            try:
                if native_reader is not None:
                    source_graphics = native_reader.graphics(page_no, bbox)
                    page_scans = [obj for obj in source_graphics if native_reader.is_page_scan(obj, bbox)]
                    source_graphics = [obj for obj in source_graphics if not native_reader.is_page_scan(obj, bbox)]
                    native = source_table_candidates.get(item.self_ref) or scan_candidates.get(item.self_ref)
                    if native:
                        native_method = 'pdf_geometry' if item.self_ref in source_table_candidates else 'raster_geometry'
                        join_geometry = native[1]
                    else:
                        native = native_reader.read(page_no, bbox, [raw for _, raw in raw_candidates], source_image=image)
                        join_geometry = native_reader.grid(page_no, bbox)
                        if join_geometry is None and pdf_repair_enabled(args, 'boundaries'):
                            join_geometry = native_reader.open_edge_grid(bbox)
                    if page_scans:
                        if args.primary_ocr_only:
                            # Scan discovery can already own a verified grid.
                            # Reuse its exact padded render for stable cell OCR
                            # keys; a tight crop can clip an outer source rule.
                            render_box = (native[1].get('source_render_bbox') if native
                                          and native[1].get('source_grid_verified') else None)
                            if render_box:
                                table_source_bounds = list(native[1]['bbox'])
                                image.close()
                                bbox = list(render_box)
                                image = regions.crop(page_no, bbox)
                            else:
                                from docling_pdf_scan_recovery import read_source_scan_grid
                                probe = read_source_scan_grid(native_reader, regions, page_no, bbox,
                                    [raw for _, raw in raw_candidates], sys.modules[__name__])
                                if probe and (probe['native'][1].get('source_grid_verified') or native is None):
                                    image.close()
                                    native, image, bbox = probe['native'], probe['image'], list(probe['region'])
                                    table_source_bounds = list(native[1]['bbox'])
                                elif probe:
                                    probe['image'].close()
                            raster_positions = raster_column_positions(image)
                            expected = len(raster_positions)-1 if raster_positions else None
                        elif native is None:
                            native = native_reader.read_raster_table(page_no,bbox,image,[raw for _,raw in raw_candidates])
                        if native:
                            native_method = 'raster_geometry'
                            join_geometry = native[1]
                if join_geometry is None:
                    join_geometry = detected_rules or image_backed_tables.get(item.self_ref, {}).get('geometry')
                if native:
                    from docling_pdf_partial_rules import refine_partial_dividers
                    refined_html,refined_geometry,divider_proof=refine_partial_dividers(
                        native[0],native[1],native_reader,page_no,image,bbox,sys.modules[__name__])
                    if divider_proof:
                        native=(refined_html,refined_geometry);join_geometry=refined_geometry
                        manifest.setdefault('source_partial_divider_repairs',[]).append(
                            {**divider_proof,'page':page_no,'ref':item.self_ref})
                    from docling_pdf_illustrative_frames import split_drawing_frame_footer
                    separated=split_drawing_frame_footer(native[0],native[1],image,bbox,
                        page_no,doc,manifest,sys.modules[__name__])
                    if separated:
                        main_html,main_geometry,frame_html,_,frame_proof=separated
                        drawing_frame_proof = frame_proof
                        native=(main_html,main_geometry);join_geometry=main_geometry
                        bbox=list(main_geometry['bbox'])
                        table_source_bounds = list(bbox)
                        image.close();image=regions.crop(page_no,bbox)
                        raster_positions=raster_column_positions(image)
                        expected=len(raster_positions)-1 if raster_positions else None
                        from docling_core.types.doc import BoundingBox,CoordOrigin,ProvenanceItem
                        for prov in item.prov:
                            if prov.page_no==page_no:
                                prov.bbox=BoundingBox(l=bbox[0],t=bbox[1],r=bbox[2],b=bbox[3],
                                    origin=CoordOrigin.TOPLEFT)
                        sibling=item
                        frame_nodes=BeautifulSoup(frame_html,'html.parser').find_all('figure',recursive=False)
                        if len(frame_nodes)!=len(frame_proof['source_illustrations']):
                            raise RuntimeError('Illustrative frame crop/provenance count mismatch')
                        for frame_node,illustration in zip(frame_nodes,frame_proof['source_illustrations']):
                            bounds=illustration['bbox']
                            picture=doc.insert_picture(sibling=sibling,after=True,prov=ProvenanceItem(
                                page_no=page_no,charspan=(0,0),bbox=BoundingBox(l=bounds[0],t=bounds[1],
                                r=bounds[2],b=bounds[3],origin=CoordOrigin.TOPLEFT)))
                            picture_routes[picture.self_ref]=str(frame_node)
                            drawing_frame_pictures.append(dict(ref=picture.self_ref, bbox=list(bounds),
                                html_sha256=hashlib.sha256(str(frame_node).encode()).hexdigest()))
                            manifest.setdefault('source_illustration_picture_refs',[]).append(picture.self_ref)
                            sibling=picture
                    if args.primary_ocr_only and page_scans and native[1].get('source_grid_verified'):
                        from docling_pdf_primary_cells import recover_missing_mixed_text
                        recovered_text = recover_missing_mixed_text(native, image, bbox, page_no,
                            args, manifest, sys.modules[__name__], reader=native_reader)
                        if recovered_text:
                            native = recovered_text
                            join_geometry = native[1]
                    raw_candidates.append((native_method, native[0]))
                diagnostic_event('TABLE_NATIVE_RECOVERY_RESULT', page_no=page_no, ref=item.self_ref,
                                 recovered=native is not None, method=native_method if native else None,
                                 source_graphics_count=len(source_graphics), page_scan_count=len(page_scans),
                                 join_geometry=diagnostic_summary(join_geometry), detected_rules=diagnostic_summary(detected_rules))
            except Exception as exc:
                diagnostic_event('TABLE_NATIVE_RECOVERY_ERROR', page_no=page_no, ref=item.self_ref,
                                 error_type=type(exc).__name__, error=diagnostic_summary(str(exc)))
                print(f"PDF geometry recovery: page {page_no}; {type(exc).__name__}: {exc}", file=sys.stderr)
            direction_conflict = getattr(native_reader, 'has_raster_text_direction_conflict', None)
            if callable(direction_conflict) and direction_conflict(page_no, bbox):
                header_geometry = next((entry.get('geometry') for entry in
                    getattr(native_reader,'raster_text_direction_conflicts',())
                    if entry['page']==page_no and overlap_ratio(entry['bbox'],bbox)>.6
                    and entry.get('geometry')),None)
                if header_geometry:
                    join_geometry=header_geometry
                    expected=len(header_geometry['x_grid'])-1
                fresh_ocr = []
                for method, raw in raw_candidates:
                    if method != 'surya_page':
                        continue
                    try:
                        repaired, _ = repair_table_spans(clean_table_html(raw))
                        fresh = table_profile(repaired)
                        if not fresh['issues'] and fresh['rows'] >= 2 and fresh['columns'] >= 2:
                            fresh_ocr.append(method)
                    except (ValueError, TypeError):
                        pass
                if fresh_ocr:
                    # TableFormer's cell matching may copy the same old PDF
                    # text. It is not an independent numeric OCR vote when
                    # this exact source region has a verified direction conflict.
                    raw_candidates = [(method, raw) for method, raw in raw_candidates if method != 'tableformer']
                    diagnostic_event('TABLE_NATIVE_TEXT_QUARANTINE', page_no=page_no, ref=item.self_ref,
                                     bbox=bbox, excluded_methods=['tableformer'], retained_methods=fresh_ocr,
                                     reason='source_hidden_ocr_direction_conflict', affects_numeric_votes=True)
            # A clipped or broken raster rule is not an authoritative column count.
            header_widths = []
            for _, raw in raw_candidates:
                parsed = BeautifulSoup(raw, "html.parser").find("table")
                if parsed and parsed.find("tr"):
                    header_widths.append(sum(int(cell.get("colspan", 1)) for cell in
                                             parsed.find("tr").find_all(["th", "td"], recursive=False)))
            raster_expected = expected
            # A table-only raster proves ownership, but a preview's missing
            # short/merged rules cannot override a complete positioned OCR grid.
            image_table_support = image_backed_tables.get(item.self_ref)
            span_geometry = join_geometry
            if image_table_support and native is None:
                expected = None
                span_geometry = None
                diagnostic_event('TABLE_RASTER_GRID_HINT_ONLY', page_no=page_no, ref=item.self_ref,
                    preview_columns=raster_expected, retained_columns=image_table_support['columns'],
                    reason='preview_rules_do_not_define_merged_cell_occupancy')
            # A raster preview can miss short/merged rails. Verified cells have
            # already passed glyph ownership and rectangular-grid checks.
            if native:
                expected = len(native[1]["x_grid"]) - 1
                if raster_expected and raster_expected != expected:
                    print(f"Table grid: page {page_no}; raster preview {raster_expected}; "
                          f"verified source cells {expected}; using source cells", flush=True)
            elif join_geometry and not image_table_support and pdf_repair_enabled(args, 'boundaries'):
                expected = len(join_geometry['x_grid'])-1
            elif (expected and expected not in header_widths
                  and (join_geometry or {}).get('basis') != 'raster_numbered_header_rules'):
                expected = None
            diagnostic_event('TABLE_EXPECTED_GRID', page_no=page_no, ref=item.self_ref,
                             raster_columns=raster_expected, expected_columns=expected,
                             header_widths=header_widths, verified_source_grid=native is not None)
            candidates = []
            for method, raw in raw_candidates:
                if _DIAGNOSTICS is not None:
                    diagnostic_event('TABLE_CANDIDATE_RAW', page_no=page_no, ref=item.self_ref, method=method,
                                     html=diagnostic_summary(raw))
                try:
                    fragment = clean_table_html(raw)
                    if image_table_support and native is None and not args.primary_ocr_only:
                        from docling_pdf_structure import numbered_source_header_plan, apply_numbered_source_header
                        initial_profile = table_profile(fragment)
                        if any(issue.startswith('numbered_header_width_') for issue in initial_profile['issues']):
                            source_bounds = image_table_support['image_bboxes'][0]
                            source_table_image = regions.crop(page_no, source_bounds)
                            header_plan = None
                            try:
                                header_plan = numbered_source_header_plan(fragment, source_table_image,
                                    source_bounds, sys.modules[__name__])
                                for target in header_plan['targets']:
                                    target['source_cell_interior'] = True
                                args.table_request_context = dict(stage='source_header',page=page_no,
                                    bbox=source_bounds,element=item.self_ref)
                                diagnostic_event('TABLE_SOURCE_HEADER_REQUEST',page_no=page_no,ref=item.self_ref,
                                    source_bbox=source_bounds,targets=len(header_plan['targets']))
                                labels = recognize_scan_header_cells(args, header_plan['targets'],
                                    local_request_seconds([SimpleNamespace(max_tokens=2048)]))
                                fragment, header_proof = apply_numbered_source_header(header_plan, labels,
                                    sys.modules[__name__])
                                from docling_pdf_structure import image_table_content_proof
                                image_table_support['source_header_repair'] = header_proof
                                image_table_support['verified_header_content'] = image_table_content_proof(
                                    table_profile(fragment))
                                join_geometry = dict(basis='source_raster_header_cells',
                                    bbox=[header_plan['x_grid'][0],header_plan['y_grid'][0],
                                          header_plan['x_grid'][-1],header_plan['y_grid'][-1]],
                                    x_grid=header_plan['x_grid'],y_grid=header_plan['y_grid'],
                                    header_rows=header_plan['source_number_row']+1)
                                manifest.setdefault('source_numbered_headers',[]).append(
                                    dict(page=page_no,ref=item.self_ref,**header_proof))
                                diagnostic_event('TABLE_SOURCE_HEADER_RESTORED',page_no=page_no,
                                    ref=item.self_ref,**header_proof)
                            finally:
                                source_table_image.close()
                                if header_plan:
                                    for target in header_plan['targets']:
                                        target['image'].close()
                    fragment, header_repairs = repair_numbered_header_spans(fragment,span_geometry)
                    fragment, span_repairs = repair_table_spans(fragment, expected)
                    fragment, rule_repairs = align_table_spans_to_rules(fragment, span_geometry)
                    column_proof = None
                    if native and method == 'surya_page':
                        from docling_pdf_primary_grid import normalize_primary_columns
                        fragment, column_proof = normalize_primary_columns(fragment, native[1], sys.modules[__name__])
                        if column_proof:
                            proof = dict(page=page_no, ref=item.self_ref, **column_proof)
                            manifest.setdefault('primary_column_grid_repairs', []).append(proof)
                            diagnostic_event('TABLE_PRIMARY_SOURCE_COLUMNS_RESTORED', **proof)
                    profile = table_profile(fragment, expected)
                    if column_proof:
                        profile['source_columns_proof'] = column_proof
                    profile["span_repairs"] = header_repairs + span_repairs + rule_repairs
                    candidates.append((profile["score"] - 3*len(profile.get('warnings',[])), method, fragment, profile))
                    diagnostic_event('TABLE_CANDIDATE_REPAIRED', page_no=page_no, ref=item.self_ref, method=method,
                                     score=profile.get('score'), ranking_score=candidates[-1][0], rows=profile.get('rows'), columns=profile.get('columns'),
                                     issues=profile.get('issues',[]), warnings=profile.get('warnings',[]),
                                     text_preview=profile.get('text','')[:320],
                                     span_repair_count=len(header_repairs)+len(span_repairs), rule_repair_count=len(rule_repairs),
                                     header_repairs=header_repairs)
                except Exception as exc:
                    diagnostic_event('TABLE_CANDIDATE_REJECTED', page_no=page_no, ref=item.self_ref, method=method,
                                     error_type=type(exc).__name__, error=diagnostic_summary(str(exc)))
                    print(f"Table candidate rejected: page {page_no}; {method}; {exc}", file=sys.stderr)
            candidates = reject_collapsed_table_candidates(candidates, join_geometry,
                diagnostic_event, page_no=page_no, ref=item.self_ref)
            # Verified PDF cells retain both source text and source graphics.
            candidates.sort(key=lambda candidate: (not candidate[3]["issues"],
                            candidate[1] in {"pdf_geometry", "raster_geometry"}, candidate[0],
                            candidate[3].get("math_count", 0), candidate[1] == "surya_page"), reverse=True)
            chosen = candidates[0] if candidates else None
            diagnostic_event('TABLE_CHOSEN', phase='initial', page_no=page_no, ref=item.self_ref,
                             method=chosen[1] if chosen else None, profile=diagnostic_summary(chosen[3]) if chosen else None)
            conflict = False
            valid_candidates = [candidate for candidate in candidates if not candidate[3]["issues"]]
            if (chosen and chosen[1] not in {"pdf_geometry", "raster_geometry"} and len(valid_candidates) > 1
                    and (valid_candidates[0][3]["rows"], valid_candidates[0][3]["columns"])
                    == (valid_candidates[1][3]["rows"], valid_candidates[1][3]["columns"])):
                first_numbers, second_numbers = (numeric_cells(candidate[3]) for candidate in valid_candidates[:2])
                common = set(first_numbers) & set(second_numbers)
                conflict = len(common) >= 3 and any(first_numbers[key] != second_numbers[key] for key in common)
                diagnostic_event('TABLE_NUMERIC_COMPARISON', page_no=page_no, ref=item.self_ref,
                                 methods=[candidate[1] for candidate in valid_candidates[:2]],
                                 comparable_cell_count=len(common), conflict=conflict)
            attempt_log = []
            retry_stop_reason = 'attempt_limit'
            for attempt in range(TABLE_REGION_ATTEMPTS):
                if args.primary_ocr_only:
                    retry_stop_reason = 'secondary_content_ocr_disabled'
                    diagnostic_event('TABLE_RETRY_SKIP', page_no=page_no, ref=item.self_ref,
                                     reason=retry_stop_reason)
                    break
                if not (chosen is None or chosen[3]['issues'] or conflict):
                    retry_stop_reason = 'usable_candidate_without_numeric_conflict'
                    diagnostic_event('TABLE_RETRY_SKIP', page_no=page_no, ref=item.self_ref, attempt=attempt+1,
                                     reason=retry_stop_reason, request_count=retries, request_limit=retry_limit)
                    break
                try:
                    args.table_request_context = {'page':page_no,'bbox':bbox,'attempt':attempt+1}
                    scale = (1.0, 1.25, .85, 1.5)[attempt % 4]
                    retry_image = image if scale == 1 else image.resize((round(image.width*scale),round(image.height*scale)))
                    seconds = local_request_seconds([SimpleNamespace(max_tokens=8192)])
                    retries += 1
                    diagnostic_event('TABLE_RETRY_REQUEST', page_no=page_no, ref=item.self_ref, attempt=attempt+1,
                                     request_count=retries, request_limit=retry_limit, timeout_seconds=seconds,
                                     scale=scale, crop_width=retry_image.width, crop_height=retry_image.height,
                                     numeric_conflict=conflict, current_issues=chosen[3].get('issues',[]) if chosen else ['no_candidate'])
                    fragment = recover_table_crop(args, retry_image, seconds)
                    if _DIAGNOSTICS is not None:
                        diagnostic_event('TABLE_CANDIDATE_RAW', page_no=page_no, ref=item.self_ref,
                                         method='surya_region', attempt=attempt+1, html=diagnostic_summary(fragment))
                    fragment, span_repairs = repair_table_spans(fragment, expected)
                    fragment, rule_repairs = align_table_spans_to_rules(fragment, span_geometry)
                    profile = table_profile(fragment, expected)
                    profile["span_repairs"] = span_repairs + rule_repairs
                    retry = (profile["score"] - 3*len(profile.get('warnings',[])), "surya_region", fragment, profile)
                    column_support = sum(not candidate[3]['issues'] and chosen is not None
                                         and candidate[3]['columns'] == chosen[3]['columns']
                                         for candidate in valid_candidates)
                    if (chosen and not chosen[3]['issues'] and expected == chosen[3]['columns']
                            and profile['columns'] < expected and column_support >= 2):
                        # A read that loses confirmed columns cannot resolve a
                        # disagreement in their numbers. Keep the complete reads.
                        diagnostic_event('TABLE_RETRY_GRID_REJECT', page_no=page_no, ref=item.self_ref,
                                         attempt=attempt+1, reason='lost_confirmed_columns',
                                         expected_columns=expected, returned_columns=profile['columns'],
                                         supporting_reads=column_support)
                        attempt_log.append({'attempt':attempt+1,'reason':'lost_confirmed_columns',
                                            'expected_columns':expected,'returned_columns':profile['columns']})
                        continue
                    candidates.append(retry)
                    attempt_log.append({'attempt':attempt+1,'issues':profile['issues'],'warnings':profile.get('warnings',[])})
                    diagnostic_event('TABLE_RETRY_RESULT', page_no=page_no, ref=item.self_ref, attempt=attempt+1,
                                     score=profile.get('score'), ranking_score=retry[0], rows=profile.get('rows'), columns=profile.get('columns'),
                                     issues=profile.get('issues',[]), warnings=profile.get('warnings',[]),
                                     span_repair_count=len(span_repairs), rule_repair_count=len(rule_repairs))
                    if chosen is None or retry[0] > chosen[0] or (conflict and not profile["issues"]):
                        chosen = retry
                        # Require the regional read to agree with at least one previous numeric read.
                        read = numeric_cells(profile)
                        comparable = [numeric_cells(candidate[3]) for candidate in valid_candidates
                                      if (candidate[3]['rows'],candidate[3]['columns']) == (profile['rows'],profile['columns'])
                                      and len(set(read)&set(numeric_cells(candidate[3])))>=3]
                        # No comparable earlier read is not a disagreement.
                        conflict = bool(comparable) and not any(all(read[k]==values[k] for k in set(read)&set(values))
                                                                               for values in comparable)
                        diagnostic_event('TABLE_CHOSEN', phase='regional', page_no=page_no, ref=item.self_ref,
                                         attempt=attempt+1, method=chosen[1], numeric_conflict=conflict, profile=diagnostic_summary(chosen[3]))
                except Exception as exc:
                    diagnostic_event('TABLE_RETRY_ERROR', page_no=page_no, ref=item.self_ref, attempt=attempt+1,
                                     error_type=type(exc).__name__, error=diagnostic_summary(str(exc)))
                    attempt_log.append({'attempt':attempt+1,'error':f'{type(exc).__name__}: {exc}'})
                    print(f"Table regional recovery: page {page_no}; {type(exc).__name__}: {exc}", file=sys.stderr)
            diagnostic_event('TABLE_RETRY_END', page_no=page_no, ref=item.self_ref,
                             reason=retry_stop_reason, attempt_count=len(attempt_log),
                             document_request_count=retries, document_request_limit=retry_limit,
                             candidate_available=chosen is not None, numeric_conflict=conflict)
            header_recovery = None
            retained_graphics = []
            primary_region_guard = None
            if conflict:
                complete_reads = [candidate for candidate in candidates if not candidate[3]["issues"]]
                if complete_reads:
                    chosen = table_consensus_candidate(complete_reads)
                    diagnostic_event('TABLE_CHOSEN', phase='consensus', page_no=page_no, ref=item.self_ref,
                                     method=chosen[1], complete_read_count=len(complete_reads), profile=diagnostic_summary(chosen[3]))
            if (chosen and native and drawing_frame_proof and args.primary_ocr_only
                    and getattr(args, 'pdf_primary_region_ownership', True)
                    and pdf_repair_enabled(args, 'boundaries')):
                source_font = _primary_note_source_font(dict(page=page_no, bbox=native[1]['bbox'],
                    pdf_geometry=native[1]), native_reader)
                primary_region_guard = _primary_region_ownership_guard(native[1], drawing_frame_proof,
                    records, page_no, table_profile(native[0]), image, bbox, source_font, sys.modules[__name__])
                if primary_region_guard:
                    bbox = primary_region_guard['fallback_bbox']
                    table_source_bounds = list(bbox)
                    image.close(); image = regions.crop(page_no, bbox)
                    removed = _primary_region_exclude_owned_frame_pictures(drawing_frame_pictures, bbox,
                                                                          picture_routes, excluded)
                    primary_region_guard.update(ref=item.self_ref, removed_owned_frame_pictures=removed)
                    manifest.setdefault('primary_region_ownership_repairs', []).append(primary_region_guard)
                    diagnostic_event('PRIMARY_REGION_OWNERSHIP_FALLBACK', **primary_region_guard)
                    from docling_core.types.doc import BoundingBox, CoordOrigin
                    for prov in item.prov:
                        if prov.page_no == page_no:
                            prov.bbox = BoundingBox(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3], origin=CoordOrigin.TOPLEFT)
                    chosen, join_geometry = None, None
            source_cell_fallback = False
            source_cell_ownership = (args.primary_ocr_only and getattr(args, 'pdf_source_cell_ownership', True)
                                     and pdf_repair_enabled(args, 'boundaries'))
            if chosen and native and chosen[1] in {'pdf_geometry', 'raster_geometry'} and source_cell_ownership:
                prepared_native = prepare_source_cell_ownership(native,page_no,item.self_ref,manifest,sys.modules[__name__])
                if prepared_native is None:
                    chosen, join_geometry, source_cell_fallback = None, None, True
                else:
                    native = prepared_native
                    chosen = (chosen[0],chosen[1],native[0],table_profile(native[0],expected))
            if chosen and not chosen[3]['issues'] and native and chosen[1] in {'pdf_geometry','raster_geometry'}:
                method, content, profile = chosen[1:]
                if native and method in {'pdf_geometry', 'raster_geometry'}:
                    from docling_pdf_table_audit import protect_source_graphic_assets
                    content, protected_graphics = protect_source_graphic_assets(content, native[1], page_no,
                        item.self_ref, manifest, sys.modules[__name__],
                        caption=' '.join(c.resolve(doc).text for c in item.captions))
                    from docling_pdf_primary import preserve_primary_cells
                    content, primary_cells = preserve_primary_cells(content, native[1], records,
                        bbox, page_no, manifest, sys.modules[__name__], source_image=image, reader=native_reader)
                    if primary_cells:
                        profile = table_profile(content, expected)
                    elif args.primary_ocr_only:
                        primary_choices = [c for c in candidates if c[1] == 'surya_page' and not c[3]['issues']]
                        if len({c[2] for c in primary_choices}) == 1:
                            primary_profile = primary_choices[0][3]
                            positions = lambda p: {(c['row'], c['column'], c['row_span'], c['column_span'])
                                                   for c in p['cells']}
                            if ((native[1].get('source_grid_verified') or native[1].get('positioned_source_grid_verified'))
                                    and not primary_profile.get('source_columns_proof')
                                    and positions(primary_profile) != positions(profile)):
                                proof = dict(page=page_no, ref=item.self_ref,
                                    reason='primary_payload_cannot_replace_verified_source_grid',
                                    source_rows=profile['rows'], source_columns=profile['columns'],
                                    primary_rows=primary_profile['rows'], primary_columns=primary_profile['columns'])
                                manifest.setdefault('primary_grid_fallbacks_blocked', []).append(proof)
                                diagnostic_event('TABLE_PRIMARY_GRID_FALLBACK_BLOCKED', **proof)
                            else:
                                _, method, content, profile = primary_choices[0]
                                from docling_pdf_review import register_primary_grid_review
                                register_primary_grid_review(content,image,bbox,page_no,item.self_ref,
                                    manifest,sys.modules[__name__])
                    if args.primary_ocr_only and method in {'pdf_geometry', 'raster_geometry'}:
                        if source_cell_ownership:
                            content, source_kept = mask_source_owned_cells(content,native[1],image,bbox,
                                page_no,manifest,sys.modules[__name__])
                        else:
                            from docling_pdf_primary import mask_nonprimary_content
                            content, source_kept = mask_nonprimary_content(content, native[1], image, bbox,
                                page_no, manifest, sys.modules[__name__])
                        if content is not None and source_kept:
                            profile = table_profile(content, expected)
                if content is None:
                    chosen, join_geometry, source_cell_fallback = None, None, True
                else:
                    chosen = (chosen[0],method,content,profile)
            if chosen and not chosen[3]["issues"]:
                method, content, profile = chosen[1:]
                pending=(native[1].get('pending_cell_ocr',[]) if native and method=='raster_geometry'
                         and not args.primary_ocr_only else [])
                used=getattr(args,'scan_numeric_requests',0)
                targets=[scan_numeric_crop(image,bbox,t) for t in pending]
                targets=[t for t in targets if t is not None]
                if targets:
                    seconds=local_request_seconds([SimpleNamespace(max_tokens=2048)])
                    if seconds>0:
                        args.scan_numeric_requests=used+len(targets);retries+=len(targets)
                        diagnostic_event('SCAN_NUMERIC_CELL_REQUEST',page_no=page_no,ref=item.self_ref,
                            cells=[dict(row=t['row'],column=t['column'],bbox=t['bbox'],source_lines=t['source_lines']) for t in targets],
                            timeout_seconds=seconds)
                        try:
                            labels=recognize_scan_header_cells(args,targets,seconds)
                            parsed=BeautifulSoup(content,'html.parser');nodes=parsed.table.find_all(['th','td'])
                            for label in labels:
                                index=next((i for i,c in enumerate(profile['cells']) if
                                            c['row']==label['row'] and c['column']==label['column']),None)
                                if index is not None:
                                    nodes[index].clear();nodes[index].append(BeautifulSoup(label['html'],'html.parser'))
                            content=str(parsed.table);profile=table_profile(content,expected)
                            diagnostic_event('SCAN_NUMERIC_CELL_RESTORED',page_no=page_no,ref=item.self_ref,labels=labels)
                        except Exception as exc:
                            diagnostic_event('SCAN_NUMERIC_CELL_ERROR',page_no=page_no,ref=item.self_ref,
                                             error_type=type(exc).__name__,error=str(exc))
                if method not in {'pdf_geometry','raster_geometry'} and not args.primary_ocr_only:
                    content,header_recovery = recover_complete_table_header(content,candidates)
                    if header_recovery:profile=table_profile(content,expected)
                    missing=missing_scan_header_cells(content,join_geometry,image,bbox)
                    used=getattr(args,'scan_header_requests',0)
                    if missing:
                        seconds=local_request_seconds([SimpleNamespace(max_tokens=2048)])
                        if seconds>0:
                            args.scan_header_requests=used+len(missing)
                            retries+=len(missing)
                            diagnostic_event('TABLE_HEADER_TEXT_REQUEST',page_no=page_no,ref=item.self_ref,
                                cells=[dict(row=t['row'],column=t['column'],bbox=t['bbox']) for t in missing],
                                timeout_seconds=seconds)
                            try:
                                labels=recognize_scan_header_cells(args,missing,seconds)
                                parsed=BeautifulSoup(content,'html.parser')
                                nodes=parsed.table.find_all(['th','td'])
                                for label in labels:
                                    index=next((i for i,c in enumerate(profile['cells']) if
                                                c['row']==label['row'] and c['column']==label['column']),None)
                                    if index is not None and not nodes[index].get_text(strip=True):
                                        nodes[index].append(BeautifulSoup(label['html'],'html.parser'))
                                content=str(parsed.table)
                                profile=table_profile(content,expected)
                                diagnostic_event('TABLE_HEADER_TEXT_RESTORED',page_no=page_no,ref=item.self_ref,
                                                 labels=labels)
                            except Exception as exc:
                                diagnostic_event('TABLE_HEADER_TEXT_ERROR',page_no=page_no,ref=item.self_ref,
                                                 error_type=type(exc).__name__,error=str(exc))
                if method in {"pdf_geometry", "raster_geometry"}:
                    rendered = BeautifulSoup(content, "html.parser")
                    rendered.table["class"] = "pdf-native"
                    rendered.table["style"] = f"min-width:{profile['columns'] * 6}em"
                    grid = native[1]["x_grid"]
                    group = rendered.new_tag("colgroup")
                    for a, b in zip(grid, grid[1:]):
                        column = rendered.new_tag("col")
                        column["style"] = f"width:{100 * (b - a) / (grid[-1] - grid[0]):.3f}%"
                        group.append(column)
                    rendered.table.insert(0, group)
                    content = str(rendered.table)
                if source_graphics and method not in {"pdf_geometry", "raster_geometry"}:
                    retained_source_graphics = []
                    for obj in source_graphics:
                        bounds = [obj['x0'], obj['top'], obj['x1'], obj['bottom']]
                        if table_owns_source_image(image_backed_tables.get(item.self_ref), bounds, profile):
                            diagnostic_event('TABLE_SOURCE_RASTER_ALREADY_STRUCTURED', page_no=page_no,
                                ref=item.self_ref, bbox=bounds, method=method,
                                reason='source_table_image_owned_by_complete_structured_read')
                        else:
                            retained_source_graphics.append(obj)
                    ownership_unmapped = []
                    if source_cell_ownership:
                        retained_source_graphics, ownership_unmapped = source_owned_graphic_objects(
                            content,join_geometry,retained_source_graphics,page_no,item.self_ref,manifest,sys.modules[__name__])
                    content, retained_graphics, unmapped = retain_graphics_in_table(
                        content, join_geometry, retained_source_graphics, image, bbox)
                    unmapped = ownership_unmapped + unmapped
                    # An unruled/ambiguous grid must not make an illustration
                    # disappear. Keep its original positioned picture if it
                    # exists, or insert only that illustration next to the table.
                    for obj in unmapped:
                        bounds = [obj["x0"], obj["top"], obj["x1"], obj["bottom"]]
                        area = max(.01, (bounds[2]-bounds[0])*(bounds[3]-bounds[1]))
                        if any((reg := item_region(picture, doc)) and reg[0] == page_no
                               and picture_routes.get(picture.self_ref) != ""
                               and .7 < (reg[1][2]-reg[1][0])*(reg[1][3]-reg[1][1])/area < 1.4
                               and overlap_ratio(reg[1], bounds) > .9 for picture in doc.pictures):
                            continue
                        from docling_core.types.doc import BoundingBox, CoordOrigin, ProvenanceItem
                        picture = doc.insert_picture(sibling=item, after=True, prov=ProvenanceItem(
                            page_no=page_no, charspan=(0, 0), bbox=BoundingBox(
                                l=bounds[0], t=bounds[1], r=bounds[2], b=bounds[3], origin=CoordOrigin.TOPLEFT)))
                        picture_routes[picture.self_ref] = image_html(regions.crop(page_no, bounds), bounds[2]-bounds[0], "picture")
                    profile = table_profile(content, expected)
                if native and method not in {'pdf_geometry', 'raster_geometry'}:
                    from docling_pdf_table_audit import protect_source_graphic_assets
                    content,protected_graphics=protect_source_graphic_assets(content,native[1],page_no,
                        item.self_ref,manifest,sys.modules[__name__],
                        caption=' '.join(c.resolve(doc).text for c in item.captions))
                if native and '<img' in content:
                    from docling_pdf_cell_rules import remove_source_underlines,remove_source_rule_fragments
                    if native_reader is not None and 'source_link_rects' not in native[1]:
                        source_page=native_reader.load_page(page_no)
                        native[1]['source_link_rects']=[[a['x0'],a['top'],a['x1'],a['bottom']]
                            for a in (getattr(source_page,'annots',None) or []) if a.get('uri')]
                    content,underline_repairs=remove_source_underlines(content,native[1],page_no,
                        manifest,sys.modules[__name__])
                    content,rule_fragment_repairs=remove_source_rule_fragments(content,native[1],page_no,
                        manifest,sys.modules[__name__])
                    if underline_repairs or rule_fragment_repairs:profile=table_profile(content,expected)
                if native and '<img' in content and pdf_repair_enabled(args,'math'):
                    from docling_scan_text import restore_formula_cells
                    args.pdf_export_context=dict(stage='source_cell_formula',page=page_no,
                        element=item.self_ref,bbox=bbox)
                    content,coherent_formula_repairs=restore_formula_cells(content,native[1],page_no,
                        manifest,sys.modules[__name__],formula_recognizer,image,bbox)
                    if coherent_formula_repairs:profile=table_profile(content,expected)
                if page_scans and native and not args.primary_ocr_only:
                    from docling_scan_text import restore as restore_raster_cell_text
                    content,cell_text_repairs=restore_raster_cell_text(content,native[1],page_no,
                        manifest,sys.modules[__name__],formula_recognizer,image,bbox)
                    if cell_text_repairs:
                        profile=table_profile(content,expected)
                if (page_scans and native and method == 'raster_geometry' and not args.primary_ocr_only
                        and not native[1].get('hidden_ocr_direction_conflict')):
                    from docling_table_content import restore_scan_table_scripts
                    def recognize_cell_script(data, metadata):
                        nonlocal retries
                        retries += 1
                        if args.pdf_formula_reader=='p2t':
                            from docling_p2t import read,resolve_device
                            device=resolve_device() if args.pdf_formula_device=='auto' else args.pdf_formula_device
                            return latex_math(read(data,device),'inline')
                        return formula_recognizer(data, metadata)
                    content, script_repairs = restore_scan_table_scripts(
                        content, native[1], image, bbox, native_reader.chars,
                        recognize_cell_script, image_html, diagnostic_event, page_no)
                    if script_repairs:
                        native[1].setdefault('restored_cell_math', []).extend(script_repairs)
                        profile = table_profile(content, expected)
                if pdf_repair_enabled(args, 'math') and native:
                    from docling_pdf_repairs import repair_table_math
                    args.pdf_export_context = dict(stage='table_math', page=page_no, element=item.self_ref, bbox=bbox)
                    manifest['active_math_element'] = item.self_ref
                    content, math_repairs = repair_table_math(content, native[1], formula_recognizer,
                        page_no, manifest, sys.modules[__name__], source_image=image, source_region=bbox,
                        source_reader=None if native[1].get('hidden_ocr_direction_conflict') else native_reader)
                    if math_repairs:
                        native[1].setdefault('restored_cell_math', []).extend(math_repairs)
                        profile = table_profile(content, expected)
                if native and '<img' in content and not args.primary_ocr_only:
                    from docling_scan_text import restore as restore_raster_cell_text
                    content,final_cell_repairs=restore_raster_cell_text(content,native[1],page_no,
                        manifest,sys.modules[__name__],formula_recognizer,image,bbox)
                    if final_cell_repairs:
                        profile=table_profile(content,expected)
                if native and not args.primary_ocr_only:
                    from docling_scan_text import restore_unreliable_cells,restore_code_labels
                    content,unreliable_cell_repairs=restore_unreliable_cells(content,native[1],image,bbox,
                        page_no,manifest,sys.modules[__name__])
                    content,code_label_repairs=restore_code_labels(content,native[1],records,bbox,
                        page_no,manifest,sys.modules[__name__])
                    if unreliable_cell_repairs or code_label_repairs:
                        profile=table_profile(content,expected)
                from docling_pdf_table_audit import source_grid as audit_source_cell_grid
                audit_source_cell_grid(content,native[1] if native else None,page_no,item.self_ref,
                    manifest,sys.modules[__name__])
                item.data = TableData(num_rows=profile["rows"], num_cols=profile["columns"], table_cells=[
                    TableCell(text=cell["text"], row_span=cell["row_span"], col_span=cell["column_span"],
                              start_row_offset_idx=cell["row"], end_row_offset_idx=cell["row"] + cell["row_span"],
                              start_col_offset_idx=cell["column"], end_col_offset_idx=cell["column"] + cell["column_span"],
                              column_header=cell["header"]) for cell in profile["cells"]
                ])
            else:
                text_region = None
                if (args.primary_ocr_only and not chosen and native is None and not source_graphics
                        and not primary_region_guard and not source_cell_fallback and not conflict
                        and pdf_repair_enabled(args, 'boundaries')):
                    from docling_pdf_primary_text_region import recover_primary_text_region
                    text_region = recover_primary_text_region(image, bbox, records, page_no, sys.modules[__name__])
                if text_region is not None:
                    content, text_proof = text_region
                    method = 'primary_text_region'
                    profile = dict(issues=[], columns=0, rows=0, text=semantic_ocr_text(content))
                    item.data = TableData(num_rows=0, num_cols=0, table_cells=[])
                    text_proof['ref'] = item.self_ref
                    manifest.setdefault('primary_text_regions', []).append(text_proof)
                    diagnostic_event('PRIMARY_TEXT_REGION_RESTORED', **text_proof)
                else:
                    method = "source_image"
                    actual_source_image_bbox = list(bbox)
                    if getattr(args, 'pdf_source_image_bounds', True) and not primary_region_guard:
                        if native_reader is None:
                            try:
                                native_reader = NativeTableReader(normalized)
                            except (OSError, ValueError, RuntimeError):
                                pass
                        image, actual_source_image_bbox = _source_image_bounds_crop(image, bbox,
                            page_no, item.self_ref, doc, native_reader, regions, manifest)
                    content = image_html(image, actual_source_image_bbox[2] - actual_source_image_bbox[0], "table")
                    profile = chosen[3] if chosen else {"issues": [primary_region_guard['reason'] if primary_region_guard else
                                                           'source_cell_ownership_not_proven' if source_cell_fallback else 'no_candidate'],
                                                       "columns": 0, "rows": 0, "text": ""}
                    # Never persist rejected cell contents as the repaired table data.
                    item.data = TableData(num_rows=0, num_cols=0, table_cells=[])
                    diagnostic_event('TABLE_SOURCE_IMAGE_FALLBACK', page_no=page_no, ref=item.self_ref, bbox=bbox,
                                     selected_method=chosen[1] if chosen else None, issues=profile.get('issues',[]),
                                     numeric_conflict=conflict, retry_stop_reason=retry_stop_reason,
                                     attempted_reads=len(candidates), crop_width=image.width, crop_height=image.height)
            diagnostic_event('TABLE_CHOSEN', phase='final', page_no=page_no, ref=item.self_ref, method=method,
                             rows=profile.get('rows'), columns=profile.get('columns'), score=profile.get('score'),
                             issues=profile.get('issues',[]), warnings=profile.get('warnings',[]), numeric_conflict=conflict,
                             retained_ocr_graphic_count=len(retained_graphics))
            routes[item.self_ref] = content
            if join_geometry and len(join_geometry["x_grid"]) - 1 != profile.get("columns"):
                join_geometry = None
            if not join_geometry and raster_positions and len(raster_positions) - 1 == profile.get("columns"):
                join_geometry = {"basis": "raster_rules", "bbox": bbox,
                                 "x_grid": [bbox[0] + x / image.width * (bbox[2] - bbox[0]) for x in raster_positions]}
            if not join_geometry and bbox[1] < .15 * doc.pages[page_no].size.height:
                positions = raster_column_positions(image, page_edge=True)
                if positions and len(positions) - 1 == profile.get("columns"):
                    join_geometry = {"basis": "raster_edge_rules", "bbox": bbox,
                                     "x_grid": [bbox[0] + x / image.width * (bbox[2] - bbox[0]) for x in positions]}
            table_regions.append((page_no, bbox, profile.get("text", "")))
            manifest["tables"].append({"ref": item.self_ref, "page": page_no, "bbox": table_source_bounds,
                "source_bbox": source_bbox(table_source_bounds, orientation[page_no]),
                "source_render_bbox": list(bbox),
                "actual_source_image_bbox": actual_source_image_bbox if method == 'source_image' else None,
                "method": method, "raster_columns": raster_expected, "selected": profile,
                "numeric_disagreement": conflict,
                "regional_attempts": attempt_log,
                "header_recovery": header_recovery,
                "pdf_geometry": native[1] if native else None,
                "source_graphics_count": len(source_graphics), "detected_raster_rules": detected_rules,
                "retained_ocr_cell_graphics": retained_graphics,
                "page_scan_backgrounds": len(page_scans),
                "join_geometry": join_geometry,
                "fallback_reason": (profile.get("issues", []) + (["numeric_disagreement"] if conflict else [])) if method == "source_image" else [],
                "html": content if method != "source_image" else None,
                "candidates": [{"method": candidate[1], **candidate[3]} for candidate in candidates]})
            print(f"Table: page {page_no}; {method}; columns {profile.get('columns')}; "
                  f"issues {profile.get('issues', [])}; numeric disagreement {conflict}", flush=True)
            if profile.get("span_repairs"):
                print(f"Table grid repaired: page {page_no}; {len(profile['span_repairs'])} operations; cell contents retained", flush=True)
            if retained_graphics:
                print(f"Source graphics retained in OCR cells: page {page_no}; {len(retained_graphics)} fragments", flush=True)
            if method == "source_image" and profile.get("numeric_outliers"):
                print(f"Rejected numeric cells: page {page_no}; "
                      f"{json.dumps(profile['numeric_outliers'], ensure_ascii=False)}", flush=True)

        # Remove only spatially coincident flattened table copies, preserving notes/captions.
        manifest["preserved_table_notes"] = []
        protected_source_notes = set(manifest.get('protected_image_table_text_refs', []))
        for item in doc.texts:
            if item.self_ref in protected_source_notes:
                manifest['preserved_table_notes'].append(item.self_ref)
                diagnostic_event('SOURCE_TABLE_NOTE_DUPLICATE_PROTECTED',ref=item.self_ref,
                    reason='source_insert_note_has_separate_publication_ownership')
                continue
            if item.label not in {DocItemLabel.TEXT, DocItemLabel.LIST_ITEM}:
                continue
            region = item_region(item, doc)
            if region:
                for record in records:
                    if (record["page_no"] != region[0] or "<table" not in record["html"]
                            or overlap_ratio(region[1], record["bbox"]) <= 0.75):
                        continue
                    # Exact textual correspondence avoids deleting a nearby paragraph
                    # merely because it shares words or coordinates with the table.
                    full = semantic_ocr_text(record["html"])
                    if "".join(item.text.split()) != "".join(full.split()):
                        continue
                    notes = semantic_ocr_text(record["html"], outside_tables=True)
                    item.text = item.orig = notes
                    for prov in item.prov:
                        prov.charspan = (0, len(notes))
                    if notes:
                        manifest["preserved_table_notes"].append(item.self_ref)
                    else:
                        excluded.add(item.self_ref)
                    break
            # A list label alone is not evidence of duplication. For lists,
            # require the exact same OCR block and position checked above.
            if item.label == DocItemLabel.LIST_ITEM:
                continue
            text_tokens = set(re.findall(r"\w+", item.text.lower()))
            if not region or len(text_tokens) < 8:
                continue
            for page_no, bbox, table_text in table_regions:
                table_tokens = set(re.findall(r"\w+", table_text.lower()))
                if (region[0] == page_no and overlap_ratio(region[1], bbox) > 0.75
                        and len(text_tokens & table_tokens) / len(text_tokens) > 0.72):
                    excluded.add(item.self_ref)
                    break
        manifest["excluded_flattened_copies"] = sorted(excluded)

        # Layout assembly may absorb the OCR block into a TableItem and omit
        # its trailing footnote. Preserve text outside the semantic table even
        # when no flattened TextItem was produced in the first place.
        from docling_core.types.doc import BoundingBox, CoordOrigin, ProvenanceItem
        for record in table_records:
            notes = semantic_ocr_text(record["html"], outside_tables=True)
            compact = "".join(notes.split())
            if not compact:
                continue
            page_no = record["page_no"]
            if any((reg := item_region(text, doc)) and reg[0] == page_no
                   and compact in "".join(text.text.split()) for text in doc.texts
                   if text.self_ref not in excluded):
                continue
            sibling = next((table for table in doc.tables if
                            (reg := item_region(table, doc)) and reg[0] == page_no
                            and overlap_ratio(reg[1], record["bbox"]) > 0.6), None)
            if sibling is None:
                continue
            box = record["bbox"]
            prov = ProvenanceItem(page_no=page_no, charspan=(0, len(notes)),
                                  bbox=BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3],
                                                   origin=CoordOrigin.TOPLEFT))
            restored = doc.insert_text(sibling=sibling, label=DocItemLabel.TEXT,
                                       text=notes, orig=notes, prov=prov, after=True)
            manifest["preserved_table_notes"].append(restored.self_ref)

        if pdf_repair_enabled(args, 'boundaries'):
            from docling_pdf_repairs import recover_uncovered_ocr_table_text, join_source_footnote_continuations
            recover_uncovered_ocr_table_text(doc, native_reader, table_records, routes, excluded,
                note_routes, manifest, sys.modules[__name__])
            join_source_footnote_continuations(doc, native_reader, excluded, manifest, sys.modules[__name__])
        from docling_pdf_repairs import retain_source_matched_table_tokens
        retain_source_matched_table_tokens(doc, records, routes, excluded, manifest, sys.modules[__name__])
        restore_ocr_paragraphs(doc, records, excluded, manifest, sys.modules[__name__])
        clean_pdf_text_flow(doc, records, excluded, manifest, native_reader)
        from docling_pdf_artifacts import suppress_repeated_margin_artifacts
        suppress_repeated_margin_artifacts(doc, native_reader, picture_routes,
            excluded, manifest, sys.modules[__name__])
        for picture in doc.pictures:
            reg = item_region(picture, doc)
            if not reg or picture.children or picture.captions:
                continue
            for ref, entry in image_backed_tables.items():
                if entry['page'] != reg[0] or ref not in routes:
                    continue
                final_profile = table_profile(routes[ref])
                if table_owns_source_image(entry, reg[1], final_profile):
                    picture_routes[picture.self_ref] = ''
                    excluded.add(picture.self_ref)
                    diagnostic_event('TABLE_SOURCE_PICTURE_DUPLICATE_REMOVED', ref=picture.self_ref,
                        table=ref, page_no=reg[0], bbox=reg[1], reason='source_image_already_structured')
                    break
        if not args.primary_ocr_only:
            from docling_pdf_repairs import repair_source_references
            repair_source_references(doc,native_reader,excluded,manifest,sys.modules[__name__])
        from docling_pdf_table_rasters import suppress_layout_table_rasters
        suppress_layout_table_rasters(doc,manifest,excluded,sys.modules[__name__])
        suppress_table_picture_duplicates(doc, manifest, picture_routes, excluded)
        restore_table_reading_order(doc, excluded, manifest)
        if pdf_repair_enabled(args, 'boundaries'):
            from docling_pdf_repairs import mark_terminal_source_tables
            mark_terminal_source_tables(doc, native_reader, manifest, sys.modules[__name__])
        if args.primary_ocr_only and pdf_repair_enabled(args, 'subrows'):
            from docling_pdf_primary_layout import repair_primary_fraction_tables
            repair_primary_fraction_tables(doc,routes,manifest,regions,native_reader,records,sys.modules[__name__])
        if pdf_repair_enabled(args, 'boundaries'):
            from docling_pdf_table_layout import compact_source_rule_rows,join_double_rule_headers
            compact_source_rule_rows(doc,routes,manifest,native_reader,regions,sys.modules[__name__])
            join_double_rule_headers(doc,routes,manifest,native_reader,regions,sys.modules[__name__])
        if args.primary_ocr_only and pdf_repair_enabled(args, 'subrows'):
            from docling_pdf_primary_layout import expand_primary_fraction_rows
            expand_primary_fraction_rows(doc,routes,manifest,sys.modules[__name__])
        if args.primary_ocr_only and pdf_repair_enabled(args, 'boundaries'):
            from docling_pdf_formula_bands import recover_formula_band_routes
            recover_formula_band_routes(doc,routes,excluded,manifest,native_reader,regions,records,sys.modules[__name__])
        if (args.primary_ocr_only and getattr(args, 'pdf_borderless_rows', True)
                and pdf_repair_enabled(args, 'subrows')):
            repair_borderless_table_routes(doc,routes,manifest,native_reader,regions,sys.modules[__name__])
        if (args.primary_ocr_only and getattr(args, 'pdf_duplicate_primary_tables', True)
                and pdf_repair_enabled(args, 'boundaries')):
            table_owned_refs = {item.self_ref: {role: [getattr(ref, 'cref', None) or str(ref)
                for ref in (getattr(item, role, None) or [])]
                for role in ('captions', 'footnotes', 'children')} for item in doc.tables}
            apply_recovered_packet_ownership_guard(routes,excluded,manifest,records,original_table_refs,
                sys.modules[__name__],table_owned_refs=table_owned_refs)
        join_page_tables(doc, routes, excluded, manifest, repair_boundaries=pdf_repair_enabled(args, 'boundaries'),regions=regions,
                         primary_row_boundaries=getattr(args, 'pdf_primary_row_boundaries', True))
        from docling_pdf_flow import join_same_page_sections
        join_same_page_sections(doc, routes, excluded, note_routes, manifest, sys.modules[__name__])
        if pdf_repair_enabled(args, 'empty-columns'):
            from docling_pdf_repairs import compact_table_routes
            compact_table_routes(doc, routes, manifest, sys.modules[__name__])
        if pdf_repair_enabled(args, 'boundaries'):
            from docling_pdf_repairs import attach_recovered_source_notes
            attach_recovered_source_notes(routes, excluded, note_routes, manifest, sys.modules[__name__])
        if args.primary_ocr_only and pdf_repair_enabled(args, 'subrows'):
            from docling_pdf_semantic_rows import finalize_source_record_routes
            finalize_source_record_routes(doc, routes, excluded, manifest, sys.modules[__name__])
        diagnostic_event('TABLE_JOIN_SUMMARY', source_fragment_count=len(manifest['tables']),
                         logical_table_count=sum(not entry.get('joined_into') for entry in manifest['tables']),
                         structured_logical_count=sum(not entry.get('joined_into') and '<table' in routes.get(entry['ref'],'')
                                                      for entry in manifest['tables']),
                         joined_fragment_count=len(manifest.get('joined_tables',[])),
                         decision_count=len(manifest.get('table_join_decisions',[])), excluded_item_count=len(excluded))

        repaired_slots, repaired_slot_pages = {}, set()
        if pdf_repair_enabled(args, 'math'):
            from docling_pdf_repairs import repair_inline_placeholders
            repaired_slots, repaired_slot_pages = repair_inline_placeholders(doc, native_reader, regions,
                formula_recognizer, picture_routes, excluded, manifest, sys.modules[__name__])
        inline_math, inline_math_pages = ({}, set()) if args.primary_ocr_only else prepare_pdf_inline_math(
            doc, native_reader, regions, picture_routes, formula_recognizer)
        for ref, replacements in repaired_slots.items():
            inline_math.setdefault(ref, []).extend(replacements)
        inline_math_pages.update(repaired_slot_pages)
        paragraph_pairs = source_parenthesis_continuations(doc,native_reader)
        paragraph_refs = {ref for pair in paragraph_pairs for ref in pair}
        from docling_pdf_notes import append_owned_footnotes

        if captioned_figures:
            reconcile_captioned_figures(doc, captioned_figures, captioned_figure_routes, routes, picture_routes,
                                       excluded, manifest, regions, sys.modules[__name__])

        class RoutedTables(HTMLTableSerializer):
            def serialize(self, *, item, doc_serializer, doc, **kwargs):
                if item.self_ref in captioned_figures:
                    fragment = serialize_captioned_figure(item, captioned_figures[item.self_ref],
                        captioned_figure_routes[item.self_ref], doc_serializer, **kwargs)
                    fragment = append_owned_footnotes(fragment, item, doc, doc_serializer,
                        excluded, manifest, sys.modules[__name__], kwargs) if fragment else ''
                    return create_ser_result(text=fragment, span_source=item)
                if item.self_ref not in routes:
                    diagnostic_event("TABLE_SERIALIZE", ref=item.self_ref, route="default",
                                     pages=[prov.page_no for prov in (item.prov or [])][:16], excluded=item.self_ref in excluded)
                    result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
                    diagnostic_html(getattr(result, "text", ""), "table_serializer", ref=item.self_ref, route="default")
                    fragment = append_owned_footnotes(result.text, item, doc, doc_serializer,
                        excluded, manifest, sys.modules[__name__], kwargs)
                    return create_ser_result(text=fragment,span_source=item)
                fragment = routes[item.self_ref]
                if not fragment:
                    diagnostic_event("TABLE_SERIALIZE", ref=item.self_ref, route="empty",
                                     pages=[prov.page_no for prov in (item.prov or [])][:16], excluded=item.self_ref in excluded)
                    return create_ser_result(text="", span_source=item)
                caption = doc_serializer.serialize_captions(item=item, tag="caption", **kwargs).text
                parsed = BeautifulSoup(fragment, "html.parser")
                if parsed.table:
                    parsed.table['data-pdf-table-ref']=item.self_ref
                if caption and parsed.table and not parsed.table.caption:
                    parsed.table.insert(0, BeautifulSoup(caption, "html.parser"))
                elif caption and not parsed.table:
                    fragment = caption + fragment
                if parsed.table:
                    fragment = str(parsed)
                route = "routed_table" if parsed.table else "routed_fragment"
                fragment = append_owned_footnotes(fragment, item, doc, doc_serializer,
                    excluded, manifest, sys.modules[__name__], kwargs)
                diagnostic_event("TABLE_SERIALIZE", ref=item.self_ref, route=route,
                                 pages=[prov.page_no for prov in (item.prov or [])][:16], excluded=item.self_ref in excluded,
                                 caption_present=bool(caption), caption_in_table=bool(parsed.table and parsed.table.caption))
                diagnostic_html(fragment, "table_serializer", ref=item.self_ref, route=route)
                return create_ser_result(text=fragment, span_source=item)

        class SourceFormulas(HTMLTextSerializer):
            def serialize(self, *, item, doc_serializer, doc, **kwargs):
                region = item_region(item, doc)
                args.pdf_export_context = dict(stage='text_serialization', element=item.self_ref,
                    page=region[0] if region else None, bbox=region[1] if region else None)
                if item.self_ref in excluded:
                    diagnostic_event('TEXT_SERIALIZE_SKIP',ref=item.self_ref,reason='excluded')
                    return create_ser_result(text="", span_source=item)
                if item.self_ref in note_routes:
                    return create_ser_result(text=note_routes[item.self_ref], span_source=item)
                if item.label != DocItemLabel.FORMULA and "<math" in item.text:
                    parsed = BeautifulSoup(item.text, "html.parser")
                    if any(latex_math(tag.get_text(), "inline") is None for tag in parsed.find_all("math")):
                        region = item_region(item, doc)
                        if region:
                            if pdf_repair_enabled(args, 'math'):
                                raise RuntimeError(f'Unrenderable inline equation on page {region[0]}, {item.self_ref}; no HTML published')
                            diagnostic_event('TEXT_IMAGE_FALLBACK',ref=item.self_ref,page=region[0],bbox=region[1],reason='inline_math_cannot_render')
                            rendered = image_html(regions.crop(*region), region[1][2] - region[1][0], "text")
                            return create_ser_result(text=rendered, span_source=item)
                result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
                if item.self_ref not in inline_math and item.self_ref not in paragraph_refs:
                    return result
                fragment=result.text
                if item.self_ref in inline_math:
                    fragment=apply_inline_math(fragment,inline_math[item.self_ref])
                if item.self_ref in paragraph_refs:
                    parsed=BeautifulSoup(fragment,'html.parser')
                    root=parsed.find(['p','li','h1','h2','h3','h4','h5','h6'])
                    if root:root['data-pdf-text-ref']=item.self_ref
                    fragment=str(parsed)
                return create_ser_result(text=fragment,span_source=item)

            def _process_formula(self, *, item, text, orig, doc, image_mode, formula_to_mathml, is_inline_scope):
                formula = text or orig
                if "<math" in formula:
                    parsed = BeautifulSoup(formula, "html.parser")
                    formula = " ".join(tag.get_text() for tag in parsed.find_all("math"))
                rendered = latex_math(formula, "inline" if is_inline_scope else "block")
                region = item_region(item, doc)
                if rendered and region and region[0] in inline_math_pages and not args.primary_ocr_only:
                    page = native_reader.load_page(region[0])
                    assets = [obj for obj in page.images if obj['width'] > 40 and obj['height'] < 35
                              and overlap_ratio([obj['x0'],obj['top'],obj['x1'],obj['bottom']],region[1]) > .8]
                    if len(assets) == 1:
                        obj = assets[0]
                        asset = regions.crop(region[0],[obj['x0']-2,obj['top']-2,obj['x1']+2,obj['bottom']+2])
                        try:
                            buffer=BytesIO();asset.save(buffer,'PNG')
                            checked=formula_recognizer(buffer.getvalue(),dict(formula_item=True,
                                width=asset.width,height=asset.height,paragraph_text='',inline=False,media_name=item.self_ref))
                            if checked:
                                parsed=BeautifulSoup(checked,'html.parser').math
                                candidate=parsed.get('data-latex','')
                                previous=BeautifulSoup(rendered,'html.parser').math
                                def structure(math):
                                    bases=[node.find(recursive=False).get_text() for node in math.find_all(['msub','msubsup'])
                                           if node.find_parent(['msub','msubsup']) is None and node.find(recursive=False)]
                                    return bases,[node.get_text() for node in math.find_all('mo')]
                                if (candidate and re.findall(r'\d+',candidate)==re.findall(r'\d+',formula)
                                        and structure(previous)==structure(parsed)):
                                    formula=candidate;rendered=checked;parsed['display']='block';rendered=str(parsed)
                                    diagnostic_event('PDF_RASTER_FORMULA_CHECK',page=region[0],ref=item.self_ref,latex=candidate)
                        except Exception as error:
                            diagnostic_event('PDF_RASTER_FORMULA_CHECK_ERROR',page=region[0],error=str(error))
                        finally:asset.close()
                if rendered is None:
                    if region is None:
                        diagnostic_event('FORMULA_ERROR',ref=item.self_ref,reason='no_renderable_math_or_provenance')
                        raise RuntimeError(f"Formula has neither renderable mathematics nor source provenance: {item.self_ref}")
                    # Extend the SOURCE region before cropping as well as adding
                    # white OCR padding; whitespace cannot restore cut indices.
                    box = region[1]
                    image = regions.crop(region[0], [box[0]-2, box[1]-2, box[2]+2, box[3]+2])
                    try:
                        buffer = BytesIO()
                        image.save(buffer, format='PNG')
                        if not args.primary_ocr_only:
                            rendered = formula_recognizer(buffer.getvalue(), dict(
                                media_name=item.self_ref, width=image.width, height=image.height,
                                formula_item=True, paragraph_text='', inline=is_inline_scope))
                        else:
                            diagnostic_event('PDF_PRIMARY_FORMULA_SOURCE_KEEP', ref=item.self_ref,
                                page=region[0], bbox=box, reason='no_renderable_primary_surya_formula')
                        if rendered:
                            recovered_math = BeautifulSoup(rendered, 'html.parser').math
                            recovered_math['display'] = 'inline' if is_inline_scope else 'block'
                            rendered = str(recovered_math)
                    except Exception as exc:
                        diagnostic_event('PDF_FORMULA_RECOVERY_ERROR',ref=item.self_ref,error=str(exc))
                    if rendered is None:
                        if pdf_repair_enabled(args, 'math'):
                            raise RuntimeError(f'Equation has no MathML on page {region[0]}, {item.self_ref}; no HTML published')
                        rendered = image_html(image, box[2]-box[0]+4, "formula")
                    image.close()
                diagnostic_event('FORMULA_RESULT',ref=item.self_ref,page=region[0] if region else None,
                                 bbox=region[1] if region else None,method='mathml' if '<math' in rendered else 'source_image',
                                 reason='renderable_latex' if '<math' in rendered else 'latex_cannot_render',latex=diagnostic_summary(formula))
                manifest["formulas"].append({"ref": item.self_ref, "page": region[0] if region else None,
                    "bbox": region[1] if region else None, "latex": formula,
                    "source_bbox": source_bbox(region[1], orientation[region[0]]) if region else None,
                    "method": "mathml" if "<math" in rendered else "source_image"})
                return rendered

        class SourcePictures(HTMLPictureSerializer):
            def serialize(self, *, item, doc_serializer, doc, **kwargs):
                if item.self_ref in captioned_figures:
                    fragment = serialize_captioned_figure(item, captioned_figures[item.self_ref],
                        captioned_figure_routes[item.self_ref], doc_serializer, **kwargs)
                    fragment = append_owned_footnotes(fragment, item, doc, doc_serializer,
                        excluded, manifest, sys.modules[__name__], kwargs) if fragment else ''
                    return create_ser_result(text=fragment, span_source=item)
                if item.self_ref in excluded:
                    diagnostic_event('PICTURE_SERIALIZE_SKIP',ref=item.self_ref,
                        reason='verified_source_owned_duplicate')
                    return create_ser_result(text='',span_source=item)
                if item.self_ref in picture_routes:
                    route = "routed" if picture_routes[item.self_ref] else "empty"
                    diagnostic_event("PICTURE_SERIALIZE", ref=item.self_ref, route=route,
                                     pages=[prov.page_no for prov in (item.prov or [])][:16], excluded=item.self_ref in excluded,
                                     caption_refs=[str(getattr(caption, "cref", ""))[:160] for caption in (item.captions or [])][:16],
                                     caption_count=len(item.captions or []))
                    diagnostic_html(picture_routes[item.self_ref], "picture_serializer", ref=item.self_ref, route=route)
                    fragment = append_owned_footnotes(picture_routes[item.self_ref],item,doc,doc_serializer,
                        excluded,manifest,sys.modules[__name__],kwargs)
                    return create_ser_result(text=fragment, span_source=item)
                diagnostic_event("PICTURE_SERIALIZE", ref=item.self_ref, route="default",
                                 pages=[prov.page_no for prov in (item.prov or [])][:16], excluded=item.self_ref in excluded,
                                 caption_refs=[str(getattr(caption, "cref", ""))[:160] for caption in (item.captions or [])][:16],
                                 caption_count=len(item.captions or []))
                result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
                diagnostic_html(getattr(result, "text", ""), "picture_serializer", ref=item.self_ref, route="default")
                fragment = append_owned_footnotes(result.text,item,doc,doc_serializer,
                    excluded,manifest,sys.modules[__name__],kwargs)
                return create_ser_result(text=fragment,span_source=item)

        serializer = HTMLDocSerializer(doc=doc, table_serializer=RoutedTables(), text_serializer=SourceFormulas(),
                                       picture_serializer=SourcePictures(),
                                       params=HTMLParams(image_mode=ImageRefMode.EMBEDDED, formula_to_mathml=True))
        content = render_surya_math(serializer.serialize().text)
        soup = BeautifulSoup(content, "html.parser")
        if getattr(args, 'pdf_table_notes', True) and pdf_repair_enabled(args, 'boundaries'):
            attach_primary_table_notes(soup,manifest,records,sys.modules[__name__], reader=native_reader,
                remove_duplicates=getattr(args, 'pdf_remove_duplicate_table_notes', False))
        manifest['metric_unit_normalizations'] = normalize_metric_units(soup)
        if pdf_repair_enabled(args, 'math', format_only=True):
            if getattr(args, 'pdf_source_unit_composition', True):
                repair_source_unit_compositions(soup,manifest,regions,native_reader,records,sys.modules[__name__])
            if getattr(args, 'pdf_source_scripts', True):
                repair_source_script_duplicates(soup,manifest,regions,native_reader,sys.modules[__name__])
            from docling_pdf_footnote_math import restore_table_math_note_markers
            restore_table_math_note_markers(soup,manifest,sys.modules[__name__])
            from docling_pdf_primary_layout import merge_primary_denominator_tails
            merge_primary_denominator_tails(soup,manifest,regions,native_reader,sys.modules[__name__])
        if getattr(args, 'pdf_table_notes', True) and pdf_repair_enabled(args, 'boundaries'):
            _primary_note_normalize_footers(soup,manifest,sys.modules[__name__],
                remove_duplicates=getattr(args, 'pdf_remove_duplicate_table_notes', False))
            from docling_pdf_primary_partitions import attach_primary_external_notes
            attach_primary_external_notes(soup,manifest,sys.modules[__name__])
        for left,right in paragraph_pairs:
            a=soup.find(attrs={'data-pdf-text-ref':left});b=soup.find(attrs={'data-pdf-text-ref':right})
            if a and b:
                a.append(' ')
                for child in list(b.contents):a.append(child.extract())
                b.decompose()
                diagnostic_event('PDF_PARAGRAPH_CONTINUATION',left=left,right=right,basis='source_font_and_parentheses')
        for node in soup.select('[data-pdf-text-ref]'):del node['data-pdf-text-ref']
        for style in soup.find_all("style"):
            style.decompose()
        style = soup.new_tag("style")
        style.string = LOCAL_CSS
        soup.head.append(style)
        soup.html["lang"] = "ru"
        for table in soup.find_all("table"):
            container = soup.new_tag("div", attrs={"class": "table-scroll"})
            if table_profile(str(table))["columns"] >= 7:
                container["class"] = "table-scroll wide"
            table.wrap(container)
        for figure in soup.select("figure.source-table"):
            figure.wrap(soup.new_tag("div", attrs={"class": "table-scroll wide"}))
        for bad in soup.select(".formula-not-decoded"):
            raise RuntimeError("Formula placeholder blocked before HTML publication")
        from docling_pdf_table_audit import register_final_graphic_assets
        register_final_graphic_assets(soup,manifest,sys.modules[__name__])
        from docling_scan_text import audit as audit_editable_tables
        manifest['table_content_issues']=(audit_editable_tables(soup,manifest,sys.modules[__name__])
            +manifest.get('source_grid_mismatches',[])+manifest.get('unreliable_cells_unresolved',[])
            +manifest.get('source_graphic_losses',[])+manifest.get('known_table_raster_policy_issues',[]))
        manifest['table_checks_policy']='diagnostic_only'
        manifest['table_quality_warnings']=[dict(issue,severity='warning') for issue in
            manifest['table_content_issues']+manifest.get('source_grid_reviews',[])]
        from docling_pdf_review import annotate_primary_reviews
        annotate_primary_reviews(soup,manifest,sys.modules[__name__])
        manifest["regional_requests"] = retries
        diagnostic_event('PDF_IMAGE_BUDGET_SUMMARY', counters=manifest.get('image_budgets', {}),
                         classification_assets=sum(p['assets'] for p in manifest.get('image_budgets', {}).get('classification', {}).get('pages', {}).values()),
                         formula_candidates=sum(p['assets'] for p in manifest.get('image_budgets', {}).get('formula', {}).get('pages', {}).values()),
                         graphics_kept=sum(manifest.get('source_graphics_kept', {}).values()),
                         deferred=len(manifest.get('deferred_math', [])), rejected_grid=len(manifest.get('rejected_grid_math', [])))
        return str(soup), manifest
    finally:
        diagnostics = getattr(args, 'pdf_diagnostics_current', None)
        if diagnostics:
            try:
                (diagnostics/'quality.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, TypeError) as error:
                print(f'WARNING: Could not save PDF quality diagnostics: {error}', file=sys.stderr)
        formula_recognizer.close()
        if native_reader is not None:
            native_reader.close()
        regions.close()
        _PRIMARY_OCR_ONLY.reset(policy_token)
        if prior_content_policy is None:
            os.environ.pop('DOCLING_PRIMARY_OCR_ONLY', None)
        else:
            os.environ['DOCLING_PRIMARY_OCR_ONLY'] = prior_content_policy


API_PROMPT = r"""Transcribe this PDF page image to semantic HTML. Output HTML only: no Markdown fences, no commentary.
Minified HTML: no style/class/id attributes, no indentation, no comments except printed page-number comments.
Keep all visible text in reading order (Russian Cyrillic, numbers, decimal commas, symbols, units). Never invent, infer or silently correct source values or formulas. If a fragment is illegible, write [НЕРАЗБОРЧИВО].
Use headings and paragraphs where appropriate. Recognize tables even without ruling lines: <table>, <thead> for repeated headers, <tbody>, <tr>, <th>, <td> with exact rowspan/colspan; keep captions and table numbers. On every <table> set data-columns="N" (total logical column count) and data-continued="true" if it continues from the preceding page (usually a grid at the top of the page with body rows and no new caption/header), otherwise data-continued="false". Keep rowspans within the fragment; do not guess unseen cells.
Multi-page tables: retain the full logical column grid on every fragment, even without repeated headers. Count all columns, including empty ones; never omit a column, collapse equal adjacent values into one cell, or invent extra colspan columns. Each row must fill the grid, accounting for active rowspans. Mark continuations even when no "continued" label is printed; use <thead> only for actual repeated headers, <tfoot> only for final notes. When a table number is visible, set data-table-id to that number. Mark running page headers and footers, including standalone document identifiers in the page margins, with data-page-furniture="true". Never mark body citations, section headings, the main cover title, table cells or footnotes as page furniture.
Printed page/sheet numbers in the bottom margin (left, center or right), such as "45", "Лист 3" or "Стр. 12", must appear only as <!-- Номер листа: N --> at the end of the page HTML. Use the printed number, never the physical PDF page index; do not invent missing page numbers. Never apply this rule to numbers in body text, tables or formulas.
Formulas: LaTeX without dollar delimiters inside <span data-formula="inline" data-bbox="left,top,right,bottom">LATEX</span> or <div data-formula="display" data-bbox="left,top,right,bottom">LATEX</div>. Bbox coordinates use the same entire-page 0..1000 system as diagrams. If a formula cannot be transcribed, preserve it as an <img data-bbox="..." alt="source formula">; never replace a visible formula with a missing/unrecognized-formula message. Preserve the source alphabet for EACH symbol: Latin, Greek and Cyrillic may coexist in one formula, including its indices and \text{}/\mathrm{} contents. Do not select one alphabet for a whole expression, transliterate Cyrillic, or normalize visually similar letters across alphabets. Use adjacent source definitions only as evidence for that particular symbol; never infer or correct numerical values or indices from a familiar equation. Russian explanatory words such as "при", "где", "если" stay OUTSIDE data-formula elements.
Diagrams (also inside table cells): <img data-bbox="left,top,right,bottom" alt="source diagram"> at the original position; integer coordinates normalized to 0..1000 over the ENTIRE page image, origin top-left, including all labels and axes. Never replace a diagram with a text description, ASCII art, SVG or an invented URL."""


def output_base(args, source):
    directory = args.output
    suffix = getattr(args, 'output_suffix', '')
    if source.suffix.lower() == ".pdf":
        label = f"_p{args.pages}" if args.pages else "_all"
        return directory / f"{source.stem}_{args.mode}{label}{suffix}"
    return directory / f"{source.stem}_{source.suffix[1:].lower()}{suffix}"


def discover_files(input_dir):
    """Walk source folders, excluding generated output and runtime folders."""
    input_dir = Path(input_dir).resolve()
    excluded_names = {".venv", ".gitignore", "__pycache__", ".cache", "output", "convert_docling_log"}

    def scan_error(error):
        print(f"WARNING: Could not scan {error.filename}: {error}", file=sys.stderr, flush=True)

    for directory, folders, filenames in os.walk(input_dir, followlinks=False, onerror=scan_error):
        folders[:] = [
            name for name in folders
            if name.casefold() not in excluded_names
            and not (Path(directory) / name).is_symlink()
            and not (Path(directory) / name).is_junction()
            and (Path(directory) / name).resolve().is_relative_to(input_dir)
        ]
        for name in filenames:
            if name.casefold() in excluded_names:
                continue
            path = Path(directory) / name
            if path.resolve().is_relative_to(input_dir) and path.is_file():
                yield path


def should_skip(args, source):
    if getattr(args, "force", False):
        return False
    path = Path(f"{output_base(args, source)}.{args.format}")
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if path.suffix.lower() == '.html':
        from bs4 import BeautifulSoup
        content = path.read_text(encoding='utf-8-sig')
        published = BeautifulSoup(content, 'html.parser')
        marker = published.find('meta', attrs={'name': 'docling-publication-schema'})
        embedded = any(str(image.get('src', '')).lower().startswith('data:')
                       for image in published.find_all('img'))
        if marker is None or marker.get('content') != '7' or embedded:
            # This is an export migration, not an OCR/cache schema change.
            # Reuse the existing HTML and its source pixels without inference.
            write_atomic(path, content)
            diagnostic_event('HTML_PUBLICATION_MIGRATED', path=path, schema=7)
    return True


def finalize_published_html(path, content):
    """Finalize user-facing HTML only after all source-evidence repairs."""
    from bs4 import BeautifulSoup
    from local_web.assets import externalize_images
    from local_web.structured_export import normalize_table_notes
    from local_web.table_continuations import join_captioned_tables
    from local_web.table_star_notes import normalize_table_star_notes
    from docling_pdf_review import strip_review_annotations

    path = Path(path)
    soup = BeautifulSoup(content, 'html.parser')
    strip_review_annotations(soup)
    note_warnings = normalize_table_notes(soup)
    joins = join_captioned_tables(soup)
    if joins:
        diagnostic_event('HTML_TABLE_CONTINUATIONS_JOINED', joins=joins)
    star_notes = normalize_table_star_notes(soup)
    if star_notes:
        diagnostic_event('HTML_TABLE_STAR_NOTES_INLINED', replacements=star_notes)
    expected_images = [image for image in soup.find_all('img') if image.get('src')]
    assets = externalize_images(soup, path, path.parent / 'src', resource_root=path.parent)
    failed = [image for image in expected_images if image.get('data-image-unavailable') == 'true']
    if failed:
        diagnostic_event('HTML_IMAGE_PUBLICATION_FAILED', path=path, images=len(failed),
                         warnings=assets.get('warnings', []))
        raise ValueError('Не удалось сохранить изображения документа отдельными файлами: '
                         + '; '.join(assets.get('warnings', [])))
    if soup.head is None:
        head = soup.new_tag('head')
        (soup.html or soup).insert(0, head)
    marker = soup.find('meta', attrs={'name': 'docling-publication-schema'})
    if marker is None:
        marker = soup.new_tag('meta', attrs={'name': 'docling-publication-schema'})
        soup.head.append(marker)
    marker['content'] = '7'
    diagnostic_event('HTML_PUBLICATION_FINALIZED', path=path, schema=7,
                     images=len(assets.get('assets', [])), warnings=assets.get('warnings', []),
                     table_note_warnings=note_warnings)
    return str(soup)


def write_atomic(path, content):
    path = Path(path)
    if path.suffix.lower() == '.html':
        content = finalize_published_html(path, strip_html_hyperlinks(content))
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        diagnostic_event('OUTPUT_WRITE_BEGIN',path=path,temporary=temporary,characters=len(content))
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        diagnostic_file(path,'published_output')
        diagnostic_event('OUTPUT_WRITE_END',path=path,published=True)
    finally:
        temporary.unlink(missing_ok=True)


def strip_html_hyperlinks(content):
    """Keep visible text and math, remove navigation from every HTML route."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, 'html.parser')
    links = list(soup.find_all('a'))
    for link in links:
        link.unwrap()
    for area in soup.find_all('area', href=True):
        del area['href']
    if links:
        diagnostic_event('HTML_LINKS_REMOVED', count=len(links))
    return str(soup) if links or soup.find('area') else content


def export_office_document(args, source):
    from docling_office import export_docx_html
    from docling_office_math import OfficeFormulaRecognizer

    diagnostic_event('DOCUMENT_BEGIN', source=source, mode='office-source-order', pages='all')
    diagnostic_file(source, 'source_document')
    print(f'Processing {source} | office-source-order', flush=True)
    data = source
    if source.suffix.lower() in {'.rtf', '.doc', '.dot'}:
        from docling.backend.docx.drawingml.utils import convert_to_modern_format
        diagnostic_event('OFFICE_NORMALIZE_BEGIN', source=source, target='in_memory_docx')
        data = convert_to_modern_format(source, source.suffix[1:], 'docx')
        diagnostic_event('OFFICE_NORMALIZE_END', source=source, bytes=data.getbuffer().nbytes)
    recognizer = OfficeFormulaRecognizer(args, configure_surya, resolve_device,
                                         local_surya_backend, latex_math, diagnostic_event)
    try:
        content, stats = export_docx_html(data, recognizer, diagnostic_event, source_name=source.name)
        content = recognizer.finalize(content)
        output = Path(f'{output_base(args, source)}.{args.format}')
        output.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(output, content)
        diagnostic_html(content, 'published_office_html', source=str(source), output=str(output))
        diagnostic_event('DOCUMENT_END', source=source, **stats)
        print(f'Status: SUCCESS; tables: {stats["tables"]}; image equations: {stats["image_math"]}; '
              f'diagrams: {stats["images"]}; links removed: {stats["links_removed"]}', flush=True)
        print(f'Saved: {output}', flush=True)
    finally:
        recognizer.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Конвертация документов из Input и её подпапок в HTML в Output."
    )
    p.add_argument("--pages", metavar="START-END", default=None,
                   help="страницы PDF, например 1-10 (по умолчанию все)")
    p.add_argument("--mode", choices=("ai", "local"), default="local",
                   help="local: локальная Surya 2 (по умолчанию); ai: OpenAI")
    p.add_argument("--format", choices=("html",), default="html", help="формат результата (html)")
    p.add_argument("--api-key", default=None, help="ключ OpenAI; в режиме ai при отсутствии запрашивается видимым вводом")
    p.add_argument("--input", dest="selected_inputs", nargs="+", type=Path,
                   help="только указанные файлы внутри папки скрипта; относительные пути — от неё")
    p.add_argument("--force", action="store_true", help="повторно обработать уже готовые файлы")
    p.add_argument('--pdf-repairs', nargs='+', default=None,
                   choices=('boundaries', 'math', 'subrows', 'empty-columns', 'all', 'none'),
                   help='исправления локального PDF; по умолчанию все группы')
    p.add_argument('--no-pdf-borderless-rows', dest='pdf_borderless_rows', action='store_false', default=True,
                   help='отключить разбиение склеенных строк по координатам текста в таблицах без внутренних горизонтальных линий')
    p.add_argument('--no-pdf-table-notes', dest='pdf_table_notes', action='store_false', default=True,
                   help='отключить перенос принадлежащих таблице примечаний в последнюю строку')
    p.add_argument('--pdf-remove-duplicate-table-notes', action='store_true', default=False,
                   help='удалять точные внешние дубликаты уже существующих примечаний таблицы с сохранением полного payload в диагностике')
    p.add_argument('--no-pdf-source-cell-ownership', dest='pdf_source_cell_ownership', action='store_false', default=True,
                   help='отключить проверку принадлежности source-кропов физическим ячейкам и полный pixel fallback смешанных ячеек')
    p.add_argument('--no-pdf-source-image-bounds', dest='pdf_source_image_bounds', action='store_false', default=True,
                   help='отключить расширение целых source-image fallback до однозначно принадлежащих исходных глифов')
    p.add_argument('--no-pdf-primary-row-boundaries', dest='pdf_primary_row_boundaries', action='store_false', default=True,
                   help='разрешить прежнее слияние первичных строк только по открытым физическим границам страницы')
    p.add_argument('--no-pdf-source-unit-composition', dest='pdf_source_unit_composition', action='store_false', default=True,
                   help='отключить подтверждение составного знака промилле по исходному растровому объекту без изменения первичных чисел')
    p.add_argument('--no-pdf-source-scripts', dest='pdf_source_scripts', action='store_false', default=True,
                   help='отключить проверку лишних индексов по изображению исходной ячейки')
    p.add_argument('--no-pdf-primary-region-ownership', dest='pdf_primary_region_ownership', action='store_false', default=True,
                   help='отключить сохранение всей рамки изображением при недоказанной привязке первичного OCR к внутренней таблице')
    p.add_argument('--no-pdf-duplicate-primary-tables', dest='pdf_duplicate_primary_tables', action='store_false', default=True,
                   help='отключить защиту от повторного создания таблицы из одного физического первичного OCR-блока')
    p.add_argument('--no-pdf-captioned-figures', dest='pdf_captioned_figures', action='store_false', default=True,
                   help='отключить сохранение объектов с подписью Рисунок N — Название как полных исходных изображений')
    p.add_argument('--output-dir', type=Path, default=None, help='отдельная папка новых HTML')
    p.add_argument('--output-suffix', default='', help='суффикс имени результата перед .html, например _пров')
    p.add_argument('--log-dir', type=Path, default=None, help='отдельная папка логов этого запуска')
    p.add_argument('--diagnostics-dir', type=Path, default=None,
                   help='JSON Docling до экспорта, ответы Surya и данные проверки; уникальная папка на документ')
    p.add_argument('--pdf-math-max-assets', type=int, default=None, help='необязательный лимит формул на страницу; по умолчанию без ограничений, 0 также отключает лимит')
    p.add_argument('--pdf-math-seconds', type=float, default=None, help='необязательный общий бюджет формул на страницу, сек; по умолчанию без ограничений')
    p.add_argument('--pdf-classification-max-assets', type=int, default=None, help='необязательный лимит классификаций на страницу; по умолчанию без ограничений')
    p.add_argument('--pdf-classification-seconds', type=float, default=None, help='необязательный общий бюджет классификации на страницу, сек; по умолчанию без ограничений')
    p.add_argument('--ocr-cache-dir', type=Path, default=DATA_DIR/'.cache'/'docling_ocr',
                   help='постоянные OCR/layout снимки до экспорта, по SHA256 PDF и конфигурации')
    p.add_argument('--pdf-formula-reader', choices=('p2t', 'verified', 'surya'), default='surya',
                   help='surya: сохранять первичное распознавание; p2t/verified отключены для local')
    p.add_argument('--pdf-formula-device', choices=('auto', 'cpu', 'cuda'), default='auto',
                   help='устройство Pix2Text; auto: CUDA при доступном ONNX CUDA provider и свободной памяти, иначе CPU')
    p.add_argument('--formula-corrections', type=Path, default=None,
                   help='карта визуально сверенных формул с SHA256 исходного PDF и изображения')
    args = p.parse_args(argv)
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', args.output_suffix) or args.output_suffix.endswith(('.', ' ')):
        p.error('--output-suffix must contain only filename characters and cannot end in a dot or space')
    args.primary_ocr_only = args.mode == 'local'
    if args.primary_ocr_only and args.pdf_formula_reader != 'surya':
        p.error('Auxiliary content OCR disabled in local mode: --pdf-formula-reader must be surya')
    if args.pdf_repairs is None:
        args.pdf_repairs = ['all'] if args.mode == 'local' else []
    if 'none' in args.pdf_repairs:
        if len(args.pdf_repairs) != 1:
            p.error('--pdf-repairs none cannot be combined with other groups')
        args.pdf_repairs = []
    if args.formula_corrections is None and args.mode == 'local' and not args.primary_ocr_only:
        reviewed = SCRIPT_DIR/'conversion_config'/'pdf_formula_corrections.json'
        if reviewed.is_file():
            args.formula_corrections = reviewed
    for name in ('pdf_math_max_assets', 'pdf_math_seconds', 'pdf_classification_max_assets', 'pdf_classification_seconds'):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value < 0):
            p.error('Optional PDF recognition limits must be nonnegative and finite')
        if value == 0:
            setattr(args, name, None)
    args.ocr_cache_dir = args.ocr_cache_dir.expanduser().resolve()
    if _DIAGNOSTICS is not None and args.api_key:
        _DIAGNOSTICS.secrets.add(args.api_key)
    args.input_dir = DATA_DIR if args.selected_inputs else DATA_DIR/'Input'
    args.output = (args.output_dir or DATA_DIR / "Output").expanduser().resolve()
    if args.log_dir:
        args.log_dir = args.log_dir.expanduser().resolve()
    if args.diagnostics_dir:
        args.diagnostics_dir = args.diagnostics_dir.expanduser().resolve()
    if args.formula_corrections:
        args.formula_corrections = args.formula_corrections.expanduser().resolve()
        if not args.formula_corrections.is_file():
            p.error('--formula-corrections must name an existing JSON file')
    if args.pdf_repairs and args.mode != 'local':
        p.error('--pdf-repairs applies to --mode local')
    args.device = "cpu"  # API mode does not need local CUDA or MPS.

    from docling.datamodel.base_models import FormatToExtensions, InputFormat

    supported_formats = (
        InputFormat.PDF, InputFormat.DOCX, InputFormat.DOC,
        InputFormat.PPTX, InputFormat.PPT, InputFormat.XLSX, InputFormat.XLS, InputFormat.RTF,
    )
    args.supported_extensions = {
        f".{extension.lower()}"
        for input_format in supported_formats
        for extension in FormatToExtensions[input_format]
    }
    if args.selected_inputs:
        selected = []
        for path in args.selected_inputs:
            path = path.expanduser()
            path = (args.input_dir / path).resolve()
            if not path.is_relative_to(args.input_dir):
                p.error(f"--input must stay inside the workspace directory: {path}")
            selected.append(path)
        if any(not path.is_file() for path in selected):
            p.error("Every --input path must name an existing file")
    else:
        selected = discover_files(args.input_dir)
    all_files = sorted(
        set(selected),
        key=lambda path: path.as_posix().casefold(),
    )
    args.input_files = [path for path in all_files if path.suffix.lower() in args.supported_extensions]
    args.unsupported_count = len(all_files) - len(args.input_files)
    args.pdf_files = [path for path in args.input_files if path.suffix.lower() == ".pdf"]
    args.office_files = [path for path in args.input_files if path.suffix.lower() != ".pdf"]
    if args.pages:
        try:
            start, end = map(int, args.pages.split("-", 1))
        except (ValueError, AttributeError):
            p.error("Use --pages START-END, e.g. 20-30")
        if start < 1 or end < start:
            p.error("Page numbers must satisfy 1 <= START <= END")
        args.page_range = (start, end)
    else:
        args.page_range = None
    output_owners = {}
    collisions = []
    for source in args.input_files:
        output_path = Path(f"{output_base(args, source)}.{args.format}")
        key = unicodedata.normalize("NFC", str(output_path.resolve())).casefold()
        if key in output_owners:
            collisions.append(f"{output_owners[key]} and {source} -> {output_path.name}")
        else:
            output_owners[key] = source
    if collisions:
        p.error("Output filename collisions found before conversion: " + "; ".join(collisions))

    args.pending_pdf_files = [source for source in args.pdf_files if not should_skip(args, source)]
    if args.pages and args.office_files:
        print("WARNING: --pages applies only to PDF; Office files will be converted in full.", file=sys.stderr)
    if args.pending_pdf_files:
        if args.mode == "local":
            try:
                args.device = resolve_device()
                configure_surya(args)
            except (ImportError, ValueError, OSError, subprocess.SubprocessError) as exc:
                p.error(str(exc))
        else:
            args.api_key = (args.api_key or "").strip()
            if not args.api_key:
                try:
                    args.api_key = input("Введите API ключ OpenAI (ввод видимый): ").strip()
                except (EOFError, KeyboardInterrupt):
                    p.error("Введите ключ в консоли или передайте --api-key")
                if not args.api_key:
                    p.error("API ключ не может быть пустым")
            if _DIAGNOSTICS is not None:
                _DIAGNOSTICS.secrets.add(args.api_key)
    return args


def retrying_api_pipeline(args):
    from docling.datamodel.base_models import VlmPrediction, VlmStopReason
    from docling.models.vlm_pipeline_models.api_vlm_model import ApiVlmModel
    from docling.pipeline.vlm_pipeline import VlmPipeline

    def complete(prediction):
        return prediction is not None and bool(prediction.text.strip()) and prediction.stop_reason not in (
            VlmStopReason.INFERENCE_ERROR, VlmStopReason.LENGTH, VlmStopReason.CONTENT_FILTERED,
        )

    class RetryingApiVlmModel(ApiVlmModel):
        def __call__(self, conv_res, page_batch):
            for page in page_batch:
                image = page.get_image(scale=self.vlm_options.scale, max_size=self.vlm_options.max_size)
                if image is None:
                    yield from super().__call__(conv_res, [page])
                    continue
                if BLANK_INK_THRESHOLD:
                    ink = sum(image.convert("L").histogram()[:240]) / (image.width * image.height)
                    if ink < BLANK_INK_THRESHOLD:
                        page.predictions.vlm_response = VlmPrediction(
                            text=BLANK_PAGE_HTML, stop_reason=VlmStopReason.END_OF_SEQUENCE,
                        )
                        print(f"SKIP BLANK: page {page.page_no} (ink={ink:.8f})", flush=True)
                        yield page
                        continue
                for attempt in range(API_RETRIES + 1):
                    print(f"API: page {page.page_no}; image {image.width}x{image.height}; attempt {attempt + 1}/{API_RETRIES + 1}", flush=True)
                    page = next(super().__call__(conv_res, [page]))
                    prediction = page.predictions.vlm_response
                    if complete(prediction):
                        usage = prediction.usage if isinstance(prediction.usage, dict) else {}
                        print(f"API complete: page {page.page_no}; input tokens: {usage.get('prompt_tokens', usage.get('input_tokens', '?'))}", flush=True)
                        break
                    error = prediction.error_message if prediction is not None else "Missing VLM response"
                    error = error or f"Incomplete output: {prediction.stop_reason.value}"
                    retryable = any(token in error.lower() for token in (
                        "timeout", "timed out", "connectionerror", "sslerror", "proxyerror",
                        "connection reset", "remote disconnected", "http 429:", "resource unavailable",
                    ))
                    if not retryable or attempt == API_RETRIES:
                        raise RuntimeError(f"Page {page.page_no}: {error}")
                    delay = min(2 ** (attempt + 2), 60)
                    print(f"WARNING: Page {page.page_no}: {error}; retry in {delay}s", file=sys.stderr, flush=True)
                    time.sleep(delay)
                yield page

    class RetryingVlmPipeline(VlmPipeline):
        def __init__(self, pipeline_options):
            super().__init__(pipeline_options)
            # Preserve Docling's rendering and HTML parsing; only wrap its API model.
            self.build_pipe = [
                RetryingApiVlmModel(enabled=model.enabled,
                                  enable_remote_services=pipeline_options.enable_remote_services,
                                  vlm_options=model.vlm_options)
                if isinstance(model, ApiVlmModel) else model
                for model in self.build_pipe
            ]

    return RetryingVlmPipeline


def build_converter(args):
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    if not args.pending_pdf_files:
        return DocumentConverter()

    from docling.datamodel.accelerator_options import AcceleratorOptions

    if args.mode == "local":
        from docling.datamodel.pipeline_options import (
            OcrMode, PdfPipelineOptions, TableFormerMode, TableStructureOptions,
        )
        from docling_surya_ocr import SuryaOcrOptions

        ocr = SuryaOcrOptions(
            backend="surya2", document_analysis_backend="surya2_ocr_only",
            ocr_mode="full_page", languages=("ru", "en"), device=args.device,
            recognition_batch_size=OCR_BATCH_SIZE, cpu_threads=NUM_THREADS,
            surya2_inference_backend=SURYA_BACKEND,
            surya2_inference_parallel=OCR_BATCH_SIZE,
            surya2_startup_timeout_seconds=600,
        )
        ocr.mode = OcrMode.FULL_PAGE
        ocr.scale = IMAGE_SCALE
        options = PdfPipelineOptions(
            allow_external_plugins=True, do_ocr=True, ocr_options=ocr,
            do_table_structure=True, do_formula_enrichment=False,
            enable_remote_services=False, images_scale=RETAINED_IMAGE_SCALE,
            generate_page_images=False, generate_picture_images=True,
            ocr_batch_size=1, layout_batch_size=1, table_batch_size=1, queue_max_size=2,
            table_structure_options=TableStructureOptions(
                mode=TableFormerMode.ACCURATE, do_cell_matching=True,
            ),
        )
        pipeline_cls = surya_pdf_pipeline(args)
    else:
        from docling.datamodel.pipeline_options import VlmPipelineOptions
        from docling.datamodel.pipeline_options_vlm_model import ApiVlmOptions, ResponseFormat

        options = VlmPipelineOptions(
            vlm_options=ApiVlmOptions(
                url=API_URL,
                params={"model": API_MODEL, "max_completion_tokens": API_MAX_TOKENS,
                        "reasoning_effort": "none", "service_tier": "flex"},
                headers={"Authorization": f"Bearer {args.api_key}"},
                temperature=1.0, prompt=API_PROMPT, response_format=ResponseFormat.HTML,
                scale=API_IMAGE_SCALE, timeout=API_TIMEOUT, concurrency=1,
            ),
            enable_remote_services=True, images_scale=API_IMAGE_SCALE,
            generate_page_images=True, generate_picture_images=False,
        )
        pipeline_cls = retrying_api_pipeline(args)

    options.accelerator_options = AcceleratorOptions(device=args.device, num_threads=NUM_THREADS)
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_cls=pipeline_cls, pipeline_options=options),
    })


def export_api_html(result, base):
    from bs4 import BeautifulSoup, Comment

    def edge_prose(root, first=False):
        children = root.find_all(recursive=False)
        if not children:
            return None
        node = children[0] if first else children[-1]
        while node.name in {"div", "section", "article", "ul", "ol"}:
            children = node.find_all(recursive=False)
            if not children:
                break
            node = children[0] if first else children[-1]
        return node if node.name in {"p", "li"} and not node.find(["table", "img"]) else None

    # Repetition is evidence for older cached responses without furniture tags.
    running_designations = {}
    for source_page in result.pages:
        response = source_page.predictions.vlm_response
        if response is None:
            continue
        parsed = BeautifulSoup(response.text, "html.parser")
        root = parsed.body or parsed
        for node in root.find_all(recursive=False)[:3]:
            text = node.get_text(" ", strip=True)
            if node.name not in {"table", "figure"} and is_document_designation(text):
                running_designations.setdefault(furniture_key(text), set()).add(source_page.page_no)

    def comment_page_number(root):
        # Also handle cached responses that still have a visible footer paragraph.
        numbered = False
        for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
            match = re.fullmatch(r"\s*Номер\s+листа:\s*(\d+)\s*", str(comment), re.I)
            if match:
                comment.replace_with(Comment(f" Номер листа: {match[1]} "))
                numbered = True
        if numbered:
            return
        texts = [node for node in root.find_all(string=True) if not isinstance(node, Comment) and node.strip()]
        if not texts:
            return
        footer = texts[-1]
        while footer.parent is not root:
            footer = footer.parent
            if footer.name in ("p", "div", "footer"):
                break
        if footer.find_parent(["table", "math"]) is not None:
            return
        if getattr(footer, "name", None) is not None:
            if footer.name not in ("p", "div", "footer", "span") or footer.find(["table", "math", "img"]) is not None:
                return
            text = footer.get_text(" ", strip=True)
        else:
            text = str(footer).strip()
        match = re.fullmatch(r"(?:(?:Лист|Стр(?:аница)?\.?)\s*(?:№\s*)?)?(\d+)", text, re.I)
        if match:
            footer.replace_with(Comment(f" Номер листа: {match[1]} "))

    def column_count(table):
        pending = {}
        widths = set()
        try:
            for row in table.find_all("tr"):
                if row.find_parent("table") is not table:
                    continue
                column = 0
                for cell in row.find_all(["td", "th"], recursive=False):
                    while column in pending:
                        column += 1
                    colspan, rowspan = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
                    if colspan < 1 or rowspan < 1 or any(i in pending for i in range(column, column + colspan)):
                        return 0
                    pending.update((i, rowspan) for i in range(column, column + colspan))
                    column += colspan
                width = max(pending, default=-1) + 1
                if len(pending) != width:
                    return 0
                widths.add(width)
                pending = {i: remaining - 1 for i, remaining in pending.items() if remaining > 1}
        except (ValueError, TypeError):
            return 0
        return widths.pop() if len(widths) == 1 and not pending else 0

    def row_signature(row):
        return tuple((re.sub(r"\s+", "", cell.get_text()), cell.get("rowspan", "1"), cell.get("colspan", "1"))
                     for cell in row.find_all(["td", "th"], recursive=False))

    def header_signature(table):
        header = table.find("thead", recursive=False)
        return tuple(row_signature(row) for row in header.find_all("tr")) if header is not None else ()

    def table_number(text):
        match = re.search(r"т\s*а\s*б\s*л\s*и\s*ц\s*[аы]\s+([A-ZА-ЯЁ]?\s*\.?\s*\d+(?:[.\-]\d+)*)", text, re.I)
        return normalize_number(match[1]) if match else None

    def normalize_number(value):
        return re.sub(r"\s+", "", value).upper().translate(str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX"))

    def continuation_label(node):
        return (getattr(node, "name", None) is not None
                and re.match(r"^(?:Продолжение|Окончание)(?:\s+таблицы\b|$)", node.get_text(" ", strip=True), re.I) is not None)

    def boundary_node(node, identity):
        if isinstance(node, Comment):
            return True
        if getattr(node, "name", None) is None:
            return not str(node).strip()
        text = node.get_text(" ", strip=True)
        if node.get("data-page-furniture") == "true":
            return True
        if not text:
            return node.name not in ("img", "math", "table") and node.find(["img", "math", "table"]) is None
        if identity and table_number(text) == identity:
            return True
        if continuation_label(node):
            number = table_number(text)
            return not number or not identity or number == identity
        return bool(re.fullmatch(r"\d+|(?:СП|СН|СНиП|ГОСТ(?:\s+Р)?)(?:\s+(?:ИСО|ISO|МЭК|IEC))*\s+\d[\d.\s/–—*\-]*", text, re.I))

    def row_number(table, last=False):
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table
                and row.find_parent(["thead", "tfoot"]) is None]
        numbers = []
        remaining = 0
        for row in rows:
            if remaining:
                remaining -= 1
                continue
            cell = row.find(["td", "th"], recursive=False)
            if cell is None or not re.fullmatch(r"\d+", cell.get_text(strip=True)):
                return None
            numbers.append(int(cell.get_text(strip=True)))
            try:
                remaining = max(0, int(cell.get("rowspan", 1)) - 1)
            except (ValueError, TypeError):
                return None
            if not last:
                break
        return numbers[-1] if numbers else None

    output = BeautifulSoup("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body></body></html>", "html.parser")
    style = output.new_tag("style")
    style.string = LOCAL_CSS
    output.head.append(style)
    previous_table = None
    previous_page = None
    previous_tail = []
    previous_prose = None
    for page in result.pages:
        response = page.predictions.vlm_response
        if response is None or not response.text.strip():
            raise RuntimeError(f"Missing VLM response on page {page.page_no}")
        # Process the model output before HTML import discards metadata.
        raw = response.text.strip()
        if raw == BLANK_PAGE_HTML:
            continue
        if response.stop_reason.value in ("length", "content_filter", "inference_error"):
            raise RuntimeError(f"Incomplete VLM output on page {page.page_no}: {response.stop_reason.value}")
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "link", "iframe", "object"]):
            tag.decompose()
        for tag in soup.find_all(True):
            for attr in list(tag.attrs):
                if attr.lower().startswith("on"):
                    del tag[attr]
        for img in soup.find_all("img"):
            try:
                l, t, r, b = map(float, img.get("data-bbox", "").split(","))
                if not (0 <= l < r <= 1000 and 0 <= t < b <= 1000):
                    raise ValueError("invalid normalized coordinates")
                source = page.image
                if source is None:
                    # VlmPipeline releases Page caches but keeps document images.
                    stored_page = result.document.pages.get(page.page_no)
                    if stored_page is not None and stored_page.image is not None:
                        source = stored_page.image.pil_image
                if source is None:
                    raise ValueError("page image unavailable")
                bounds = (math.floor(l * source.width / 1000), math.floor(t * source.height / 1000),
                          math.ceil(r * source.width / 1000), math.ceil(b * source.height / 1000))
                buffer = BytesIO()
                source.crop(bounds).save(buffer, format="PNG")
                img["src"] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                img["width"] = str(round((r - l) * page.size.width * 96 / 72000))
            except ValueError as exc:
                raise RuntimeError(f"Page {page.page_no}: diagram crop failed: {exc}") from exc
        for formula in soup.select("[data-formula]"):
            latex = formula.get_text().strip()
            if re.search(r"[\u0400-\u052f]", latex):
                diagnostic_event('FORMULA_SOURCE_CYRILLIC', page=page.page_no,
                                 policy='preserve_each_source_symbol_in_mixed_alphabets')
            try:
                converted = latex_math(latex, "block" if formula["data-formula"] == "display" else "inline")
                if converted is None:
                    raise ValueError("Formula requires its source image")
                mathml = BeautifulSoup(converted, "html.parser")
                formula.replace_with(mathml.math)
            except Exception as exc:
                print(f"API formula source preservation: page {page.page_no}; {exc}", file=sys.stderr)
                source = page.image
                if source is None:
                    stored = result.document.pages.get(page.page_no)
                    source = stored.image.pil_image if stored and stored.image else None
                if source is None:
                    raise RuntimeError(f"Source image unavailable for formula on page {page.page_no}") from exc
                try:
                    left, top, right, bottom = map(float, formula.get("data-bbox", "").split(","))
                    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                        raise ValueError("Invalid formula bbox")
                    cropped = source.crop((math.floor(left * source.width / 1000), math.floor(top * source.height / 1000),
                                           math.ceil(right * source.width / 1000), math.ceil(bottom * source.height / 1000)))
                    width = (right - left) * page.size.width / 1000
                except ValueError:
                    cropped, width = source, page.size.width
                formula.replace_with(BeautifulSoup(image_html(cropped, width, "formula"), "html.parser"))
        root = soup.body or soup
        comment_page_number(root)
        for node in list(root.select('[data-page-furniture="true"], header, footer')):
            if node.parent is not None and node.find_parent("table") is None:
                node.decompose()
        for node in list(root.find_all(recursive=False)[:3]):
            text = node.get_text(" ", strip=True)
            if (page.page_no != result.pages[0].page_no
                    and node.name not in {"table", "figure"} and is_document_designation(text)
                    and len(running_designations.get(furniture_key(text), ())) >= 2):
                node.decompose()
        tables = [table for table in soup.find_all("table") if table.find_parent("table") is None]
        tail = list(tables[-1].next_siblings) if tables else []
        for table in tables:
            normalize_table_word_wraps(table)
            caption = table.find("caption", recursive=False) or table.find_previous_sibling()
            if table.get("data-table-id"):
                table["data-table-id"] = normalize_number(table["data-table-id"])
            elif caption is not None:
                identity = table_number(caption.get_text(" ", strip=True))
                if identity:
                    table["data-table-id"] = identity
        if tables and previous_table is not None and previous_page == page.page_no - 1:
            current = tables[0]
            identity, old_identity = current.get("data-table-id"), previous_table.get("data-table-id")
            header, old_header = header_signature(current), header_signature(previous_table)
            first_number, last_number = row_number(current), row_number(previous_table, last=True)
            leading = list(current.previous_siblings)
            continued = (current.get("data-continued") == "true"
                         or any(continuation_label(node) for node in leading)
                         or bool(identity and identity == old_identity)
                         or bool(header and header == old_header and (first_number is None or last_number is None))
                         or (first_number is not None and last_number is not None and first_number == last_number + 1))
            # Keep ordinary text in place; remove only furniture at a joined boundary.
            boundary = (current.parent is root and all(boundary_node(node, old_identity) for node in [*previous_tail, *leading]))
            columns, old_columns = column_count(current), column_count(previous_table)
            if continued and boundary and (not identity or not old_identity or identity == old_identity) and previous_table.find("tfoot") is None:
                if columns and columns == old_columns and (not header or header == old_header):
                    header_tag = current.find("thead", recursive=False)
                    if header_tag is not None:
                        header_tag.decompose()
                    rows = [row for row in current.find_all("tr") if row.find_parent("table") is current
                            and row.find_parent("tfoot") is None]
                    if not header and old_header and tuple(row_signature(row) for row in rows[:len(old_header)]) == old_header:
                        for row in rows[:len(old_header)]:
                            row.decompose()
                        rows = rows[len(old_header):]
                    bodies = previous_table.find_all("tbody", recursive=False)
                    target = bodies[-1] if bodies else previous_table
                    for node in [*previous_tail, *leading]:
                        if isinstance(node, Comment):
                            target.append(node.extract())
                    for row in rows:
                        target.append(row.extract())
                    footer = current.find("tfoot", recursive=False)
                    if footer is not None:
                        previous_table.append(footer.extract())
                    previous_table["data-columns"] = str(columns)
                    for node in [*previous_tail, *leading]:
                        if not isinstance(node, Comment):
                            node.extract()
                    current.decompose()
                    tables[0] = previous_table
                    print(f"Joined table continuation on page {page.page_no}")
                else:
                    print(f"WARNING: Page {page.page_no}: incompatible table grids ({old_columns} vs {columns}) or headers; fragments kept", file=sys.stderr)
            elif current.get("data-continued") == "true":
                print(f"WARNING: Page {page.page_no}: table boundary contains text, a different caption or final notes; fragments kept", file=sys.stderr)
        first_prose = edge_prose(root, first=True)
        merged_prose = None
        if (previous_prose is not None and previous_page == page.page_no - 1
                and first_prose is not None and first_prose.name == "p"
                and paragraph_continuation(previous_prose.get_text(), first_prose.get_text())):
            previous_prose.append(paragraph_separator(previous_prose.get_text()))
            for node in list(first_prose.contents):
                previous_prose.append(node.extract())
            first_prose.decompose()
            merged_prose = previous_prose
        last_prose = edge_prose(root)
        previous_prose = last_prose or (merged_prose if not root.find_all(recursive=False) else None)
        for node in list(root.contents):
            if getattr(node, "name", None) not in ("head", "html"):
                output.body.append(node.extract())
        previous_table = tables[-1] if tables else None
        previous_page = page.page_no
        previous_tail = tail
    tables = len(output.find_all("table"))
    for table in output.find_all("table"):
        css = "table-scroll wide" if column_count(table) >= 7 else "table-scroll"
        table.wrap(output.new_tag("div", attrs={"class": css}))
    print(f"API output: {tables} tables; {len(output.find_all('img'))} diagram crops; {len(output.find_all('math'))} formulas")
    diagnostic_html(output,'published_api_html',output=str(Path(f'{base}.html')))
    write_atomic(Path(f"{base}.html"), str(output))
    return tables


def convert_document(args, converter, source):
    if source.suffix.lower() in {'.docx', '.docm', '.dotx', '.dotm', '.rtf', '.doc', '.dot'}:
        return export_office_document(args, source)
    from docling.datamodel.base_models import ConversionStatus
    from docling_core.types.doc import ImageRefMode

    is_pdf = source.suffix.lower() == ".pdf"
    kwargs = {"source": source}
    if is_pdf:
        progress = PageConversionProgress(source)
        kwargs["progress_callback"] = progress
    if is_pdf and args.page_range is not None:
        kwargs["page_range"] = args.page_range
    mode = args.mode if is_pdf else "docling-native"
    pages = (args.pages or "all") if is_pdf else "all"
    diagnostic_event('DOCUMENT_BEGIN',source=source,mode=mode,pages=pages)
    diagnostic_file(source,'source_document')
    print(f"Processing {source} | {mode} | pages {pages}")
    if is_pdf and args.mode == "local":
        from docling_pdf_runtime import OcrSnapshot, DiagnosticExportError, write_failure_artifacts, CachedPostprocessModel
        args.pdf_diagnostics_current = None
        args.pdf_quality_current = {}
        args.pdf_export_context = {'stage': 'ocr'}
        args.surya_records = []
        args.table_region_memory = {}
        args.scan_header_requests = 0
        args.scan_numeric_requests = 0
        snapshot = OcrSnapshot(source, args, sys.modules[__name__])
        cached = snapshot.load()
        if not cached and os.environ.get('DOCLING_REQUIRE_OCR_CACHE') == '1':
            raise RuntimeError('Required primary OCR snapshot is missing or invalid; new inference is disabled')
        if cached and getattr(args, 'surya_ocr_model', None) is None:
            args._cached_postprocess_model = CachedPostprocessModel()
            args.surya_ocr_model = args._cached_postprocess_model
        context = nullcontext((cached.normalized, cached.orientation)) if cached else normalized_pdf(source, args)
        with context as (normalized, orientation):
            args.current_source_hash = snapshot.source_hash
            args.current_orientation = orientation
            kwargs["source"] = normalized
            if not cached:
                progress.start(len(orientation))
            if cached:
                result = cached
                args.surya_records = cached.records
                progress.successful_pages.update(orientation)
                print(f'Конвертация: OCR cache; {len(orientation)} из {len(orientation)} страниц восстановлено', flush=True)
            else:
                converter = shared_converter(args, converter)
                result = converter.convert(**kwargs)
            diagnostic_model(result.document,getattr(args,'surya_records',()))
            diagnostic_event('CONVERTER_RESULT',status=str(result.status),errors=diagnostic_summary(getattr(result,'errors',())))
            if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
                raise RuntimeError(f"Docling failed: {result.status}. Errors: {result.errors}")
            if not cached:
                snapshot.save(result, args.surya_records, normalized, orientation)
            if getattr(args, 'diagnostics_dir', None):
                diagnostics = args.diagnostics_dir/f'{source.stem[:64]}_{uuid4().hex[:12]}'
                diagnostics.mkdir(parents=True, exist_ok=False)
                args.pdf_diagnostics_current = diagnostics
                result.document.save_as_json(diagnostics/'docling_before_export.json', ensure_ascii=False)
                (diagnostics/'surya_records.json').write_text(json.dumps(args.surya_records, ensure_ascii=False, indent=2), encoding='utf-8')
                diagnostic_source = normalized
                if normalized.resolve() != source.resolve():
                    diagnostic_source = diagnostics/'normalized.pdf'
                    shutil.copy2(normalized, diagnostic_source)
                (diagnostics/'input.json').write_text(json.dumps(dict(source=str(source), normalized=str(diagnostic_source),
                    orientation=orientation, page_range=args.page_range, pdf_repairs=getattr(args, 'pdf_repairs', []),
                    source_sha256=args.current_source_hash), ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"Подготовка HTML: {source}", flush=True)
            content = None
            try:
                args.pdf_export_context = {'stage': 'assembly'}
                content, quality = export_local_html(args, result, normalized, orientation, source)
                if quality.get('deferred_math'):
                    error = RuntimeError('Unverified/deferred math candidates: diagnostic draft only; final HTML not replaced')
                    error.context = dict(quality['deferred_math'][0], stage='math_budget')
                    raise error
                args.pdf_export_context = {'stage': 'publication'}
                base = output_base(args, source)
                base.parent.mkdir(parents=True, exist_ok=True)
                from docling_pdf_review import write_quality_report
                publication = write_quality_report(Path(f'{base}.html'),source,quality,sys.modules[__name__])
                write_atomic(Path(f"{base}.html"), content)
            except Exception as error:
                artifacts = write_failure_artifacts(source, args, sys.modules[__name__], error,
                    snapshot=snapshot, document=result.document, content=content)
                raise DiagnosticExportError(str(error), artifacts) from error
            audit=diagnostic_html(content,'published_local_html',source=str(source),output=str(Path(f'{base}.html').resolve()))
            diagnostic_event('DOCUMENT_END',source=str(source),source_table_fragments=len(quality['tables']),
                             logical_tables=audit.get('tables'),formula_records=len(quality['formulas']),
                             regional_requests=quality.get('regional_requests'))
            print(f"Status: {publication['status']}; table fragments: {len(quality['tables'])}; formulas: {len(quality['formulas'])}")
            if publication['status']=='REVIEW':
                print(f"WARNING: Требуется визуальная проверка {publication['review_fragments']} табличных фрагментов; отчёт: {publication['report']}",flush=True)
            print(f"Saved: {base}.html", flush=True)
        return publication
    if is_pdf:
        with normalized_pdf(source, args) as (normalized, orientation):
            kwargs["source"] = normalized
            progress.start(len(orientation))
            result = shared_converter(args, converter).convert(**kwargs)
    else:
        result = shared_converter(args, converter).convert(**kwargs)

    diagnostic_model(result.document,getattr(args,'surya_records',()))
    diagnostic_event('CONVERTER_RESULT',status=str(result.status),errors=diagnostic_summary(getattr(result,'errors',())))

    if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        raise RuntimeError(f"Docling failed: {result.status}. Errors: {result.errors}")
    if result.status == ConversionStatus.PARTIAL_SUCCESS:
        print(f"WARNING: Partial conversion: {result.errors}", file=sys.stderr)

    base = output_base(args, source)
    base.parent.mkdir(parents=True, exist_ok=True)
    doc = result.document
    output_file = Path(f"{base}.{args.format}")
    print(f"Подготовка HTML: {source}", flush=True)
    if is_pdf and args.mode == "ai":
        tables = export_api_html(result, base)
    else:
        from docling_core.types.doc import ImageRefMode

        content = doc.export_to_html(image_mode=ImageRefMode.EMBEDDED)
        content = normalize_html_table_words(content)
        if is_pdf and args.mode == "local":
            content = render_surya_math(content)
        write_atomic(output_file, content)
        diagnostic_html(content,'published_html',source=str(source),output=str(output_file))
        tables = len(doc.tables)
    diagnostic_event('DOCUMENT_END',source=str(source),model_tables=tables)
    print(f"Status: {result.status}; tables: {tables}")
    print(f"Saved: {output_file}")


def shared_converter(args, converter=None):
    """Cache hits need no layout/OCR models; all misses share one converter."""
    converter = converter or getattr(args, '_shared_converter', None)
    if converter is None:
        diagnostic_event('SHARED_CONVERTER_INITIALIZING')
        converter = build_converter(args)
    args._shared_converter = converter
    return converter


def run_conversion(args, report_router, report_lock):
    from docling_pdf_runtime import DiagnosticExportError, save_json
    if args.unsupported_count:
        print(f"Ignoring {args.unsupported_count} files with unsupported formats in {args.input_dir}")
    if not args.input_files:
        print(f"No supported PDF or Office documents found in: {args.input_dir}")
        return
    log_paths = diagnostic_log_paths(args.input_files, getattr(args, 'log_dir', None))
    converter = None
    failed = 0
    statuses = []
    current_source = None
    status_path = (getattr(args, 'log_dir', None) or Path.cwd()/'convert_docling_log')/'conversion_status.json'
    def save_status(state):
        try:
            counts = {label: sum(item['status']==label for item in statuses) for label in ('OK','REVIEW','SKIP','DIAG','FAIL')}
            save_json(status_path, dict(status=state, documents=statuses, pid=os.getpid(),
                total_documents=len(args.input_files), completed_documents=len(statuses),
                current_source=current_source if state=='running' else None, counts=counts))
        except OSError as error:
            diagnostic_event('STATUS_REPORT_WRITE_ERROR', path=status_path, error=str(error))
            print(f'STATUS_REPORT_WRITE_ERROR: {status_path}: {error}', file=sys.stderr, flush=True)
    def record(source, status, **extra):
        entry = dict(source=str(source), status=status, output=str(output_base(args, source))+'.html', **extra)
        statuses.append(entry)
        save_status('running')
        print(f'{status}: {source}', flush=True)
    for source in args.input_files:
        current_source = str(source)
        save_status('running')
        try:
            skip_error = None
            try:
                skip = should_skip(args, source)
            except Exception as error:
                skip, skip_error = False, error
            if skip:
                print(f"SKIP: {source}; output already exists: {output_base(args, source)}.{args.format}")
                record(source, 'SKIP')
                continue
            with diagnostic_document_log(report_router, report_lock, log_paths[source], source, args) as report:
                try:
                    report_event(
                        report, report_lock, 'run',
                        f'python={sys.version.split()[0]}; platform={platform.platform()}; '
                        f'cwd={Path.cwd()}; input_dir={args.input_dir}; mode={args.mode}; device={args.device}; '
                        f'pages={args.pages or "all"}; supported_files={len(args.input_files)}; '
                        f'image_scale={IMAGE_SCALE}; threads={NUM_THREADS}',
                    )
                    if skip_error is not None:
                        raise skip_error
                    report_event(report, report_lock, "memory",
                                 f"before document: {source}; {memory_snapshot(args.device)}")
                    outcome = convert_document(args, converter, source)
                    converter = getattr(args, '_shared_converter', converter)
                    if isinstance(outcome,dict) and outcome.get('status')=='REVIEW':
                        record(source,'REVIEW',**{k:v for k,v in outcome.items() if k!='status'})
                    else:
                        record(source, 'OK')
                    report_event(report, report_lock, "memory",
                                 f"after document success: {source}; {memory_snapshot(args.device)}")
                except BaseException as exc:
                    report_event(report, report_lock, "exception",
                                 f"document={source}; {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                    report_event(report, report_lock, "memory",
                                 f"after document failure: {source}; {memory_snapshot(args.device)}")
                    raise
        except Exception as exc:
            failed += 1
            record(source, 'DIAG' if isinstance(exc, DiagnosticExportError) else 'FAIL',
                   reason=str(exc), **(getattr(exc, 'artifacts', {}) or {}))
            print(f"ERROR: {source}: {type(exc).__name__}: {exc}", file=sys.stderr)
    save_status('failed' if failed else 'review_required' if any(s['status']=='REVIEW' for s in statuses) else 'success')
    print('SUMMARY: ' + '; '.join(f"{label}={sum(item['status']==label for item in statuses)}"
          for label in ('OK','REVIEW','SKIP','DIAG','FAIL')) + f'; status report: {status_path}', flush=True)
    if failed:
        print(f"ERROR: {failed} of {len(args.input_files)} document conversions failed", file=sys.stderr)
        raise SystemExit(1)


def main():
    from io import StringIO
    report_router = DiagnosticReportRouter()
    startup = StringIO()
    lock = threading.RLock()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = TeeTextStream(original_stdout, report_router, lock)
    sys.stderr = TeeTextStream(original_stderr, report_router, lock)
    report_router.target = startup
    try:
        startup_failed = False
        try:
            with diagnostic_session(report_router, lock, trace_functions=False):
                try:
                    diagnostic_startup()
                except Exception as error:
                    diagnostic_event('DIAGNOSTIC_ERROR', operation='startup', error=diagnostic_clean(error)[:400])
                try:
                    args = parse_args()
                except BaseException as exc:
                    startup_failed = not (isinstance(exc, SystemExit) and exc.code in (None, 0))
                    if startup_failed:
                        report_event(report_router, lock, 'startup exception',
                                     f'{type(exc).__name__}: {exc}\n{traceback.format_exc()}')
                    raise
                if not args.input_files:
                    print(f'No supported PDF or Office documents found in: {args.input_dir}')
                    diagnostic_event('NO_INPUT_FILES', input_dir=args.input_dir)
                    startup_failed = True
        finally:
            report_router.target = None
            if startup_failed:
                startup_path = Path.cwd()/'convert_docling_log'/'_startup_log.txt'
                try:
                    startup_path.parent.mkdir(parents=True, exist_ok=True)
                    startup_path.write_text(startup.getvalue(), encoding='utf-8')
                    print(f'Conversion startup log: {startup_path}', flush=True)
                except OSError as exc:
                    print(f'WARNING: Could not create startup log {startup_path}: {exc}', file=sys.stderr)
        if args.input_files:
            run_conversion(args, report_router, lock)
    finally:
        try:
            text_ocr=sys.modules.get('docling_source_text')
            if text_ocr is not None:
                try:
                    text_ocr.close_text_reader()
                except Exception as error:
                    diagnostic_event('SOURCE_TEXT_RUNTIME_CLOSE_ERROR',error=str(error))
                    print(f'SOURCE_TEXT_RUNTIME_CLOSE_ERROR: {error}',file=sys.stderr)
            cached_model = getattr(locals().get('args'), '_cached_postprocess_model', None)
            if cached_model is not None:
                try:
                    cached_model.close()
                except Exception as error:
                    diagnostic_event('POSTPROCESS_RUNTIME_CLOSE_ERROR', error=str(error))
                    print(f'POSTPROCESS_RUNTIME_CLOSE_ERROR: {error}', file=sys.stderr)
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr
            report_router.target = None
            startup.close()


if __name__ == "__main__":
    main()
