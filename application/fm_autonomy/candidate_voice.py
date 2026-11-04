"""Explicit candidate backend, created only inside the shared scheduler's child.

All three graphs use the SAME DeviceOwner. Nothing imports the SDK until the
acceptance configuration has passed. ASR replacement is forbidden in production.
"""
from contextlib import ExitStack
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from .dependencies import add_vendor
from .vehicle_nlu import VehicleNLU, SCHEMA

FIXED_REPLIES = ('我在', '语音模型加载完成。', '未识别出支持的指令。',
                 '数字或单位不明确，未执行。', '底盘反馈异常，未继续运动。')


def read_config(root):
    config = json.loads((Path(root) / 'voice_candidate.json').read_text(encoding='utf-8'))
    return validate_config(config)


def validate_config(config):
    if config.get('schema') != 'fm-voice-candidate-v1':
        raise ValueError('unsupported_voice_candidate_configuration')
    track = config.get('release_track')
    if track == 'all_icraft339_final':
        if config.get('asr_backend') != 'asr100_rebuilt':
            raise ValueError('all_339_requires_rebuilt_asr_no_legacy_fallback')
        audit_all_339(config)
        if config.get('execution_mode') != 'authorized_candidate_validation':
            raise RuntimeError('all_339_production_quality_hold_validation_mode_required')
        from .voice_validation_gate import validate_evidence, validate_authorization
        validate_evidence(config, verified_file)
        validate_authorization(config, verified_file)
        return config
    if track != 'legacy_baseline':
        raise ValueError('explicit_release_track_required')
    if config.get('asr_backend') != 'original_npu100_native':
        raise ValueError('new_asr_on_hold_production_replacement_forbidden')
    if config.get('hardware_acceptance') != {
            'runtime_339_compatible': True, 'bert_tts_same_owner_validated': True,
            'original_asr_on_selected_runtime_validated': True}:
        raise RuntimeError('voice_candidate_pending_hardware_acceptance')
    return config


def verified_file(entry):
    path = Path(entry['path'])
    if not path.is_absolute():
        from .asset_binding import inside
        from .dependencies import ROOT
        path = inside(ROOT, entry['path'])
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != entry['sha256']:
        raise ValueError('candidate_asset_identity_mismatch:' + str(path))
    return path


def audit_all_339(config):
    """Actual compiled metadata and identities; SDK readability is not compilation."""
    identities = {}
    for kind in ('asr', 'bert', 'tts'):
        manifest = verified_file(config[kind + '_manifest'])
        bundle = json.loads(manifest.read_text(encoding='utf-8'))
        paths = {}
        for role in ('network_json', 'network_raw'):
            descriptor = bundle['files'][role]
            path = (manifest.parent / descriptor['path']).resolve()
            if manifest.parent.resolve() not in path.parents:
                raise ValueError('model_role_outside_bundle')
            paths[role] = verified_file(dict(path=str(path), sha256=descriptor['sha256']))
        network = json.loads(paths['network_json'].read_text(encoding='utf-8'))
        if (bundle.get('compiler_sdk') != '3.39.0' or network.get('icraft_version') != 'v3.39.0'
                or network.get('icraft_xir_version') != '3.39.0.0'):
            raise ValueError('final_model_not_compiled_by_icraft339:' + kind)
        identities[kind] = dict(manifest=str(manifest), compiler_sdk='3.39.0',
                               production_replacement_allowed=bundle.get('production_replacement_allowed'))
    return identities


def preflight_bundles(config):
    """Validate every file role and the business contract before opening hardware."""
    add_vendor('voice_runtime')
    from model_bundle import ModelBundle
    bundles={kind:ModelBundle(verified_file(config[kind+'_manifest']).parent) for kind in ('asr','bert','tts')}
    interface=json.loads(bundles['bert'].role('interface').read_text(encoding='utf-8'))
    contract=bundles['bert'].role('command_contract').read_bytes()
    local=Path(__file__).resolve().parents[1]/'command_contract.json'
    if hashlib.sha256(contract).hexdigest()!=interface['command_contract_sha256']:
        raise ValueError('command_contract_identity_mismatch')
    if local.read_bytes()!=contract:raise ValueError('candidate_business_contract_not_supported')
    return bundles


def bind_adapters(owner, config, stack, progress=None, *, validated_bundles=None):
    """Dependency-injected owner also permits host tests with graphs, never devices."""
    final = config.get('release_track') == 'all_icraft339_final'
    if final and validated_bundles is None:
        validate_config(config)
    elif not final and config.get('release_track') != 'legacy_baseline':
        raise ValueError('explicit_release_track_required')
    add_vendor('voice_runtime', 'voice_policy')
    from model_bundle import ModelBundle
    from bert_adapter import BertAdapter
    from tts_adapter import TTSAdapter
    from original_npu_asr import OriginalNPUASR
    from graph_runtime import ModelSpec
    from tokenizers import Tokenizer
    bundles = dict(validated_bundles or {})
    for kind in ('bert', 'tts'):
        if kind not in bundles:
            manifest = verified_file(config[kind + '_manifest'])
            bundles[kind] = ModelBundle(manifest.parent)
    interface = json.loads(bundles['bert'].role('interface').read_text(encoding='utf-8'))
    contract = bundles['bert'].role('command_contract')
    if hashlib.sha256(contract.read_bytes()).hexdigest() != interface['command_contract_sha256']:
        raise ValueError('command_contract_identity_mismatch')
    local_contract = Path(__file__).resolve().parents[1] / 'command_contract.json'
    if contract.read_bytes() != local_contract.read_bytes():
        raise ValueError('candidate_business_contract_not_supported')
    if progress: progress.stage(2)
    if final:
        from .candidate_asr import ValidationASR
        bundle = bundles.get('asr') or ModelBundle(verified_file(config['asr_manifest']).parent)
        asr = ValidationASR(owner.load(bundle.spec), bundle)
    else:
        if config['asr_backend'] != 'original_npu100_native':
            raise ValueError('new_asr_on_hold_production_replacement_forbidden')
        assets = {key: verified_file(config[key]) for key in ('asr_json', 'asr_raw', 'asr_cmvn', 'asr_spm')}
        graph = owner.load(ModelSpec('original_asr100', str(assets['asr_json']), str(assets['asr_raw'])))
        asr = OriginalNPUASR(graph, assets['asr_cmvn'], assets['asr_spm'], 'legacy')
    stack.callback(asr.close)
    if progress: progress.stage(2, True); progress.stage(3)
    bert_graph = owner.load(bundles['bert'].spec)
    nlu = VehicleNLU(BertAdapter.from_bundle(bert_graph, bundles['bert'],
                     Tokenizer.from_file(str(bundles['bert'].role('tokenizer')))))
    if progress: progress.stage(3, True); progress.stage(4)
    tts_graph = owner.load(bundles['tts'].spec)
    # Model/frontend-bound immutable PCM, preserving the previous bounded cache.
    tts = TTSAdapter.from_bundle(tts_graph, bundles['tts'], threads=1,
                                fixed_prompts=FIXED_REPLIES, fixed_cache_bytes=655360)
    stack.callback(tts.close)
    if progress: progress.stage(4, True)
    return asr, nlu, tts


class CandidateTurn:
    """ASR -> model-bound BERT -> structured intent. No household executor."""
    def __init__(self, asr, nlu, tts, check):
        self.asr, self.nlu, self.tts, self.check = asr, nlu, tts, check

    def run(self, op, payload):
        self.check()
        started=time.perf_counter()
        if op == 'command_text':
            validate_text_input(payload)
            envelope=self.nlu.understand(payload)
            self.check()
            return dict(text=payload,message='文字已解析，等待控制层校验',intent=SCHEMA,
                vehicle_nlu=envelope,audio=None,control_validation_required=True,hardware_executed=False,
                timing=dict(nlu_seconds=time.perf_counter()-started))
        if op == 'text':
            validate_text_input(payload)
            from .vehicle_nlu import validate_envelope
            from .confirmed_semantics import guard
            envelope=self.nlu.understand(payload)
            self.check()
            decision=guard(payload,awake=False,motion_active=False)
            reason=None
            try:validate_envelope(payload,envelope)
            except ValueError as exc:reason=str(exc)
            valid=reason is None and decision['kind']=='candidate'
            reply='文字解析完成，当前仅诊断，未执行车辆动作。'
            if reason:reply='模型结果与指令规则不一致，未执行车辆动作。'
            elif decision['kind']=='clarify':reply='请补充距离或角度及单位，当前未执行车辆动作。'
            elif decision['kind']!='candidate':reply='请发送包含小雅小雅的完整车辆指令，当前未执行车辆动作。'
            elif decision['semantic']['intent']=='stop':reply='已识别停车文本，当前仅诊断，未发送控制指令。'
            self.check()
            return dict(text=payload,message=reply,diagnostic_reply=reply,intent=SCHEMA,
                vehicle_nlu=envelope,guard_decision=decision,semantic_validated=valid,
                rejection_reason=reason,nlu_prediction_present=envelope.get('prediction') is not None,
                diagnostic_only=True,audio=None,hardware_executed=False)
        if op == 'audio':
            import numpy as np
            add_vendor('voice_runtime')
            from asr_bucket_adapter import AsrInputLengthError
            if not isinstance(payload, bytes) or len(payload) % 2 or not 800 <= len(payload) <= 960000:
                raise ValueError('bounded_pcm16_mono_16khz_required')
            try:
                asr = self.asr.transcribe(np.frombuffer(payload, dtype='<i2').copy())
            except AsrInputLengthError as exc:
                self.check()
                return dict(text='',message='这段录音过长，本次未执行；稍停顿后重新说一句。',intent='asr_input_rejected',
                            audio=None,hardware_executed=False,input_rejected=True,rejection_reason='asr_input_too_long',
                            feature_frames=exc.frames,max_feature_frames=exc.limit,
                            timing=dict(asr_seconds=time.perf_counter()-started,audio_seconds=len(payload)/32000))
            asr_done=time.perf_counter()
            text = asr['text']
            self.check()
            envelope = self.nlu.understand(text)
            nlu_done=time.perf_counter()
            envelope['asr_evidence'] = {key:asr[key] for key in ('ids','valid_length','feature_frames',
                'timing','execution_mode','production_replacement_allowed') if key in asr}
            self.check()
            # Parent emits a control receipt before any optional speech. A model
            # response must never assert that the vehicle has already moved.
            return dict(text=text, message='语音已解析，等待控制层校验', intent=SCHEMA,
                        vehicle_nlu=envelope, audio=None, hardware_executed=False,
                        timing=dict(asr_seconds=asr_done-started,nlu_seconds=nlu_done-asr_done))
        if op == 'speak':
            pcm, metrics = self.tts.synthesize(payload)
            self.check()
            if pcm.channels != 1 or len(pcm.data) > 4194304:
                raise ValueError('PCM_budget_exceeded')
            return dict(text=payload, message=payload, intent='text_playback',
                        audio=dict(data=pcm.data, channels=pcm.channels, sample_rate=pcm.sample_rate),
                        hardware_executed=False,
                        timing=dict(tts_seconds=time.perf_counter()-started,cache_hit=metrics.get('cache_hit'),
                                    cache_kind=metrics.get('cache_kind'),audio_seconds=len(pcm.data)/(2*pcm.sample_rate)))
        raise ValueError('unknown_candidate_model_operation')


def validate_text_input(text):
    if (not isinstance(text,str) or not text.strip() or len(text)>120 or '\0' in text or
            len(text.encode('utf-8'))>600):
        raise ValueError('Text requires 1..120 characters, <=600 bytes, without NUL')


class CandidateBoardModels:
    def __init__(self, root, generation):
        started=time.monotonic()
        from .voice_progress import LoadProgress
        progress = LoadProgress(root)
        progress.stage(0)
        config = read_config(root)  # Gate BEFORE imports that can open native hardware.
        companion_prepared = None
        if config.get('resident_bev'):
            from .resident_companion import prepare
            companion_prepared = prepare(config['resident_bev'], verified_file)
            progress.stages = progress.stages[:-1] + ('加载共同驻留的视觉模型', '语音与视觉模型就绪')
        bundles = None
        if config.get('release_track')=='all_icraft339_final':
            bundles = preflight_bundles(config)
        progress.stage(0, True); progress.stage(1)
        self.config = config
        for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
            os.environ[name]='1'
        for path in config.get('dependency_roots',[]):
            if path not in sys.path:sys.path.append(path)
        # Missing CPU dependencies must not be discovered after opening NPU.
        import importlib
        for module in ('numpy','tokenizers','onnxruntime','kaldi_native_fbank','sentencepiece','pypinyin'):
            importlib.import_module(module)
        if companion_prepared is not None:
            importlib.import_module('cv2')
        if config.get('release_track') == 'all_icraft339_final':
            from .voice_validation_gate import validate_authorization, require_selected_runtime
            validate_authorization(config, verified_file,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            require_selected_runtime(config)
        progress.stage(1, True)
        self.generation, self.epoch = generation, generation.value
        self.stack = ExitStack()
        add_vendor('voice', 'voice_runtime', 'voice_policy')
        from graph_runtime import DeviceOwner
        from voice_runtime_factory import close_board_owner
        metrics = CandidateMetrics(Path(root)/'validation_metrics.json')
        self.metrics=metrics
        try:
            owner = DeviceOwner(metrics)
            self.owner=owner
            self.stack.callback(close_board_owner, owner, metrics)
            self.turn = CandidateTurn(*bind_adapters(owner, config, self.stack, progress,
                                      validated_bundles=bundles), self.check)
            progress.stage(5)
            self.check()
            prewarm=self.turn.tts.prewarm()
            metrics.emit('fixed_replies_prewarmed',replies=prewarm)
            progress.stage(5,True)
            self.companion = None
            if companion_prepared is not None:
                from .resident_companion import ResidentCompanion
                progress.stage(6)
                self.companion = ResidentCompanion(owner, companion_prepared)
                self.stack.callback(self.companion.close)
                metrics.emit('voice_bev_joint_resident', **self.companion.evidence)
                progress.stage(6,True)
            self.load_seconds=time.monotonic()-started
            metrics.emit('candidate_resident_ready',load_seconds=self.load_seconds,
                         allocation=owner.allocations(),production_replacement_allowed=False)
            progress.stage(len(progress.stages)-1, True)
        except BaseException:
            self.stack.close()
            raise

    def check(self):
        if self.config.get('release_track') == 'all_icraft339_final':
            from .voice_validation_gate import validate_authorization
            try:
                validate_authorization(self.config, verified_file)
            except ValueError as exc:
                from model_process import Cancelled
                raise Cancelled('candidate_validation_grant_expired_or_changed') from exc
        if self.generation.value != self.epoch:
            from model_process import Cancelled
            raise Cancelled('candidate_interaction_generation_cancelled')

    def run(self, op, epoch, payload):
        self.epoch = epoch
        self.check()
        if op=='validation_status':
            import resource
            return dict(load_seconds=self.load_seconds,allocation=self.owner.allocations(),
                ps_hwm_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                resident_graphs=[graph.spec.name for graph in self.owner.graphs],
                companion=self.companion.evidence if self.companion else None,
                execution_mode=self.config.get('execution_mode'),production_replacement_allowed=False)
        if op=='bev_photo':
            if self.companion is None:raise RuntimeError('resident_bev_not_configured')
            result=self.companion.infer(payload)
            self.check()
            return result
        return self.turn.run(op, payload)

    def quiescent_after_error(self):
        owner=self.owner
        with owner.mutex:
            return not (owner.closed or owner.failed or owner.quarantined or owner.busy) and all(
                graph.inflight is None for graph in owner.graphs)

    def close(self):
        self.stack.close()
        self.metrics.emit('candidate_closed',quiescence_receipt=True)


class CandidateMetrics:
    def __init__(self,path):
        self.path=path;self.events=[];self.last_snapshot=0.
    def emit(self,event_name,**value):
        now=time.monotonic()
        snapshot_due=event_name!='graph_forward' or now-self.last_snapshot>=1
        if snapshot_due and Path('/proc/self/status').is_file():
            from metrics import memory
            value['model_process_memory'] = memory(include_pss=False)
            value['model_process_pid'] = os.getpid()
        record=dict(event=event_name,monotonic_s=now,**value)
        self.events.append(record)
        del self.events[:-4096]
        # Keep complete append-only evidence without rewriting an ever-growing
        # JSON array inside every decoder window's latency-critical path.
        with self.path.with_suffix('.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
        if snapshot_due:
            temporary=self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.events,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            os.replace(temporary,self.path)
            self.last_snapshot=now
