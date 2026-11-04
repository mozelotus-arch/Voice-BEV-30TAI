"""Confirmed distance/angle commands. No duration conversion or actuator access."""
from .confirmed_semantics import guard, WAKE
from .vehicle_nlu import validate_envelope, command_body


class ConfirmedCommands:
    def __init__(self, application):
        self.app = application
        self.awake = False

    def clear(self):
        self.awake = False

    def activate(self):
        # Interaction state, not a motion ticket. Every command still checks epoch.
        self.awake = True

    def handle(self, text, epoch, envelope=None):
        app, arb = self.app, self.app.arbiter
        with arb.lock:
            # Every result, including a wake or stop, belongs to its captured epoch.
            arb._context(epoch)
            if app.stopped:
                return dict(accepted=False, reason='explicit_resume_required')
            canonical = command_body(text)
            guarded_text = WAKE + '，' + canonical if text.strip().startswith(WAKE) else canonical
            decision = guard(guarded_text, awake=self.awake, motion_active=arb.mode in ('manual', 'command', 'auto','metric'))
            if envelope is not None:
                try:
                    validate_envelope(text, envelope)
                except ValueError as exc:
                    return dict(accepted=False, reason=str(exc), hardware_executed=False)
            kind = decision['kind']
            if text.strip().startswith(WAKE):
                self.activate()
            if kind == 'wake':
                return dict(accepted=True, kind='wake', message='我在', hardware_executed=False)
            if kind == 'clarify':
                semantic = decision['semantic']
                message = '请说明要走多少米或多少厘米' if semantic['intent'] == 'move' else '请说明要转多少度'
                return dict(accepted=False, kind=kind, message=message, semantic=semantic, hardware_executed=False)
            if kind != 'candidate':
                return dict(accepted=False, reason=kind, hardware_executed=False,
                            message='请先说小雅小雅' if kind == 'sleeping' else '指令不在已确认范围内，未执行')
            semantic = decision['semantic']
            if semantic['intent'] == 'stop':
                # Physical stop is requested before interaction cancellation can wait.
                arb.stop('confirmed_voice_stop')
                return dict(accepted=True, kind='stop', control=arb.status(),
                            hardware_executed=False, stop_requested=True,
                            message='已请求停车并清空未完成动作')
            photo=getattr(app,'photo_service',None)
            if photo is not None and photo.busy:
                return dict(accepted=False,reason='photo_task_requires_stationary',hardware_executed=False,
                            message='正在生成鸟瞰图，请保持小车静止。')
            if semantic['intent'] == 'auto_start':
                if arb.confirmed_auto_seconds is not None:
                    return dict(accepted=True,control=arb.start_auto(epoch,arb.confirmed_auto_seconds),
                                message='已请求启动自动避障，状态以控制回执为准')
                # No confirmed duration/speed/avoidance profile: never reuse old 30s default.
                return dict(accepted=False, semantic=semantic, hardware_executed=False,
                            reason='automatic_avoidance_profile_and_hardware_acceptance_required',
                            message='自动避障尚未完成配置和验收，未启动')
            if getattr(arb,'metric',None) is not None:
                return arb.start_metric(epoch,semantic)
            return dict(accepted=False, semantic=semantic, hardware_executed=False,
                        reason='distance_angle_feedback_limits_calibration_and_acceptance_required',
                        message='距离和角度控制尚未完成闭环配置、标定与验收，未执行车辆动作')
