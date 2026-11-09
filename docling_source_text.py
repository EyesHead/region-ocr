"""Independent, local literal-text OCR for source-proven index labels.

Windows OCR is used only for literal text, never algebra or page recognition.
One private worker reuses its installed language models across the Python batch.
It receives image bytes as JSON data on stdin; document text is never shell code.
"""
import atexit
from functools import lru_cache
import base64
from io import BytesIO
import json
import os
from pathlib import Path
from queue import Queue, Empty
import re
import subprocess
from threading import Thread

from PIL import Image, ImageOps


class WindowsTextReader:
    def __init__(self):
        if os.name!='nt':
            raise RuntimeError('Independent literal-index OCR requires Windows OCR')
        executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.process=subprocess.Popen([str(executable),'-NoProfile','-ExecutionPolicy','Bypass','-File',
            str(Path(__file__).with_name('docling_windows_text.ps1'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
        self.replies=Queue()
        def receive():
            for line in self.process.stdout:
                self.replies.put(line)
            self.replies.put(None)
        self.receiver=Thread(target=receive,daemon=True)
        self.receiver.start()

    def read(self,data,language):
        request=dict(image=base64.b64encode(data).decode(),language=language)
        self.process.stdin.write(json.dumps(request)+'\n');self.process.stdin.flush()
        try:
            line=self.replies.get(timeout=30)
        except Empty as error:
            self.close()
            raise RuntimeError('Windows literal OCR did not finish its request') from error
        if line is None:
            raise RuntimeError('Windows literal OCR worker exited')
        result=json.loads(line)
        if result.get('status')!='OK':
            raise RuntimeError('Windows literal OCR: '+str(result.get('reason','invalid response')))
        return result

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write('{"command":"exit"}\n');self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError,subprocess.TimeoutExpired):
                self.process.terminate();self.process.wait(timeout=3)
        for stream in (self.process.stdin,self.process.stdout):
            if stream is not None:
                stream.close()


_reader=None


def close_text_reader():
    global _reader
    if _reader is not None:
        _reader.close();_reader=None
    read_text.cache_clear()


@lru_cache(maxsize=128)
def _cached_read_text(data,language):
    global _reader
    if _reader is None:
        _reader=WindowsTextReader()
    try:
        return _reader.read(data,language)
    except Exception:
        close_text_reader()
        raise


def read_text(data,language):
    if os.environ.get('DOCLING_PRIMARY_OCR_ONLY') == '1':
        raise RuntimeError('Auxiliary content OCR is disabled: retain the primary Surya result')
    return _cached_read_text(data,language)


read_text.cache_clear = _cached_read_text.cache_clear


atexit.register(close_text_reader)


def verify_literal(data,expected,context=None):
    """Verify every character in its actual script, without homoglyph folding.

    A mixed label is read in both installed languages at both source scales.
    English confirms Latin letters; Russian confirms Cyrillic. Punctuation and
    numbers must match in every language. No letter/number substitution or case
    normalization is permitted. Greek/algebraic expressions keep math readers.
    """
    if os.environ.get('DOCLING_PRIMARY_OCR_ONLY') == '1':
        return None
    if not re.fullmatch(r'[A-Za-zА-Яа-яЁё0-9,]+',expected):
        return None
    languages=[]
    if re.search('[A-Za-z0-9]',expected):languages.append('en-US')
    if re.search('[А-Яа-яЁё]',expected):languages.append('ru')
    if not languages:return None
    offset,total=0,len(expected)
    context_proof=None
    if context and context.get('count')==len(expected):
        data=context['data'];offset=context['offset'];total=context['total'];context_proof=context['proof']
    with Image.open(BytesIO(data)) as original:
        source=original.convert('RGB')
    readings=[]
    try:
        for scale in (3,4):
            enlarged=source.resize((source.width*scale,source.height*scale),Image.Resampling.LANCZOS)
            probe=ImageOps.expand(enlarged,border=32,fill='white');enlarged.close()
            stream=BytesIO();probe.save(stream,'PNG');probe.close()
            values={}
            for language in languages:
                result=read_text(stream.getvalue(),language)
                value=re.sub(r'\s+','',result['text'])
                if len(value)!=total:return None
                values[language]=value[offset:offset+len(expected)]
            for i,char in enumerate(expected):
                language='ru' if re.fullmatch('[А-Яа-яЁё]',char) else 'en-US'
                checks=([values[language]] if char.isalpha() else list(values.values()))
                if any(value[i]!=char for value in checks):return None
            readings.append(dict(scale=scale,readings=values))
        return dict(method='two_scale_independent_literal_text_ocr',expected=expected,
                    engine='Windows.Media.Ocr',languages=languages,readings=readings,
                    source_context=context_proof,character_range=[offset,offset+len(expected)])
    finally:
        source.close()
