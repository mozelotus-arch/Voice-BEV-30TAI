"""Real Windows RFCOMM tests using the module identity read on this boot."""
import ctypes as C,hashlib,json,os,select,socket,struct,sys,time,uuid,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parent
HEADER=struct.Struct('<4sHHI');SPP=uuid.UUID('00001101-0000-1000-8000-00805f9b34fb')
ws=C.WinDLL('Ws2_32.dll');ws.connect.argtypes=[C.c_size_t,C.c_void_p,C.c_int];ws.connect.restype=C.c_int
ws.WSAGetLastError.argtypes=[];ws.WSAGetLastError.restype=C.c_int

class Link:
    def __init__(self):
        rows=[json.loads(l) for l in (ROOT/'uart_events_initial.jsonl').read_text().splitlines()]
        opened=[r for r in rows if r['event']=='opened'];assert len(opened)==1
        trial=json.loads((ROOT/'trial_boot.json').read_text());assert opened[0]['value']['boot_id']==trial['active_candidate_boot_id']
        replies={r['value']['command']:r['value']['reply_text'].strip() for r in rows if r['event']=='query'}
        assert replies['AT+VERSION'].startswith('+VERSION=JDY-33-')
        assert replies['AT+BAUD']=='+BAUD=4','Expected current JDY 9600-baud setting'
        self.mac=replies['AT+LADDR'].split('=',1)[1].replace(':','').upper()
        assert len(self.mac)==12 and all(c in '0123456789ABCDEF' for c in self.mac)
        self.name=replies['AT+NAME'].split('=',1)[1];self.sock=None;self.sequence=0;self.report=[]
        self.log=(ROOT/'pc_bluetooth_events.jsonl').open('x');self.connect()
    def event(self,name,value):
        self.log.write(json.dumps({'monotonic':time.monotonic(),'event':name,'value':value})+'\n');self.log.flush()
    def connect(self):
        if self.sock is not None:raise RuntimeError('Already connected')
        s=socket.socket(socket.AF_BLUETOOTH,socket.SOCK_STREAM,socket.BTPROTO_RFCOMM);s.setblocking(False)
        address=struct.pack('<HQ16sI',32,int(self.mac,16),SPP.bytes_le,0);buffer=C.create_string_buffer(address)
        try:
            if ws.connect(s.fileno(),buffer,len(address)):
                error=ws.WSAGetLastError()
                if error!=10035:raise OSError(error,'SPP connect failed')
                _,ready,failed=select.select([],[s],[s],12)
                if not ready and not failed:raise TimeoutError('SPP connect timeout')
                error=s.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
                if error:raise OSError(error,'SPP connect failed')
            s.settimeout(5);self.sock=s
            r={'name':self.name,'mac':self.mac,'peer':s.getpeername(),'transport':'Bluetooth RFCOMM SPP'}
            self.event('connected',r);print(json.dumps(r),flush=True)
        except BaseException:
            s.close();raise
    def exact(self,length):
        data=bytearray();deadline=time.monotonic()+6
        while len(data)<length:
            if time.monotonic()>deadline:raise TimeoutError('Framed Bluetooth response deadline')
            part=self.sock.recv(length-len(data))
            if not part:raise ConnectionError('Bluetooth stream closed')
            data.extend(part)
        return bytes(data)
    def case(self,payload,phase):
        payload=bytes(payload);assert len(payload)<=1024
        self.sequence+=1;seq=self.sequence
        packet=HEADER.pack(b'FMQ1',seq,len(payload),zlib.crc32(payload)&0xffffffff)+payload
        self.event('case_attempt',{'phase':phase,'sequence':seq,'sent_bytes':len(payload),'sent_packet_hex':packet.hex()})
        started=time.monotonic();self.sock.sendall(packet)
        reply_header=self.exact(HEADER.size)
        magic,reply_seq,length,crc=HEADER.unpack(reply_header)
        assert magic==b'FMQ2' and reply_seq==seq and length==len(payload)+8
        reply=self.exact(length);assert zlib.crc32(reply)&0xffffffff==crc
        assert reply[:-8]==bytes(v^0xa5 for v in payload)
        row={'phase':phase,'sequence':seq,'sent_bytes':len(payload),'sent_sha256':hashlib.sha256(payload).hexdigest(),
             'received_bytes':len(reply),'received_sha256':hashlib.sha256(reply).hexdigest(),'board_nonce_hex':reply[-8:].hex(),
             'roundtrip_ms':(time.monotonic()-started)*1000,'data_sent_over_ssh':False,
             'sent_packet_hex':packet.hex(),'received_packet_hex':(reply_header+reply).hex()}
        self.report.append(row);self.event('case_pass',row);return row
    def cases(self,phase):
        payloads=[b'',b'\x00',bytes(128),bytes([255])*128,bytes([85,170])*128,bytes(range(256)),
                  '新板蓝牙双向验证'.encode('utf-8')+os.urandom(8),b'FMQ1\r\n+DISC:SUCCESS\r\n+CONNECTED\r\nAT\r\nFMQ2'+os.urandom(8)]+[os.urandom(n) for n in (127,129,255,257,512,1024)]
        try:
            for payload in payloads:
                row=self.case(payload,phase);print('SPP_CASE_PASS',phase,row['sequence'],len(payload),flush=True)
            self.event('phase_pass',{'phase':phase,'cases':len(payloads),'scope':'Payload integrity; UART error counters assessed separately'})
        except BaseException as exc:self.event('failed',{'phase':phase,'error':repr(exc)});raise
    def stress(self,seconds=600):
        assert 30<=seconds<=900
        started=time.monotonic();count=0
        try:
            while time.monotonic()-started<seconds:
                self.case(os.urandom([64,128,256,512,1024][count%5]),'stress');count+=1
                if count%20==0:print('SPP_STRESS',count,round(time.monotonic()-started,1),flush=True)
            r={'seconds':time.monotonic()-started,'cases':count};self.event('stress_pass',r);print(json.dumps(r),flush=True)
        except BaseException as exc:self.event('failed',{'phase':'stress','error':repr(exc)});raise
    def close(self):
        if self.sock is not None:self.sock.close();self.sock=None;self.event('disconnected',{})
        print('SPP_DISCONNECTED',flush=True)

if __name__=='__main__':
    if not sys.flags.inspect:raise SystemExit('Use interactive Python to manage reconnect/reset phases')
    pc=Link()
