"""Worker-side adapter for the bounded camera stream service."""
import json,os,socket,subprocess,sys,time,uuid
from pathlib import Path
import recording_capture as base
ROOT=Path(__file__).resolve().parent
SOCKET='/tmp/fm-camera-continuous.sock'

def request(value,timeout=3):
    sock=socket.socket(socket.AF_UNIX);sock.settimeout(timeout);sock.connect(SOCKET)
    sock.sendall((json.dumps(value)+'\n').encode());return sock

def ensure(config,output,boot,progress):
    import fcntl
    identity=dict(boot_id=boot,mapping_sha256=config['mapping']['sha256'])
    def status():
        with request(dict(identity,action='status')) as sock:
            with sock.makefile('rb') as stream:return json.loads(stream.readline(8192))
    with open('/tmp/fm-camera-continuous-start.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            s=status()
            if s.get('ready') and s.get('backend')=='libuvc_four_camera_v1' and s.get('owner')==os.getppid():return s
            raise RuntimeError('existing camera service is not healthy or belongs to a different board service')
        except (FileNotFoundError,ConnectionRefusedError):pass
        spec=ROOT/('runtime_'+boot+'.json')
        value={'capture':config,'capture_root':str(output.parent)}
        if spec.exists():assert json.loads(spec.read_text())==value,'runtime capture config changed'
        else:spec.write_text(json.dumps(value));spec.chmod(0o600)
        env=os.environ.copy()
        for key in ('LD_PRELOAD','LD_LIBRARY_PATH','PYTHONPATH','FM_PL_DRIVER'):env.pop(key,None)
        for attempt in range(2):
            progress(dict(event='camera_starting',phase='启动四路视频流并预热一次' if attempt==0 else '首次开流未出帧，已恢复设备，正在重试一次',
                startup_attempt=attempt+1,completed_images=0,total_images=24))
            began=time.time();log=(ROOT/'daemon.log').open('ab')
            p=subprocess.Popen([sys.executable,'-u',str(ROOT/'continuous_daemon.py'),str(spec),'--owner',str(os.getppid())],
                 stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,env=env);log.close()
            deadline=time.monotonic()+18
            while time.monotonic()<deadline and p.poll() is None:
                try:
                    s=status()
                    if s.get('ready'):return s
                except (OSError,ValueError):pass
                time.sleep(.05)
            if p.poll() is None:p.terminate();p.wait(timeout=20)
            receipt=json.loads((ROOT/'last_close.json').read_text())
            if not receipt.get('restored') or receipt.get('closed_at',0)<began:
                raise RuntimeError('camera startup failed; driver restoration was not confirmed')
            if attempt==1:raise RuntimeError('camera stream startup failed twice; see daemon.log')
            time.sleep(.3)

def capture_burst(output_dir,boot_id,*,config,progress=None):
    progress=progress or (lambda event:None)
    config,mapping=base.validate_config(dict(config));output=Path(output_dir)
    if base.Platform().boot_id()!=boot_id or config['expected_boot_id']!=boot_id:raise ValueError('boot mismatch')
    if not output.is_absolute() or output.exists():raise ValueError('new absolute capture directory required')
    output=output.resolve();started=time.monotonic();ensure(config,output,boot_id,progress)
    identity=dict(boot_id=boot_id,mapping_sha256=config['mapping']['sha256'])
    with request(dict(identity,action='capture',output_dir=str(output)),timeout=8) as sock:
        with sock.makefile('rb') as stream:
            while True:
                line=stream.readline(32769)
                if not line:raise RuntimeError('camera service ended before complete manifest')
                if len(line)>32768:raise ValueError('camera event too large')
                event=json.loads(line)
                if event.get('event')=='failed':raise RuntimeError(event.get('error','capture failed'))
                event['elapsed_seconds']=time.monotonic()-started;progress(event)
                if event.get('event')=='complete':
                    path=Path(event['capture_manifest']).resolve()
                    if path!=output/'capture.json':raise ValueError('returned manifest path mismatch')
                    return path
