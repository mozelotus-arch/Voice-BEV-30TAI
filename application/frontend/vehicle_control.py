"""Browser hold lease and an injectable, non-queuing vehicle gateway.

This module never opens radio/CAN; production uses the sole PC HTTP gateway.
The adapter must fence stale commands with its own gateway session and deadline.
"""
import copy
import threading
import time
import uuid

ACTIONS = ('forward', 'backward', 'rotate_left', 'rotate_right',
           'front_left', 'front_right', 'back_left', 'back_right')
PRESETS = {'bench': {'linear_mm_s': 100, 'angular_mrad_s': 800},
           'ground': {'linear_mm_s': 100, 'angular_mrad_s': 400}}


class NotReadyAdapter:
    def status(self):
        return dict(gateway_available=False, connected=False, transport_validated=False, directions_verified=False,
                    feedback_age_ms=None, can_state='unobserved', vx_mm_s=None, wz_mrad_s=None,
                    battery_mv=None, standstill_observed=False, mode='unavailable',
                    message='车辆网关尚未接入；此页暂不能控制或停止实车。')

    def arm(self, guard):
        raise RuntimeError('车辆控制网关尚未接入。')

    def hold(self, session, seq, action, preset, guard, sent_at_ms=None):
        raise RuntimeError('车辆控制网关尚未接入。')

    def stop(self):
        return dict(acknowledged=False, hardware_executed=False, message='未连接控制网关，未确认实车停止。')

    def connect(self, guard):
        raise RuntimeError('8890 车辆控制网关尚未接入。')

    def disconnect(self):
        return self.stop()


class MockVehicleAdapter:
    """Explicit offline display/interaction fixture; never imported by a radio owner."""
    def __init__(self):
        self.vx = self.wz = 0
        self.armed = False
        self.connected = True
        self.calls = []

    def status(self):
        return dict(gateway_available=True, connected=self.connected, transport_validated=True, directions_verified=True,
                    feedback_age_ms=0 if self.connected else None, can_state='simulated', vx_mm_s=self.vx, wz_mrad_s=self.wz,
                    battery_mv=12400, standstill_observed=self.vx == 0 and self.wz == 0,
                    mode='mock', message='离线演示：没有连接蓝牙、板卡或电机。')

    def arm(self, guard):
        guard(); self.armed = True; self.calls.append(('arm',))
        return dict(session=uuid.uuid4().hex, hardware_executed=False)

    def hold(self, session, seq, action, preset, guard, sent_at_ms=None):
        guard()
        if not self.armed:
            raise RuntimeError('explicit_arm_required')
        preset = PRESETS[preset]
        # These display-only signs are NOT a physical direction calibration.
        x, z = {'forward': (1, 0), 'backward': (-1, 0), 'rotate_left': (0, 1), 'rotate_right': (0, -1),
                'front_left': (1, 1), 'front_right': (1, -1), 'back_left': (-1, -1), 'back_right': (-1, 1)}[action]
        self.vx, self.wz = x * preset['linear_mm_s'], z * preset['angular_mrad_s']
        self.calls.append(('hold', seq, action))
        return dict(accepted=True, hardware_executed=False)

    def stop(self):
        self.armed = False; self.vx = self.wz = 0; self.calls.append(('stop',))
        return dict(acknowledged=True, hardware_executed=False, message='离线演示已停止。')

    def connect(self, guard):
        guard(); self.connected = True

    def disconnect(self):
        result=self.stop(); self.connected=False; return result


class VehicleControl:
    INPUT_TIMEOUT = .35
    SESSION_TIMEOUT = 30

    def __init__(self, adapter=None, clock=time.monotonic):
        self.adapter = adapter or NotReadyAdapter()
        self.clock = clock
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.instance = uuid.uuid4().hex
        self.epoch = 0
        self.revision = 0
        self.armed = False
        self.session = None
        self.gateway_generation = None
        self.client_lease = None
        self.seq = -1
        self.active = None
        self.expires = self.arm_expires = 0
        self.stopped = True
        self.reason = 'initial'
        self.receipt = None
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._watchdog, name='browser-hold-watchdog', daemon=True)
        self.thread.start()

    def _ready(self, state):
        age = state.get('feedback_age_ms')
        if state.get('mode') == 'gateway':
            cache_age = state.get('gateway_status_age_ms')
            return bool(state.get('connected') is True and state.get('transport_validated') is True
                        and state.get('directions_verified') is True and state.get('gateway_ready') is True
                        and state.get('can_state') == 'feedback_fresh' and state.get('feedback_fresh') is True
                        and type(cache_age) in (int, float) and 0 <= cache_age <= 250)
        return bool(state.get('connected') is True and state.get('transport_validated') is True and state.get('directions_verified') is True
                    and state.get('can_state') in ('active', 'ERROR-ACTIVE', 'simulated')
                    and type(age) in (int, float) and 0 <= age <= 200)

    def _status(self):
        try:
            return self.adapter.status()
        except Exception:
            return dict(connected=False, transport_validated=False, directions_verified=False,
                        feedback_age_ms=None, can_state='error', standstill_observed=False,
                        mode='unavailable', message='控制网关状态读取失败，禁止续发动作。')

    def snapshot(self):
        state = self._status()  # Must be a local cache, not a full radio query.
        with self.lock:
            return copy.deepcopy(dict(instance_id=self.instance, epoch=self.epoch, revision=self.revision,
                ready=self._ready(state), armed=self.armed, stopped=self.stopped, action=self.active,
                reason=self.reason, stop_receipt=self.receipt, adapter=state,
                heartbeat_ms=100, input_timeout_ms=int(self.INPUT_TIMEOUT * 1000),
                session_timeout_s=self.SESSION_TIMEOUT, actions=list(ACTIONS), presets=PRESETS))

    def _context(self, data):
        if data.get('instance_id') != self.instance or type(data.get('epoch')) is not int or data['epoch'] != self.epoch:
            raise RuntimeError('车辆页面状态过期；请刷新状态后重新启用。')
        if self.closed.is_set():
            raise RuntimeError('车辆网页控制已关闭。')

    def _guard(self, epoch):
        with self.lock:
            if epoch != self.epoch or self.closed.is_set():
                raise RuntimeError('车辆请求已被停止或失效。')
            if self.armed and (self.clock() >= self.arm_expires or self.clock() >= self.expires):
                raise RuntimeError('车辆输入许可已到期。')

    def arm(self, data):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('已有车辆请求在途；不排队。')
        attempted = False
        try:
            with self.lock:
                self._context(data)
                state = self._status()
                if not self._ready(state) or state.get('standstill_observed') is not True:
                    raise RuntimeError('需要已验收的控制通路和新鲜静止反馈才能启用。')
                if self.armed:
                    raise RuntimeError('已启用；不重复申请运动许可。')
                epoch = self.epoch
            attempted = True
            reply = self.adapter.arm(lambda: self._guard(epoch))
            with self.lock:
                self._guard(epoch)
                self.session = reply['session']; self.seq = -1
                self.gateway_generation = self._status().get('generation')
                self.client_lease = uuid.uuid4().hex
                self.armed = True; self.stopped = False; self.active = None
                self.arm_expires = self.clock() + self.SESSION_TIMEOUT
                self.expires = self.clock() + self.INPUT_TIMEOUT
                self.receipt = None; self.reason = 'armed'; self.revision += 1
            return dict(self.snapshot(), lease=self.client_lease)
        except Exception:
            if attempted or self.armed:
                self.stop('arm_failed')
            raise
        finally:
            self.operation.release()

    def connect(self, data):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('已有车辆请求在途；不排队。')
        try:
            with self.lock:
                self._context(data)
                if self.armed:
                    raise RuntimeError('请先停止车辆，再改变连接。')
                epoch=self.epoch
            self.adapter.connect(lambda:self._guard(epoch))
            self._guard(epoch)
            return self.snapshot()
        finally:
            self.operation.release()

    def disconnect(self):
        self.stop('disconnect')
        self.adapter.disconnect()
        return self.snapshot()

    def hold(self, data):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('已有车辆请求在途；不排队。')
        try:
            with self.lock:
                self._context(data)
                sent_at = data.get('sent_at_ms')
                age = time.time() * 1000 - sent_at if type(sent_at) is int else 1000000
                if (not self.armed or self.clock() >= self.arm_expires or not self._ready(self._status())
                    or data.get('lease') != self.client_lease
                    or data.get('action') not in ACTIONS or data.get('preset') not in PRESETS
                    or type(data.get('seq')) is not int or data['seq'] <= self.seq or not -50 <= age <= 200):
                    raise RuntimeError('方向、状态、序号或许可无效，本次动作已取消。')
                epoch, session = self.epoch, self.session
                self.seq = data['seq']
                self.active = data['action']
                self.expires = self.clock() + self.INPUT_TIMEOUT - max(0, age) / 1000
                self.revision += 1
            reply = self.adapter.hold(session, data['seq'], data['action'], data['preset'], lambda:self._guard(epoch), sent_at)
            with self.lock:
                self._guard(epoch)
                if reply.get('accepted') is not True:
                    raise RuntimeError('控制网关未接受当前动作。')
            return self.snapshot()
        except Exception:
            self.stop('hold_failed')
            raise
        finally:
            self.operation.release()

    def stop(self, reason='operator'):
        with self.lock:
            self.epoch += 1; self.revision += 1
            self.armed = False; self.stopped = True; self.session = None; self.active = None
            self.gateway_generation = None
            self.client_lease = None
            self.reason = reason; self.expires = self.arm_expires = 0
        try:
            receipt = self.adapter.stop()
        except Exception:
            receipt = dict(acknowledged=False, hardware_executed=False, message='停止请求结果未知；未自动恢复。')
        with self.lock:
            self.receipt = receipt; self.revision += 1
        return self.snapshot()

    def _watchdog(self):
        while not self.closed.wait(.02):
            with self.lock:
                now = self.clock()
                expired = self.armed and (now >= self.arm_expires or now >= self.expires)
                armed = self.armed
                generation = self.gateway_generation
            if expired:
                self.stop('input_timeout')
            elif armed:
                state = self._status()
                if not self._ready(state) or state.get('generation') != generation:
                    self.stop('feedback_stale_or_gateway_changed')

    def close(self):
        if self.closed.is_set():
            return
        with self.lock:
            owned = self.armed or self.operation.locked()
            self.closed.set()
        if owned:
            self.stop('server_close')
        self.thread.join(1)
        close = getattr(self.adapter, 'close', None)
        if close:
            close()
