"""Reuse the complete accepted ASR/BERT/TTS controller under one NPU lease."""
from pathlib import Path
import time
from .dependencies import add_vendor
from .accelerator import NpuLease


def make_voice(root, event, application_getter, hub=None, *, mic_factory=None, player_factory=None):
    add_vendor('voice')
    from controller import Controller
    from model_process import ProcessModels, BoardModels

    class VoiceController(Controller):
        def status(self):
            from .voice_progress import read_progress
            result = super().status()
            result['load_progress'] = read_progress(self.root)
            app = application_getter()
            result['local_active'] = bool(app and app.local_voice_active)
            result['awake'] = bool(app and app.confirmed.awake)
            if app:
                control = app.arbiter.status()
                result['motion'] = {k:control.get(k) for k in ('mode','reason','metric','last_metric_report')}
            return result

        def _cancel_locked(self, reason, stopped=True):
            result = super()._cancel_locked(reason,stopped=stopped)
            if hub is not None: hub.cancel()
            return result

        def admit_microphone(self):
            job = super().admit_microphone()
            if job is not None:
                job['control_epoch'] = application_getter().arbiter.epoch
            return job

        def finish_microphone(self, job, pcm):
            job['capture_completed_at'] = time.monotonic()
            return super().finish_microphone(job, pcm)

    def wrapped_event(kind, value):
        event(kind, value)
        candidate = kind == 'vehicle_nlu_ready'
        legacy = (kind == 'job_finished' and value.get('op') == 'listen' and
                  value.get('state') == 'completed' and not value.get('vehicle_nlu_delivered'))
        if candidate or legacy:
            app = application_getter()
            try:
                if candidate and not isinstance(value.get('nlu'), dict):
                    raise ValueError('missing_vehicle_nlu_envelope')
                result = app.voice_command(value.get('text', ''), value.get('control_epoch'),
                                           value.get('capture_completed_at', float('-inf')),
                                           nlu=value.get('nlu') if candidate else None)
                event('voice_control_receipt', result)
                return result
            except Exception as exc:
                from .voice_feedback import rejection
                event('voice_control_rejected', {'reason': str(exc)[:140]})
                return dict(accepted=False, reason=str(exc)[:140],
                            message=rejection(str(exc)))

    # The process backend acquires the lease before any SDK/model construction.
    # Its root supplies lock-path via an explicit config read in the child.
    if (Path(root) / 'voice_candidate.json').exists() and hub is None:
        raise RuntimeError('voice_candidate_requires_shared_npu_hub')
    factory = hub.voice_models if hub is not None else (
        lambda source_root, generation: ProcessModels(source_root, generation, ExclusiveBoardModels))
    options = {}
    if mic_factory is not None: options['mic_factory'] = mic_factory
    if player_factory is not None: options['player_factory'] = player_factory
    return VoiceController(root, wrapped_event, models_factory=factory, **options)


class ExclusiveBoardModels:
    def __init__(self, root, generation):
        import json
        add_vendor('voice')
        from model_process import BoardModels
        config = Path(root) / 'npu_owner.json'
        lock_path = json.loads(config.read_text(encoding='utf-8'))['lock_path']
        self.lease = NpuLease(lock_path)
        self.lease.__enter__()
        try:
            self.backend = BoardModels(root, generation)
        except BaseException as exc:
            # Factory failure could leave native DMA unsettled. Keep the owner
            # in the existing process rather than hand the NPU to a competitor.
            self.quarantined = True
            self.lease.quarantine('voice_initialization_failed:' + str(exc)[:100])
            raise

    def run(self, op, epoch, payload):
        return self.backend.run(op, epoch, payload)

    def close(self):
        try:
            self.backend.close()
        except BaseException as exc:
            self.lease.quarantine('voice_close_failed:' + str(exc)[:100])
            raise
        else:
            self.lease.__exit__()
