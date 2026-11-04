"""Bind configured model assets to one owned voice runtime.

cpu_reference uses real CPU ASR/ONNX for offline integration. board_hybrid uses
CPU ASR, one shared ZG330 owner for BERT and TTS Decoder, CPU TTS Encoder.
Acceptance is recorded separately for exact sources/models/inputs. Failed
ASR500 candidates are not selected.
"""
from contextlib import ExitStack, contextmanager
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
import numpy as np
from bert_adapter import BertAdapter
from model_bundle import ModelBundle, digest
from runtime_contract import InputContract, validate_inputs
from tts_adapter import TTSAdapter
from voice_turn_runtime import VoiceTurnRuntime


class SherpaCPUASR:
    def __init__(self, model, tokens, threads):
        from sherpa_onnx import OfflineRecognizer
        self.model = OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=threads,
            sample_rate=16000, provider='cpu', language='zh', use_itn=True)

    def transcribe(self, pcm):
        if self.model is None:
            raise RuntimeError('ASR is closed')
        if pcm.dtype != np.int16 or pcm.ndim != 1 or not 400 <= len(pcm) <= 480000:
            raise ValueError('ASR expects bounded mono int16 PCM at 16 kHz')
        started = time.monotonic()
        stream = self.model.create_stream()
        stream.accept_waveform(16000, pcm.astype(np.float32)/32768.)
        self.model.decode_stream(stream)
        text = re.sub(r'<\|.*?\|>', '', stream.result.text).strip()
        return dict(text=text, processing_seconds=time.monotonic()-started, backend='sherpa_cpu_int8')

    def close(self):
        self.model = None


class CPUReferenceGraph:
    """Explicit CPU substitute, never an iCraft Host/NPU execution claim."""
    def __init__(self, bundle, session, kind):
        self.spec, self.session, self.kind = bundle.spec, session, kind
        compiled = json.loads(bundle.role('network_json').read_text(encoding='utf-8'))
        inputs = next(op['outputs'] for op in compiled['ops'] if op['_type_key'] == 'icraft::xir::Input')
        self.contracts = [InputContract.from_sdk_json(json.dumps(v['dtype'])) for v in inputs]
        self.owner = self
        self.lock = threading.RLock()
        self.calls = 0

    @contextmanager
    def lease(self, graph):
        with self.lock:
            if graph is not self or self.session is None:
                raise RuntimeError('CPU graph is closed or does not own this lease')
            yield graph

    def forward(self, arrays, reducer):
        with self.lease(self):
            arrays = validate_inputs(arrays, self.contracts)
            if self.kind == 'bert':
                length, hidden = self.contracts[0].shape[1:3]
                feed = dict(embedding_input=arrays[0].reshape(1, 1, length, hidden),
                            mask_input=arrays[1].reshape(1, 1, length, length))
            elif self.kind == 'tts':
                feed = {self.session.get_inputs()[0].name:
                        np.ascontiguousarray(arrays[0][:, 0, :, :].transpose(0, 2, 1))}
            else:
                raise ValueError('Unsupported CPU reference graph')
            outputs = self.session.run(None, feed)
            self.calls += 1
            return reducer(outputs)

    def close(self):
        with self.lock:
            self.session = None


def cpu_session(path, threads):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    return ort.InferenceSession(str(path), options, providers=['CPUExecutionProvider'])


def close_board_owner(owner, metrics):
    """Observe successful unload while the device remains open, then close it.

    A poisoned device uses the owner's conservative cleanup path instead of
    issuing new readiness-dependent operations. Close still runs if logging
    or an explicit unload fails.
    """
    try:
        if not owner.failed:
            for graph in reversed(list(owner.graphs)):
                owner.unload(graph)
            metrics.emit('all_graphs_unloaded', plddr=owner.allocations())
    finally:
        owner.close()


@contextmanager
def configured_runtime(config_path, sink, *, backend='cpu_reference', metrics=None):
    if backend not in ('cpu_reference', 'board_hybrid'):
        raise ValueError('Unknown backend; no automatic device fallback')
    if backend == 'board_hybrid' and (platform.system() != 'Linux' or metrics is None):
        raise ValueError('Board backend requires Linux and explicit SDK metrics')
    def phase(name):
        if metrics is not None:
            metrics.emit('initialization_phase', phase=name)
    phase('configuration_begin')
    config_path = Path(config_path).absolute()
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('schema') != 'voice-runtime-config-v1':
        raise ValueError('Unsupported runtime configuration')
    threads = config['cpu_threads']
    if type(threads) is not int or not 1 <= threads <= 4:
        raise ValueError('CPU thread count must be 1..4')
    def path(value):
        candidate = Path(value)
        return Path(os.path.abspath(str(candidate if candidate.is_absolute() else config_path.parent / candidate)))
    identities = {}
    def verified(name, entry):
        target = path(entry['path'])
        actual = digest(target)
        if actual != entry['sha256']:
            raise ValueError('Asset identity changed: '+name)
        identities[name] = dict(path=str(target), sha256=actual, bytes=target.stat().st_size)
        return target
    # These are explicit existing dependency folders; no installation/download.
    for folder in config.get('dependency_roots', []):
        location = path(folder)
        if not location.is_dir():
            raise FileNotFoundError('Configured dependency directory is missing')
        if str(location) not in sys.path:
            sys.path.append(str(location))
    source = path(config['policy_source'])
    for name, expected in config['policy_source_hashes'].items():
        if Path(name).name != name or not name.endswith('.py'):
            raise ValueError('Policy module must be a direct Python source file')
        verified('policy/'+name, dict(path=str(source/name), sha256=expected))
    sys.path.insert(0, str(source))
    from tokenizers import Tokenizer
    from session_ex import DuplexSession
    from voice_policy_bridge import BertNLU
    # Reject stale modules from a different project loaded earlier in this process.
    for name in config['policy_source_hashes']:
        module = importlib.import_module(name[:-3])
        if Path(os.path.abspath(module.__file__)) != source/name:
            raise ValueError('Policy import came from another project: '+name)
    phase('imports_complete_asset_verification_begin')
    asr_backend = config.get('asr_backend', 'sherpa_cpu_int8')
    if asr_backend not in ('sherpa_cpu_int8', 'original_npu100_native'):
        raise ValueError('Unknown ASR backend')
    if asr_backend == 'original_npu100_native' and backend != 'board_hybrid':
        raise ValueError('NPU ASR requires the board backend')
    asr_assets = {}
    keys = ('asr_model', 'asr_tokens') if asr_backend == 'sherpa_cpu_int8' else ('asr_json', 'asr_raw', 'asr_cmvn', 'asr_spm')
    for key in keys:
        asr_assets[key] = verified(key, config[key])
    os.environ['VOICE_BRIEF_REPLIES'] = '1' if config.get('brief_replies', False) else '0'
    bert = ModelBundle(path(config['bert_bundle']))
    tts = ModelBundle(path(config['tts_bundle']))
    identities['bert_bundle'] = dict(path=str(bert.folder), sha256=digest(bert.folder/'BUNDLE.json'))
    identities['tts_bundle'] = dict(path=str(tts.folder), sha256=digest(tts.folder/'BUNDLE.json'))
    reference_bert = reference_tts = None
    if backend == 'cpu_reference':
        for key, bundle in [('cpu_bert_reference', bert), ('cpu_tts_reference', tts)]:
            if config[key].get('for_bundle_sha256') != digest(bundle.folder/'BUNDLE.json'):
                raise ValueError('CPU reference is not bound to the selected model bundle: '+key)
        reference_bert = verified('cpu_bert_reference', config['cpu_bert_reference'])
        reference_tts = verified('cpu_tts_reference', config['cpu_tts_reference'])
    with ExitStack() as lifetime:
        phase('assets_verified_asr_load_begin')
        owner = None
        if backend == 'board_hybrid':
            from graph_runtime import DeviceOwner
            owner = DeviceOwner(metrics)
            lifetime.callback(close_board_owner, owner, metrics)
        if asr_backend == 'original_npu100_native':
            from graph_runtime import ModelSpec
            from original_npu_asr import OriginalNPUASR
            asr_graph = owner.load(ModelSpec('original_asr100', str(asr_assets['asr_json']), str(asr_assets['asr_raw'])))
            asr = OriginalNPUASR(asr_graph, asr_assets['asr_cmvn'], asr_assets['asr_spm'], config.get('asr_length_mode', 'legacy'))
        else:
            asr = SherpaCPUASR(asr_assets['asr_model'], asr_assets['asr_tokens'], threads)
        lifetime.callback(asr.close)
        phase('asr_loaded')
        if backend == 'board_hybrid':
            bert_graph = owner.load(bert.spec)
            tts_graph = owner.load(tts.spec)
            metrics.emit('joint_graphs_loaded', plddr=owner.allocations())
        else:
            bert_graph = CPUReferenceGraph(bert, cpu_session(reference_bert, threads), 'bert')
            lifetime.callback(bert_graph.close)
            tts_graph = CPUReferenceGraph(tts, cpu_session(reference_tts, threads), 'tts')
            lifetime.callback(tts_graph.close)
        phase('graphs_loaded_bert_adapter_begin')
        nlu = BertNLU(BertAdapter.from_bundle(bert_graph, bert, Tokenizer.from_file(str(bert.role('tokenizer')))))
        lifetime.callback(nlu.close)
        phase('bert_adapter_loaded_tts_adapter_begin')
        voice = TTSAdapter.from_bundle(tts_graph, tts, threads=threads,
                                      cache_bytes=config['tts_cache_bytes'], cache_entries=config['tts_cache_entries'],
                                      fixed_prompts=config.get('fixed_prompts', []),
                                      fixed_cache_bytes=config.get('fixed_cache_bytes', 0))
        lifetime.callback(voice.close)
        phase('tts_adapter_loaded')
        if config.get('fixed_prompts'):
            phase('fixed_reply_prewarm_begin')
            warmed = voice.prewarm()
            if metrics is not None:
                metrics.emit('fixed_reply_prewarm_complete', replies=warmed)
        runtime = VoiceTurnRuntime(asr, DuplexSession(nlu), voice, sink,
            max_audio_seconds=config['max_audio_seconds'], max_pcm_bytes=config['max_output_pcm_bytes'],
            health_check=owner.check_ready if owner is not None else None)
        lifetime.callback(runtime.close)
        runtime.identity = dict(backend=backend, config_sha256=digest(config_path), assets=identities,
                                asr_backend=asr_backend, cpu_threads=threads, brief_replies=config.get('brief_replies', False),
                                versions={name:version(name) for name in ('numpy','sherpa-onnx','onnxruntime','tokenizers','pypinyin')},
                                board_integration_accepted=False,
                                scope='Configured runtime; acceptance is external and bound to exact sources/models/inputs')
        phase('runtime_ready')
        yield runtime
