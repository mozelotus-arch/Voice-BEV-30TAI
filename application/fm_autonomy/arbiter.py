"""The only caller of the independent CAN actor. No inference runs under this lock."""
import math
import threading
import time
from .contracts import Decision, finite


class Arbiter:
    def __init__(self, actor, planner, perception, boot_id, *, allow_synthetic=False,
                 autonomy_enabled=False, clock=time.monotonic, metric_calibration=None,
                 confirmed_auto_seconds=None, event=None):
        self.actor, self.planner, self.perception = actor, planner, perception
        self.boot_id, self.clock = boot_id, clock
        self.allow_synthetic, self.autonomy_enabled = allow_synthetic, autonomy_enabled
        self.lock = threading.RLock()
        self.motion_inhibited = lambda: False
        self.mode, self.epoch, self.reason = 'stopped', 0, 'startup'
        self.connected = False
        self.ticket = None
        self.motion_id = None
        self.auto_until = 0
        self.pulse_until = 0
        self.pulse_velocity = None
        self.last_decision = Decision(0, 0, 'stopped', 'startup')
        self.fault = None
        self.event = event or (lambda name,value:None)
        if confirmed_auto_seconds is not None and not 0 < finite(confirmed_auto_seconds,'confirmed_auto_seconds') <= 30:
            raise ValueError('bounded_confirmed_auto_session_required')
        self.confirmed_auto_seconds=confirmed_auto_seconds
        self.metric = None
        self.last_metric_report = None
        if metric_calibration is not None:
            from .metric_motion import MetricMotion, MetricCalibration
            self.metric = MetricMotion(MetricCalibration(**metric_calibration))

    def _stop_locked(self, reason):
        # Invalidate first: even an IPC error cannot leave a reusable command.
        self.epoch += 1
        self.mode, self.reason = 'stopped', reason
        self.ticket = self.motion_id = None
        self.auto_until = 0
        self.pulse_until = 0
        self.pulse_velocity = None
        terminal=None
        if self.metric is not None:
            if self.metric.last is not None:
                terminal=dict(self.metric.status(),stop_reason=reason)
                self.last_metric_report=terminal
            self.metric.reset()
        self.planner.reset()
        self.last_decision = Decision(0, 0, 'stopped', reason)
        self.actor.cancel(reason)
        if terminal is not None:
            # Log only after requesting zero. Repeated stop must not erase the
            # completed target report with an idle snapshot.
            self.event('metric_terminal',terminal)

    def stop(self, reason='operator_stop'):
        with self.lock:
            self._stop_locked(reason)
            return self.status()

    def set_connected(self, connected):
        with self.lock:
            self._stop_locked('reconnected_no_resume' if connected else 'link_disconnected')
            self.connected = connected

    def begin_stream(self, stream_id):
        with self.lock:
            self._stop_locked('perception_stream_changed')
            self.perception.begin_stream(stream_id)

    def _context(self, epoch):
        if type(epoch) is not int or epoch != self.epoch:
            raise ValueError('stale_control_epoch')
        if not self.connected:
            raise RuntimeError('explicit_connection_required')
        if self.fault:
            raise RuntimeError('control_fault_requires_restart')

    def _require_motion_admission(self):
        # Pure flag read while holding arbiter.lock; never acquire photo/voice
        # locks here. Stops and status do not pass through this gate.
        if self.motion_inhibited():raise RuntimeError('photo_task_requires_stationary')

    def manual_arm(self, epoch):
        with self.lock:
            self._require_motion_admission()
            self._context(epoch)
            # A manual arm always takes ownership away from autonomous work.
            self._stop_locked('manual_takeover')
            self.mode = 'manual'
            try:
                return self.actor.dispatch('vehicle.arm', {})
            except Exception:
                self._stop_locked('manual_arm_rejected')
                raise

    def manual_drive(self, args):
        with self.lock:
            self._require_motion_admission()
            if not self.connected or self.mode != 'manual':
                raise RuntimeError('manual_control_not_armed')
            try:
                return self.actor.dispatch('vehicle.drive', args)
            except Exception:
                self._stop_locked('manual_command_rejected')
                raise

    def start_auto(self, epoch, seconds=30):
        with self.lock:
            self._require_motion_admission()
            self._context(epoch)
            if not self.autonomy_enabled:
                raise RuntimeError('autonomy_pending_hardware_acceptance')
            if not 0 < finite(seconds, 'auto_seconds') <= 30:
                raise ValueError('bounded_auto_session_required')
            self._stop_locked('auto_mode_change')
            snapshot = self.perception.get()
            reason = self._perception_rejection(snapshot)
            if reason:
                raise RuntimeError(reason)
            reply = self.actor.dispatch('vehicle.arm', {})
            self.mode, self.reason = 'auto', 'explicit_auto_start'
            self.motion_id, self.ticket = reply['m'], reply['t']
            self.auto_until = self.clock() + seconds
            return self.status()

    def _perception_rejection(self, snapshot):
        if snapshot is None:
            return 'perception_missing'
        return snapshot.rejection(self.clock(), self.boot_id, self.planner.chassis.max_frame_age_s,
                                  self.allow_synthetic)

    def pulse(self, epoch, velocity, seconds):
        with self.lock:
            self._require_motion_admission()
            self._context(epoch)
            if type(velocity) is not list or len(velocity) != 3 or any(type(v) is not int for v in velocity):
                raise ValueError('integer_velocity_required')
            if velocity[1] or abs(velocity[0]) > 100 or abs(velocity[2]) > 800:
                raise ValueError('command_speed_limit')
            if not .1 <= finite(seconds, 'pulse_seconds') <= 1:
                raise ValueError('command_duration_limit')
            self._stop_locked('explicit_bounded_command')
            reply = self.actor.dispatch('vehicle.arm', {})
            self.motion_id, self.ticket = reply['m'], reply['t']
            self.pulse_until = self.clock() + seconds
            self.pulse_velocity = velocity
            self.mode = 'command'
            return self.status()

    def tick(self):
        with self.lock:
            if self.mode == 'metric':
                try:
                    if not self.connected:raise RuntimeError('metric_disconnected')
                    feedback=self.actor.dispatch('vehicle.status',{})
                    velocity,reason=self.metric.step(feedback,self.clock())
                    if reason:
                        self._stop_locked(reason)
                        return self.last_decision
                    reply=self.actor.dispatch('metric.drive',dict(m=self.motion_id,t=self.ticket,v=velocity,
                        valid_until=min(self.metric.started+self.metric.config.timeout_s,
                            feedback['feedback_received_at']+self.metric.config.feedback_timeout_s)))
                    self.ticket=reply['t']
                    self.last_decision=Decision(velocity[0],velocity[2],'metric','feedback_tracking')
                except Exception as exc:
                    self._stop_locked('metric_motion_rejected:'+str(exc)[:100])
                return self.last_decision
            if self.mode == 'command':
                if not self.connected or self.clock() >= self.pulse_until:
                    self._stop_locked('bounded_command_completed')
                    return self.last_decision
                try:
                    reply = self.actor.dispatch('command.drive', dict(m=self.motion_id, t=self.ticket,
                        v=self.pulse_velocity, valid_until=min(self.pulse_until, self.clock() + .35)))
                    self.ticket = reply['t']
                    self.last_decision = Decision(self.pulse_velocity[0], self.pulse_velocity[2],
                                                  'command', 'explicit_bounded_command')
                except Exception as exc:
                    self._stop_locked('bounded_command_rejected:' + str(exc)[:100])
                return self.last_decision
            if self.mode != 'auto':
                return self.last_decision
            if not self.connected or self.clock() >= self.auto_until:
                self._stop_locked('auto_session_expired_or_disconnected')
                return self.last_decision
            snapshot = self.perception.get()
            reason = self._perception_rejection(snapshot)
            if reason:
                self._stop_locked(reason)
                return self.last_decision
            try:
                decision = self.planner.step(snapshot, self.clock())
                if decision.state == 'wait':
                    self._stop_locked(decision.reason)
                    return self.last_decision
                valid_until = min(snapshot.captured_at + self.planner.chassis.max_frame_age_s,
                                  self.auto_until)
                reply = self.actor.dispatch('autonomy.drive', dict(m=self.motion_id, t=self.ticket,
                    v=decision.velocity, valid_until=valid_until))
                self.ticket = reply['t']
                self.last_decision = decision
                self.reason = decision.reason
            except Exception as exc:
                self._stop_locked('auto_control_rejected:' + str(exc)[:100])
            return self.last_decision

    def vehicle_status(self):
        with self.lock:
            return self.actor.dispatch('vehicle.status', {})

    def start_metric(self,epoch,semantic):
        with self.lock:
            self._require_motion_admission()
            self._context(epoch)
            if self.metric is None:raise RuntimeError('metric_calibration_and_feedback_required')
            self._stop_locked('metric_mode_change')
            try:
                feedback=self.actor.dispatch('vehicle.status',{})
                self.metric.start(semantic,feedback,self.clock())
                reply=self.actor.dispatch('vehicle.arm',{})
                self.motion_id,self.ticket=reply['m'],reply['t']
                self.mode,self.reason='metric','explicit_feedback_target'
                return dict(accepted=True,control=self.status(),metric=self.metric.status(),
                            message='已启动反馈闭环验证；完成情况以控制回执为准',hardware_executed=False)
            except Exception:
                self._stop_locked('metric_start_rejected')
                raise

    def status(self):
        # Status never calls NPU or waits for voice/model locks.
        with self.lock:
            return dict(mode=self.mode, epoch=self.epoch, connected=self.connected,
                        reason=self.reason, autonomy_enabled=self.autonomy_enabled,
                        target=self.last_decision.velocity, planner_state=self.last_decision.state,
                        fault=self.fault,metric=self.metric.status() if self.metric else None,
                        last_metric_report=self.last_metric_report)


class ControlLoop:
    def __init__(self, arbiter, period_s=.025):
        self.arbiter, self.period_s = arbiter, period_s
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name='autonomy-control-only', daemon=True)

    def _run(self):
        try:
            while not self.done.wait(self.period_s):
                self.arbiter.tick()
        except BaseException as exc:
            self.arbiter.fault = str(exc)
            self.arbiter.stop('control_loop_failure')

    def start(self):
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread.is_alive():
            self.thread.join(1)
        self.arbiter.stop('control_loop_closed')
