"""Connection lifecycle for FVB1; never resets hardware or clears its counters."""
import secrets
import time
from protocol import Endpoint, Stream

ERROR_FIELDS = ('framing_errors','rx_overruns','bad_writes','bad_reads',
                'breaks','invalid_stops','tx_dropped','tx_overflow')

class LinkSession:
    def __init__(self, key, dispatch, cancel, event, clock=time.monotonic,
                 lease_seconds=6.0, quiet_seconds=.25):
        self.dispatch=dispatch; self.cancel=cancel; self.event=event; self.clock=clock
        self.lease_seconds=lease_seconds; self.quiet_seconds=quiet_seconds
        self.stream=Stream(key); self.endpoint=None; self.state='settling'
        self.previous=None; self.last_rx=clock(); self.last_valid=clock()
        self.nonce=None; self.generation=0; self._new_endpoint()
        self.module_connected=None

    def _new_endpoint(self):
        self.endpoint=Endpoint(secrets.token_hex(16),self._dispatch)
        self.nonce=None; self.generation+=1

    def _dispatch(self,op,args):
        if op=='goodbye':return {'closing':True}
        if op=='ping':return {'pong':True}
        return self.dispatch(op,args)

    def invalidate(self,reason,detail=None):
        previous=self.endpoint.session
        dropped=self.stream.reset()
        was_ready=self.state=='ready'
        self.state='settling';self.last_rx=self.clock()
        self._new_endpoint()
        # The callback cancels queued work/increments the application epoch.
        # It must not block on inference or perform an accelerator reset.
        self.cancel(reason)
        self.event('session_invalidated',{'reason':reason,'old_sid':previous,
                   'was_ready':was_ready,'partial_bytes_discarded':dropped,
                   'generation':self.generation,'detail':detail})

    def observe(self,status,debug):
        if self.state=='closed':return
        now=self.clock()
        if self.previous is not None:
            if status['reset_epoch']!=self.previous['reset_epoch']:
                self.invalidate('unexpected_uart_reset')
                raise RuntimeError('Unexpected UART reset; service stopped')
            changed={k:status[k]-self.previous[k] for k in ERROR_FIELDS
                     if status[k]!=self.previous[k]}
            if changed:self.invalidate('uart_error',{'delta':changed,'debug':debug})
        self.previous=dict(status)
        if self.state=='ready' and now-self.last_valid>=self.lease_seconds:
            self.invalidate('heartbeat_timeout')
        if self.state=='settling' and now-self.last_rx>=self.quiet_seconds:
            if debug['rx_high'] and not debug['error_pending'] and not status['flags']&14:
                self.stream.reset();self.state='await_hello'
                self.event('ready_for_hello',{'generation':self.generation})
        if self.stream.buffer and now-self.last_rx>=2:
            self.invalidate('partial_frame_timeout')

    def feed(self,data):
        if not data or self.state=='closed':return []
        self.last_rx=self.clock()
        if self.state=='settling':
            self.event('settling_discard',{'bytes':len(data),'hex':data.hex()})
        replies=[]
        requests=self.stream.feed(data)
        for notice in list(self.stream.notifications):
            self.event('module_notice',{'text':notice})
            if notice=='CONNECTED' or notice.startswith(('+CONNECTING<<','+CONNECTED')):
                self.module_connected=True
            elif notice.startswith('+DISC:'):
                self.module_connected=False;self.invalidate('module_disconnected',{'notice':notice})
        for request in requests:
            if self.state=='settling':
                self.event('command_blocked',{'reason':'line_settling','op':request.get('op')})
                continue
            op=request.get('op')
            if op=='hello':
                check=self.endpoint.handle(request)
                if not check.get('ok'):
                    replies.append(check);continue
                nonce=request['args']['nonce']
                if self.state=='ready' and nonce!=self.nonce:
                    self.invalidate('new_handshake')
                self.state='ready';self.nonce=nonce;self.last_valid=self.clock()
                self.module_connected=True
                reply=self.endpoint.handle(request)
                self.event('handshake',{'sid':self.endpoint.session,'nonce':nonce})
            elif self.state!='ready':
                self.event('command_blocked',{'reason':'handshake_required','op':op,'seq':request.get('seq')})
                continue
            else:
                reply=self.endpoint.handle(request)
                if reply.get('ok'):self.last_valid=self.clock()
                self.event('request',{'op':op,'seq':request.get('seq'),
                           'sid':request.get('sid'),'ok':reply.get('ok'),'error':reply.get('error')})
                if op=='goodbye' and reply.get('ok'):
                    self.invalidate('graceful_goodbye')
                    replies.append(reply)
                    break
            replies.append(reply)
        return replies

    def close(self):
        self.invalidate('service_shutdown')
        self.state='closed'
