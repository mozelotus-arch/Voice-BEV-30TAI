"""FV-WEB/1 client for the sole local radio owner. No implicit connect or arm."""
import copy
import json
import math
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from provider import NoRedirect

PROTOCOL = 'FV-WEB/1'


class GatewayAdapter:
    def __init__(self, port=8890, poll_seconds=.1):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('网关端口无效。')
        self.base = 'http://127.0.0.1:' + str(port)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.lock = threading.RLock()
        self.data = None
        self.received = self.latency_ms = self.version_at = 0
        self.token = self.token_instance = self.protocol = self.bound = None
        self.cancel_epoch = 0
        self.closed = threading.Event()
        self.poll_seconds = poll_seconds
        self.thread = threading.Thread(target=self._poll, name='local-gateway-cache', daemon=True)
        self.thread.start()

    def _request(self, path, body=None, timeout=.4):
        headers = {}
        if body is not None:
            with self.lock:
                token = self.token
            headers = {'Content-Type': 'application/json', 'X-Control-Token': token or ''}
        req = Request(self.base + path, None if body is None else json.dumps(body).encode(), headers)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('oversize')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('object_required')
            return data
        except HTTPError as exc:
            code = exc.code
            try: reason=str(json.loads(exc.read(4096)).get('error','request_rejected'))[:180]
            except (ValueError,AttributeError): reason='request_rejected'
            exc.close()
            raise RuntimeError('控制网关拒绝请求（HTTP %d）：%s；没有自动重试。' % (code,reason)) from None
        except Exception:
            raise RuntimeError('控制网关不可达或回复格式不匹配，结果未确认。') from None

    @staticmethod
    def _valid_context(data):
        return (isinstance(data, dict) and isinstance(data.get('instance_id'), str)
                and 0 < len(data['instance_id']) <= 256 and type(data.get('epoch')) is int and data['epoch'] >= 0)

    def _authenticate(self, expected_instance=None):
        with self.lock:
            if self.token and self.protocol == PROTOCOL and (expected_instance is None or self.token_instance == expected_instance):
                return
        meta = self._request('/api/vehicle/meta')
        if (meta.get('web_protocol') != PROTOCOL or not isinstance(meta.get('instance_id'), str)
            or not isinstance(meta.get('control_token'), str) or not 16 <= len(meta['control_token']) <= 256
            or (expected_instance is not None and meta['instance_id'] != expected_instance)):
            raise RuntimeError('网关协议或实例尚未匹配 FV-WEB/1。')
        with self.lock:
            self.token = meta['control_token']
            self.token_instance = meta['instance_id']
            self.protocol = meta['web_protocol']

    def _accept(self, data, latency, started):
        if not self._valid_context(data):
            raise RuntimeError('网关缺少实例和代次。')
        with self.lock:
            if started < self.version_at:
                return
            if self.data and self.data['instance_id'] == data['instance_id'] and data['epoch'] < self.data['epoch']:
                return
            self.data = copy.deepcopy(data)
            self.received = time.monotonic()
            self.latency_ms = latency
            self.version_at = started

    def _poll(self):
        while not self.closed.is_set():
            started = time.monotonic()
            try:
                data = self._request('/api/vehicle/status')
                if not self._valid_context(data):
                    raise RuntimeError('status_context_missing')
                self._authenticate(data['instance_id'])
                self._accept(data, (time.monotonic() - started) * 1000, started)
            except Exception:
                with self.lock:
                    if started >= self.version_at:
                        self.data = None
            self.closed.wait(self.poll_seconds)

    def status(self):
        with self.lock:
            data = copy.deepcopy(self.data)
            received, latency = self.received, self.latency_ms
            protocol, instance = self.protocol, self.token_instance
        if data is None:
            return dict(gateway_available=False, connected=False, transport_validated=False, directions_verified=False,
                        feedback_age_ms=None, can_state='unobserved', standstill_observed=False, mode='unavailable',
                        message='8890 网关尚未就绪或协议未匹配；本页未连接蓝牙。')
        matches = protocol == PROTOCOL and instance == data['instance_id']
        cache_age = max(0, (time.monotonic() - received) * 1000)
        fresh = matches and cache_age <= 500 and not self.closed.is_set()
        age = data.get('feedback_age_ms')
        age = age + cache_age + latency if type(age) in (int, float) and math.isfinite(age) and age >= 0 else None
        def number(name):
            value = data.get(name)
            return value if age is not None and type(value) in (int, float) and math.isfinite(value) else None
        return dict(gateway_available=fresh, connected=fresh and data.get('gateway_connected') is True,
                    transport_validated=matches and data.get('transport_validated') is True,
                    directions_verified=matches and data.get('directions_verified') is True,
                    gateway_ready=fresh and data.get('ready') is True,
                    gateway_status_age_ms=cache_age + latency,
                    feedback_fresh=fresh and age is not None and cache_age + latency <= 250 and data.get('can_state') == 'feedback_fresh',
                    feedback_age_ms=age, can_state=data.get('can_state', 'unobserved'),
                    vx_mm_s=number('vx_mm_s'), wz_mrad_s=number('wz_mrad_s'), battery_mv=number('battery_mv'),
                    standstill_observed=fresh and data.get('standstill_observed') is True,
                    mode='gateway', generation=(data['instance_id'], data['epoch']),
                    motion_scope=data.get('mode'),preset=data.get('preset'),limits=data.get('limits'),
                    message=data.get('message') or '由唯一 8890 网关转发，尚未通过实测时按钮保持禁用。')

    def _context(self):
        with self.lock:
            if not self.data or not self.status()['gateway_available']:
                raise RuntimeError('需要状态新鲜的 FV-WEB/1 控制网关。')
            return dict(instance_id=self.data['instance_id'], epoch=self.data['epoch'])

    def _receipt_context(self, reply, expected=None):
        if not self._valid_context(reply) or (expected and reply['instance_id'] != expected['instance_id']):
            raise RuntimeError('网关回执的实例或代次无效。')
        with self.lock:
            if self.data and reply['instance_id'] == self.data['instance_id']:
                if reply['epoch'] < self.data['epoch']:
                    raise RuntimeError('网关回执已过期。')
                self.data['epoch'] = reply['epoch']
                self.version_at = time.monotonic()
                # Command acknowledgment must not renew telemetry freshness.

    def connect(self, guard):
        ctx = self._context()
        self._authenticate(ctx['instance_id'])
        guard()
        reply = self._request('/api/vehicle/connect', ctx, timeout=36)
        guard()
        if reply.get('ok') is not True:
            raise RuntimeError('网关未确认连接请求。')
        self._receipt_context(reply, ctx)

    def disconnect(self):
        self._authenticate()
        with self.lock:
            self.cancel_epoch += 1
            self.bound = None
        reply = self._request('/api/vehicle/disconnect', {}, timeout=7)
        if reply.get('ok') is not True:
            raise RuntimeError('网关未确认断开请求。')
        if self._valid_context(reply):
            self._receipt_context(reply)
        else:
            with self.lock:
                self.data = None
        return reply

    def arm(self, guard):
        ctx = self._context()
        self._authenticate(ctx['instance_id'])
        with self.lock:
            fence = self.cancel_epoch
        guard()
        started=time.monotonic()
        reply = self._request('/api/vehicle/arm', ctx, timeout=1.2)
        guard()
        if (reply.get('ok') is not True or not isinstance(reply.get('session'), str)
            or not 1 <= len(reply['session']) <= 256):
            raise RuntimeError('网关未提供有效控制会话。')
        self._receipt_context(reply, ctx)
        if reply.get('telemetry_in_arm_reply') is True:
            # Read the local gateway cache after its real arm sample arrived.
            # Do not count the radio wait as loopback cache transport latency.
            observed=time.monotonic()
            status=self._request('/api/vehicle/status')
            self._accept(status,(time.monotonic()-observed)*1000,observed)
        with self.lock:
            if fence != self.cancel_epoch:
                raise RuntimeError('控制申请已取消。')
            self.bound = dict(instance_id=reply['instance_id'], epoch=reply['epoch'], session=reply['session'])
        return dict(session=reply['session'])

    def hold(self, session, seq, action, preset, guard, sent_at_ms=None):
        with self.lock:
            bound = copy.deepcopy(self.bound)
            fence = self.cancel_epoch
        if not bound or bound['session'] != session:
            raise RuntimeError('没有与当前按住操作匹配的网关会话。')
        ctx = self._context()
        if any(bound[k] != ctx[k] for k in ('instance_id', 'epoch')):
            raise RuntimeError('网关实例或代次已改变，旧动作不会重发。')
        guard()
        reply = self._request('/api/vehicle/hold', dict(bound, seq=seq, action=action, preset=preset,
                                                       sent_at_ms=sent_at_ms), timeout=.18)
        guard()
        with self.lock:
            if fence != self.cancel_epoch:
                raise RuntimeError('控制输入已取消。')
        if reply.get('accepted') is not True:
            raise RuntimeError('网关未接受本次按住信号。')
        return dict(accepted=True, hardware_executed=False)

    def stop(self):
        with self.lock:
            self.cancel_epoch += 1
            self.bound = None
        try:
            # Stop is attempted even with stale telemetry, without an old epoch.
            self._authenticate()
            reply = self._request('/api/vehicle/stop', {}, timeout=.4)
            if reply.get('ok') is not True or type(reply.get('acknowledged')) is not bool:
                raise RuntimeError('停止回执格式无效。')
            self._receipt_context(reply)
            return dict(requested=True, acknowledged=reply['acknowledged'], hardware_executed=False,
                        message='已收到底盘停止回执；请查看新鲜的静止反馈。' if reply['acknowledged'] else '已请求停车，等待底盘回执与静止反馈。')
        except Exception:
            return dict(acknowledged=False, hardware_executed=False,
                        message='停止请求回执未确认；本页已停止续发，不会自动恢复。')

    def close(self):
        self.closed.set()
        self.thread.join(1)
