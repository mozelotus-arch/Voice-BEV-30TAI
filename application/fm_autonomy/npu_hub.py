"""Concrete voice / FastBEV / CAM++ adapters sharing one model scheduler.

The CAN control worker is independent. Only model operations enter this queue.
Hardware profiles require measured DDR and latency budgets; no guessed defaults.
"""
import json
from pathlib import Path
import time
from .accelerator import Budget,SerialModelScheduler,RequestCancelled
from .dependencies import add_vendor


class VoiceProcessBackend:
    def __init__(self,root,generation):
        add_vendor('voice')
        from model_process import ProcessModels,Cancelled,InitializationClosedError,QuiescentOperationError
        self.cancelled=Cancelled
        self.quiescent_error=QuiescentOperationError
        # Parent scheduler owns the process-wide lease. No competing child lock.
        backend_factory = None
        if (Path(root) / 'voice_candidate.json').exists():
            from .candidate_voice import CandidateBoardModels
            backend_factory = CandidateBoardModels
        try:
            self.models=ProcessModels(root,generation,backend_factory)
        except InitializationClosedError as exc:
            from .accelerator import QuiescentInitializationError
            raise QuiescentInitializationError(str(exc)) from exc

    def run(self,op,payload):
        if op=='load':return {'loaded':True}
        try:return self.models.run(op,payload['epoch'],payload['payload'])
        except self.cancelled as exc:raise RequestCancelled(str(exc)) from exc
        except self.quiescent_error as exc:
            from .accelerator import QuiescentOperationError
            raise QuiescentOperationError(str(exc)) from exc

    def close(self):
        # The shared protocol requires closed + exit 0, and sets closed only
        # after verification. Failed teardown remains retryably unconfirmed.
        self.models.close()


class ScheduledVoiceModels:
    def __init__(self,hub,root,generation):
        self.hub=hub
        if hub.voice_context is not None and hub.voice_context!=(str(root),generation):
            raise RuntimeError('voice_controller_already_bound')
        hub.voice_context=(str(root),generation)
        self.closed=False
        self.pinned = hub.profile['models']['voice'].get('pin_resident_for_validation') is True
        if self.pinned: hub.scheduler.pin('voice')
        self._call('load',{})

    def _call(self,op,payload):
        if self.closed:raise RuntimeError('voice_adapter_closed')
        limits=self.hub.scheduler.budgets['voice']
        budget=limits.measured_max_call_s
        if op=='load' and limits.load_timeout_s is not None:
            budget += limits.load_timeout_s
        # Queue residence counts against deadline. Stop/cancel still operates
        # independently and invalidates generation before this native call ends.
        try:
            return self.hub.scheduler.submit('voice',op,payload,time.monotonic()+budget,
                                             priority=0).result()
        except RequestCancelled as exc:
            add_vendor('voice')
            from model_process import Cancelled
            raise Cancelled(str(exc)) from exc

    def run(self,op,epoch,payload):return self._call(op,dict(epoch=epoch,payload=payload))
    def close(self):
        self.closed=True
        if self.pinned:self.hub.scheduler.unpin('voice')


class NpuHub:
    def __init__(self,profile,*,hardware=True,factory_overrides=None):
        checked_config = None
        if hardware:
            from .profile import require_supported_hardware
            checked_config = require_supported_hardware(profile)
        self.profile=profile
        self.voice_context=None
        self.authorized_validation = False
        if hardware and profile.get('authorized_voice_validation') is not None:
            config=checked_config
            if config.get('release_track')!='all_icraft339_final':
                raise ValueError('all339_validation_profile_required')
            if (set(profile['models'])!={'voice'} or
                    profile['models']['voice'].get('pin_resident_for_validation') is not True or
                    profile['models']['voice'].get('budget_evidence_kind')!='supervised_capacity_ceiling_not_measurement' or
                    profile['models']['voice']['budget'].get('load_timeout_s') is None):
                raise ValueError('voice_validation_only_bounded_residence_required')
            self.authorized_validation = True
        budgets={name:Budget(**entry['budget']) for name,entry in profile['models'].items()}
        factories={}
        for name,entry in profile['models'].items():
            if factory_overrides and name in factory_overrides:
                factories[name]=factory_overrides[name]
            elif name=='voice':factories[name]=self._voice
            elif name=='campp':
                factories[name]=lambda e=entry:self._campp(e)
            elif name=='bev':
                factories[name]=lambda e=entry:self._bev(e)
            else:raise ValueError('unsupported_model:'+name)
        self.scheduler=SerialModelScheduler(factories,budgets,profile['memory_limit_bytes'],
            profile['lock_path'],hardware=hardware,max_pending=4,
            supervised_capacity_validation=self.authorized_validation)

    @classmethod
    def from_file(cls,path):
        from .profile import load_profile
        return cls(load_profile(path),hardware=True)

    def _voice(self):
        if self.voice_context is None:raise RuntimeError('voice_controller_not_bound')
        if self.profile.get('authorized_voice_validation'):
            import hashlib
            config_path=Path(self.voice_context[0])/'voice_candidate.json'
            if hashlib.sha256(config_path.read_bytes()).hexdigest()!=self.profile['authorized_voice_validation']['sha256']:
                raise ValueError('voice_run_must_match_budgeted_configuration')
        return VoiceProcessBackend(*self.voice_context)

    def _campp(self,entry):
        if entry.get('backend_family')=='explicit_split339':
            from .campp_split import CamppSplitBackend
            return CamppSplitBackend(entry,owner_guard=self.scheduler.assert_owner)
        from .campp_sdk import CamppBackend
        return CamppBackend(entry['model_json'],entry['model_raw'],
            owner_guard=self.scheduler.assert_owner,dma_base=entry['dma_base'])

    def _bev(self,entry):
        if entry.get('backend_family') == 'nuscenes_pro_sa_managed_static':
            from .voice_validation_gate import require_selected_runtime
            require_selected_runtime(entry)
            from .managed_pro import ManagedProBackend
            return ManagedProBackend(entry,owner_guard=self.scheduler.assert_owner,
                owner_marker=str(Path(self.profile['lock_path']).with_name(Path(self.profile['lock_path']).name+'.active.json')))
        from .managed_bev import ManagedBevBackend
        return ManagedBevBackend(entry,owner_guard=self.scheduler.assert_owner,
                                 owner_marker=str(Path(self.profile['lock_path']).with_name(Path(self.profile['lock_path']).name+'.active.json')))

    def voice_models(self,root,generation):return ScheduledVoiceModels(self,root,generation)
    def perception(self,payload,deadline):
        return self.scheduler.submit('bev','infer',payload,deadline,key='six_camera',priority=10)
    def pro_static(self,payload,deadline):
        return self.scheduler.submit('bev','infer_static',payload,deadline,key='pro_static',priority=10)
    def pro_temporal(self,payload,deadline):
        return self.scheduler.submit('bev','infer_temporal',payload,deadline,key='pro_temporal',priority=10)
    def bev_photo(self,payload,deadline):
        # The companion is inside the already pinned voice child/DeviceOwner.
        # No backend switch, model unload, second child or second Device.Open.
        if self.voice_context is None:raise RuntimeError('load_joint_models_before_bev_photo')
        generation=self.voice_context[1]
        return self.scheduler.submit('voice','bev_photo',dict(epoch=generation.value,payload=payload),
                                     deadline,key='resident_bev_photo',priority=10)
    def embedding(self,features,deadline):
        return self.scheduler.submit('campp','embedding',features,deadline,priority=5)
    def embedding_pcm16(self,pcm,deadline):
        return self.scheduler.submit('campp','embedding_pcm16',pcm,deadline,priority=5)
    def cancel(self):self.scheduler.cancel()
    def close(self,timeout=None):return self.scheduler.close(timeout)
