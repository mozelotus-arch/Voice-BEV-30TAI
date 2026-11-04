"""Local chat application; the existing 8880 service remains the only board bridge."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import signal
import threading
from urllib.parse import urlsplit, parse_qs
from bev_result import BoardBevResult
from board_gateway import BoardGateway, BoardError, PendingVoiceGateway
from chat import Chat
from settings import Settings
from vehicle_control import VehicleControl, MockVehicleAdapter
from gateway_adapter import GatewayAdapter

ROOT = Path(__file__).resolve().parent


class LocalServer(ThreadingHTTPServer):
    def server_close(self):
        if getattr(self,'photo',None) is not None:
            self.photo.close();self.photo=None
        if getattr(self,'local_voice',None) is not None:
            self.local_voice.close()
            self.local_voice=None
        if hasattr(self, 'vehicle'):
            self.vehicle.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, result, status=200, mime='application/json; charset=utf-8'):
        body = result if isinstance(result, bytes) else json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        for name, value in {'Content-Type': mime, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def allowed(self):
        host = self.headers.get('Host')
        return (host in self.server.hosts and self.headers.get('Origin') in (None, 'http://' + str(host))
                and self.headers.get('Sec-Fetch-Site') in (None, 'none', 'same-origin'))

    def do_GET(self):
        if not self.allowed():
            return self.reply(dict(error='仅允许本机同源访问。'), 403)
        path = urlsplit(self.path).path
        if path == '/':
            body = (ROOT / 'web/index.html').read_text(encoding='utf-8').replace('CONTROL_TOKEN', self.server.token)
            panel = (ROOT / 'web/bev_panel.html').read_text(encoding='utf-8') if self.server.bev.folder else ''
            body = body.replace('<!-- BOARD_BEV_PANEL -->', panel)
            body = body.replace('<!-- BOARD_BEV_SCRIPT -->', '<script src="/bev.js" defer></script>' if panel else '')
            return self.reply(body.encode(), mime='text/html; charset=utf-8')
        if path in ('/app.js', '/styles.css', '/vehicle.js', '/vehicle.css', '/autonomy.js', '/bev.js', '/local_voice.js', '/workspace_ui.js'):
            return self.reply((ROOT / 'web' / path[1:]).read_bytes(),
                              mime='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
        if path == '/api/bev/status':
            value=self.server.bev.status()
            if self.server.photo is not None:value['task']=self.server.photo.status()
            return self.reply(value)
        if path == '/api/bev/source':
            try:
                if self.server.photo is None:raise ValueError('photo_unavailable')
                sha=parse_qs(urlsplit(self.path).query).get('sha256',[])
                if len(sha)!=1:raise ValueError('source_sha_required')
                return self.reply(self.server.photo.source(sha[0]),mime='image/jpeg')
            except (OSError,ValueError):return self.reply(dict(error='原始照片不可用'),409)
        if path == '/api/bev/image':
            try:
                meta,raw=self.server.bev.read()
                if parse_qs(urlsplit(self.path).query).get('sha256')!=[meta['sha256']]:raise ValueError('帧已更新，请刷新')
                return self.reply(raw,mime='image/png')
            except (OSError,ValueError,TypeError,KeyError):return self.reply(dict(error='板端返回图像不可用'),409)
        if path == '/api/autonomy/status':
            return self.reply(dict(connected=False,simulated=False,mode='unavailable',error='当前仅恢复原页面功能，车辆移动暂停。'))
        if path == '/api/status':
            return self.reply(self.server.chat.snapshot())
        if path == '/api/capabilities':
            from feature_catalog import catalog
            return self.reply(catalog())
        if path == '/api/board/status':
            return self.reply(self.server.chat.board.status())
        if path == '/api/local-voice/status':
            bridge=self.server.local_voice
            return self.reply(dict(available=bridge is not None,state=bridge.status() if bridge else None))
        if path == '/api/vehicle/status':
            view=parse_qs(urlsplit(self.path).query).get('view',[])
            if self.server.gateway is not None and view in (['manual'],['other']):
                self.server.gateway.note_manual_view(view==['manual'])
            elif self.server.gateway is not None and view==['voice']:
                self.server.gateway.resume_application_polling()
            return self.reply(dict(self.server.vehicle.snapshot(), global_stopped=self.server.chat.snapshot()['stopped']))
        if path == '/favicon.ico':
            return self.reply(b'', 204, 'image/x-icon')
        return self.reply(dict(error='没有此路径。'), 404)

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-Control-Token') != self.server.token:
            return self.reply(dict(error='需要本机控制令牌和同源请求。'), 403)
        try:
            if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                raise ValueError('需要 JSON。')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32768:
                raise ValueError('请求大小超限。')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('请求不完整。')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('需要 JSON 对象。')
            path = urlsplit(self.path).path
            fields = {
                '/api/chat/send': {'id', 'text', 'auto_read', 'board_context'},
                '/api/chat/read': {'id', 'message_id', 'board_context'},
                '/api/chat/clear': set(), '/api/interaction/resume': set(),
                '/api/interaction/stop': set(), '/api/settings': {'settings'},
                '/api/board/control': {'action', 'mode', 'board_context'}, '/api/service/shutdown': set(),
                '/api/vehicle/arm': set(), '/api/vehicle/hold': {'seq', 'action', 'preset', 'lease', 'sent_at_ms'},
                '/api/vehicle/stop': {'reason'},
                '/api/vehicle/connect': set(), '/api/vehicle/disconnect': set(),
                '/api/local-voice/control': {'action','interaction_epoch'},
                '/api/bev/control': {'action','id','stationary_confirmed'},
                '/api/bev/timing': {'report'},
            }
            if path not in fields:
                return self.reply(dict(error='此能力尚未接入。'), 404)
            if set(data) - (fields[path] | {'instance_id', 'epoch'}):
                raise ValueError('请求包含未知字段。')
            chat = self.server.chat
            if path=='/api/bev/timing':
                if self.server.photo is None:raise RuntimeError('photo_unavailable')
                return self.reply(self.server.photo.record_timing(data.get('report')))
            if path=='/api/bev/control':
                if self.server.photo is None:raise RuntimeError('板端视觉任务入口尚未部署')
                chat.require_context(data)
                if chat.snapshot()['stopped'] or self.server.vehicle.snapshot()['armed']:
                    raise RuntimeError('请先恢复交互并停止小车')
                return self.reply(self.server.photo.request(data.get('action'),data.get('id'),data.get('stationary_confirmed')),202)
            if path == '/api/local-voice/control':
                bridge=self.server.local_voice
                if bridge is None:raise RuntimeError('本地语音入口尚未部署')
                action=data.get('action')
                if action not in ('start','stop'):raise ValueError('local_voice_action_required')
                if action=='start' and (chat.snapshot()['stopped'] or self.server.vehicle.snapshot()['armed']):
                    raise RuntimeError('请先恢复交互并停止车辆，再开始本地语音')
                if self.server.gateway is not None:self.server.gateway.resume_application_polling()
                return self.reply(bridge.request(action,data),202)
            if path.startswith('/api/vehicle/'):
                if path == '/api/vehicle/stop':
                    result = self.server.vehicle.stop('operator')
                elif path == '/api/vehicle/disconnect':
                    result = self.server.vehicle.disconnect()
                else:
                    if chat.snapshot()['stopped']:
                        raise RuntimeError('全部交互已停止，请先恢复交互，再显式启用车辆。')
                    if path.endswith('/arm') or path.endswith('/connect'):
                        with self.server.admission:
                            if path.endswith('/arm') and self.server.photo is not None and self.server.photo.status()['busy']:
                                raise RuntimeError('视觉任务期间请保持小车静止')
                            if chat.snapshot()['busy'] or chat.snapshot()['board_control_pending']:
                                raise RuntimeError('当前聊天或语音任务尚未结束，请稍候启用车辆。')
                            result = self.server.vehicle.arm(data) if path.endswith('/arm') else self.server.vehicle.connect(data)
                    else:
                        result = self.server.vehicle.hold(data)
                return self.reply(dict(result, global_stopped=chat.snapshot()['stopped']), 202)
            if path == '/api/interaction/stop':
                self.server.vehicle.stop('stop_all')
                if self.server.photo is not None:self.server.photo.cancel()
                if self.server.local_voice is not None:self.server.local_voice.request('stop',{})
                chat.stop()
            elif path == '/api/chat/send':
                with self.server.admission:
                    if data.get('auto_read') and self.server.vehicle.snapshot()['armed']:
                        raise RuntimeError('请先停止车辆，再使用板端朗读；文字聊天仍可使用。')
                    chat.send(data)
            elif path == '/api/chat/read':
                with self.server.admission:
                    if self.server.vehicle.snapshot()['armed']:
                        raise RuntimeError('请先停止车辆，再使用板端朗读。')
                    chat.read(data)
            elif path == '/api/chat/clear':
                chat.clear(data)
            elif path == '/api/interaction/resume':
                chat.resume(data)
            elif path == '/api/settings':
                if not isinstance(data.get('settings'), dict):
                    raise ValueError('配置格式无效。')
                chat.configure(data, data['settings'])
            elif path == '/api/board/control':
                with self.server.admission:
                    if self.server.vehicle.snapshot()['armed']:
                        raise RuntimeError('请先停止车辆，再操作板端语音模式。')
                    chat.board_control(data)
            elif path == '/api/service/shutdown':
                chat.require_context(data)
                chat.close()
                self.server.vehicle.close()
                self.reply(dict(success=True), 202)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.reply(chat.snapshot(), 202)
        except (ValueError, KeyError, TypeError, RecursionError):
            return self.reply(dict(error='输入无效，请检查字段、长度和配置格式。'), 400)
        except (RuntimeError, BoardError) as exc:
            return self.reply(dict(error=str(exc)), 409)
        except Exception:
            return self.reply(dict(error='本机服务处理失败；没有自动重试。'), 500)

    def log_message(self, *unused):
        pass


def make_server(port=8883, folder=None, chat=None, board_port=8880, vehicle=None, bev_folder=None, local_voice=None, gateway=None,photo=None):
    server = LocalServer(('127.0.0.1', port), Handler)
    server.gateway=gateway
    server.daemon_threads = True
    server.hosts = {'127.0.0.1:%d' % server.server_port, 'localhost:%d' % server.server_port}
    server.token = secrets.token_urlsafe(32)
    server.admission = threading.Lock()
    server.bev = BoardBevResult(bev_folder)
    server.photo=photo
    server.local_voice = local_voice
    server.chat = chat or Chat(Settings(folder or ROOT / 'private'), PendingVoiceGateway(board_port))
    server.vehicle = vehicle or VehicleControl()
    from feature_catalog import register_workspace
    register_workspace(server.chat.registry, server.chat.board, server.vehicle, local_voice)
    if gateway is not None:
        from workspace_agent import register_agent_tools
        register_agent_tools(server.chat.registry, gateway, server.chat.board, server.vehicle, local_voice)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8883)
    parser.add_argument('--mock-vehicle', action='store_true', help='仅模拟车辆显示与按键，不连接硬件')
    parser.add_argument('--gateway-port', type=int, default=8890, help='唯一 PC 控制网关的本机端口')
    parser.add_argument('--voice-bridge-port',type=int,help='显式复用唯一现有语音桥；不在此进程打开蓝牙')
    parser.add_argument('--vehicle-paused',action='store_true',help='充电期间不启动车辆网关客户端')
    parser.add_argument('--bev-folder',type=Path,help='只展示板端回传的 PNG 与 latest.json')
    args = parser.parse_args()
    board=BoardGateway(args.voice_bridge_port) if args.voice_bridge_port else PendingVoiceGateway()
    server = make_server(args.port,chat=Chat(Settings(ROOT/'private'),board),bev_folder=args.bev_folder,
        vehicle=VehicleControl(None if args.vehicle_paused else MockVehicleAdapter() if args.mock_vehicle else GatewayAdapter(args.gateway_port)))
    def shutdown(*unused):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown); signal.signal(signal.SIGTERM, shutdown)
    print(json.dumps(dict(url='http://127.0.0.1:%d/' % server.server_port,
        pid=__import__('os').getpid(), board_owner=False, hardware_actions=0)), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.vehicle.close(); server.chat.close(); server.server_close()


if __name__ == '__main__':
    main()
