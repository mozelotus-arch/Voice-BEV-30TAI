"""Bounded OpenAI-compatible Chat Completions transport, without automatic retries."""
import json
import copy
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, getproxies
from urllib.parse import urlsplit


class Cancelled(Exception):
    pass


class ProviderError(RuntimeError):
    pass


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('重复 JSON 字段。')
            result[key] = value
        return result
    def invalid(unused):
        raise ValueError('非有限 JSON 数字。')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *unused, **kwargs):
        return None


def local_https_proxy(base_url,proxies=None):
    """Use the user's credential-free loopback HTTPS tunnel when configured.

    Never proxy local board services, inherit remote proxy credentials, or
    disable TLS verification. CONNECT retains HTTPS to the selected API.
    """
    target=urlsplit(base_url)
    if target.scheme!='https' or target.hostname in ('localhost','127.0.0.1','::1'):return {}
    value=(getproxies() if proxies is None else proxies).get('https','')
    try:
        proxy=urlsplit(value)
        if (proxy.scheme=='http' and proxy.hostname in ('localhost','127.0.0.1','::1') and
            proxy.port and not proxy.username and not proxy.password and
            proxy.path in ('','/') and not proxy.query and not proxy.fragment):
            return {'https':value}
    except ValueError:pass
    return {}


class Provider:
    def __init__(self, timeout=40):
        self.timeout = timeout

    def complete(self, config, messages, tools, guard):
        guard()
        if config['mode'] == 'demo':
            last = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
            return dict(content='【离线演示，没有调用 LLM】\n已收到：' + last[:300] +
                        '\n\n接入模型后，这里会生成带上下文的回答。当前动作清单为空，没有向板卡发送指令。', tool_calls=[])
        payload = dict(model=config['model'], messages=messages, stream=False)
        if urlsplit(config['base_url']).hostname == 'api.deepseek.com':
            # Official non-thinking tool-call flow; this client does not retain
            # reasoning_content. Strict schemas remain checked locally.
            payload.update(thinking={'type':'disabled'}, max_tokens=2048)
            tools=copy.deepcopy(tools)
            if not urlsplit(config['base_url']).path.rstrip('/').endswith('/beta'):
                for tool in tools:tool.get('function',{}).pop('strict',None)
        if tools:
            payload.update(tools=tools, tool_choice='auto', parallel_tool_calls=False)
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if config['api_key']:
            headers['Authorization'] = 'Bearer ' + config['api_key']
        request = Request(config['base_url'] + '/chat/completions',
                          json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), headers)
        deadline = time.monotonic() + self.timeout
        try:
            with build_opener(ProxyHandler(local_https_proxy(config['base_url'])), NoRedirect()).open(request, timeout=self.timeout) as response:
                parts = []
                size = 0
                while True:
                    guard()
                    if time.monotonic() > deadline:
                        raise ProviderError('等待模型回复超时，没有自动重试。')
                    part = response.read1(min(16384, 1_048_577 - size))
                    if not part:
                        break
                    parts.append(part); size += len(part)
                    if size > 1_048_576:
                        raise ProviderError('模型回复超过大小限制。')
                raw = b''.join(parts)
            guard()
            if len(raw) > 1_048_576:
                raise ProviderError('模型回复超过大小限制。')
            body = json_object(raw)
            choices = body['choices']
            if not isinstance(choices, list) or not choices:
                raise ValueError('empty choices')
            choice = choices[0]
            if choice.get('finish_reason') in ('length', 'content_filter'):
                raise ProviderError('模型回复被截断或过滤；未执行其中的工具调用，请缩短问题后重试。')
            reply = choice['message']
            text = reply.get('content') or reply.get('refusal') or ''
            calls = reply.get('tool_calls') or []
            if not isinstance(text, str) or len(text) > 16000 or not isinstance(calls, list) or len(calls) > 4:
                raise ValueError('unsupported reply')
            if not text.strip() and not calls:
                raise ValueError('empty reply')
            return dict(content=text, tool_calls=calls)
        except HTTPError as exc:
            # Do not echo provider bodies, URLs, headers or credentials to the UI/log.
            messages_by_code = {401: '模型服务拒绝密钥，请检查本机配置。', 403: '模型服务拒绝访问。',
                                404: '服务路径或模型不存在，请检查配置。', 429: '模型服务限流或额度不足。'}
            code = exc.code
            exc.close()
            raise ProviderError(messages_by_code.get(code, '模型服务返回 HTTP %s。未自动重试。' % code)) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            guard()
            raise ProviderError('模型服务连接失败或超时。没有自动重试，请检查地址和网络。') from None
        except (ValueError, KeyError, TypeError, IndexError, RecursionError):
            raise ProviderError('模型返回的内容不符合 Chat Completions 格式。') from None
