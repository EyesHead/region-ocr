"""Local Pix2Text MFR 1.5, without its unrelated layout/text/table engines.

The free MIT ONNX checkpoint is decoded greedily using its own tokenizer and
context length. No Optimum/Transformers downgrade or paid weights are needed.
"""
from functools import lru_cache
from io import BytesIO
import json
from pathlib import Path

MODEL_ID='breezedeus/pix2text-mfr-1.5'
REVISION='1cef9f0bdcd6a4c63df7de1311fb0894593340cc'
_BUNDLED_ROOT=Path(__file__).resolve().parent/'models/pix2text-mfr-1.5'
MODEL_ROOT=_BUNDLED_ROOT if _BUNDLED_ROOT.is_dir() else Path(__file__).resolve().parent/'.cache/docling_models/pix2text-mfr-1.5'
FILES=('config.json','generation_config.json','preprocessor_config.json',
       'special_tokens_map.json','tokenizer_config.json','tokenizer.json',
       'encoder_model.onnx','decoder_model.onnx')


def model_path():
    if not all((MODEL_ROOT/name).is_file() for name in FILES):
        from huggingface_hub import snapshot_download
        snapshot_download(MODEL_ID,revision=REVISION,local_dir=MODEL_ROOT,allow_patterns=list(FILES)+['README.md'])
    return MODEL_ROOT


@lru_cache(maxsize=1)
def resolve_device():
    import onnxruntime as ort
    if 'CUDAExecutionProvider' not in ort.get_available_providers():return 'cpu'
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.mem_get_info()[0]>=1024**3:return 'cuda'
    except (ImportError,RuntimeError):pass
    return 'cpu'


class FormulaReader:
    def __init__(self,device):
        import onnxruntime as ort
        from transformers import DeiTImageProcessor
        from tokenizers import Tokenizer
        self.device=device
        provider='CUDAExecutionProvider' if device=='cuda' else 'CPUExecutionProvider'
        if provider not in ort.get_available_providers():
            raise RuntimeError('Pix2Text CUDA requires a CUDA-enabled ONNX Runtime')
        if device=='cuda':
            import torch  # expose the installed CUDA/cuDNN libraries
            if hasattr(ort,'preload_dlls'):ort.preload_dlls()
        root=model_path()
        self.config=json.loads((root/'config.json').read_text('utf-8'))
        generation=json.loads((root/'generation_config.json').read_text('utf-8'))
        self.capacity=int(self.config['decoder']['max_position_embeddings'])
        self.start=int(generation.get('decoder_start_token_id',self.config['decoder_start_token_id']))
        eos=generation.get('eos_token_id',self.config['eos_token_id'])
        self.eos=set(eos if isinstance(eos,list) else [eos])
        # Transformers 5 auto-selects a Unigram tokenizer for this historical
        # XLM-R config, although the checkpoint actually contains a BPE model.
        # Read its own tokenizer.json directly, preserving the original IDs.
        self.image_processor=DeiTImageProcessor.from_pretrained(root,local_files_only=True)
        self.tokenizer=Tokenizer.from_file(str(root/'tokenizer.json'))
        options=ort.SessionOptions();options.intra_op_num_threads=4;options.inter_op_num_threads=1
        options.log_severity_level=3
        providers=[provider] if device=='cpu' else [provider,'CPUExecutionProvider']
        self.encoder=ort.InferenceSession(str(root/'encoder_model.onnx'),sess_options=options,providers=providers)
        self.decoder=ort.InferenceSession(str(root/'decoder_model.onnx'),sess_options=options,providers=providers)
        if any(session.get_providers()[0]!=provider for session in (self.encoder,self.decoder)):
            raise RuntimeError('Pix2Text requested device was not activated')
        print(f'PDF formula OCR: Pix2Text MFR 1.5 on {device}; free ONNX weights; models reused',flush=True)

    def recognize(self,image):
        import numpy as np
        pixels=self.image_processor(images=[image],return_tensors='np').pixel_values
        hidden=self.encoder.run(None,{'pixel_values':np.ascontiguousarray(pixels,dtype=np.float32)})[0]
        tokens=[self.start]
        while len(tokens)<self.capacity:
            logits=self.decoder.run(None,{'input_ids':np.asarray([tokens],dtype=np.int64),
                'encoder_hidden_states':hidden})[0]
            token=int(np.argmax(logits[0,-1]))
            tokens.append(token)
            if token in self.eos:
                value=self.tokenizer.decode(tokens,skip_special_tokens=True).strip()
                if not value:raise RuntimeError('Pix2Text returned an empty formula')
                return value
        raise RuntimeError('Pix2Text reached its model context before EOS; partial formula rejected')

    def readings(self,data,scales=(1.,)):
        from PIL import Image
        with Image.open(BytesIO(data)) as original:image=original.convert('RGB')
        output=[]
        try:
            for scale in scales:
                probe=image if scale==1 else image.resize((max(8,round(image.width*scale)),
                    max(8,round(image.height*scale))),Image.Resampling.LANCZOS)
                try:output.append(self.recognize(probe))
                finally:
                    if probe is not image:probe.close()
        finally:image.close()
        return tuple(output)


@lru_cache(maxsize=1)
def model(device):return FormulaReader(device)


@lru_cache(maxsize=256)
def readings(data,device):return model(device).readings(data,(1.,.65))


@lru_cache(maxsize=256)
def read(data,device):return model(device).readings(data)[0]
