"""Single radio owner, bounded requests, explicit reconnect and stop latch."""
from collections import OrderedDict, deque
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'board'))
from audit import Audit
from radio_client import CommandRejected

TERMINAL = {'completed', 'failed', 'cancelled', 'unknown'}


class Bridge:
    def __init__(self, client_factory, folder, mode='bluetooth', poll_seconds=.8):
        self.folder = Path(folder); self.folder.mkdir(parents=True, exist_ok=True)
        self.factory = client_factory; self.mode = mode; self.poll_seconds = poll_seconds
        self.lock = threading.RLock(); self.condition = threading.Condition(self.lock)
        self.instance = uuid.uuid4().hex; self.epoch = 0; self.revision = 0
        self.link = 'disconnected'; self.stopped = True; self.stop_pending = False
        self.wanted = False; self.retry_at = 0; self.retry_delay = 2
        self.stop_disconnect = False; self.mode_target = None; self.voice_job = None
        self._terminal_receipt = None
        self.stop_acknowledged = False; self.error = None; self.last_reply = None
        self.voice = {}; self.jobs = OrderedDict(); self.active = None; self.latest = None
        self.pending = None; self.client = None; self.closing = False
        self.events = deque(maxlen=60); self.event_id = 0
        self.log = Audit(self.folder / 'bridge_events.jsonl')
        self.thread = threading.Thread(target=self._run, name='sole-bluetooth-owner', daemon=False)
        self.thread.start()

    def _event(self, event, **value):
        with self.lock:
            self.revision += 1; self.event_id += 1
            row = dict(id=self.event_id, time=time.time(), event=event, **value)
            self.events.append(row)
            self.log.event(event, row)

    def status(self):
        with self.lock:
            age = time.monotonic() - self.last_reply if self.last_reply is not None else None
            fresh = self.link == 'connected' and age is not None and age < 6
            ready = fresh and bool(self.voice.get('ready'))
            busy = self.active is not None or self.voice.get('active') is not None or bool(self.voice.get('pending'))
            return copy.deepcopy(dict(
                success=True, instance_id=self.instance, revision=self.revision,
                mode=self.mode, transport='offline mock' if self.mode == 'mock' else 'Bluetooth RFCOMM SPP',
                device=dict(name='WHEELTEC', mac='12:34:59:65:88:72'),
                connection=dict(state=self.link, error=self.error, last_reply_age_seconds=age,
                                persistent=self.wanted, automatic_reconnect=self.wanted),
                interaction=dict(epoch=self.epoch, stopped=self.stopped,
                                 stop_pending=self.stop_pending, cancel_acknowledged=self.stop_acknowledged,
                                 draining=busy if fresh else None),
                voice=dict(self.voice, observed=fresh, ready=ready),
                voice_job=self.voice_job,
                can_mode=fresh and ready and not self.stopped and not self.stop_pending and self.pending is None,
                can_speak=ready and not self.stopped and not busy and self.pending is None,
                can_load=fresh and not self.stopped and not ready and not busy and self.pending is None,
                active_request=self.active, job=self.jobs.get(self.active or self.latest),
                jobs=list(self.jobs.values())[-8:], events=list(self.events)[-20:],
                capabilities=dict(text_to_speech=True, board_playback=self.mode == 'bluetooth',
                                  microphone=True, voiceprint=False, vehicle_control=False, fastbev=False),
                commands_over_ethernet=False))

    def _require_context(self, data):
        if data.get('instance_id') != self.instance or type(data.get('interaction_epoch')) is not int or data['interaction_epoch'] != self.epoch:
            raise RuntimeError('页面状态已过期，请刷新状态后重新操作。旧请求不会自动重发。')

    def request(self, action, data):
        with self.condition:
            if self.closing:
                raise RuntimeError('本机桥接正在关闭。')
            if action in ('stop', 'disconnect'):
                return self._stop(action)
            self._require_context(data)
            if action in ('connect', 'resume'):
                if self.pending is not None or self.link in ('connecting', 'disconnecting'):
                    raise RuntimeError('连接操作进行中，请稍候。')
                if self.link == 'connected' and action == 'connect':
                    return self.status()
                if action == 'resume' and not self.stopped:
                    return self.status()
                self.epoch += 1; self.error = None
                self.wanted = True
                self.pending = (action, self.epoch, None)
                self.link = 'connecting'
                self._event(action + '_requested')
                self.condition.notify_all()
                return self.status()
            if action == 'mode':
                desired = data.get('mode')
                if desired not in ('text', 'voice'): raise ValueError('模式必须为 text 或 voice。')
                if not self.status()['can_mode']: raise RuntimeError('请先加载模型并恢复交互。')
                if desired == 'voice' and (self.active is not None or self.voice.get('active') is not None):
                    raise RuntimeError('当前任务结束后才能开启监听。')
                self.epoch += 1; self.mode_target = desired
                self.pending = ('mode', self.epoch, None)
                self._event('mode_requested', mode=desired)
                self.condition.notify_all()
                return self.status()
            if action not in ('load', 'speak'):
                raise ValueError('此功能尚未接入。')
            request_id = data.get('id')
            if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
                raise ValueError('请求 id 格式无效。')
            text = data.get('text') if action == 'speak' else None
            if action == 'speak' and (not isinstance(text, str) or not text.strip() or
                                     len(text) > 120 or '\0' in text or len(text.encode()) > 600):
                raise ValueError('请输入 1–120 个字符，最多 600 个 UTF-8 字节。')
            digest = hashlib.sha256(json.dumps(dict(action=action, text=text), ensure_ascii=False,
                                              sort_keys=True).encode()).hexdigest()
            if request_id in self.jobs:
                if self.jobs[request_id]['fingerprint'] != digest:
                    raise ValueError('同一请求 id 不能用于不同内容。')
                return self.status()
            if len(self.jobs) >= 256:
                # Evict only settled receipts and fence every older HTTP request.
                if self.active is not None: raise RuntimeError('当前任务尚未完成。')
                self.epoch += 1
                while len(self.jobs) >= 128: self.jobs.popitem(last=False)
            state = self.status()
            if not state['can_' + action]:
                raise RuntimeError('请先连接蓝牙、加载语音模型，并等待当前任务结束；停止后需显式恢复。')
            job = dict(id=request_id, op=action, state='queued', fingerprint=digest,
                       created_at=time.time(), epoch=self.epoch, simulated=self.mode == 'mock')
            if text is not None:
                job.update(text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest())
            self.jobs[request_id] = job; self.active = self.latest = request_id
            self.pending = (action, self.epoch, request_id)
            self._event('request_accepted', request_id=request_id, op=action)
            self.condition.notify_all()
            return self.status()

    def _stop(self, reason):
        self._terminal_receipt = None
        self.epoch += 1; self.stopped = True; self.stop_pending = True
        self.stop_acknowledged = False; self.error = None
        self.stop_disconnect = reason in ('disconnect', 'bridge_shutdown')
        if self.stop_disconnect: self.wanted = False
        self.pending = None
        if self.active is not None:
            job = self.jobs[self.active]
            job['cancel_requested'] = True
            job['state'] = 'cancelling' if job.get('wire_attempted') else 'cancelled'
        if self.stop_disconnect: self.link = 'disconnecting'
        self._event('stop_requested', reason=reason)
        self.condition.notify_all()
        return self.status()

    def _close_radio(self):
        self._terminal_receipt = None
        client, self.client = self.client, None
        if client is None:
            return dict(goodbye_acknowledged=False)
        try:
            return client.close()
        except Exception as exc:
            return dict(goodbye_acknowledged=False, error=str(exc))

    def _finish_stop(self):
        with self.lock:
            epoch, disconnect = self.epoch, self.stop_disconnect
        if self.client is not None:
            try:
                voice = self.client.call('stop_all')
            except CommandRejected:
                # A negative/oversize reply does not authorize replaying a
                # mutating stop forever. Read actual state once instead.
                with self.lock:
                    if epoch == self.epoch:self.stop_pending=False
                voice = self.client.call('voice_status')
                if not voice.get('stopped'):
                    raise CommandRejected('停止回执未确认；已停止重发，请查看板端状态')
        else: voice = None
        if disconnect: self._close_radio()
        with self.lock:
            if epoch != self.epoch: return
            self.stop_acknowledged = bool(voice and voice.get('stopped'))
            self.stop_pending = False
            if disconnect:
                self.link = 'disconnected'; self.voice = {}; self.last_reply = None
            elif voice is not None:
                self.voice = voice; self.last_reply = time.monotonic(); self.link = 'connected'
            if self.active:
                job = self.jobs[self.active]
                job['state'] = 'cancel_requested' if self.stop_acknowledged else 'unknown'
                if not job.get('wire_attempted'):
                    job['state'] = 'cancelled'
                if disconnect or not job.get('board_job_id'): self.active = None
            self._event('interactions_stopped', cancel_acknowledged=self.stop_acknowledged,
                        bluetooth_kept=not disconnect and voice is not None)

    def _fault(self, exc):
        self._close_radio()
        with self.lock:
            self.epoch += 1; self.stopped = True; self.stop_pending = False
            self.pending = None; self.link = 'error'; self.error = str(exc)
            self.voice = {}; self.last_reply = None; self.stop_acknowledged = False
            self.retry_at = time.monotonic() + self.retry_delay
            self.retry_delay = min(30, self.retry_delay * 2)
            if self.active:
                self.jobs[self.active].update(state='unknown', error=str(exc), outcome_unknown=True)
                self.active = None
            self._event('connection_lost', error=str(exc), automatic_replay=False)

    def _connect(self, action, epoch):
        if self.client is None:
            self.client = self.factory()
            self.client.connect()
        with self.lock:
            if epoch != self.epoch:
                return
        voice = self.client.call('voice_status')
        if voice.get('api') != 2:
            raise RuntimeError('板端仍是旧版服务，请部署常驻版；旧版没有模式和停止命令。')
        if action == 'resume':
            voice = self.client.call('resume', {'epoch': voice['epoch']})
        with self.lock:
            if epoch != self.epoch:
                return
            self.voice = voice; self.last_reply = time.monotonic(); self.link = 'connected'
            self._terminal_receipt = None
            self.retry_delay = 2; self.error = None; self.stopped = bool(voice.get('stopped', True))
            # New sid cancels old work, but a native call can still be draining.
            if action == 'resume':
                if voice.get('active') is not None or voice.get('pending'):
                    self.error = '板端正在等待旧计算返回，请稍后再次点击恢复交互。'
                elif voice.get('failed'):
                    self.error = str(voice['failed'])
                else:
                    self.stopped = False; self.stop_acknowledged = False
            self._event('connected', resumed=action == 'resume' and not self.stopped)

    def _submit(self, action, epoch, request_id):
        with self.lock:
            if epoch != self.epoch or self.stopped:
                return
            job = self.jobs[request_id]
            job.update(wire_attempted=True, state='sending')
            args = {'text': job['text']} if action == 'speak' else {}
            if action == 'load': args['announce'] = True
            args['epoch'] = self.voice['epoch']
        reply = self.client.call('speak' if action == 'speak' else 'load_voice', args)
        with self.lock:
            if epoch != self.epoch:
                return
            self.last_reply = time.monotonic()
            if 'id' in reply:
                job.update(board_job_id=reply['id'], state=reply.get('state', 'queued'),
                           deadline=time.monotonic() + (300 if action == 'load' else 180))
            elif action == 'load' and reply.get('ready'):
                job.update(state='completed'); self.active = None; self.voice['ready'] = True
            else:
                raise RuntimeError('板端回复缺少任务编号，结果未知。')
            self._event('board_accepted', request_id=request_id, board_job_id=reply.get('id'))

    def _poll(self):
        with self.lock:
            epoch = self.epoch; key = self.active; client = self.client
            if client is None or self.stop_pending or self.closing: return
            job = self.jobs.get(key)
            board_id = job.get('board_job_id') if job else None
            if job and job.get('deadline', float('inf')) < time.monotonic():
                raise TimeoutError('板端任务超过时限（加载 300 秒、播报 180 秒）；已断连取消，不会重发。')
        active_reply = None
        if board_id is not None:
            reply = client.call('job', {'id': board_id})
            active_reply = reply
            with self.lock:
                if epoch != self.epoch or self.client is not client or self.stop_pending:
                    return
                self.last_reply = time.monotonic()
                if reply.get('id') != board_id or reply.get('op') != ('speak' if job['op'] == 'speak' else 'load_voice'):
                    raise RuntimeError('板端任务回执不匹配。')
                if job['op'] == 'speak' and reply.get('text_sha256') != job['text_sha256']:
                    raise RuntimeError('板端文本摘要不匹配，不能确认播放。')
                if reply.get('state') == 'completed' and job['op'] == 'speak' and not reply.get('playback_completed'):
                    raise RuntimeError('板端没有确认播放完成。')
                previous = job['state']; job['state'] = reply['state']; job['board_receipt'] = reply
                if previous != job['state']:
                    self._event('job_state', request_id=key, state=job['state'])
                if job['state'] in TERMINAL:
                    job['finished_at'] = time.time(); self.active = None
        with self.lock:
            if epoch != self.epoch or self.client is not client or self.stop_pending:
                return
        voice = client.call('voice_status')
        observed = time.monotonic()
        latest = voice.get('latest')
        with self.lock:
            if epoch != self.epoch or self.client is not client or self.stop_pending or self.closing: return
            receipt_key = (client, epoch, voice.get('epoch'), latest)
            cached = self._terminal_receipt
            receipt = copy.deepcopy(cached[1]) if cached and cached[0] == receipt_key else None
        if latest is None:
            receipt = None
        elif active_reply is not None and board_id == latest:
            receipt = active_reply
        elif receipt is None:
            receipt = client.call('job', {'id': latest})
        with self.lock:
            if epoch == self.epoch and self.client is client and not self.stop_pending and not self.closing:
                # Cache immutable results, never a status heartbeat or an unknown outcome.
                self._terminal_receipt = ((receipt_key, copy.deepcopy(receipt))
                    if receipt and receipt.get('state') in ('completed','failed','cancelled') else None)
                self.voice = voice; self.last_reply = observed; self.revision += 1
                self.voice_job = receipt
                if voice.get('stopped'): self.stopped = True
                if voice.get('failed'): self.error = str(voice['failed'])

    def _set_mode(self, epoch):
        with self.lock:
            if epoch != self.epoch or self.stopped: return
            args = dict(mode=self.mode_target, epoch=self.voice['epoch'])
        voice = self.client.call('set_mode', args)
        with self.lock:
            if epoch == self.epoch:
                self.voice = voice; self.last_reply = time.monotonic(); self.error = None
                self._event('mode_changed', mode=voice['mode'])

    def _rejected(self, exc, action):
        with self.lock:
            self.error = str(exc)
            if action in ('load', 'speak') and self.active:
                self.jobs[self.active].update(state='failed', error=str(exc)); self.active = None
            if self.client is not None: self.link = 'connected'
            self._event('request_rejected', action=action, error=str(exc))

    def _run(self):
        next_poll = time.monotonic()
        try:
            while True:
                with self.condition:
                    if self.closing or self.stop_pending:
                        task = ('stop', self.epoch, None)
                    elif self.pending is not None:
                        task, self.pending = self.pending, None
                    elif self.link == 'connected' and time.monotonic() >= next_poll:
                        task = ('poll', self.epoch, None)
                    elif self.wanted and self.link == 'error' and time.monotonic() >= self.retry_at:
                        self.link = 'connecting'
                        task = ('connect', self.epoch, None)
                    else:
                        self.condition.wait(.1); continue
                action, epoch, key = task
                try:
                    if action == 'stop':
                        self._finish_stop()
                        if self.closing:
                            break
                    elif action in ('connect', 'resume'):
                        self._connect(action, epoch)
                    elif action == 'poll':
                        self._poll()
                    elif action == 'mode':
                        self._set_mode(epoch)
                    else:
                        self._submit(action, epoch, key)
                except CommandRejected as exc:
                    self._rejected(exc, action)
                    if self.closing: self._close_radio(); break
                except Exception as exc:
                    self._fault(exc)
                    if self.closing: break
                next_poll = time.monotonic() + self.poll_seconds
        finally:
            self._close_radio()

    def close(self):
        with self.condition:
            if self.closing:
                return
            self._stop('bridge_shutdown'); self.closing = True; self.condition.notify_all()
        self.thread.join()
        self.log.close()
