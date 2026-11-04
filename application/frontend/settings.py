"""Non-secret settings on disk; credentials stay in server memory or environment."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def endpoint(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('服务地址无效。')
    parsed = urlsplit(value.strip().rstrip('/'))
    local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if parsed.scheme not in ('https', 'http') or (parsed.scheme == 'http' and not local):
        raise ValueError('远程服务需要 HTTPS；本机服务可用 HTTP。')
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('地址不能包含用户名、密码、查询参数或片段。')
    if any(ord(c) < 33 for c in value):
        raise ValueError('地址不能包含空格或控制字符。')
    try:
        parsed.port
    except ValueError:
        raise ValueError('服务端口无效。') from None
    path = parsed.path.rstrip('/')
    if path.endswith('/chat/completions'):
        path = path[:-len('/chat/completions')]
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', '')), local


class Settings:
    def __init__(self, folder, environ=None):
        self.path = Path(folder) / 'settings.json'
        env = os.environ if environ is None else environ
        stored = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.value = dict(mode='api', base_url='', model='')
        self.value.update({k: stored[k] for k in self.value if k in stored})
        for field, name in [('base_url', 'FM_LLM_BASE_URL'), ('model', 'FM_LLM_MODEL')]:
            if env.get(name):
                self.value[field] = env[name]
        self.key = env.get('FM_LLM_API_KEY', '')
        self.key_source = 'environment' if self.key else 'none'
        self._validated(self.value)

    def _validated(self, value):
        if value['mode'] not in ('api', 'demo'):
            raise ValueError('模式必须是 api 或 demo。')
        base = value['base_url']
        if base:
            value['base_url'], _ = endpoint(base)
        model = value['model']
        if not isinstance(model, str) or len(model) > 160 or any(ord(c) < 32 for c in model):
            raise ValueError('模型名称无效。')
        value['model'] = model.strip()
        return value

    def update(self, data):
        if set(data) - {'mode', 'base_url', 'model', 'api_key', 'clear_key'}:
            raise ValueError('未知配置字段。')
        value = self._validated({k: data.get(k, v) for k, v in self.value.items()})
        key = data.get('api_key', '')
        if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 for c in key):
            raise ValueError('密钥格式无效。')
        if type(data.get('clear_key', False)) is not bool:
            raise ValueError('clear_key 必须是布尔值。')
        # Never silently forward a credential to a newly selected destination.
        clear = data.get('clear_key') or value['base_url'] != self.value['base_url']
        next_key = key or ('' if clear else self.key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
        self.value = value
        self.key_source = 'session' if key else ('none' if clear else self.key_source)
        self.key = next_key
        return self.public()

    def public(self):
        local = bool(self.value['base_url'] and endpoint(self.value['base_url'])[1])
        configured = self.value['mode'] == 'demo' or bool(self.value['base_url'] and self.value['model'] and (self.key or local))
        return dict(self.value, key_present=bool(self.key), key_source=self.key_source,
                    configured=configured, protocol='chat_completions', secrets_saved=False)

    def snapshot(self):
        if not self.public()['configured']:
            raise ValueError('请先设置服务地址、模型和密钥，或选择离线演示。')
        return dict(self.value, api_key=self.key)
