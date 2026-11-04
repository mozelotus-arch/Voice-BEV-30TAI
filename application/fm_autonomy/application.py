"""One application dispatcher for FVB1 voice, manual CAN and local autonomy."""
import time
import copy
import json
import hashlib
from contextlib import nullcontext
from .intent import parse


def wire_voice_reply(value):
    """FVB1 allows 1000 UTF-8 bytes including sid/seq/envelope."""
    result=copy.deepcopy(value)
    progress=result.get('load_progress')
    if progress:
        result['load_progress']={k:progress[k] for k in
            ('stage','label','completed_stages','total_stages','started_at','updated_at','complete') if k in progress}
    receipt=result.get('control_receipt')
    if isinstance(receipt,dict):
        result['control_receipt']={k:receipt[k] for k in ('accepted','reason','message','hardware_executed') if k in receipt}
    size=lambda:len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())
    if size()>850:
        result.pop('motion',None)  # Full report remains on local HTTP/control status.
        result.pop('timing',None)
        if result.get('load_progress'):result['load_progress'].pop('label',None)
        result.pop('backend',None)
        if result.get('control_receipt'):result['control_receipt'].pop('message',None)
    if size()>850:
        # Preserve essential state and error evidence, not verbose duplicate metadata.
        for key in ('text_sha256','capture_completed_at','control_epoch','display_truncated','announce'):
            result.pop(key,None)
    return result


def wire_control_reply(value):
    """Retain control results; long evidence prose stays in board event logs."""
    result=copy.deepcopy(value)
    for key in ('metric','last_metric_report'):
        report=result.get(key)
        if isinstance(report,dict):
            evidence=report.get('calibration_evidence')
            if isinstance(evidence,str) and len(evidence.encode())>48:
                report['calibration_evidence']='sha256:'+hashlib.sha256(evidence.encode()).hexdigest()[:16]
    return result


def wire_command_reply(value):
    """Return admission evidence without duplicating full controller reports."""
    result=copy.deepcopy(value)
    control=result.get('control')
    if isinstance(control,dict):
        result['control']={k:control[k] for k in ('mode','epoch','connected','reason') if k in control}
    metric=result.get('metric')
    if isinstance(metric,dict):
        result['metric']={k:metric[k] for k in ('active','progress','reason','scope','ground_calibration_declared') if k in metric}
    return result


class IntegratedApplication:
    VOICE_OPS = ('voice_status', 'load_voice', 'speak', 'job', 'set_mode', 'resume')

    def __init__(self, arbiter, voice, clock=time.monotonic, *, command_contract='legacy_seconds', voice_motion_enabled=True):
        if command_contract not in ('legacy_seconds', 'confirmed_vehicle_v1'):
            raise ValueError('unknown_command_contract')
        self.command_contract = command_contract
        self.voice_motion_enabled = voice_motion_enabled
        self.arbiter, self.voice, self.clock = arbiter, voice, clock
        self.stopped = True
        self.closed = False
        self.local_voice_active = False
        self.radio_connected = False
        self.photo_service = None
        from .confirmed_commands import ConfirmedCommands
        self.confirmed = ConfirmedCommands(self)

    def connected(self):
        self.radio_connected = True
        self.arbiter.set_connected(True)
        self.stopped = False
        # Controller retains its own stopped epoch: explicit voice resume only.

    def require_photo_idle(self):
        if self.photo_service is not None and self.photo_service.busy:
            raise RuntimeError('视觉任务期间请保持小车静止')

    def dispatch(self, op, args):
        if self.photo_service is not None and self.photo_service.busy:
            if op in ('vehicle.arm','vehicle.drive','autonomy.start','command.model_text'):
                raise RuntimeError('视觉任务期间请保持小车静止')
            if op=='command.text':
                from .vehicle_nlu import command_body
                if parse(command_body(args.get('text','')))['kind']!='stop':
                    raise RuntimeError('视觉任务期间请保持小车静止')
        if self.photo_service is not None and self.photo_service.busy and op in ('resume','load_voice','set_mode','speak'):
            raise RuntimeError('视觉任务进行中，完成后可返回语音；模型保持驻留')
        if (op in self.VOICE_OPS or op=='stop_all') and self.voice is not None:
            with getattr(self.voice, 'lock', nullcontext()):
                if op not in ('voice_status','job','stop_all'):self.require_photo_idle()
                if self.local_voice_active and op not in ('voice_status', 'job', 'stop_all'):
                    raise RuntimeError('本地语音正在使用模型，请先停止本地语音再使用蓝牙文字模式')
                return wire_voice_reply(self._dispatch(op, args))
        result=self._dispatch(op, args)
        return wire_command_reply(result) if op in ('command.text','command.confirmed') else result

    def local_voice_dispatch(self, op, args):
        if self.closed or self.voice is None:
            raise RuntimeError('local_voice_service_unavailable')
        if op not in ('voice_status', 'job', 'resume', 'load_voice', 'set_mode', 'stop_all'):
            raise ValueError('voice_only_operation_required')
        if self.photo_service is not None and self.photo_service.busy and op not in ('voice_status','job','stop_all'):
            raise RuntimeError('视觉任务进行中，完成后可返回语音；模型保持驻留')
        with self.voice.lock:
            if op not in ('voice_status','job','stop_all'):self.require_photo_idle()
            if op in ('voice_status', 'job'):
                return self.voice.dispatch(op, args)
            if op == 'stop_all':
                if self.local_voice_active:
                    self.confirmed.clear()
                    self.local_voice_active = False
                    if self.voice_motion_enabled and not self.radio_connected:
                        self.arbiter.set_connected(False)
                        self.stopped=True
                    else:self.arbiter.stop('local_voice_stop')
                    self.voice.cancel('local_voice_stop')
                return self.voice.status()
            if op == 'resume':
                state = self.voice.status()
                if not self.local_voice_active and not state['stopped']:
                    raise RuntimeError('蓝牙文字模式正在使用语音，请先停止该模式')
                result = self.voice.dispatch(op, args)
                self.local_voice_active = True
                if self.voice_motion_enabled:
                    # Establish an explicit local interaction, not CAN authority.
                    # The actor still checks its independent grant and feedback.
                    self.arbiter.set_connected(True)
                    self.stopped=False
                return result
            if not self.local_voice_active:
                raise RuntimeError('local_voice_resume_required')
            result = self.voice.dispatch(op, args)
            if op == 'set_mode':
                self.confirmed.activate()
            return result

    def _dispatch(self, op, args):
        if self.closed:
            raise RuntimeError('application_closed')
        if not isinstance(args, dict):
            raise ValueError('object_arguments_required')
        if op == 'stop_all':
            self.cancel('stop_all')
            voice = self.voice.dispatch('voice_status', {}) if self.voice is not None else {'stopped': True}
            return dict(voice, cancel_requested=True, connection_kept=True, explicit_resume_required=True)
        if op == 'status':
            return dict(protocol='FM-INTEGRATED/1', stopped=self.stopped,
                        control=self.arbiter.status(), voice_available=self.voice is not None,
                        hardware_acceptance=False, command_contract=self.command_contract)
        if op == 'vehicle.status':
            return self.arbiter.vehicle_status()
        if op == 'vehicle.stop':
            self.arbiter.stop()
            return self.arbiter.vehicle_status()
        if op == 'autonomy.status':
            return wire_control_reply(self.arbiter.status())
        if op == 'motion.capabilities':
            cfg = self.arbiter.metric.config if self.arbiter.metric else None
            return dict(available=cfg is not None, contract=self.command_contract,
                        scope=cfg.scope if cfg else None,
                        distance_m=dict(min_exclusive=cfg.distance_tolerance_m, max=cfg.max_distance_m) if cfg else None,
                        angle_deg=dict(min_exclusive=cfg.angle_tolerance_deg, max=cfg.max_angle_deg) if cfg else None,
                        timeout_s=cfg.timeout_s if cfg else None,
                        directions=['前进','后退','左转','右转'], autonomous_motion=False)
        if op in self.VOICE_OPS:
            if self.voice is None:
                raise RuntimeError('voice_backend_unavailable')
            if self.stopped and op not in ('voice_status', 'job', 'resume'):
                raise RuntimeError('explicit_resume_required')
            with self.arbiter.lock:
                admitted_epoch = self.arbiter.epoch
            result = self.voice.dispatch(op, args)
            if op == 'set_mode':
                # Explicit text interaction, like an opened voice interaction,
                # accepts complete commands without an acoustic wake prefix.
                self.confirmed.activate()
            elif op == 'resume':
                self.confirmed.clear()
            if op == 'resume':
                with self.arbiter.lock:
                    if admitted_epoch != self.arbiter.epoch:
                        raise RuntimeError('voice_resume_cancelled_by_control_change')
                    self.stopped = False
            return result
        if op == 'interaction.resume':
            with self.arbiter.lock:
                if args != {'epoch': self.arbiter.epoch}:
                    raise ValueError('stale_control_epoch')
                # Does not arm, load models or open the microphone.
                self.stopped = False
                return self.arbiter.status()
        if op == 'vehicle.arm':
            with self.arbiter.lock:
                self.require_photo_idle()
                if self.stopped:raise RuntimeError('explicit_resume_required')
                if args:raise ValueError('arm_takes_no_arguments')
                return self.arbiter.manual_arm(self.arbiter.epoch)
        if op == 'vehicle.drive':
            with self.arbiter.lock:
                self.require_photo_idle()
                if self.stopped:raise RuntimeError('explicit_resume_required')
                return self.arbiter.manual_drive(args)
        if op == 'autonomy.start':
            if set(args) != {'epoch', 'seconds'}:
                raise ValueError('auto_context_required')
            with self.arbiter.lock:
                self.require_photo_idle()
                if self.stopped:raise RuntimeError('explicit_resume_required')
                return self.arbiter.start_auto(args['epoch'], args['seconds'])
        if op == 'command.text':
            if set(args) != {'epoch', 'text'}:
                raise ValueError('command_context_required')
            return self.command(args['text'], args['epoch'])
        if op == 'command.model_text':
            if set(args)!={'epoch','voice_epoch','text'}:raise ValueError('model_text_context_required')
            from .candidate_voice import validate_text_input
            from .vehicle_nlu import command_body
            from .confirmed_semantics import lexical
            validate_text_input(args['text'])
            if lexical(command_body(args['text']))['intent']=='stop':
                self.cancel('explicit_model_text_stop')
                return dict(accepted=True,stop_requested=True,control=self.arbiter.status(),
                            message='已请求停车并取消未完成指令')
            if self.command_contract!='confirmed_vehicle_v1' or self.voice is None:
                raise RuntimeError('candidate_model_text_backend_required')
            if self.local_voice_active:
                raise RuntimeError('local_voice_owns_models')
            with self.arbiter.lock:
                self.arbiter._context(args['epoch'])
                if self.stopped:raise RuntimeError('explicit_resume_required')
                submitted_at=self.clock()
            # No lock is held across voice dispatch. Final actuation rechecks
            # this captured control epoch and admission time after inference.
            return self.voice.dispatch('command_text',dict(epoch=args['voice_epoch'],text=args['text'],
                control_epoch=args['epoch'],submitted_at=submitted_at))
        if op == 'command.confirmed':
            if set(args) != {'epoch', 'text'}:
                raise ValueError('command_context_required')
            return self.confirmed.handle(args['text'], args['epoch'])
        raise ValueError('unknown_operation')

    def command(self, text, epoch):
        if self.command_contract == 'confirmed_vehicle_v1':
            return self.confirmed.handle(text, epoch)
        intent = parse(text)
        kind = intent['kind']
        if kind == 'stop':
            self.cancel('explicit_text_stop')
            return dict(accepted=True, control=self.arbiter.status())
        # Parse before side effects; unrecognized text never changes the mode.
        if kind == 'unrecognized':
            return dict(accepted=False, **intent)
        if kind == 'manual':
            self.arbiter.stop('manual_takeover')
            return dict(accepted=True, control=self.arbiter.status())
        with self.arbiter.lock:
            if self.stopped:raise RuntimeError('explicit_resume_required')
            self.require_photo_idle()
            self.arbiter._context(epoch)
            if kind == 'auto':
                return self.arbiter.start_auto(epoch, intent['seconds'])
            return self.arbiter.pulse(epoch, intent['velocity'], intent['seconds'])

    def voice_command(self, text, epoch, completed_capture_at, max_latency_s=.5, nlu=None):
        if self.local_voice_active:
            # Local microphone processing is independent of the radio session.
            # Local interaction owns its own epoch; Bluetooth is not required.
            # CAN authority and fresh feedback remain independent prerequisites.
            from .vehicle_nlu import validate_envelope, command_body
            if self.voice.status()['stopped']:
                return dict(accepted=False, message='本地语音已停止')
            semantic = validate_envelope(text, nlu)
            body = command_body(text)
            if not body:
                if self.voice_motion_enabled and self.arbiter.connected and not self.stopped:
                    return self.confirmed.handle(text, epoch, nlu)
                return dict(accepted=False, reason='wake_only', semantic=semantic,
                            message='我在')
            if semantic['intent'] == 'unknown':
                from .voice_feedback import rejection
                reason = semantic.get('reason', 'unsupported_command')
                return dict(accepted=False, reason=reason, semantic=semantic,
                            message=rejection(reason))
            if semantic.get('needs_clarification'):
                return dict(accepted=False, reason='missing_parameter', semantic=semantic,
                            message='请说完整指令，我还没有执行。')
            if parse(command_body(text))['kind'] == 'stop':
                self.arbiter.stop('local_voice_stop_command')
                return dict(accepted=True, message='已请求停车，请查看停止反馈')
            if not self.voice_motion_enabled:
                return dict(accepted=False, reason='local_voice_manual_only', semantic=semantic,
                            message='已识别，当前请用遥控。')
        # Recording checks grant manual motion separately from model output.
        if not self.voice_motion_enabled:
            from .vehicle_nlu import command_body
            # The controller checks its voice generation after inference. Also
            # reject a result superseded by a manual takeover/control change.
            with self.arbiter.lock:
                if self.stopped:
                    return dict(accepted=False, reason='voice_result_stale_or_stopped')
                self.arbiter._context(epoch)
                if not command_body(text):
                    return dict(accepted=True,kind='wake',message='我在',hardware_executed=False)
                if parse(command_body(text))['kind']=='stop':
                    self.cancel('voice_stop')
                    return dict(accepted=True,message='已请求停车，请查看停止反馈')
                return dict(accepted=False,reason='manual_control_only',message='已识别，当前请用遥控。')
        # Called by the bounded microphone/text hook, never from model tool prose.
        if nlu is not None:
            with self.arbiter.lock:
                if self.stopped:
                    return dict(accepted=False, reason='control_interaction_stopped',
                                message='指令已识别，但控制交互已停止，请重新开始语音。')
                if not self.arbiter.connected:
                    return dict(accepted=False, reason='control_interaction_disconnected',
                                message='指令已识别，但控制交互未连接，请重新开始语音。')
                if epoch != self.arbiter.epoch:
                    return dict(accepted=False, reason='stale_control_epoch',
                                message='识别期间控制状态已改变，未执行旧指令，请重新说。')
                if not 0 <= self.clock() - completed_capture_at <= max_latency_s:
                    return dict(accepted=False, reason='voice_result_timeout',
                                message='本次识别结果超过执行时限，未执行，请重新说。')
                from .vehicle_nlu import validate_envelope
                try:
                    semantic=validate_envelope(text,nlu)
                except ValueError as exc:
                    return dict(accepted=False, reason=str(exc), hardware_executed=False,
                                message='模型理解与指令不一致，未执行。')
                if self.arbiter.metric is not None and semantic.get('intent') in ('move','turn') and not semantic.get('needs_clarification'):
                    feedback=self.arbiter.vehicle_status()
                    if not feedback.get('motion_allowed'):
                        return dict(accepted=False,reason='motion_authorization_unavailable',
                                    message='指令已识别，但运动测试授权未生效或已到期，未执行。')
                    if not feedback.get('feedback_fresh'):
                        return dict(accepted=False,reason='motion_feedback_unavailable',
                                    message='指令已识别，但没有新鲜的底盘反馈，未执行。')
                return self.confirmed.handle(text, epoch, nlu)
        if self.command_contract == 'confirmed_vehicle_v1':
            return dict(accepted=False, reason='vehicle_nlu_envelope_required')
        if parse(text)['kind'] == 'stop':
            self.cancel('voice_stop')
            return dict(accepted=True)
        if self.stopped or not 0 <= self.clock() - completed_capture_at <= max_latency_s:
            return dict(accepted=False, reason='voice_result_stale_or_stopped')
        return self.command(text, epoch)

    def cancel(self, reason):
        independent_disconnect = self.local_voice_active and reason in ('disconnect', 'goodbye', 'link_timeout',
            'heartbeat_timeout', 'module_disconnected', 'graceful_goodbye', 'new_handshake',
            'uart_error', 'unexpected_uart_reset', 'partial_frame_timeout')
        if not independent_disconnect:
            self.confirmed.clear()
        try:
            with self.arbiter.lock:
                transport_reasons = ('disconnect', 'goodbye', 'link_timeout', 'heartbeat_timeout',
                    'module_disconnected', 'graceful_goodbye', 'service_shutdown', 'new_handshake',
                    'uart_error', 'unexpected_uart_reset', 'partial_frame_timeout')
                if reason in transport_reasons:
                    self.radio_connected=False
                    keep_local=self.local_voice_active and self.voice_motion_enabled and reason!='service_shutdown'
                    self.arbiter.set_connected(keep_local)
                    self.stopped=not keep_local
                else:
                    self.stopped=True
                    self.arbiter.stop(reason)
        finally:
            if self.voice is not None:
                independent_transport = reason in ('disconnect', 'goodbye', 'link_timeout',
                    'heartbeat_timeout', 'module_disconnected', 'graceful_goodbye',
                    'new_handshake', 'uart_error', 'unexpected_uart_reset', 'partial_frame_timeout')
                with getattr(self.voice, 'lock', nullcontext()):
                    if not (independent_transport and self.local_voice_active):
                        self.local_voice_active = False
                        self.voice.cancel(reason)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.cancel('application_shutdown')
            self.arbiter.actor.close()
        finally:
            if self.voice is not None:
                self.voice.close()
