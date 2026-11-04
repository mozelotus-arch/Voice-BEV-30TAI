"""Single PC Bluetooth owner; localhost HTTP facade with explicit arm and leases."""
import argparse
import collections
import hashlib
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent
BASELINE=ROOT.parent/'config/private/baseline'
from fm_autonomy.dependencies import add_vendor
add_vendor('vehicle', 'vehicle/vendor_fvb1', 'radio')
sys.path.insert(0,str(ROOT/'vendor_fvb1'))
sys.path.append(str(BASELINE))
from control_client import ControlClient
from pc_vehicle import VehicleClient,PCBluetoothLock

ACTIONS={'forward':(100,0,0),'backward':(-100,0,0),
         'rotate_left':(0,0,800),'rotate_right':(0,0,-800),
         'front_left':(100,0,800),'front_right':(100,0,-800),
         'back_left':(-100,0,-800),'back_right':(-100,0,800)}


class Bluetooth:
    def __init__(self,folder):
        from pc_bluetooth import Link
        from host_awake import KeepAwake
        self.log=(folder/'radio.jsonl').open('x',encoding='utf-8',buffering=1)
        outer=self
        class Radio(Link):
            def __init__(self):self.mac='123459658872';self.name='WHEELTEC';self.sock=None
            def event(self,name,value):outer.log.write(json.dumps({'t':time.monotonic(),'event':name,'value':value})+'\n')
        key=bytes.fromhex((BASELINE/'link.key').read_text().strip())
        self.commands=ControlClient(Radio(),key);self.vehicle=VehicleClient(self.commands)
        self.lease=PCBluetoothLock();self.awake=KeepAwake(folder/'awake.jsonl');self.owned=False
    def connect(self):
        self.lease.__enter__();self.owned=True;self.awake.__enter__()
        try:return self.commands.connect()
        except BaseException:self.close();raise
    def close(self):
        try:return self.commands.close()
        finally:
            if self.owned:
                self.owned=False;self.awake.__exit__(None,None,None);self.lease.__exit__(None,None,None)
            self.log.close()


class Gateway:
    def __init__(self,folder,validated=False,factory=Bluetooth,clock=time.monotonic,wall=time.time,ground_user=False):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.clock=clock;self.wall=wall;self.factory=factory
        self.simulated=getattr(factory,'simulated',False)
        self.ground_user=ground_user
        self.preset='ground' if ground_user else 'bench'
        self.angular_limit=400 if ground_user else 800
        self.instance_id=secrets.token_hex(16);self.token=secrets.token_hex(24);self.epoch=1
        self.lock=threading.RLock();self.wake=threading.Event();self.done=False
        self.tasks=collections.deque();self.radio=None;self.connected=False
        self.session=None;self.seq=0;self.desired=None;self.input_until=0;self.session_until=0
        self.manual_view_until=0;self.manual_poll_resume_until=0
        self.cached={};self.cached_at=0;self.cached_transport_ms=0
        self.validated=validated;self.stop_requested=True;self.message='请显式连接车辆网关'
        self.events=(self.folder/'gateway.jsonl').open('x',encoding='utf-8',buffering=1)
        self.worker=threading.Thread(target=self._work,name='single-bluetooth-owner',daemon=True);self.worker.start()
    def event(self,name,**value):
        self.events.write(json.dumps({'t':self.clock(),'event':name,**value},ensure_ascii=False)+'\n')
    def _invalidate(self,reason):
        self.epoch+=1;self.session=None;self.desired=None;self.input_until=0;self.seq=0
        self.stop_requested=True;self.message=reason
    def _guard(self,args):
        if args.get('instance_id')!=self.instance_id or type(args.get('epoch')) is not int or args['epoch']!=self.epoch:
            raise ValueError('stale_gateway_instance_or_epoch')
    def status(self):
        with self.lock:
            s=dict(self.cached);t=s.get('telemetry') or {};v=t.get('v',[None,None,None])
            age=s.get('feedback_age_ms')
            age=None if age is None else age+self.cached_transport_ms+max(0,(self.clock()-self.cached_at)*1000)
            # This is a conservative PC display-age bound including an entire
            # wireless RTT. The CAN actor independently enforces 200 ms.
            fresh=self.connected and s.get('feedback_fresh') is True and age is not None and age<600
            enabled=t.get('disabled')==0
            ready=bool(self.validated and fresh and enabled and s.get('motion_allowed') and s.get('state')!='fault')
            message=self.message
            if self.connected and not ready:
                if not self.validated:message='当前控制会话未授权，方向控制不可用。'
                elif not s.get('motion_allowed'):message='板端未允许运动，请查看当前控制会话。'
                elif not fresh:message='底盘反馈暂不可用，方向控制已停。'
                elif not enabled:message='底盘报告禁用状态，方向控制不可用。'
            return {'web_protocol':'FV-WEB/1','instance_id':self.instance_id,'epoch':self.epoch,
                    'connected':self.connected,'gateway_connected':self.connected,'ready':ready,
                    'armed':self.session is not None,'transport_validated':self.validated,
                    'directions_verified':True,'feedback_age_ms':None if age is None else round(age),
                    'can_state':'feedback_fresh' if fresh else 'unavailable',
                    'vx_mm_s':v[0],'vy_mm_s':v[1],'wz_mrad_s':v[2],'battery_mv':t.get('battery_mv'),
                    'stop_requested':self.stop_requested,'zero_enqueued':bool(s.get('zero_enqueued')),
                    'standstill_observed':bool(fresh and s.get('standstill_observed')),
                    'mode':'offline_mock' if self.simulated else ('ground_user_command_demo' if self.ground_user else 'raised_wheels_bench'),
                    'message':('离线模拟，未连接硬件。' if self.simulated else '')+message,
                    'simulated':self.simulated,'hardware_executed':self.connected and not self.simulated,
                    'state':s.get('state','disconnected'),'motion_allowed':bool(s.get('motion_allowed')),
                    'limits':{'linear_mm_s':100,'angular_mrad_s':self.angular_limit,'vy_mm_s':0},'preset':self.preset}
    def _submit(self,op,args=None,timeout=3):
        task={'op':op,'args':args or {},'event':threading.Event(),'result':None,'error':None}
        with self.lock:
            if len(self.tasks)>=4:raise RuntimeError('gateway_busy_no_command_queue')
            self.tasks.append(task);self.wake.set()
        if not task['event'].wait(timeout):
            if op in ('arm','application'):self.stop('request_response_timeout')
            raise TimeoutError('gateway_response_timeout')
        if task['error']:raise RuntimeError(task['error'])
        return task['result']
    def note_manual_view(self,active):
        # A short-lived polling preference, never a connection or motion permit.
        with self.lock:self.manual_view_until=self.clock()+3 if active else 0
    def manual_view_active(self):
        with self.lock:return self.manual_poll_resume_until<=self.clock()<self.manual_view_until
    def resume_application_polling(self):
        # Give an explicit mode request time to observe fresh state, even when
        # another visible manual page keeps renewing its short-lived hint.
        with self.lock:
            self.manual_view_until=0
            self.manual_poll_resume_until=self.clock()+3
    def application(self,op,args=None):
        allowed={'voice_status','load_voice','speak','job','set_mode','stop_all','resume',
                 'autonomy.status','autonomy.start','command.text','command.model_text','interaction.resume','status','motion.capabilities'}
        if op not in allowed:raise ValueError('application_operation_not_enabled')
        if op=='stop_all':self.stop('stop_all')
        with self.lock:
            if op not in ('voice_status','job','autonomy.status','status','motion.capabilities'):
                self.resume_application_polling()
            if self.session is not None and op!='stop_all':
                raise RuntimeError('stop_manual_drive_before_voice_or_mode_request')
            fence=self.epoch
        return self._submit('application',{'op':op,'args':args or {},'epoch':fence},timeout=4)
    def observe_motion(self):
        """Read terminal + raw CAN feedback together through the sole radio owner."""
        with self.lock:
            if not self.connected:raise RuntimeError('connect_required')
            if self.session is not None:raise RuntimeError('manual_control_active')
            fence=self.epoch
        return self._submit('motion_observation',{'epoch':fence},timeout=12)

    def _observe_motion_wire(self,fence):
        started=self.clock()
        def current():
            with self.lock:return self.connected and self.epoch==fence and self.session is None
        cancelled=dict(consistent=False,cancelled=True,control={},feedback={},source='board_serial_observation')
        if not current():return cancelled
        before=self.radio.commands.call('autonomy.status',{})
        if not current():return cancelled
        feedback_started=self.clock()
        feedback=self.radio.vehicle.status()
        self._cache(feedback,self.clock()-feedback_started)
        if not current():return cancelled
        after=self.radio.commands.call('autonomy.status',{})
        # The board itself establishes fresh CAN feedback after its stop request.
        # PC display-age includes Bluetooth RTT and is not that board-side fact.
        return dict(control=after,feedback=feedback,consistent=current() and before.get('epoch')==after.get('epoch'),
                    source='board_serial_observation',gateway_epoch=fence,round_trip_seconds=self.clock()-started)
    def connect(self):return self._submit('connect',timeout=35)
    def disconnect(self):
        self.stop('explicit_disconnect')
        return self._submit('disconnect',timeout=6)
    def arm(self,args):
        with self.lock:
            self._guard(args)
            if self.session:raise ValueError('already_armed')
            if not self.status()['ready']:raise ValueError('fresh_validated_vehicle_required')
            if not self.status()['standstill_observed']:raise ValueError('standstill_required')
            session=secrets.token_hex(16);self.session=session;self.seq=0
            self.input_until=self.clock()+.35;self.session_until=self.clock()+(5 if self.ground_user else 30)
            guard={'epoch':self.epoch,'session':session}
        return self._submit('arm',guard)
    def hold(self,args):
        with self.lock:
            try:
                self._guard(args)
                if not self.session or args.get('session')!=self.session:raise ValueError('stale_motion_session')
                if type(args.get('seq')) is not int or args['seq']<=self.seq:raise ValueError('stale_input_sequence')
                sent=args.get('sent_at_ms')
                if type(sent) not in (int,float):raise ValueError('input_timestamp_required')
                age=self.wall()*1000-sent
                if not -50<=age<=200:raise ValueError('stale_input_timestamp')
                if args.get('preset')!=self.preset or args.get('action') not in ACTIONS:raise ValueError('invalid_action_or_preset')
                if self.clock()>=self.session_until:raise ValueError('motion_session_expired')
                self.seq=args['seq'];self.desired=ACTIONS[args['action']]
                if self.ground_user:
                    x,y,z=self.desired;self.desired=(x,y,int(z/2))
                self.input_until=self.clock()+(.35-max(0,age)/1000)
                self.wake.set()
                return {'ok':True,'accepted':True,'seq':self.seq,'instance_id':self.instance_id,'epoch':self.epoch,
                        'hardware_executed':False,'message':'输入已接收，实车执行见反馈'}
            except (ValueError,TypeError):
                self.stop('rejected_or_stale_input');raise
    def stop(self,reason='operator_stop'):
        with self.lock:
            self._invalidate(reason)
            # A stop cancels pending arms before any further radio action.
            for t in list(self.tasks):
                if t['op'] in ('arm','application','motion_observation'):
                    self.tasks.remove(t);t['error']='arm_cancelled_by_stop';t['event'].set()
            if not any(t['op']=='stop' for t in self.tasks):
                self.tasks.appendleft({'op':'stop','args':{},'event':threading.Event(),'result':None,'error':None})
            self.wake.set();self.event('stop_requested',reason=reason,epoch=self.epoch)
            return {'ok':True,'stop_requested':True,'acknowledged':False,'hardware_executed':False,
                    'message':'已请求停车，等待底盘零速回执','instance_id':self.instance_id,'epoch':self.epoch}
    def _cache(self,s,rtt):
        with self.lock:self.cached=s;self.cached_at=self.clock();self.cached_transport_ms=rtt*1000
    def _work(self):
        next_status=0
        status_before_read=False
        read_only={'voice_status','job','autonomy.status','status','motion.capabilities'}
        while not self.done:
            task=None;operation=None
            try:
                with self.lock:
                    if self.session and (self.clock()>=self.input_until or self.clock()>=self.session_until):
                        self.stop('input_lease_expired');continue
                    elif self.tasks:
                        only_reads=all(t['op']=='application' and t['args'].get('op') in read_only for t in self.tasks)
                        if (only_reads and not status_before_read and self.radio and self.connected
                                and self.session is None and self.clock()>=next_status):
                            operation=('status',None,None);status_before_read=True
                        else:
                            task=self.tasks.popleft();status_before_read=False
                    elif self.radio and self.connected and self.session and self.desired:
                        operation=('drive',tuple(self.desired),self.epoch)
                    elif self.radio and self.connected and self.session is None and self.clock()>=next_status:
                        operation=('status',None,None)
                if task:
                    op=task['op'];started=self.clock();feedback_updated=False
                    if op=='connect':
                        if not self.connected:
                            self.radio=self.factory(self.folder/str(time.time_ns())) if self.factory is not Bluetooth else self._new_radio()
                            self.radio.connect();self._cache(self.radio.vehicle.stop(),self.clock()-started);feedback_updated=True
                            with self.lock:self.connected=True;self._invalidate('已连接，请按住方向控制')
                        task['result']={'ok':True,**self.status()}
                    elif op=='application':
                        with self.lock:
                            if task['args']['epoch']!=self.epoch:raise ValueError('application_cancelled')
                        if not self.connected or self.radio is None:raise RuntimeError('connect_required')
                        try:task['result']=self.radio.commands.call(task['args']['op'],task['args']['args'])
                        except RuntimeError as exc:
                            if self.radio.commands.sid is None:raise
                            task['error']=str(exc)
                    elif op=='motion_observation':
                        try:task['result']=self._observe_motion_wire(task['args']['epoch'])
                        except RuntimeError as exc:
                            if self.radio.commands.sid is None:raise
                            task['error']=str(exc)
                    elif op=='arm':
                        with self.lock:
                            if task['args']['epoch']!=self.epoch or task['args']['session']!=self.session:raise ValueError('arm_cancelled')
                        armed=self.radio.vehicle.arm()
                        if isinstance(armed.get('feedback'),dict):
                            self._cache(armed['feedback'],self.clock()-started);feedback_updated=True
                        with self.lock:
                            if task['args']['epoch']!=self.epoch or task['args']['session']!=self.session:
                                self.radio.vehicle.stop();raise ValueError('arm_cancelled')
                            self.input_until=self.clock()+.35
                            self.stop_requested=False;self.message='按住控制中'
                            task['result']={'ok':True,**self.status(),'session':self.session,
                                            'telemetry_in_arm_reply':isinstance(armed.get('feedback'),dict)}
                    elif op=='stop':
                        if self.connected:
                            self._cache(self.radio.vehicle.stop(),self.clock()-started);feedback_updated=True
                        task['result']={'ok':True,**self.status()}
                    elif op=='disconnect':
                        if self.radio:self.radio.close();self.radio=None
                        with self.lock:self.connected=False;self.cached={};self._invalidate('已断开，重连不会恢复动作')
                        task['result']={'ok':True}
                    task['event'].set()
                    if feedback_updated:next_status=self.clock()+.02
                elif operation:
                    started=self.clock()
                    if operation[0]=='drive':
                        r=self.radio.vehicle.drive(list(operation[1]));s=r['s']
                        self._cache({'state':'active','motion_allowed':True,'feedback_fresh':True,'feedback_age_ms':0,
                                     'telemetry':{'disabled':s[0],'v':s[1:4],'battery_mv':s[4]},
                                     'zero_enqueued':False,'standstill_observed':False},self.clock()-started)
                        self.event('drive_reply',target=operation[1],feedback=s,epoch=operation[2],rtt_ms=(self.clock()-started)*1000)
                    else:self._cache(self.radio.vehicle.status(),self.clock()-started);next_status=self.clock()+.02
                else:self.wake.wait(.01);self.wake.clear()
            except Exception as exc:
                self.event('transport_error',error=repr(exc))
                with self.lock:self.connected=False;self._invalidate(str(exc));self.cached={}
                if self.radio:
                    try:self.radio.close()
                    except Exception:pass
                    self.radio=None
                if task:task['error']=str(exc);task['event'].set()
        if self.radio:
            try:self.radio.vehicle.stop();self.radio.close()
            except Exception:pass
    def _new_radio(self):
        folder=self.folder/str(time.time_ns());folder.mkdir();return Bluetooth(folder)
    def close(self):
        self.stop('gateway_shutdown')
        until=time.monotonic()+2
        while self.tasks and time.monotonic()<until:time.sleep(.02)
        self.done=True;self.wake.set();self.worker.join(5);self.events.close()


def make_server(gateway,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value,status=200):
            raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def trusted(self):
            host=self.headers.get('Host','').split(':')[0]
            origin=self.headers.get('Origin')
            return host in ('127.0.0.1','localhost') and (not origin or urlparse(origin).netloc in ('127.0.0.1:8883','localhost:8883'))
        def do_GET(self):
            if not self.trusted():return self.reply({'ok':False,'error':'forbidden_origin'},403)
            if self.path=='/api/vehicle/meta':return self.reply({'web_protocol':'FV-WEB/1','instance_id':gateway.instance_id,'control_token':gateway.token})
            if self.path=='/api/vehicle/status':return self.reply(gateway.status())
            self.reply({'ok':False,'error':'not_found'},404)
        def do_POST(self):
            if not self.trusted() or not secrets.compare_digest(self.headers.get('X-Control-Token',''),gateway.token):
                return self.reply({'ok':False,'error':'forbidden'},403)
            try:
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('json_required')
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=4096:raise ValueError('invalid_body_size')
                args=json.loads(self.rfile.read(n))
                if not isinstance(args,dict):raise ValueError('object_required')
                op=self.path.removeprefix('/api/vehicle/')
                if op=='connect':result=gateway.connect()
                elif op=='disconnect':result=gateway.disconnect()
                elif op=='arm':result=gateway.arm(args)
                elif op=='hold':result=gateway.hold(args)
                elif op=='stop':result=gateway.stop()
                else:raise ValueError('unknown_operation')
                self.reply(result)
            except (ValueError,RuntimeError,TimeoutError) as exc:self.reply({'ok':False,'error':str(exc)},409)
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def serve(gateway,port):
    server=make_server(gateway,port)
    try:server.serve_forever(poll_interval=.2)
    finally:server.server_close();gateway.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8890)
    a=p.parse_args();receipt=ROOT/'evidence/bluetooth_control_acceptance.json'
    verified=receipt.exists() and json.loads(receipt.read_text(encoding='utf-8')).get('result')=='PASS'
    g=Gateway(ROOT/'gateway_runs'/str(time.time_ns()),validated=verified)
    print('VEHICLE_GATEWAY http://127.0.0.1:'+str(a.port),flush=True);serve(g,a.port)
