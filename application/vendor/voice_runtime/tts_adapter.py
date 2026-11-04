"""Model-bound CPU encoder / shared-owner NPU decoder with bounded PCM reuse.

The adapter preserves production whole-utterance prosody/gain. It returns owned
PCM, not audio-device actions. Replacing a model creates another bundle-bound
adapter. Current family is single-speaker pinyin Piper with FP32 NHWC decoder
input; another model family needs an explicit adapter, not guessed dimensions.
"""
from collections import deque
import hashlib
import json
from pathlib import Path
import threading
import time
import numpy as np
from pcm_cache import PCM, PCMCache


class HybridGraphSession:
    def __init__(self, graph, encoder, interface):
        self.graph = graph
        self.encoder = encoder
        self.lock = threading.RLock()
        self.closed = False
        if interface.get('schema') != 'piper-pinyin-hybrid-v1':
            raise ValueError('Unsupported TTS interface')
        for key in ('latent_channels','decoder_frames','core_frames','samples_per_frame',
                    'max_latent_frames','max_phoneme_ids'):
            if type(interface.get(key)) is not int or interface[key] < 1:
                raise ValueError('Invalid TTS integer contract: '+key)
        if type(interface.get('context_frames')) is not int or interface['context_frames'] < 0:
            raise ValueError('Invalid decoder context')
        self.channels = interface['latent_channels']
        self.window = interface['decoder_frames']
        self.core = interface['core_frames']
        self.context = interface['context_frames']
        self.upsample = interface['samples_per_frame']
        self.max_frames = interface['max_latent_frames']
        self.max_ids = interface['max_phoneme_ids']
        if self.core + 2*self.context > self.window or self.core < self.context:
            raise ValueError('Core/context do not fit decoder window')
        expected = (1,1,self.window,self.channels)
        if len(graph.contracts) != 1 or graph.contracts[0].shape != expected or graph.contracts[0].dtype != 'float32':
            raise ValueError('Compiled decoder input differs from TTS package')
        inputs = {i.name:i for i in encoder.get_inputs()}
        if not {'input','input_lengths','scales'}.issubset(inputs) or set(inputs)-{'input','input_lengths','scales','sid'}:
            raise ValueError('Unsupported CPU encoder inputs')
        for name, item in inputs.items():
            if item.type != ('tensor(float)' if name == 'scales' else 'tensor(int64)'):
                raise ValueError('Unexpected CPU encoder dtype for '+name)
        self.names = set(inputs)
        self.buffer = np.zeros(expected, dtype=np.float32)
        self.metrics = deque(maxlen=256)

    def _ready(self):
        if self.closed:
            raise RuntimeError('TTS session is closed')

    def get_inputs(self):
        self._ready()
        return self.encoder.get_inputs()

    def _validate_feed(self, feed):
        if set(feed) != self.names:
            raise ValueError('TTS feed does not match encoder inputs')
        ids = feed['input']
        if not isinstance(ids,np.ndarray) or ids.dtype != np.int64 or ids.ndim != 2 or ids.shape[0] != 1 or not 0 < ids.shape[1] <= self.max_ids or np.any(ids < 0):
            raise ValueError('TTS phoneme IDs must be bounded nonnegative INT64 [1,N]')
        expected = {'input_lengths':(np.int64,(1,)), 'scales':(np.float32,(3,))}
        if 'sid' in self.names:
            expected['sid'] = (np.int64,(1,))
        for name,(dtype,shape) in expected.items():
            value=feed[name]
            if not isinstance(value,np.ndarray) or value.dtype != dtype or value.shape != shape or not np.isfinite(value).all():
                raise ValueError('Invalid encoder feed '+name)
        if feed['input_lengths'][0] != ids.shape[1] or np.any(feed['scales'] < 0) or feed['scales'][1] <= 0:
            raise ValueError('Invalid phoneme length or synthesis scales')
        if 'sid' in self.names and feed['sid'][0] != 0:
            raise ValueError('Current family uses single-speaker ID zero')

    def _decode(self, encoded):
        if not isinstance(encoded,np.ndarray) or encoded.dtype != np.float32 or encoded.ndim != 3 or encoded.shape[:2] != (1,self.channels) or not 0 < encoded.shape[2] <= self.max_frames or not np.isfinite(encoded).all():
            raise ValueError('Invalid CPU latent tensor; no silent truncation/casting')
        frames=encoded.shape[2]
        output=np.empty(frames*self.upsample,dtype=np.float32)
        count_windows=0
        for core_start in range(0,frames,self.core):
            begin=max(0,core_start-self.context)
            end=min(frames,begin+self.window)
            self.buffer.fill(0)
            self.buffer[0,0,:end-begin,:]=encoded[0,:,begin:end].T
            offset=core_start-begin
            count=min(self.core,frames-core_start)
            def consume(values):
                if len(values)!=1 or values[0].size != self.window*self.upsample or not np.issubdtype(values[0].dtype,np.floating) or not np.isfinite(values[0]).all():
                    raise ValueError('Invalid compiled decoder audio contract')
                # Detach only the useful center before the shared owner resets.
                return values[0].reshape(-1)[offset*self.upsample:(offset+count)*self.upsample].astype(np.float32,copy=True)
            piece=self.graph.forward([self.buffer],reducer=consume)
            output[core_start*self.upsample:(core_start+count)*self.upsample]=piece
            count_windows+=1
        return output.reshape(1,1,-1),count_windows

    def decode_latents(self, encoded):
        with self.lock:
            self._ready()
            with self.graph.owner.lease(self.graph):
                return self._decode(encoded)[0]

    def run(self, output_names, feed):
        with self.lock:
            self._ready()
            self._validate_feed(feed)
            with self.graph.owner.lease(self.graph):
                started=time.perf_counter()
                outputs=self.encoder.run(None,feed)
                if len(outputs)!=1:
                    raise ValueError('Expected one CPU encoder latent output')
                encoded=outputs[0]
                encoded_at=time.perf_counter()
                audio,windows=self._decode(encoded)
                done=time.perf_counter()
                self.metrics.append(dict(encoder_seconds=encoded_at-started,decoder_graph_seconds=done-encoded_at,
                                         latent_frames=int(encoded.shape[2]),decoder_windows=windows))
                return [audio]

    def close(self):
        with self.lock:
            self.closed=True
            self.encoder=None
            self.graph=None
            self.buffer=None
            self.metrics.clear()


class TTSAdapter:
    @classmethod
    def from_bundle(cls, graph, bundle, *, encoder=None, threads=2, cache_bytes=1024*1024, cache_entries=32,
                    fixed_prompts=(), fixed_cache_bytes=0):
        if bundle.metadata.get('adapter') != 'piper-pinyin-hybrid-v1' or graph.spec != bundle.spec:
            raise ValueError('TTS graph and package identity/family differ')
        interface=json.loads(bundle.role('interface').read_text(encoding='utf-8'))
        config=json.loads(bundle.role('config').read_text(encoding='utf-8'))
        if interface.get('frontend') != 'xiaoya-prosody-v2' or interface.get('speaker_id') != 0:
            raise ValueError('Unsupported frontend or speaker contract')
        if config.get('phoneme_type') != 'pinyin' or config.get('num_speakers') != 1 or not isinstance(config.get('phoneme_id_map'),dict):
            raise ValueError('Expected single-speaker pinyin voice config')
        rate=config.get('audio',{}).get('sample_rate')
        if type(rate) is not int or not 8000 <= rate <= 96000:
            raise ValueError('Invalid voice sample rate')
        if type(threads) is not int or not 1 <= threads <= 4:
            raise ValueError('CPU threads must be 1..4')
        if encoder is None:
            import onnxruntime as ort
            options=ort.SessionOptions()
            options.intra_op_num_threads=threads
            options.inter_op_num_threads=1
            options.enable_cpu_mem_arena=False
            options.enable_mem_pattern=False
            encoder=ort.InferenceSession(str(bundle.role('encoder')),sess_options=options,providers=['CPUExecutionProvider'])
        import xiaoya_prosody,prosody_frontend,tts_engine
        # Runtime code remains code, rather than executable contents loaded
        # from arbitrary model packages. Bind its identity to cache entries.
        frontend_hashes={module.__name__:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                         for module in (xiaoya_prosody,prosody_frontend,tts_engine)}
        namespace=hashlib.sha256(json.dumps(dict(bundle=bundle.metadata,frontend=frontend_hashes),sort_keys=True).encode()).hexdigest()
        session=HybridGraphSession(graph,encoder,interface)
        frontend=xiaoya_prosody.LocalTTS.__new__(xiaoya_prosody.LocalTTS)
        frontend.style='warm'
        frontend.config=config
        frontend.sample_rate=rate
        frontend.memory_arena=False
        frontend.load_seconds=0.
        frontend.session=session
        return cls(frontend,namespace,cache_bytes,cache_entries, fixed_prompts, fixed_cache_bytes)

    def __init__(self, frontend, namespace, cache_bytes, cache_entries, fixed_prompts=(), fixed_cache_bytes=0):
        if not 0 <= fixed_cache_bytes <= cache_bytes:
            raise ValueError('Fixed cache must fit within the total PCM cache budget')
        if any(not isinstance(p, str) or not p for p in fixed_prompts):
            raise ValueError('Fixed replies must be nonempty exact strings')
        self.frontend=frontend
        self.session=frontend.session
        self.namespace=namespace
        self.fixed_prompts=frozenset(fixed_prompts)
        self.cache=PCMCache(namespace,cache_bytes-fixed_cache_bytes,cache_entries)
        self.fixed_cache=PCMCache(namespace,fixed_cache_bytes,len(self.fixed_prompts))
        self.lock=threading.RLock()
        self.closed=False

    def synthesize(self, text, style='warm'):
        from xiaoya_prosody import STYLES
        started=time.perf_counter()
        with self.lock:
            acquired=time.perf_counter()
            if self.closed:
                raise RuntimeError('TTS adapter is closed')
            if style not in STYLES:
                raise ValueError('Unsupported speaking style')
            self.session.metrics.clear()
            speech_metrics=None
            def make():
                nonlocal speech_metrics
                speech=self.frontend.synthesize(text,style=style)
                speech_metrics=speech.metrics
                return PCM(speech.pcm.astype('<i2',copy=False).tobytes(),speech.sample_rate)
            fixed = text in self.fixed_prompts and style == 'warm'
            selected = self.fixed_cache if fixed else self.cache
            pcm,hit=selected.get_or_create(text,dict(style=style,settings=STYLES[style]),make)
            return pcm,dict(cache_hit=hit,queue_seconds=acquired-started,api_seconds=time.perf_counter()-started,
                            cache=self.cache.stats(),fixed_cache=self.fixed_cache.stats(), cache_kind='fixed' if fixed else 'dynamic',
                            neural_chunks=list(self.session.metrics),speech=speech_metrics,
                            scope='Full owned PCM ready; no playback or acoustic timing')

    def prewarm(self):
        results = []
        for text in sorted(self.fixed_prompts):
            pcm, metrics = self.synthesize(text)
            results.append(dict(text=text, bytes=len(pcm.data), metrics=metrics))
        if self.fixed_cache.stats()['entries'] != len(self.fixed_prompts):
            raise ValueError('Fixed reply PCM exceeds reserved budget; no partial prewarm accepted')
        return results

    def close(self):
        with self.lock:
            if not self.closed:
                self.cache.clear()
                self.fixed_cache.clear()
                self.session.close()
                self.frontend=None
                self.closed=True
        # Graph belongs to the shared owner; caller explicitly unloads it after
        # closing this adapter. Existing immutable PCM remains safe to play.
