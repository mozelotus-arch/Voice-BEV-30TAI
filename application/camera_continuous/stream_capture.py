"""Persistent low-memory camera process and current-boot burst manifest bridge.

No NPU/CAN/SSH. Start after models load; keep streams warm while the user drives.
Only a caller-confirmed stop may request a fresh stationary snapshot window.
"""
from collections import deque
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
try:
    from . import recording_capture as base
except ImportError:
    if Path(__file__).with_name('recording_capture.py').is_file():
        import recording_capture as base
    else:
        import capture_service as base


def jpeg_dimensions(raw):
    if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):raise ValueError('complete JPEG required')
    position=2
    while position+3<len(raw):
        if raw[position]!=255:raise ValueError('invalid JPEG marker')
        while position<len(raw) and raw[position]==255:position+=1
        marker=raw[position];position+=1
        if marker in (0xd8,0x01) or 0xd0<=marker<=0xd7:continue
        if marker in (0xda,0xd9):break
        length=int.from_bytes(raw[position:position+2],'big')
        if length<2 or position+length>len(raw):raise ValueError('truncated JPEG segment')
        if marker in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf):
            if length<8:raise ValueError('invalid JPEG size header')
            return int.from_bytes(raw[position+3:position+5],'big'),int.from_bytes(raw[position+5:position+7],'big')
        position+=length
    raise ValueError('JPEG dimensions not found')


def assemble_manifest(folder,events,boot_id,mapping,mapping_sha):
    folder=Path(folder);rows=[e for e in events if e.get('event')=='image_captured']
    if len(rows)!=24 or {(r['frame_index'],r['slot']) for r in rows}!={(i,s) for i in range(6) for s in range(4)}:
        raise ValueError('24 distinct actual frame/slot records required')
    cutoffs={r['stationary_cutoff_monotonic_s'] for r in rows}
    if len(cutoffs)!=1:raise ValueError('one stationary request cutoff required')
    cutoff=cutoffs.pop();frames=[];next_tick=cutoff
    if not isinstance(cutoff,(int,float)) or not math.isfinite(cutoff):raise ValueError('finite request cutoff required')
    for index in range(6):
        sources=[];timestamps=[]
        for slot in range(4):
            row=next(r for r in rows if r['frame_index']==index and r['slot']==slot)
            camera=mapping['cameras'][slot];name='frame%d_slot%d.jpg'%(index,slot)
            if row['run_id']!=folder.name or row['path']!=name:raise ValueError('frame identity/path mismatch')
            if row['device']!=camera['device'] or row['physical_camera']!=camera['physical_camera'] or ('/'+camera['usb_port']+'/') not in row['sysfs']:
                raise ValueError('physical camera source mismatch')
            stamp=row['driver_monotonic_s'];received=row['received_monotonic_s']
            written=row['written_monotonic_s']
            if not all(math.isfinite(t) for t in (stamp,received,written)):raise ValueError('finite frame timestamps required')
            if (row['v4l2_flags']&0xe000)!=0x2000 or not next_tick-1e-9<=stamp<=received+.02 or received-stamp>.20 or written<received:
                raise ValueError('pre-stop/stale/non-monotonic-clock frame rejected')
            if index:
                prior=frames[-1]['sources'][slot]
                if stamp<=prior['source_driver_monotonic_s'] or row['sequence']==prior['source_sequence']:
                    raise ValueError('repeated source frame')
            raw=(folder/name).read_bytes()
            if len(raw)>512*1024 or jpeg_dimensions(raw)!=(240,320):raise ValueError('bounded 320x240 JPEG required')
            sources.append(dict(slot=slot,physical_camera=camera['physical_camera'],device=camera['device'],
                sysfs=row['sysfs'],path=name,sha256=hashlib.sha256(raw).hexdigest(),
                captured_at=row.get('board_event_received_at',datetime.datetime.now(datetime.timezone.utc).isoformat()),
                captured_monotonic_s=received,capture_started_monotonic_s=cutoff,
                source_driver_monotonic_s=stamp,source_sequence=row['sequence'],v4l2_timestamp_flags=row['v4l2_flags'],
                file_written_monotonic_s=row['written_monotonic_s'],stationary_cutoff_monotonic_s=cutoff))
            timestamps.append(stamp)
        if max(timestamps)-min(timestamps)>.10:raise ValueError('cross-camera skew exceeded 100ms')
        frames.append(dict(frame_index=index,sources=sources,source_skew_seconds=max(timestamps)-min(timestamps)))
        next_tick=max(timestamps)+.5
    manifest=dict(schema='FM-CAMERA-BURST/1',run_id=folder.name,board_boot_id=boot_id,real_capture=True,
        stationary_camera_rig=True,stationary_scene_required=True,camera_mapping_confirmed=True,
        mapping_evidence=mapping['confirmed_by'],mapping_sha256=mapping_sha,mapping_source_boot_id=mapping.get('board_boot_id'),
        active_slots=[0,1,2,3],timestamp_kind='V4L2 declared monotonic source timestamp plus board dequeued completion; wall labels are receipt times',
        synchronized_cameras=False,capture_method='persistent_v4l2_mjpeg_post_stop_window_v1',
        selected_time_indices=[5,4,2,0],stationary_cutoff_monotonic_s=cutoff,
        capture_started_monotonic_s=cutoff,capture_completed_monotonic_s=max(r['written_monotonic_s'] for r in rows),
        maximum_source_skew_seconds=max(f['source_skew_seconds'] for f in frames),frames=frames,status='complete')
    path=folder/'capture.json';base.save_json(path,manifest);return path


class StreamCapture:
    def __init__(self,capture_root,boot_id,*,config):
        if config.get('expected_boot_id',boot_id)!=boot_id:raise ValueError('configured boot differs from service boot')
        self.config=dict(config);self.config['expected_boot_id']=boot_id
        self.config,self.mapping=base.validate_config(self.config)
        if self.config['camera_lock']!='/tmp/fm-camera-capture.lock':raise ValueError('native process uses the shared /tmp/fm-camera-capture.lock')
        self.root=Path(capture_root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.boot_id=boot_id;self.platform=base.Platform()
        binding=self.config['native_binary'];self.binary=Path(binding['path'])
        if not self.binary.is_absolute() or base.sha(self.binary)!=binding['sha256']:
            raise ValueError('native capture binary identity mismatch')
        self.condition=threading.Condition(threading.RLock());self.capture_lock=threading.Lock()
        self.close_lock=threading.Lock();self.close_receipt=None;self.closing=False
        self.process=None;self.reader=None;self.error=None;self.ready=False;self.last_status=None
        self.pending=None;self.events=deque(maxlen=128);self.stderr=None;self.closed=False

    def _send(self,command):
        with self.condition:
            if self.process is None or self.process.poll() is not None:raise RuntimeError('camera stream process not running')
            self.process.stdin.write(command+'\n');self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line)>65536:raise ValueError('oversized camera event')
                event=json.loads(line);event['board_event_received_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
                callback=None
                with self.condition:
                    self.events.append(event);kind=event.get('event')
                    if kind=='ready':self.ready=True
                    if kind=='status':self.last_status=event;self.ready=event.get('ready') is True
                    if kind in ('fatal','closed'):
                        self.ready=False
                        if kind=='fatal':self.error=event.get('error','camera fatal error')
                    pending=self.pending
                    if pending and event.get('run_id')==pending['run_id']:
                        pending['events'].append(event);callback=pending['progress']
                    self.condition.notify_all()
                if callback:
                    try:callback(dict(event,phase=kind,message='正在选取停车后的新画面',
                        completed_images=sum(e.get('event')=='image_captured' for e in pending['events']),total_images=24))
                    except Exception as exc:
                        with self.condition:pending['error']=str(exc);pending['done']=True;self.condition.notify_all()
                        self._send('CANCEL')
                with self.condition:
                    if pending and event.get('run_id')==pending['run_id'] and kind in ('capture_complete','capture_failed'):
                        pending['done']=True
                        if kind=='capture_failed':pending['error']=event.get('error','capture failed')
                        self.condition.notify_all()
        except Exception as exc:
            with self.condition:self.error=str(exc);self.ready=False;self.condition.notify_all()
        finally:
            with self.condition:
                self.ready=False
                if self.pending and not self.pending['done']:self.pending.update(done=True,error='camera stream ended')
                self.condition.notify_all()

    def start(self,timeout=15):
        try:return self._start(timeout)
        except BaseException:
            self.close()
            raise

    def _start(self,timeout):
        if self.platform.boot_id()!=self.boot_id:raise ValueError('current boot changed')
        for camera in self.mapping['cameras']:self.platform.camera_identity(camera)
        with self.condition:
            if self.closed or self.closing:raise RuntimeError('camera service closed or closing')
            if self.process is None:
                self.stderr=(self.root/('stream_'+uuid.uuid4().hex+'.stderr.log')).open('w',encoding='utf-8')
                self.process=subprocess.Popen([str(self.binary),str(self.root)]+[c['device'] for c in self.mapping['cameras']],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,text=True,encoding='utf-8',bufsize=1)
                self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()
            deadline=time.monotonic()+timeout
            while not self.ready:
                if self.error or self.process.poll() is not None:raise RuntimeError(self.error or 'camera stream exited')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('camera streams did not become warm and fresh')
                self.condition.wait(min(.1,remaining))
        return self.status()

    def status(self):
        with self.condition:
            return dict(ready=self.ready,error=self.error,pid=self.process.pid if self.process else None,
                running=self.process is not None and self.process.poll() is None,
                phase='持续取流就绪' if self.ready else '摄像头预热或等待新帧',last_native_status=self.last_status,
                pending=self.pending['run_id'] if self.pending else None,hardware_motion=False,npu_opened=False)

    def capture_burst(self,output_dir,boot_id,*,config=None,progress=None,timeout=12):
        if boot_id!=self.boot_id or self.platform.boot_id()!=self.boot_id:raise ValueError('capture boot changed')
        output=Path(output_dir).resolve()
        if output.parent!=self.root or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',output.name) or output.exists():
            raise ValueError('fresh capture directory directly under configured root required')
        with self.capture_lock:
            with self.condition:
                if not self.ready:raise RuntimeError('请先启动并预热摄像头')
                pending=dict(run_id=output.name,events=[],done=False,error=None,progress=progress);self.pending=pending
            try:
                self._send('CAPTURE '+output.name);deadline=time.monotonic()+timeout
                with self.condition:
                    while not pending['done']:
                        if self.error:raise RuntimeError(self.error)
                        remaining=deadline-time.monotonic()
                        if remaining<=0:self._send('CANCEL');raise TimeoutError('fresh stationary camera window timed out')
                        self.condition.wait(min(.1,remaining))
                if pending['error']:raise RuntimeError(pending['error'])
                return assemble_manifest(output,pending['events'],self.boot_id,self.mapping,self.config['mapping']['sha256'])
            finally:
                with self.condition:self.pending=None

    def close(self):
        with self.close_lock:
            with self.condition:
                if self.closed:return dict(self.close_receipt)
                self.closing=True;self.ready=False
            forced=False;errors=[]
            try:
                if self.process is not None:
                    if self.process.poll() is None:
                        try:self._send('STOP')
                        except (OSError,RuntimeError,ValueError) as exc:errors.append('STOP: '+str(exc))
                    try:self.process.stdin.close()
                    except (OSError,ValueError) as exc:errors.append('stdin close: '+str(exc))
                    for action in (None,'terminate','kill'):
                        if self.process.poll() is not None:break
                        if action:
                            forced=True
                            try:getattr(self.process,action)()
                            except OSError as exc:errors.append(action+': '+str(exc))
                        try:self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:continue
                        except OSError as exc:errors.append('wait: '+str(exc))
            finally:
                if self.reader:self.reader.join(timeout=2)
                if self.stderr:
                    try:self.stderr.close()
                    except OSError as exc:errors.append('stderr close: '+str(exc))
                with self.condition:
                    self.closed=self.process is None or self.process.poll() is not None
                    self.close_receipt=dict(closed=self.closed,exitcode=self.process.returncode if self.process else None,
                        forced_camera_process_termination=forced,close_errors=errors,
                        reader_stopped=self.reader is None or not self.reader.is_alive(),
                        native_closed_receipt=any(e.get('event')=='closed' and e.get('camera_close_verified') is True for e in self.events))
            return dict(self.close_receipt)
