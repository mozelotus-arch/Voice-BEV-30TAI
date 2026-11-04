"""Compact authenticated vehicle packets; unchanged FVB1 for voice/handshake.

Each request is 68 serial bytes and each reply 70 (COBS delimiters included),
about 144 ms combined at 9600/8N1. The existing 500 ms board-issued deadline,
session identity, increasing sequence, one-use ticket, CRC and 128-bit MAC stay.
"""
import hashlib
import hmac
import secrets
import struct
import zlib
from protocol import Stream, Endpoint, cobs_encode, cobs_decode, decode, encode, MAX_ENCODED
from link_session import LinkSession

REQUEST=struct.Struct('<16sIB8s8shh')
REPLY=struct.Struct('<16sIB8sHHBhhhHB')
ARM_REPLY=struct.Struct('<16sIB8s8sH')
ARM_FEEDBACK=struct.Struct('<HBhhhHB')
ERRORS=['','invalid_envelope','stale_session','stale_sequence','sequence_conflict',
        'explicit_arm_required','stale_motion_session','invalid_or_consumed_ticket',
        'expired_ticket','feedback_stale','chassis_disabled','can_tx_failed',
        'velocity_limit_or_lateral_motion','reconnect_required_after_stop_all',
        'motion_locked_pending_operator_authorization']


def _hex(value,n):
    if not isinstance(value,str) or len(value)!=n*2:raise ValueError('invalid_identifier')
    raw=bytes.fromhex(value)
    if len(raw)!=n:raise ValueError('invalid_identifier')
    return raw


def _packet(magic,payload,key):
    body=magic+payload
    body+=struct.pack('<I',zlib.crc32(body)&0xffffffff)
    body+=hmac.new(key,body,hashlib.sha256).digest()[:16]
    return b'\0'+cobs_encode(body)+b'\0'


def encode_request(request,key):
    op=request.get('op')
    if op not in ('vehicle.drive','vehicle.stop','vehicle.status','vehicle.arm'):return encode(request,key)
    seq=request.get('seq')
    if request.get('v')!=1 or type(seq) is not int or not 0<seq<2**32:raise ValueError('invalid_control_envelope')
    sid=_hex(request.get('sid'),16);args=request.get('args',{})
    if op=='vehicle.drive':
        if not isinstance(args,dict) or set(args)!={'m','t','v'}:raise ValueError('invalid_drive_fields')
        v=args['v']
        if not isinstance(v,(list,tuple)) or len(v)!=3 or any(type(x) is not int or not -32768<=x<=32767 for x in v) or v[1]!=0:
            raise ValueError('invalid_velocity')
        payload=REQUEST.pack(sid,seq,1,_hex(args['m'],8),_hex(args['t'],8),v[0],v[2])
    else:
        if args!={}:raise ValueError('status_or_stop_takes_no_arguments')
        payload=REQUEST.pack(sid,seq,{'vehicle.stop':2,'vehicle.status':3,'vehicle.arm':4}[op],bytes(8),bytes(8),0,0)
    return _packet(b'FVCQ',payload,key)


def encode_reply(reply,key):
    if reply.get('_wire')!=2:return encode(reply,key)
    data=reply.get('data',{});ok=reply.get('ok') is True
    code=0 if ok else (ERRORS.index(reply.get('error')) if reply.get('error') in ERRORS else 255)
    if reply.get('_control_op')==4:
        payload=ARM_REPLY.pack(_hex(reply['sid'],16),reply['seq'],code,
                               _hex(data['m'],8) if ok else bytes(8),
                               _hex(data['t'],8) if ok else bytes(8),max(0,min(500,data.get('h',0))))
        feedback=data.get('feedback')
        if ok and isinstance(feedback,dict):
            telemetry=feedback.get('telemetry') or {}
            age=feedback.get('feedback_age_ms')
            flags=(int(bool(feedback.get('feedback_fresh'))) |
                   int(bool(feedback.get('motion_allowed')))<<1 |
                   int(bool(feedback.get('standstill_observed')))<<2)
            payload+=ARM_FEEDBACK.pack(65535 if age is None else min(65535,max(0,age)),
                telemetry.get('disabled',1),*telemetry.get('v',[0,0,0]),telemetry.get('battery_mv',0),flags)
            return _packet(b'FVCB',payload,key)
        return _packet(b'FVCA',payload,key)
    flags=0
    is_status=reply.get('_control_op') in (2,3)
    if is_status:
        flags=(128 | int(bool(data.get('zero_enqueued'))) | (int(bool(data.get('standstill_observed')))<<1)
               | (int(bool(data.get('feedback_fresh')))<<2) | (int(bool(data.get('motion_allowed')))<<3)
               | (16 if data.get('state')=='armed' else 32 if data.get('state')=='active' else 64 if data.get('state') in ('fault','unavailable') else 0))
    t=data.get('telemetry') or {}
    s=data.get('s',[t.get('disabled',1),*t.get('v',[0,0,0]),t.get('battery_mv',0)])
    token=_hex(data['t'],8) if ok and 't' in data else bytes(8)
    age=data.get('feedback_age_ms');h=(65535 if age is None else min(65535,max(0,age))) if is_status else max(0,min(500,data.get('h',0)))
    d=max(0,min(500,data.get('deadline_ms',0) if is_status else data.get('d',0)))
    payload=REPLY.pack(_hex(reply['sid'],16),reply['seq'],code,token,h,d,
                       s[0],s[1],s[2],s[3],s[4],flags)
    return _packet(b'FVCR',payload,key)


def decode_wire(encoded,key):
    packet=cobs_decode(encoded)
    if packet[:4] not in (b'FVCQ',b'FVCR',b'FVCA',b'FVCB'):
        result=decode(encoded,key)
        result.pop('_wire',None);result.pop('_control_op',None)
        return result
    layout={b'FVCQ':REQUEST,b'FVCR':REPLY,b'FVCA':ARM_REPLY,b'FVCB':ARM_REPLY}[packet[:4]]
    extended_arm=packet[:4]==b'FVCB'
    if len(packet)!=4+layout.size+20+(ARM_FEEDBACK.size if extended_arm else 0):raise ValueError('control_packet_size')
    body,tag=packet[:-16],packet[-16:]
    if not hmac.compare_digest(hmac.new(key,body,hashlib.sha256).digest()[:16],tag):raise ValueError('authentication_failed')
    payload,crc=body[:-4],body[-4:]
    if struct.pack('<I',zlib.crc32(payload)&0xffffffff)!=crc:raise ValueError('control_crc')
    fields=layout.unpack(payload[4:4+layout.size])
    if layout is REQUEST:
        sid,seq,op,m,t,x,z=fields
        if not seq or op not in (1,2,3,4):raise ValueError('invalid_control_opcode_or_sequence')
        if op!=1 and (m!=bytes(8) or t!=bytes(8) or x or z):raise ValueError('invalid_status_or_stop_fields')
        return {'v':1,'sid':sid.hex(),'seq':seq,'op':{1:'vehicle.drive',2:'vehicle.stop',3:'vehicle.status',4:'vehicle.arm'}[op],
                'args':{'m':m.hex(),'t':t.hex(),'v':[x,0,z]} if op==1 else {},'_wire':2,'_control_op':op}
    if layout is ARM_REPLY:
        sid,seq,code,m,t,h=fields
        result=dict(sid=sid.hex(),seq=seq,ok=code==0,**(
            {'error':ERRORS[code] if code<len(ERRORS) else 'compact_command_failed'} if code else
            {'data':{'m':m.hex(),'t':t.hex(),'h':h}}))
        if extended_arm and code==0:
            age,disabled,x,y,z,battery,flags=ARM_FEEDBACK.unpack(payload[4+layout.size:])
            result['data']['feedback']=dict(state='armed',feedback_age_ms=None if age==65535 else age,
                feedback_fresh=bool(flags&1),motion_allowed=bool(flags&2),standstill_observed=bool(flags&4),
                telemetry=dict(disabled=disabled,v=[x,y,z],battery_mv=battery),zero_enqueued=True)
        return result
    sid,seq,code,t,h,d,disabled,x,y,z,battery,flags=fields
    result={'sid':sid.hex(),'seq':seq,'ok':code==0}
    if code:result['error']=ERRORS[code] if code<len(ERRORS) else 'compact_command_failed'
    elif flags&128:
        result['data']={'state':'fault' if flags&64 else 'active' if flags&32 else 'armed' if flags&16 else 'stopped',
                        'zero_enqueued':bool(flags&1),'standstill_observed':bool(flags&2),'motion_allowed':bool(flags&8),
                        'feedback_fresh':bool(flags&4),'feedback_age_ms':None if h==65535 else h,'deadline_ms':d,
                        'telemetry':{'disabled':disabled,'v':[x,y,z],'battery_mv':battery}}
    else:result['data']={'t':t.hex(),'h':h,'d':d,'s':[disabled,x,y,z,battery]}
    return result


class WireStream(Stream):
    def feed(self,data):
        self.notifications=[];messages=[]
        for byte in data:
            if byte==0:
                if self.buffer and not self.discard:
                    try:messages.append(decode_wire(bytes(self.buffer),self.key));self.accepted+=1
                    except (ValueError,UnicodeError,struct.error):self.rejected+=1
                self.buffer.clear();self.discard=False
            elif not self.discard:
                self.buffer.append(byte)
                if byte==10:
                    line=bytes(self.buffer).strip(b'\r\n')
                    if not line or line==b'CONNECTED' or line.startswith((b'+CONNECTING<<',b'+CONNECTED',b'+DISC:',b'+VERSION=',b'+STAT=')) or line==b'+OK':
                        if line:self.notifications.append(line.decode('ascii',errors='replace'))
                        self.buffer.clear()
                if len(self.buffer)>MAX_ENCODED:
                    self.buffer.clear();self.discard=True;self.rejected+=1
        return messages


class ControlEndpoint(Endpoint):
    def handle(self,request):
        result=super().handle(request)
        if request.get('_wire')==2:
            result=dict(result,_wire=2,_control_op=request['_control_op'])
        return result


class ControlLinkSession(LinkSession):
    def __init__(self,key,*args,**kwargs):
        super().__init__(key,*args,**kwargs);self.stream=WireStream(key)
    def _new_endpoint(self):
        self.endpoint=ControlEndpoint(secrets.token_hex(16),self._dispatch)
        self.nonce=None;self.generation+=1
