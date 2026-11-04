"""Fixed loopback bridge client. No Bluetooth, SSH, UART, NPU or CAN imports."""
import hashlib
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
from provider import NoRedirect


class BoardError(RuntimeError):
    pass


def context(state):
    return dict(instance_id=state['instance_id'], interaction_epoch=state['interaction']['epoch'])


def split_speech(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 2400 or '\0' in text:
        raise ValueError('朗读支持 1–2400 个字符；较长回复请让模型缩短后再朗读。')
    rest = text.strip()
    chunks = []
    while rest:
        end = min(len(rest), 120)
        if len(rest) > 120:
            candidates = [i + 1 for i, c in enumerate(rest[:120]) if c in '。！？；\n.!?;' and i >= 35]
            if candidates:
                end = candidates[-1]
        chunk, rest = rest[:end], rest[end:]
        if chunk.strip():
            chunks.append(chunk.strip())
    return chunks


class BoardGateway:
    def __init__(self, port=8880):
        # Port is a local startup option, never a remote/browser-supplied destination.
        self.base = 'http://127.0.0.1:' + str(int(port))
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.cache_lock = threading.Lock()
        self.cached = dict(available=False, message='板端状态尚未读取。')
        self.cache_at = 0

    def _read(self, path, body=None):
        headers = {}
        if body is not None:
            with self.opener.open(self.base + '/', timeout=3) as response:
                html = response.read(200000).decode()
            token = re.search(r'name="control-token" content="([^"]+)"', html)
            if not token:
                raise BoardError('现有桥接服务缺少控制令牌。')
            headers = {'Content-Type': 'application/json', 'X-Control-Token': token.group(1)}
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        with self.opener.open(Request(self.base + path, data, headers), timeout=4) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise BoardError('桥接回复超出大小限制。')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise BoardError('桥接回复格式无效。')
        return result

    def status(self, fresh=False):
        with self.cache_lock:
            if not fresh and time.monotonic() - self.cache_at < .6:
                return self.cached
            try:
                result = self._read('/api/status')
                if result.get('voice', {}).get('api') not in (None, 2) or not result.get('instance_id'):
                    raise BoardError('桥接版本不匹配。')
                self.cached = dict(available=True, state=result)
            except (OSError, URLError, ValueError, BoardError) as exc:
                if isinstance(exc, HTTPError):
                    exc.close()
                self.cached = dict(available=False, message='8880 桥接未运行或不可达。文字聊天仍可使用。')
            self.cache_at = time.monotonic()
            return self.cached

    def command(self, action, expected=None, **data):
        paths = {'connect': '/api/connect', 'resume': '/api/interaction/resume', 'load': '/api/voice/load',
                 'speak': '/api/speak', 'mode': '/api/interaction/mode', 'stop': '/api/interaction/stop',
                 'disconnect': '/api/disconnect'}
        if action not in paths:
            raise ValueError('未接入此板端能力。')
        if action not in ('stop', 'disconnect'):
            if not isinstance(expected, dict) or set(expected) != {'instance_id', 'interaction_epoch'}:
                raise ValueError('缺少页面观察到的板端状态；请刷新后重试。')
        body = dict(expected or {}, **data)
        try:
            result = self._read(paths[action], body)
            self.cache_at = 0
            return result
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (400, 409):
                raise BoardError('板端状态已改变或正忙，本次请求未自动重试。请刷新状态。') from None
            raise BoardError('桥接拒绝请求。') from None
        except (URLError, OSError, ValueError):
            raise BoardError('桥接不可达或回执缺失；本次结果未知，没有自动重发。') from None

    def require_speech(self, expected):
        result = self.status(fresh=True)
        if not result['available']:
            raise BoardError(result['message'])
        state = result['state']
        if context(state) != expected:
            raise BoardError('板端状态已改变，请刷新状态后重新朗读。')
        if not state['can_speak'] or state.get('voice', {}).get('mode') != 'text':
            raise BoardError('朗读前请连接蓝牙、恢复板端、加载模型并切到文字模式。')

    def speak(self, text, expected, request_id, guard, progress):
        chunks = split_speech(text)
        for index, chunk in enumerate(chunks):
            guard()
            self.require_speech(expected)
            guard()
            job_id = request_id + '_' + str(index)
            self.command('speak', expected, id=job_id, text=chunk)
            progress('playing', index, len(chunks))
            deadline = time.monotonic() + 185
            while time.monotonic() < deadline:
                guard()
                result = self.status(fresh=True)
                if not result['available']:
                    raise BoardError('朗读时桥接中断，后续分段已取消；当前结果未知。')
                state = result['state']
                if context(state) != expected or state['interaction']['stopped']:
                    raise BoardError('板端已停止或模式已变化，后续分段已取消。')
                receipt = next((j for j in state['jobs'] if j['id'] == job_id), None)
                if receipt and receipt['state'] == 'completed':
                    if receipt.get('text_sha256') != hashlib.sha256(chunk.encode()).hexdigest():
                        raise BoardError('朗读回执内容不匹配。')
                    # The accepted bridge validates the board's playback-complete receipt.
                    progress('playing', index + 1, len(chunks))
                    break
                if receipt and receipt['state'] in ('failed', 'cancelled', 'unknown', 'cancel_requested'):
                    raise BoardError('板端未完成本段朗读；后续分段已取消。')
                time.sleep(.25)
            else:
                raise BoardError('等待朗读回执超时；没有重发或提交后续分段。')
        guard()
        progress('completed', len(chunks), len(chunks))


class PendingVoiceGateway(BoardGateway):
    """The vehicle UI cannot reopen the old SPP owner while 8890 owns the radio."""
    def status(self, fresh=False):
        return dict(available=False, message='板端语音等待接入唯一控制网关；原语音版本已保留。文字聊天可独立使用。')

    def command(self, action, expected=None, **data):
        raise BoardError('此版本尚未接入统一语音网关，未向板端发送命令。')
