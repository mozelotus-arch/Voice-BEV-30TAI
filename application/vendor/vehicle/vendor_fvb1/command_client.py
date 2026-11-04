"""FVB1 client: no automatic action replay after disconnect or timeout."""
import secrets,socket,time
from protocol import Stream,encode

class CommandClient:
    def __init__(self,raw_link,key):
        self.raw=raw_link;self.key=key;self.stream=Stream(key)
        self.sid=None;self.seq=0;self.last_request=None;self.last_frame=None

    def _forget(self):
        self.stream.reset();self.sid=None;self.seq=0
        self.last_request=None;self.last_frame=None

    def abort(self):
        try:self.raw.close()
        finally:self._forget()

    def connect(self):
        self.abort()
        for attempt in range(3):
            try:
                getattr(self.raw,'event',lambda *args:None)('connection_attempt',{'attempt':attempt+1})
                self.raw.connect();self.raw.sock.settimeout(.25)
                time.sleep(.4)
                nonce=secrets.token_hex(16)
                reply=self.exchange({'v':1,'seq':0,'sid':'','op':'hello','args':{'nonce':nonce}})
                if not reply.get('ok') or reply.get('nonce')!=nonce:raise RuntimeError('Handshake failed')
                self.sid=reply['sid'];self.seq=reply['last'];return reply
            except (OSError,TimeoutError,ConnectionError) as exc:
                self.abort()
                getattr(self.raw,'event',lambda *args:None)('connection_attempt_failed',{'attempt':attempt+1,'error':repr(exc)})
                if attempt==2:raise
                time.sleep(.5*(attempt+1))
            except BaseException:
                self.abort();raise

    def exchange(self,request,timeout=6):
        if self.raw.sock is None:raise ConnectionError('Bluetooth is disconnected')
        self.last_request=request;self.last_frame=encode(request,self.key)
        try:
            self.raw.sock.sendall(self.last_frame)
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                try:data=self.raw.sock.recv(2048)
                except socket.timeout:continue
                # A suspended PC can resume inside recv with an old buffered reply.
                # Check the clock again before accepting any response.
                if time.monotonic()>=deadline:
                    raise TimeoutError('Late reply rejected; outcome unknown, action was not replayed')
                if not data:raise ConnectionError('Bluetooth peer closed')
                for reply in self.stream.feed(data):
                    if reply.get('seq')!=request['seq']:continue
                    if request['op']=='hello':
                        if reply.get('nonce')!=request['args']['nonce']:continue
                    elif reply.get('sid')!=self.sid:continue
                    return reply
            raise TimeoutError('Reply timeout; outcome unknown, action was not replayed')
        except BaseException:
            self.abort();raise

    def call(self,op,args=None):
        if self.sid is None:raise ConnectionError('Handshake required')
        self.seq+=1
        reply=self.exchange({'v':1,'sid':self.sid,'seq':self.seq,'op':op,'args':args or {}})
        if not reply.get('ok'):raise RuntimeError(reply.get('error','command_failed'))
        return reply['data']

    def close(self):
        result={'goodbye_acknowledged':False}
        try:
            if self.sid is not None and self.raw.sock is not None:
                result['goodbye_acknowledged']=self.call('goodbye').get('closing') is True
        except Exception as exc:result['error']=str(exc)
        finally:self.abort()
        return result
