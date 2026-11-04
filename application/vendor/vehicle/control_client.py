"""One owner, one outstanding authenticated request; no replay or reconnect arm."""
import socket
import time
from collections import deque
from command_client import CommandClient
from control_wire import WireStream,encode_request


class ControlClient(CommandClient):
    def __init__(self,raw_link,key):
        super().__init__(raw_link,key);self.stream=WireStream(key);self.timings=deque(maxlen=2000)
    def exchange(self,request,timeout=6):
        if self.raw.sock is None:raise ConnectionError('Bluetooth is disconnected')
        self.last_request=request;self.last_frame=encode_request(request,self.key)
        started=time.monotonic()
        try:
            self.raw.sock.sendall(self.last_frame);deadline=started+timeout
            while time.monotonic()<deadline:
                try:data=self.raw.sock.recv(2048)
                except socket.timeout:continue
                if time.monotonic()>=deadline:raise TimeoutError('late_reply_no_replay')
                if not data:raise ConnectionError('Bluetooth peer closed')
                for reply in self.stream.feed(data):
                    if reply.get('seq')!=request['seq']:continue
                    if request['op']=='hello':
                        if reply.get('nonce')!=request['args']['nonce']:continue
                    elif reply.get('sid')!=self.sid:continue
                    self.timings.append({'op':request['op'],'request_bytes':len(self.last_frame),
                                         'rtt_ms':(time.monotonic()-started)*1000,'ok':reply.get('ok')})
                    return reply
            raise TimeoutError('reply_timeout_no_replay')
        except BaseException:self.abort();raise
