"""Persistent UART service. One shared board lock; models in a child process."""
import fcntl,hashlib,json,os,signal,threading,time
from pathlib import Path
from uart_v3 import Uart,require_trial_boot
from audit import Audit
from controller import Controller
from protocol import encode,canonical
from link_session import LinkSession,ERROR_FIELDS

ROOT=Path(__file__).resolve().parent

def debug(uart):
    word=uart.reg.read(0x40)
    return {'rx_high':bool(word&0x100),'error_pending':bool(word&0x200),
            'last_invalid_data':word&255,'low_at_invalid_clocks':uart.reg.read(0x44),
            'last_invalid_low_width_clocks':uart.reg.read(0x48),
            'trace_words':[uart.reg.read(p) for p in (0x50,0x54,0x58,0x5c,0x60)],
            'declared_clock_hz':100000000,'not_external_waveform_measurement':True}

def main(application_factory=Controller):
    manifest=json.loads((ROOT/"release_manifest.json").read_text())
    for name,digest in manifest.items():
        if Path(name).name!=name or hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
            raise RuntimeError("Release hash changed: "+name)
    boot_id=require_trial_boot()
    owner=open('/tmp/r6_validation.lock','a+');fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
    key=bytes.fromhex((ROOT/'link.key').read_text().strip());assert len(key)==32
    log=Audit(ROOT/'lifecycle_events.jsonl')
    log_lock=threading.RLock()
    def event(kind,value):
        with log_lock:log.event(kind,value)
    counts={'executions':0,'cancellations':0};application=None
    def cancel(reason):
        counts['cancellations']+=1;event('cancel_callback',{'reason':reason,'count':counts['cancellations']})
        if application is not None:application.cancel(reason)
    def dispatch(op,args):
        if application is not None and op in ('load_voice','voice_status','speak','job','set_mode','stop_all','resume'):
            return application.dispatch(op,args)
        if op=='status':return dict(counts)
        if op=='integrity':
            text=args.get('text')
            if not isinstance(text,str):raise ValueError('text required')
            return {'sha256':hashlib.sha256(text.encode()).hexdigest(),'utf8_bytes':len(text.encode()),'nonce':os.urandom(8).hex()}
        if op=='count':
            counts['executions']+=1
            return {'execution':counts['executions'],'token':args.get('token')}
        raise ValueError('Transport validation allows only status, integrity, count')
    uart=Uart();session=LinkSession(key,dispatch,cancel,event)
    if application_factory is not None:application=application_factory(ROOT,event)
    stopped=[False]
    def stop(*unused):stopped[0]=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    started=time.monotonic();last_stats=started;last_status=uart.status();outcome='unknown'
    event('opened',{'boot_id':boot_id,'status':last_status,'debug':debug(uart)})
    try:
        # Read-only startup probe also uses the manufacturer's documented
        # serial wake-up path. No baud, power mode or pairing setting changes.
        for command in ('AT+VERSION','AT+STAT'):
            uart.send((command+'\r\n').encode());uart.drain()
            t=time.monotonic();last=t;reply=bytearray()
            while time.monotonic()-t<2:
                part=uart.receive()
                if part:reply.extend(part);last=time.monotonic()
                if reply and time.monotonic()-last>.15:break
                time.sleep(.005)
            event('startup_query',{'command':command,'reply_hex':reply.hex(),'status':uart.status()})
            if command=='AT+STAT':
                if b'+STAT=00' in reply:session.module_connected=False
                elif b'+STAT=02' in reply or b'+STAT=01' in reply:session.module_connected=True
        next_wake=time.monotonic()+2
        print('BLUETOOTH_LIFECYCLE_READY',flush=True)
        while not stopped[0] and not (ROOT/'stop.request').exists():
            status=uart.status();snapshot=debug(uart)
            if any(status[k]!=last_status[k] for k in ERROR_FIELDS):
                event('counter_change',{'before':last_status,'after':status,'debug':snapshot})
            session.observe(status,snapshot);last_status=status
            data=uart.receive(256)
            # Raw speech frames are deliberately not logged.
            for reply in session.feed(data):
                packet=encode(reply,key);uart.send(packet);uart.drain()
                event('reply',{'sid':reply['sid'],'seq':reply['seq'],'ok':reply['ok'],
                              'bytes':len(packet),'sha256':hashlib.sha256(canonical(reply)).hexdigest()})
            if session.module_connected is False and session.state=='await_hello' and time.monotonic()>=next_wake and time.monotonic()-session.last_rx>.25:
                uart.send(b'AT+VERSION\r\n');uart.drain();next_wake=time.monotonic()+2
                event('disconnected_wake_query',{'command':'AT+VERSION','module_connected':False})
            if time.monotonic()-last_stats>=30:
                event('sample',{'state':session.state,'status':uart.status(),'debug':debug(uart),**counts})
                last_stats=time.monotonic()
            time.sleep(.002)
        outcome='requested_stop' if stopped[0] or (ROOT/'stop.request').exists() else 'service_stopped'
    except BaseException as exc:
        outcome='failed';event('failure',{'error':repr(exc)});raise
    finally:
        try:
            session.close()
            if application is not None:application.close()
        finally:
            try:
                uart.drain()
                event('closed',{'outcome':outcome,'status':uart.status(),'debug':debug(uart),**counts})
            finally:uart.close();owner.close();log.close()
        print('BLUETOOTH_LIFECYCLE_CLOSED '+outcome,flush=True)

if __name__=='__main__':main()
