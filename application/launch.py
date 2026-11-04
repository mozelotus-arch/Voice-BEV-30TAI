"""Single PC launch entry. Default is explicit offline mock; never opens a browser."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'pc_gateway'))
sys.path.insert(0, str(ROOT/'frontend'))
from vehicle_gateway import Gateway, Bluetooth, make_server as make_gateway_server
from server import make_server as make_web_server, Handler
from settings import Settings
from chat import Chat
from gateway_adapter import GatewayAdapter
from vehicle_control import VehicleControl
from fm_autonomy.voice_proxy import make_bridge, make_voice_facade
from fm_autonomy.intent import parse


def configure_control_routes(server, gateway, mock):
    class IntegrationHandler(Handler):
        def do_GET(self):
            if self.path == '/autonomy.js':
                if not self.allowed(): return self.reply(dict(error='origin'), 403)
                return self.reply((ROOT/'frontend/web/autonomy.js').read_bytes(), mime='text/javascript; charset=utf-8')
            if self.path == '/api/autonomy/status':
                if not self.allowed(): return self.reply(dict(error='origin'), 403)
                state = gateway.status()
                value = dict(instance_id=gateway.instance_id, epoch=state['epoch'],
                             simulated=mock, connected=state['connected'], mode='manual' if state['armed'] else 'unavailable')
                if state['connected'] and not state['armed']:
                    try:
                        value['control'] = gateway.application('autonomy.status')
                        value['voice'] = gateway.application('voice_status')
                    except Exception as exc: value['error'] = str(exc)[:140]
                return self.reply(value)
            if self.path == '/api/autonomy/motion-observation':
                if not self.allowed():return self.reply(dict(error='origin'),403)
                try:return self.reply(gateway.observe_motion())
                except (ValueError,RuntimeError,TimeoutError) as exc:return self.reply(dict(error=str(exc)),409)
            return super().do_GET()

        def do_POST(self):
            if self.path not in ('/api/autonomy/command','/api/autonomy/model-command','/api/autonomy/model-job'):
                return super().do_POST()
            if not self.allowed() or not secrets.compare_digest(self.headers.get('X-Control-Token',''), self.server.token):
                return self.reply(dict(error='origin_or_token'), 403)
            try:
                if self.headers.get_content_type()!='application/json' or self.headers.get('Transfer-Encoding'):
                    raise ValueError('json_required')
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 4096: raise ValueError('body_size')
                data = json.loads(self.rfile.read(length))
                if self.path == '/api/autonomy/model-job':
                    if not isinstance(data,dict) or set(data)!={'instance_id','epoch','voice_epoch','id'}:
                        raise ValueError('job_context_required')
                    gateway._guard(data)
                    voice=gateway.application('voice_status')
                    if type(data['voice_epoch']) is not int or data['voice_epoch']!=voice['epoch']:
                        raise RuntimeError('文字任务已被取消或交互状态已变化')
                    return self.reply(dict(result=gateway.application('job',dict(id=data['id'])),simulated=mock))
                model_text=self.path=='/api/autonomy/model-command'
                expected={'instance_id','epoch','control_epoch','text'}
                if model_text:expected.add('voice_epoch')
                if not isinstance(data,dict) or set(data)!=expected:
                    raise ValueError('command_context_required')
                kind=parse(data['text'])['kind']
                if kind=='stop':
                    gateway.stop('text_stop')
                    result=gateway.application('stop_all')
                elif kind=='manual':
                    result=gateway.stop('manual_takeover')
                else:
                    gateway._guard(data)
                    if self.server.chat.snapshot()['stopped']:
                        raise RuntimeError('请先恢复交互')
                    args=dict(epoch=data['control_epoch'],text=data['text'])
                    if model_text:args['voice_epoch']=data['voice_epoch']
                    result=gateway.application('command.model_text' if model_text else 'command.text',args)
                return self.reply(dict(result=result,simulated=mock),202)
            except (ValueError,KeyError,TypeError): return self.reply(dict(error='输入或控制状态无效'),400)
            except (RuntimeError,TimeoutError) as exc: return self.reply(dict(error=str(exc)),409)
    server.RequestHandlerClass = IntegrationHandler


def build_services(port=8883, gateway_port=8890, *, mock=True, folder=None, validated=False, settings_folder=None, bev_folder=None, local_voice_config=None,photo_control=False,ground_user=False):
    folder = Path(folder or ROOT/'runs'/str(time.time_ns()))
    folder.mkdir(parents=True, exist_ok=True)
    if mock:
        from fm_autonomy.mock_runtime import MockRadio
        factory=MockRadio
    else:
        factory=Bluetooth
    gateway=Gateway(folder/'gateway', validated=mock or validated, factory=factory,ground_user=ground_user)
    gateway_server=bridge=web=local_voice=photo=None
    try:
        gateway_server=make_gateway_server(gateway,gateway_port)
        bridge=make_bridge(gateway,folder/'voice',mock)
        if local_voice_config is not None:
            from fm_autonomy.local_voice_proxy import make_local_voice
            local_voice=make_local_voice(local_voice_config,folder/'local_voice')
        if photo_control:
            if local_voice_config is None or bev_folder is None:raise ValueError('photo_requires_local_voice_config_and_bev_folder')
            from photo_bridge import PhotoBridge
            photo=PhotoBridge(local_voice_config,bev_folder)
        facade=make_voice_facade(bridge)
        chat=Chat(Settings(settings_folder or folder/'private'),facade)
        web=make_web_server(port,chat=chat,vehicle=VehicleControl(GatewayAdapter(gateway_server.server_port)),
                            bev_folder=bev_folder,local_voice=local_voice,gateway=gateway,photo=photo)
        configure_control_routes(web,gateway,mock)
        return gateway,gateway_server,bridge,web
    except BaseException:
        if web is not None: web.server_close()
        elif local_voice is not None:local_voice.close()
        if web is None and photo is not None:photo.close()
        if bridge is not None: bridge.close()
        if gateway_server is not None: gateway_server.server_close()
        gateway.close()
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--mock',action='store_true',help='offline only; no device opened')
    mode.add_argument('--bluetooth',action='store_true',help='connect only after explicit UI action; new acceptance still required')
    p.add_argument('--port',type=int,default=8883)
    p.add_argument('--gateway-port',type=int,default=8890)
    p.add_argument('--settings-dir',type=Path,default=ROOT/'frontend/private')
    p.add_argument('--restore-conversation',type=Path,help='Restore display-only chat rows during a local service update; never restore hardware requests')
    p.add_argument('--local-voice-config',type=Path,help='Private LAN voice endpoint config; connects only on user Start')
    p.add_argument('--bev-folder',type=Path,help='Display board-returned PNG and latest.json; never runs inference on the PC')
    p.add_argument('--photo-control',action='store_true',help='Enable explicit user photo capture/BEV jobs through the existing private board endpoint')
    p.add_argument('--motion-authorization','--bench-authorization',dest='bench_authorization',type=Path,help='Current boot-bound motion scope; never auto-connect/arm')
    args=p.parse_args()
    mock=not args.bluetooth
    validated=False;ground_user=False
    if args.bench_authorization:
        from fm_autonomy.dependencies import add_vendor
        add_vendor('vehicle')
        from commissioning import load_authorization
        grant=json.loads(args.bench_authorization.read_text(encoding='utf-8'))
        if grant.get('version')==6:raise ValueError('current_launcher_requires_ground_user_v7_or_bench_scope')
        load_authorization(args.bench_authorization,grant['boot_id'])
        validated=True
        ground_user=grant.get('version')==7
    gateway,gs,bridge,web=build_services(args.port,args.gateway_port,mock=mock,
        validated=validated,settings_folder=args.settings_dir,bev_folder=args.bev_folder,local_voice_config=args.local_voice_config,photo_control=args.photo_control,ground_user=ground_user)
    if args.restore_conversation:
        saved=json.loads(args.restore_conversation.read_text(encoding='utf-8'))
        rows=saved.get('rows', [])[-48:]
        if any(not isinstance(row,dict) or row.get('role') not in ('user','assistant') or
               not isinstance(row.get('text'),str) for row in rows):
            raise ValueError('invalid_conversation_snapshot')
        for row in rows:
            if row.get('status')=='pending':row.update(status='cancelled',error='服务更新前未完成，未自动重发。')
            if row.get('speech',{}):row['speech']=dict(state='cancelled')
        web.chat.rows=rows
        for user,reply in zip(rows[::2],rows[1::2]):
            if user['role']=='user' and reply['role']=='assistant' and reply.get('status')=='completed':
                web.chat._remember([dict(role='user',content=user['text']),dict(role='assistant',content=reply['text'])])
    thread=threading.Thread(target=gs.serve_forever,kwargs={'poll_interval':.1},daemon=True)
    thread.start()
    def shutdown(*unused):threading.Thread(target=web.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
    print(json.dumps(dict(url='http://127.0.0.1:%d/'%web.server_port,mode='OFFLINE_MOCK' if mock else 'BLUETOOTH_CANDIDATE',
                         single_radio_owner=True, hardware_accepted=False, browser_opened=False)),flush=True)
    try:web.serve_forever(poll_interval=.2)
    finally:
        # Stop the CAN input before potentially slow voice/native teardown.
        gateway.stop('launcher_shutdown')
        web.server_close();bridge.close();gs.shutdown();gs.server_close();thread.join(2);gateway.close()


if __name__=='__main__':main()
