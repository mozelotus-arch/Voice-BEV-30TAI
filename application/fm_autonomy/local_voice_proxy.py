"""PC voice-only LAN client; startup never connects, loads or opens audio."""
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from .dependencies import ROOT
sys.path.insert(0, str(ROOT/'voice_proxy'))
from bridge import Bridge
from radio_client import CommandRejected


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class VoiceHTTPClient:
    def __init__(self, config):
        from urllib.parse import urlsplit
        from ipaddress import ip_address
        url = urlsplit(config['base_url'])
        if (url.scheme != 'http' or not ip_address(url.hostname).is_private or
                url.username or url.password or url.query or url.fragment or url.path not in ('','/')):
            raise ValueError('private_board_voice_address_required')
        self.base = config['base_url'].rstrip('/')
        self.token = Path(config['token_file']).read_text(encoding='utf-8').strip()
        self.instance = None
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
    def read(self, path, body=None):
        headers = {'X-Voice-Token': self.token}
        raw = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            raw = json.dumps(body).encode()
        try:
            with self.opener.open(Request(self.base+path, raw, headers), timeout=4) as response:
                raw = response.read(262145)
            if len(raw)>262144: raise ValueError('voice_reply_too_large')
            return json.loads(raw)
        except HTTPError as exc:
            try: message=json.loads(exc.read(4096)).get('error','voice_request_rejected')
            except (ValueError,AttributeError): message='voice_request_rejected'
            finally: exc.close()
            raise CommandRejected(str(message)) from None
    def connect(self):
        result=self.read('/health')
        if result.get('schema')!='FM-LOCAL-VOICE/1':raise RuntimeError('voice_endpoint_mismatch')
        self.instance=result['instance_id']
    def call(self, op, args=None):
        if op=='voice_status':
            result=self.read('/status')
            if result['instance_id']!=self.instance:raise RuntimeError('board_instance_changed')
            return dict(result['voice'], local_active=result['local_active'])
        return self.read('/command',dict(instance_id=self.instance,op=op,args=args or {}))
    def close(self):
        if self.instance is not None:
            try:self.call('stop_all')
            finally:self.instance=None
        return dict(disconnect_requested=True)


class LocalVoiceBridge(Bridge):
    def status(self):
        result=super().status()
        result.update(transport='板端本地语音 · 网线控制',commands_over_ethernet=True)
        result['capabilities']['board_playback']=True
        result['connection']['automatic_reconnect']=False
        running=result['voice'].get('local_active') and result['voice'].get('mode')=='voice' and not result['voice'].get('stopped')
        result['can_start']=not self.closing and self.active is None and not self.stop_pending and not running
        return result
    def _fault(self, exc):
        super()._fault(exc)
        with self.condition:self.wanted=False
    def request(self, action, data):
        if action!='start':return super().request(action,data)
        with self.condition:
            self._require_context(data)
            if not self.status()['can_start']:raise RuntimeError('本地语音任务正在进行')
            self.epoch+=1;self.error=None;self.wanted=True;self.link='connecting'
            key='local_'+str(self.epoch)
            self.jobs[key]=dict(id=key,op='start',state='running',phase='连接板端',epoch=self.epoch)
            self.active=self.latest=key;self.pending=('start',self.epoch,key)
            self.condition.notify_all()
            return self.status()
    def _submit(self, action, epoch, key):
        if action!='start':return super()._submit(action,epoch,key)
        def alive():
            with self.lock:
                return epoch==self.epoch and not self.closing and not self.stop_pending
        def phase(label):
            with self.lock:self.jobs[key]['phase']=label;self.revision+=1
        try:
            self._connect('connect',epoch)
            if not alive():return
            state=self.client.call('voice_status')
            if not state.get('stopped') and state.get('mode')=='voice' and state.get('ready'):
                # No replay or duplicate microphone opening on repeated click.
                raise CommandRejected('语音已经运行，请先停止后再重新开始')
            phase('恢复本地语音')
            state=self.client.call('resume',dict(epoch=state['epoch']))
            with self.lock:self.stopped=False;self.voice=state;self.last_reply=time.monotonic()
            if not alive():return
            if not state.get('ready'):
                phase('加载 ASR / BERT / TTS，请查看板端实际阶段')
                job=self.client.call('load_voice',dict(epoch=state['epoch'],announce=True))
                until=time.monotonic()+300
                while job.get('state') not in ('completed','failed','cancelled'):
                    if not alive():return
                    if time.monotonic()>=until:raise TimeoutError('语音模型加载超时')
                    time.sleep(.2)
                    job=self.client.call('job',dict(id=job['id']))
                    current=self.client.call('voice_status')
                    with self.lock:
                        self.voice=current;self.jobs[key]['board_receipt']=job;self.last_reply=time.monotonic()
                if job.get('state')!='completed':raise CommandRejected(job.get('error','语音模型未加载完成'))
            if not alive():return
            phase('开启板端麦克风')
            state=self.client.call('voice_status')
            with self.lock:
                if not alive():return
                state=self.client.call('set_mode',dict(epoch=state['epoch'],mode='voice'))
                state['local_active']=True
                self.voice=state;self.last_reply=time.monotonic()
                self.jobs[key].update(state='completed',phase='本地语音已开始')
                self.active=None;self.error=None
        except Exception as exc:
            with self.lock:
                if epoch==self.epoch:
                    self.jobs[key].update(state='failed',error=str(exc)[:180]);self.active=None
            raise


def make_local_voice(config_path,folder):
    config=json.loads(Path(config_path).read_text(encoding='utf-8'))
    return LocalVoiceBridge(lambda:VoiceHTTPClient(config),folder,mode='local',poll_seconds=.5)
