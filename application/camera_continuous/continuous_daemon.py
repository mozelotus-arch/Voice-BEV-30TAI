"""Camera-only local service with a bounded RAM ring and five minute idle release."""
import argparse,datetime,hashlib,json,os,re,signal,socket,subprocess,sys,time,uuid
from pathlib import Path
import recording_capture as base
from stream_capture import jpeg_dimensions

SOCKET='/tmp/fm-camera-continuous.sock'
ROOT=Path(__file__).resolve().parent
closing=False

def stop(*_):
    global closing
    closing=True

def resolve_camera_nodes(cameras,timeout=8):
    """USB port is identity; video node numbers can change after libuvc closes."""
    for camera in cameras:
        port=camera['usb_port'];usb=Path('/sys/bus/usb/devices')/port
        if not usb.exists():raise RuntimeError('camera USB port disconnected: '+port)
        if (usb/'idVendor').read_text().strip()!='05a3' or (usb/'idProduct').read_text().strip()!='9230':
            raise RuntimeError('camera USB identity changed: '+port)
        interface=Path(str(usb)+':1.0')
        if not (interface/'driver').exists():
            Path('/sys/bus/usb/drivers/uvcvideo/bind').write_text(port+':1.0')
        elif (interface/'driver').resolve()!=Path('/sys/bus/usb/drivers/uvcvideo'):
            raise RuntimeError('unexpected camera kernel driver: '+port)
    deadline=time.monotonic()+timeout
    while True:
        resolved=[]
        for camera in cameras:
            nodes=[p for p in Path('/sys/class/video4linux').glob('video*')
                   if camera['usb_port'] in p.resolve().parts and (p/'index').read_text().strip()=='0']
            if len(nodes)!=1:break
            resolved.append(dict(camera,device='/dev/'+nodes[0].name))
        if len(resolved)==len(cameras):return resolved
        if time.monotonic()>deadline:raise RuntimeError('camera capture nodes did not return')
        time.sleep(.1)


def frame(ring,index):
    p=ring/('cam%d.json'%index)
    first=p.read_bytes();meta=json.loads(first)
    if not re.fullmatch(r'cam%d_[0-7]\.jpg'%index,meta['file']):raise ValueError('ring filename')
    raw=(ring/meta['file']).read_bytes()
    if p.read_bytes()!=first:raise ValueError('ring advanced during read')
    stamp=meta['observed_monotonic_s']
    if not 0<=time.monotonic()-stamp<=.25:raise ValueError('stale source')
    if len(raw)!=meta['bytes'] or len(raw)>512*1024 or jpeg_dimensions(raw)!=(240,320):raise ValueError('invalid JPEG')
    return meta,raw

def capture(request,stream,ring,ordered,identities,mapping,cfg,capture_root,boot):
    output=Path(request['output_dir']).resolve()
    if output.parent!=capture_root or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',output.name):raise ValueError('output outside capture root')
    cancel=output.with_suffix('.cancel');output.mkdir(exist_ok=False)
    cutoff=time.monotonic();next_after=cutoff+.3;frames=[];count=0
    manifest=dict(schema='FM-CAMERA-BURST/1',run_id=output.name,board_boot_id=boot,real_capture=True,
        stationary_camera_rig=True,stationary_scene_required=True,camera_mapping_confirmed=True,
        mapping_evidence=mapping['confirmed_by'],mapping_sha256=cfg['mapping']['sha256'],mapping_source_boot_id=mapping.get('board_boot_id'),
        active_slots=[0,1,2,3],timestamp_kind='native_UVC_completed_frame_receipt_CLOCK_MONOTONIC_not_exposure',
        synchronized_cameras=False,capture_method='persistent_libuvc_four_camera_consecutive_v1',
        source_transport='libuvc MJPEG 320x240, 800 bytes per microframe',source_frame_interval_seconds=0,
        warmup_policy='once per stream start; idle release after 300 seconds',stationary_cutoff_monotonic_s=cutoff,
        post_stop_guard_seconds=.3,capture_started_monotonic_s=cutoff,frames=frames,status='capturing')
    path=output/'capture.json'
    def emit(kind,**extra):
        stream.write((json.dumps(dict(event=kind,completed_images=count,total_images=24,
            phase='从四路持续视频流选取新帧',elapsed_seconds=time.monotonic()-cutoff,**extra),ensure_ascii=False)+'\n').encode());stream.flush()
    try:
        emit('capture_started')
        deadline=time.monotonic()+5
        previous={}
        for index in range(6):
            while True:
                if closing or cancel.exists():raise InterruptedError('capture cancelled')
                if time.monotonic()>deadline:raise TimeoutError('fresh four-camera window exceeded five seconds')
                try:
                    selected=[frame(ring,i) for i in range(4)]
                    times=[item[0]['observed_monotonic_s'] for item in selected]
                    if min(times)<next_after or max(times)-min(times)>.08:raise ValueError('awaiting fresh aligned group')
                    if any(meta['sequence']==previous.get(i) for i,(meta,_) in enumerate(selected)):raise ValueError('repeated frame')
                    break
                except (OSError,ValueError,KeyError):time.sleep(.005)
            sources=[]
            for i,(meta,raw) in enumerate(selected):
                camera=ordered[i];slot=camera['slot'];dest=output/('frame%d_slot%d.jpg'%(index,slot));dest.write_bytes(raw)
                sources.append(dict(slot=slot,physical_camera=camera['physical_camera'],device=camera['device'],
                    sysfs=identities[slot],path=dest.name,sha256=hashlib.sha256(raw).hexdigest(),
                    captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    captured_monotonic_s=meta['observed_monotonic_s'],capture_started_monotonic_s=cutoff,
                    source_sequence=meta['sequence'],source_receipt_monotonic_s=meta['observed_monotonic_s'],
                    file_written_monotonic_s=time.monotonic(),physical_usb_port=camera['usb_port']))
                previous[i]=meta['sequence'];count+=1;emit('image_captured',slot=slot,frame_index=index)
            frames.append(dict(frame_index=index,sources=sorted(sources,key=lambda s:s['slot']),source_skew_seconds=max(times)-min(times)))
            next_after=max(times)+.001
        manifest.update(status='complete',capture_completed_monotonic_s=time.monotonic(),
            maximum_source_skew_seconds=max(f['source_skew_seconds'] for f in frames))
        base.save_json(path,manifest);emit('complete',capture_manifest=str(path),success=True)
    except Exception as e:
        manifest.update(status='failed',error=repr(e));base.save_json(path,manifest);raise

def main():
    ap=argparse.ArgumentParser();ap.add_argument('config');ap.add_argument('--owner',type=int,required=True);a=ap.parse_args()
    spec=json.loads(Path(a.config).read_text());cfg,mapping=base.validate_config(spec['capture'])
    boot=cfg['expected_boot_id'];platform=base.Platform()
    if platform.boot_id()!=boot:raise ValueError('boot mismatch')
    owner=Path('/proc')/str(a.owner)/'stat';owner_start=owner.read_text().split()[21]
    capture_root=Path(spec['capture_root']).resolve()
    ring=Path('/run')/('fm-camera-ring-'+uuid.uuid4().hex);ring.mkdir(mode=0o700)
    ordered=[]
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    child=None;server=None;bound=False
    with platform.camera_lock(cfg['camera_lock']):
        ordered=sorted(resolve_camera_nodes(mapping['cameras']),key=lambda c:int(c['device'][10:]))
        identities={c['slot']:platform.camera_identity(c) for c in ordered}
        args=[]
        for c in ordered:
            usb=Path('/sys/bus/usb/devices')/c['usb_port']
            assert (usb/'idVendor').read_text().strip()=='05a3' and (usb/'idProduct').read_text().strip()=='9230'
            args.append((usb/'devnum').read_text().strip()+':'+c['device'][10:])
        binding=json.loads((ROOT/'release.json').read_text())
        for name,sha in binding['files'].items():assert base.sha(ROOT/name)==sha,'camera release identity: '+name
        try:
            child=subprocess.Popen([str(ROOT/'ring_stream'),str(ring)]+args,stdin=subprocess.PIPE)
            deadline=time.monotonic()+15
            while True:
                if child.poll() is not None:raise RuntimeError('camera stream startup failed')
                try:
                    for i in range(4):frame(ring,i)
                    break
                except (OSError,ValueError,KeyError):
                    if time.monotonic()>deadline:raise TimeoutError('four streams did not become ready')
                    time.sleep(.02)
            if Path(SOCKET).exists():Path(SOCKET).unlink()
            server=socket.socket(socket.AF_UNIX);server.bind(SOCKET);bound=True;os.chmod(SOCKET,0o600);server.listen(2);server.settimeout(1)
            last_capture=time.monotonic()
            while not closing and time.monotonic()-last_capture<300:
                if not owner.exists() or owner.read_text().split()[21]!=owner_start:break
                if child.poll() is not None:raise RuntimeError('camera stream stopped')
                try:conn,_=server.accept()
                except socket.timeout:continue
                with conn:
                    conn.settimeout(8)
                    with conn.makefile('rwb') as stream:
                        try:
                            line=stream.readline(8193)
                            if len(line)>8192:raise ValueError('request too large')
                            req=json.loads(line)
                            if req.get('boot_id')!=boot or req.get('mapping_sha256')!=cfg['mapping']['sha256']:raise ValueError('request identity mismatch')
                            action=req.get('action')
                            if action=='status':
                                healthy=True
                                try:
                                    for i in range(4):frame(ring,i)
                                except (OSError,ValueError,KeyError):healthy=False
                                value=dict(ready=healthy,boot_id=boot,mapping_sha256=cfg['mapping']['sha256'],pid=os.getpid(),owner=a.owner,
                                    backend='libuvc_four_camera_v1',ring=str(ring),idle_seconds=time.monotonic()-last_capture)
                                stream.write((json.dumps(value)+'\n').encode());stream.flush()
                            elif action=='capture':
                                last_capture=time.monotonic();capture(req,stream,ring,ordered,identities,mapping,cfg,capture_root,boot)
                            elif action=='close':stop();stream.write(b'{"closing":true}\n');stream.flush()
                            else:raise ValueError('unsupported camera operation')
                        except Exception as exc:
                            try:stream.write((json.dumps({'event':'failed','error':str(exc)})+'\n').encode());stream.flush()
                            except OSError:pass
        finally:
            if server:server.close()
            if bound:Path(SOCKET).unlink(missing_ok=True)
            if child and child.poll() is None:
                child.stdin.close()
                try:child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.terminate();child.wait(timeout=5)
            restored=True;errors=[]
            try:
                for c in resolve_camera_nodes(mapping['cameras']):platform.camera_identity(c)
            except (OSError,ValueError,RuntimeError,KeyError) as exc:restored=False;errors.append(str(exc))
            receipt=dict(restored=restored,errors=errors,audio_present='UACDemo' in Path('/proc/asound/cards').read_text(),closed_at=time.time())
            base.save_json(ROOT/'last_close.json',receipt)
            # Only files created by this process, within its freshly generated RAM directory.
            for p in ring.iterdir():
                if p.is_file() and (re.fullmatch(r'cam[0-3]_[0-7]\.jpg',p.name) or re.fullmatch(r'cam[0-3](\.json|_image\.tmp|_meta\.tmp)',p.name)):p.unlink()
            ring.rmdir()

if __name__=='__main__':main()
