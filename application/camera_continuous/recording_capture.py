"""Camera-only adapter of the verified serial 24-photo capture procedure.

No NPU, DeviceOwner, model, microphone, CAN, motion, SSH or service control.
The invoking service pins this script/config and confirms a stationary scene.
"""
import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

CAMERAS=((0,'front','1-1.2.3'),(1,'right','1-1.1.4'),
         (2,'left','1-1.1.3'),(3,'back','1-1.2.4'))
SCHEMA='FM-RECORDING-CAPTURE-CONFIG/1'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path,value):
    path=Path(path);temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temp.replace(path)


def emit(value):print(json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)


def validate_config(config):
    if config.get('schema')!=SCHEMA:raise ValueError('capture config schema')
    uuid.UUID(config['expected_boot_id'])
    for key in ('ffmpeg','camera_lock'):
        if not Path(config[key]).is_absolute():raise ValueError('absolute '+key+' required')
    binding=config['mapping']
    if not Path(binding['path']).is_absolute() or sha(binding['path'])!=binding['sha256']:
        raise ValueError('camera mapping identity mismatch')
    mapping=json.loads(Path(binding['path']).read_text(encoding='utf-8'))
    if mapping.get('schema')!='FM-CAMERA-MAPPING/1' or mapping.get('camera_mapping_confirmed') is not True:
        raise ValueError('confirmed mapping required')
    cameras=mapping['cameras']
    if [(c['slot'],c['physical_camera'],c.get('usb_port')) for c in cameras]!=list(CAMERAS):
        raise ValueError('confirmed slot/direction/physical USB port mapping required')
    devices=[c.get('device','') for c in cameras]
    if len(set(devices))!=4 or not all(isinstance(d,str) and re.fullmatch(r'/dev/video[0-9]+',d) for d in devices):
        raise ValueError('four distinct actual video capture nodes required')
    if not mapping.get('confirmed_by') or any(not c.get('usb_port') for c in cameras):
        raise ValueError('camera physical identity required')
    return config,mapping


def load_config(path):
    return validate_config(json.loads(Path(path).read_text(encoding='utf-8')))


class Platform:
    def boot_id(self):return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def camera_identity(self,camera):
        path=(Path('/sys/class/video4linux')/Path(camera['device']).name).resolve(strict=True)
        if camera['usb_port'] not in path.parts:
            raise ValueError('USB physical camera identity mismatch: '+camera['device'])
        return str(path)

    @contextmanager
    def camera_lock(self,path):
        import fcntl
        with open(path,'a+') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield

    def capture_one(self,ffmpeg,camera,destination):
        # Preserve the already board-tested resolution, format, warmup and JPEG
        # quality. Every image comes from a separate actual capture invocation.
        command=[ffmpeg,'-hide_banner','-loglevel','warning','-f','v4l2',
            '-input_format','mjpeg','-video_size','320x240','-framerate','30',
            '-i',camera['device'],'-vf',r'select=gte(t\,5)',
            '-frames:v','1','-q:v','3',str(destination)]
        log=destination.with_suffix('.log')
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=20)
            log.write_text(result.stderr,encoding='utf-8')
        except subprocess.TimeoutExpired as exc:
            detail=exc.stderr or b''
            log.write_bytes(detail if isinstance(detail,bytes) else detail.encode())
            raise RuntimeError('camera capture timed out: '+camera['device']) from exc
        if result.returncode:
            raise RuntimeError('camera capture failed: '+camera['device']+'; '+result.stderr[-1000:])

    def validate_jpeg(self,path):
        import cv2
        image=cv2.imread(str(path),cv2.IMREAD_COLOR)
        if image is None or image.shape!=(240,320,3):
            raise ValueError('capture must decode to 320x240 BGR')


def _capture(config,mapping,*,output=None,platform=None,progress=emit):
    platform=platform or Platform()
    boot=platform.boot_id()
    if boot!=config['expected_boot_id']:raise ValueError('current board boot mismatch')
    start=time.monotonic()
    # The physical mapping is reusable across boots only after each current USB
    # route has been rechecked. Its historical boot field is never rewritten.
    with platform.camera_lock(config['camera_lock']):
        for camera in mapping['cameras']:platform.camera_identity(camera)
        if output is None:
            if not Path(config['output_root']).is_absolute():raise ValueError('absolute output_root required')
            output=Path(config['output_root']).resolve()/('camera6_live_'+str(time.time_ns()))
        else:
            output=Path(output)
            if not output.is_absolute():raise ValueError('absolute capture output directory required')
            output=output.resolve()
        run_id=output.name
        output.mkdir(parents=True,exist_ok=False)
        manifest=dict(schema='FM-CAMERA-BURST/1',run_id=run_id,board_boot_id=boot,
            real_capture=True,stationary_camera_rig=True,camera_mapping_confirmed=True,
            mapping_evidence=mapping['confirmed_by'],mapping_sha256=config['mapping']['sha256'],
            mapping_source_boot_id=mapping.get('board_boot_id'),active_slots=[0,1,2,3],
            timestamp_kind='board_completed_capture_not_exposure',synchronized_cameras=False,
            capture_method='serial_ffmpeg_per_image_5s_warmup_v1',frames=[],status='capturing',
            capture_started_monotonic_s=start,stationary_scene_required=True)
        path=output/'capture.json';save_json(path,manifest)
        count=0
        def event(name,**fields):
            progress(dict(schema='FM-CAMERA-CAPTURE-PROGRESS/1',event=name,run_id=run_id,
                phase=name,message=('采集完成' if name=='complete' else '正在采集' if name!='failed' else '采集失败')+' %d/24'%count,
                completed_images=count,total_images=24,elapsed_seconds=time.monotonic()-start,**fields))
        event('capture_started',capture_manifest=str(path))
        try:
            for index in range(6):
                sources=[]
                for camera in mapping['cameras']:
                    identity=platform.camera_identity(camera)
                    destination=output/('frame%d_slot%d.jpg'%(index,camera['slot']))
                    started=time.monotonic()
                    platform.capture_one(config['ffmpeg'],camera,destination)
                    completed=time.monotonic()
                    completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    platform.validate_jpeg(destination)
                    source=dict(slot=camera['slot'],physical_camera=camera['physical_camera'],
                        device=camera['device'],sysfs=identity,captured_at=completed_at,
                        captured_monotonic_s=completed,capture_started_monotonic_s=started,
                        sha256=sha(destination),path=destination.name)
                    sources.append(source);count+=1
                    event('image_captured',frame_index=index,slot=camera['slot'],source=source)
                manifest['frames'].append(dict(frame_index=index,sources=sources))
                save_json(path,manifest);event('batch_complete',frame_index=index)
            manifest.update(status='complete',capture_completed_monotonic_s=time.monotonic())
            save_json(path,manifest)
            event('complete',success=True,capture_manifest=str(path),board_boot_id=boot)
            return dict(success=True,capture_manifest=str(path),run_id=run_id,board_boot_id=boot,images=24)
        except BaseException as exc:
            manifest.update(status='failed',error=repr(exc),completed_images=count)
            save_json(path,manifest);event('failed',success=False,error=str(exc),capture_manifest=str(path))
            raise


def capture(config_path,*,platform=None,progress=emit):
    config,mapping=load_config(config_path)
    return _capture(config,mapping,platform=platform,progress=progress)


def capture_burst(output_dir,boot_id,*,config,progress=None,_platform=None):
    """Service API. Caller supplies a fixed trusted config and unused job path.

    A successful Path return means all 24 actual camera invocations completed.
    The service still validates the manifest before submitting it to BEV.
    """
    effective=dict(config)
    if effective.get('expected_boot_id',boot_id)!=boot_id:
        raise ValueError('configured boot differs from service boot')
    effective['expected_boot_id']=boot_id
    effective,mapping=validate_config(effective)
    result=_capture(effective,mapping,output=output_dir,platform=_platform,progress=progress or (lambda event:None))
    return Path(result['capture_manifest'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    capture(args.config)
