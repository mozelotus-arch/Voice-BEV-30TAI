"""SenseVoice model-bound preprocessing; no Torch, FunASR or SDK imports."""
import json,math,re,threading,time
from pathlib import Path
import numpy as np


class AsrInputLengthError(ValueError):
    """An overlong input rejected on CPU before any graph call."""
    def __init__(self,frames,limit):
        self.frames,self.limit=int(frames),int(limit)
        super().__init__('Input exceeds compiled bucket; no silent truncation (%d > %d frames)'%(frames,limit))


class CpuAdapter:
    def __init__(self,query,position,frames,reuse_buffers=False):
        self.query=query;self.position=position;self.frames=frames
        self.reuse_buffers=reuse_buffers
        if query.ndim!=2 or query.shape[1]!=560 or query.dtype!=np.float32:
            raise ValueError('Query table must be FP32 [rows,560]')
        if position.shape!=(1,frames+4,560) or position.dtype!=np.float32:
            raise ValueError('Position table does not match this compiled bucket')
        self.buffers=None

    def prepare(self,features,language=3,textnorm=14):
        if features.ndim!=2 or features.shape[1]!=560 or features.dtype!=np.float32:
            raise ValueError('Expected FP32 [frames,560] features')
        count=len(features);total=self.frames+4
        if count>self.frames:
            raise AsrInputLengthError(count,self.frames)
        if count<=0:raise ValueError('ASR input contains no feature frames')
        if not np.isfinite(features).all():raise ValueError('Non-finite features')
        ids=[language,1,2,textnorm]
        if any(not isinstance(i,(int,np.integer)) or not 0<=i<len(self.query) for i in ids):
            raise ValueError('Query ID is outside this model table')
        if self.buffers is None or not self.reuse_buffers:
            self.buffers=[np.empty((1,1,total,560),dtype=np.float32),
                np.empty((1,1,1,total),dtype=np.float32),np.empty((1,1,total,total),dtype=np.float32)]
        prepared,valid,bias=self.buffers
        flat=prepared.reshape(1,total,560)
        flat.fill(0);flat[0,:4]=self.query[ids];flat[0,4:count+4]=features
        np.multiply(flat,np.float32(math.sqrt(512)),out=flat)
        np.add(flat,self.position,out=flat)
        valid.fill(0);valid[0,0,0,:count+4]=1
        bias.fill(-10000);bias[:,:,:,:count+4]=0
        return self.buffers,count+4


class AsrBucketAdapter:
    """The lock covers preprocessing through output consumption for buffer reuse."""
    def __init__(self,graph,folder,frontend=None,tokenizer=None):
        self.graph=graph;self.folder=Path(folder);self.lock=threading.RLock()
        self.config=json.loads((self.folder/'ADAPTER.json').read_text(encoding='utf-8'))
        self.frames=int(self.config['frames'])
        self.output_mode=self.config.get('output_mode','logits')
        if self.output_mode not in ('logits','frame_ids'):
            raise ValueError('Unsupported ASR output contract')
        if (self.frames<=0 or self.config.get('total_frames')!=self.frames+4 or
            self.config.get('feature_size')!=560 or self.config.get('sample_rate')!=16000 or
            self.config.get('mask_bias')!=-10000):
            raise ValueError('Unsupported SenseVoice adapter configuration')
        self.cpu=CpuAdapter(np.load(str(self.folder/'query_embeddings.npy'),mmap_mode='r'),
            np.load(str(self.folder/'position_encoding.npy'),mmap_mode='r'),self.frames,True)
        if frontend is None:
            from frontend import NativeFrontend
            frontend=NativeFrontend(self.folder/'am.mvn',dither=self.config['dither'])
        if tokenizer is None:
            import sentencepiece as spm
            tokenizer=spm.SentencePieceProcessor(model_file=str(self.folder/'tokenizer.model'))
        self.frontend=frontend;self.tokenizer=tokenizer
        total=self.frames+4
        self.shapes=[(1,total,560,1),(1,1,total,1),(1,total,total,1)]
        if [c.shape for c in graph.contracts]!=self.shapes or any(c.dtype!='float32' for c in graph.contracts):
            raise ValueError('Compiled graph input contract differs from model adapter')

    def transcribe(self,pcm,language='zh',use_itn=True):
        requested=time.perf_counter()
        with self.lock:
            acquired=time.perf_counter()
            if pcm.dtype!=np.float32 or pcm.ndim!=1 or not np.isfinite(pcm).all():
                raise ValueError('Expected finite FP32 mono PCM in int16 amplitude scale at 16kHz')
            if language not in self.config['language_ids']:raise ValueError('Unsupported model language ID')
            validated=time.perf_counter()
            _,features=self.frontend.extract(pcm)
            featured=time.perf_counter()
            arrays,length=self.cpu.prepare(features,self.config['language_ids'][language],
                self.config['textnorm_ids']['withitn' if use_itn else 'woitn'])
            prepared=time.perf_counter()
            def consume(outputs):
                elements=(self.frames+4)*(25055 if self.output_mode=='logits' else 1)
                if len(outputs)!=1 or outputs[0].size!=elements:
                    raise ValueError('Unexpected ASR output contract')
                if self.output_mode=='logits':
                    valid_logits=outputs[0].reshape(-1,25055)[:length]
                    if not np.issubdtype(valid_logits.dtype,np.floating) or not np.isfinite(valid_logits).all():
                        raise ValueError('ASR valid logits must be finite floating-point values')
                    ids=valid_logits.argmax(-1)
                else:
                    indices=outputs[0].reshape(-1)[:length]
                    if (not np.issubdtype(indices.dtype,np.number) or np.iscomplexobj(indices) or
                        not np.isfinite(indices).all() or np.any(indices!=np.floor(indices)) or
                        np.any(indices<0) or np.any(indices>=25055)):
                        raise ValueError('ASR frame IDs must be exact valid vocabulary indices')
                    ids=indices.astype(np.int64)
                ids=ids[np.r_[True,ids[1:]!=ids[:-1]]]
                return ids[ids!=0].tolist()
            ids=self.graph.forward([a.reshape(s) for a,s in zip(arrays,self.shapes)],reducer=consume)
            inferred=time.perf_counter()
            text=re.sub(r'<\|.*?\|>','',self.tokenizer.decode(ids)).strip()
            decoded=time.perf_counter()
            return dict(text=text,ids=ids,feature_frames=len(features),valid_length=length,
                processing_seconds=decoded-requested,active_processing_seconds=decoded-acquired,
                timing=dict(queue_seconds=acquired-requested,input_validation_seconds=validated-acquired,
                    frontend_seconds=featured-validated,cpu_adapter_seconds=prepared-featured,
                    graph_call_seconds=inferred-prepared,text_decode_seconds=decoded-inferred),
                timing_scope='ASR API including lock queue; excludes model load, capture, endpoint, TTS and playback',
                model=self.graph.spec.name)
