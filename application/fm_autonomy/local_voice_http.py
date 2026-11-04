"""Authenticated voice-only endpoint owned by the existing board process."""
import json
import secrets
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs


def make_server(application, host, port, token, client_ip, boot_id):
    if len(token) < 32 or not client_ip or host in ('', '0.0.0.0', '::'):
        raise ValueError('explicit_bind_client_and_private_token_required')
    instance = uuid.uuid4().hex
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def setup(self):
            super().setup()
            self.connection.settimeout(4)
        def reply(self, value, status=200):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
        def trusted(self):
            return (self.client_address[0] == client_ip and
                    self.headers.get('Origin') is None and
                    secrets.compare_digest(self.headers.get('X-Voice-Token', ''), token))
        def do_GET(self):
            if not self.trusted(): return self.reply(dict(error='unauthorized'), 403)
            path=urlsplit(self.path).path
            photo=getattr(application,'photo_service',None)
            if path=='/photo/status':return self.reply(photo.status() if photo else dict(available=False))
            if path=='/photo/artifact':
                try:
                    if photo is None:raise ValueError('photo_service_unavailable')
                    names=parse_qs(urlsplit(self.path).query).get('name',[])
                    if len(names)!=1:raise ValueError('artifact_name_required')
                    raw,mime=photo.artifact(names[0])
                    self.send_response(200);self.send_header('Content-Type',mime)
                    self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers()
                    self.wfile.write(raw)
                    return
                except (OSError,ValueError):return self.reply(dict(error='photo_artifact_unavailable'),409)
            if self.path == '/health':
                return self.reply(dict(schema='FM-LOCAL-VOICE/1', instance_id=instance,
                    boot_id=boot_id, voice_only=photo is None,photo_available=photo is not None, hardware_action=False))
            if self.path == '/status':
                return self.reply(dict(instance_id=instance, local_active=application.local_voice_active,
                    voice=application.local_voice_dispatch('voice_status', {})))
            return self.reply(dict(error='not_found'), 404)
        def do_POST(self):
            if not self.trusted(): return self.reply(dict(error='unauthorized'), 403)
            if self.path not in ('/command','/photo/command'): return self.reply(dict(error='not_found'), 404)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (self.headers.get_content_type() != 'application/json' or
                        self.headers.get('Transfer-Encoding') or not 0 < size <= 4096):
                    raise ValueError('invalid_body')
                data = json.loads(self.rfile.read(size))
                if self.path=='/photo/command':
                    if not isinstance(data,dict) or set(data)!={'instance_id','action','id','stationary_confirmed'}:
                        raise ValueError('photo_command_context_required')
                    if data['instance_id']!=instance:raise RuntimeError('board_instance_changed')
                    photo=getattr(application,'photo_service',None)
                    if photo is None:raise RuntimeError('photo_service_unavailable')
                    if data['action']=='cancel':photo.cancel();return self.reply(photo.status())
                    return self.reply(photo.request(data['action'],data['id'],data['stationary_confirmed']),202)
                if not isinstance(data, dict) or set(data) != {'instance_id','op','args'}:
                    raise ValueError('command_context_required')
                if data['instance_id'] != instance: raise RuntimeError('board_instance_changed')
                if not isinstance(data['args'], dict): raise ValueError('arguments_required')
                result = application.local_voice_dispatch(data['op'], data['args'])
                return self.reply(result)
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply(dict(error=str(exc)[:160]), 400)
            except RuntimeError as exc:
                return self.reply(dict(error=str(exc)[:160]), 409)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
