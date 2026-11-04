"""User-requested photo capture and BEV jobs on the existing resident owner."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


class BoardPhotoService:
    def __init__(self,application,hub,config,boot_id,*,capture_fn=None):
        self.app,self.hub,self.config,self.boot_id=application,hub,config,boot_id
        self.capture_root=Path(config['capture_root']).resolve()
        self.output_root=Path(config['output_root']).resolve()
        self.capture_root.mkdir(parents=True,exist_ok=True)
        self.output_root.mkdir(parents=True,exist_ok=True)
        if capture_fn is None:
            capture_fn=self._capture_process
        self.capture_fn=capture_fn
        self.lock=threading.RLock();self._busy=False;self.closing=False
        self.thread=None;self.current=None;self.jobs={};self.assets={};self.capture=None;self.capture_valid=False
        self.capture_cancel_file=None
        self.stream=None;self.warmup_seconds=None
        self.app.arbiter.motion_inhibited=lambda:self._busy

    def _capture_process(self,output_dir,boot_id,*,config,progress):
        request=output_dir.with_suffix('.request.json');cancel=output_dir.with_suffix('.cancel')
        self.capture_cancel_file=cancel
        request.write_text(json.dumps(dict(output_dir=str(output_dir),boot_id=boot_id,config=config,cancel_file=str(cancel))),encoding='utf-8')
        manifest=None;failure=None
        with output_dir.with_suffix('.stderr.log').open('w',encoding='utf-8') as errors:
            process=subprocess.Popen([sys.executable,'-u',str(Path(__file__).with_name('photo_capture_worker.py')),str(request)],
                stdout=subprocess.PIPE,stderr=errors,text=True,encoding='utf-8')
            for line in process.stdout:
                try:
                    if len(line)>32768:raise ValueError('capture_progress_record_too_large')
                    value=json.loads(line)
                    if value.get('event')=='worker_complete':manifest=Path(value['capture_manifest'])
                    else:progress(value)
                except Exception as exc:
                    failure=exc;cancel.touch(exist_ok=True)
                    # Let the current bounded FFmpeg invocation finish. The
                    # worker checks this marker before starting another image.
            code=process.wait()
        self.capture_cancel_file=None
        if failure is not None:raise failure
        if code!=0 or manifest is None:raise RuntimeError('相机采集未完成，请查看采集任务日志')
        return manifest

    @property
    def busy(self):return self._busy

    def _require_stationary(self):
        if self.app.arbiter.status().get('mode')!='stopped':
            raise RuntimeError('请先停止小车，再进行视觉任务')
        state=self.app.arbiter.vehicle_status()
        if state.get('feedback_fresh') is not True or state.get('standstill_observed') is not True:
            raise RuntimeError('尚未收到新鲜静止反馈，未开始视觉任务')

    def status(self):
        with self.lock:
            return dict(available=True,busy=self._busy,boot_id=self.boot_id,
                capture_ready=self.capture is not None and self.capture_valid,
                capture=copy.deepcopy(self.capture['public']) if self.capture else None,
                job=copy.deepcopy(self.current),same_resident_owner=True,automatic_motion=False,
                camera=dict(self.stream.status() if self.stream is not None else dict(ready=False,running=False),
                    strategy=self.config['capture'].get('strategy','baseline'),
                    persistent=self.config['capture'].get('strategy')=='persistent',warmup_seconds=self.warmup_seconds))

    def request(self,action,request_id,stationary_confirmed):
        if action not in ('warmup','capture','infer'):raise ValueError('photo_action_required')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):
            raise ValueError('photo_request_id_required')
        if stationary_confirmed is not True:raise ValueError('请确认小车与场景在采集期间保持静止')
        with self.lock:
            if request_id in self.jobs:
                if self.jobs[request_id]['action']!=action:raise ValueError('photo_request_id_reused')
                return self.status()
            if self.closing or self._busy:raise RuntimeError('视觉任务正在进行，请等待当前任务结束')
            self._require_stationary()
            voice=self.app.voice
            with voice.lock:
                state=voice.status()
                if not state['ready']:raise RuntimeError('请先加载共同驻留模型')
                if state['active'] is not None or state['pending'] or state.get('playing'):
                    raise RuntimeError('当前语音任务尚未结束，请稍后再点')
                if action=='infer' and (self.capture is None or not self.capture_valid):raise RuntimeError('请先成功采集本次照片')
                if state['mode']=='voice' and not state['stopped']:
                    voice.dispatch('set_mode',dict(epoch=state['epoch'],mode='text'))
                epoch=voice.generation.value
                # Every actuator admission checks busy under this same lock.
                # Voice -> arbiter ordering matches local voice mode changes.
                with self.app.arbiter.lock:
                    self._require_stationary()
                    self._busy=True
            job=dict(id=request_id,action=action,state='running',phase='等待麦克风关闭',
                     started_at=time.time(),boot_id=self.boot_id)
            self.current=job;self.jobs[request_id]=job
            if action=='capture':self.capture_valid=False
            self.thread=threading.Thread(target=self._run,args=(job,epoch),daemon=False)
            try:self.thread.start()
            except BaseException:
                with self.app.arbiter.lock:self._busy=False
                job.update(state='failed',error='photo_worker_start_failed')
                raise
            return self.status()

    def _check(self,epoch):
        if self.closing or self.app.voice.generation.value!=epoch:raise RuntimeError('视觉任务已取消')

    def _phase(self,job,epoch,**values):
        self._check(epoch)
        with self.lock:job.update(values,updated_at=time.time())

    def _asset(self,job,path,kind,**metadata):
        path=Path(path).resolve()
        if not any(root in path.parents for root in (self.capture_root,self.output_root)):
            raise ValueError('photo_artifact_outside_workspace')
        if not path.is_file() or path.stat().st_size>16*1024*1024:raise ValueError('photo_artifact_size')
        key=uuid.uuid4().hex+path.suffix
        descriptor=dict(name=key,sha256=digest(path),bytes=path.stat().st_size,kind=kind,**metadata)
        self.assets[key]=(path,descriptor)
        return descriptor

    def _run(self,job,epoch):
        try:
            deadline=time.monotonic()+15
            while True:
                self._check(epoch);state=self.app.voice.status()
                if not state['pending'] and state['active'] is None and not state['mic']['running']:break
                if time.monotonic()>deadline:raise TimeoutError('麦克风尚未关闭，未进行视觉任务')
                time.sleep(.05)
            self._require_stationary()
            if job['action']=='warmup':
                if self.config['capture'].get('strategy')!='persistent':
                    raise RuntimeError('当前使用按需采集路径，不支持提前持续预热')
                self._phase(job,epoch,phase='启动四路视频流并提前预热')
                if self.stream is None or self.stream.closed:
                    from .stream_capture import StreamCapture
                    self.stream=StreamCapture(self.capture_root,self.boot_id,config=self.config['capture'])
                started=time.monotonic();camera=self.stream.start(timeout=15)
                self.warmup_seconds=time.monotonic()-started
                self._check(epoch);result=dict(camera=camera,warmup_seconds=self.warmup_seconds)
            elif job['action']=='capture':
                self._phase(job,epoch,phase='采集本次真实照片',completed_images=0,total_images=24)
                started=time.monotonic()
                def progress(value):self._phase(job,epoch,**value)
                capture_fn=self.capture_fn
                if self.config['capture'].get('strategy')=='persistent':
                    if self.stream is None or not self.stream.status()['ready']:
                        raise RuntimeError('请先预热摄像头；本次没有使用旧照片')
                    capture_fn=self.stream.capture_burst
                manifest=Path(capture_fn(self.capture_root/('camera6_'+uuid.uuid4().hex),self.boot_id,
                    config=self.config['capture'],progress=progress)).resolve()
                if self.capture_root not in manifest.parents:raise ValueError('capture_manifest_outside_workspace')
                # Full JPEG/geometry validation runs in the short-lived camera
                # process and again in the resident model child before BEV.
                # Do not import cv2/NumPy into this long-lived control parent.
                capture=json.loads(manifest.read_text(encoding='utf-8'))
                if capture.get('status')!='complete' or capture.get('board_boot_id')!=self.boot_id or len(capture.get('frames',[]))!=6:
                    raise ValueError('current_complete_capture_required')
                self._check(epoch)
                images=[self._asset(job,manifest.parent/s['path'],'source',slot=s['slot'],device=s['device'],
                         captured_at=s['captured_at']) for s in capture['frames'][-1]['sources']]
                public=dict(frame_id=capture['run_id'],board_boot_id=self.boot_id,images=images,
                    capture_seconds=time.monotonic()-started,manifest_sha256=digest(manifest),
                    capture_method=capture.get('capture_method','sequential_camera_burst'),
                    warmup_seconds=self.warmup_seconds,
                    synchronized_cameras=False,stationary_assumption=True)
                with self.lock:self.capture=dict(path=manifest,public=public);self.capture_valid=True
                result=dict(capture=public)
            else:
                self._phase(job,epoch,phase='板端 BEV 计算；语音模型保持驻留')
                with self.lock:capture=self.capture.copy()
                output=self.output_root/('photo_'+job['id'])
                started=time.monotonic()
                future=self.hub.bev_photo(dict(capture_manifest=str(capture['path']),output_dir=str(output),
                    boot_id=self.boot_id,save_intermediates=False),time.monotonic()+120)
                while True:
                    try:
                        manifest=future.result(timeout=.5);break
                    except FutureTimeout:
                        if future.done():raise
                        # Keep the job busy until the resident operation is
                        # quiescent, even after a user cancels its presentation.
                        try:
                            run=output/'RUN.json'
                            if run.is_file() and run.stat().st_size<=65536:
                                observed=json.loads(run.read_text(encoding='utf-8'))
                                with self.lock:
                                    job.update(model_stage=observed.get('current_stage'),
                                        board_stages=observed.get('stages',[]),updated_at=time.time())
                        except (OSError,ValueError):pass
                self._check(epoch)
                if manifest.get('capture_mode')=='replay':raise RuntimeError('新采集入口不能使用历史回放配置')
                image=self._asset(job,output/manifest['image'],'bev')
                if image['sha256']!=manifest['sha256']:raise ValueError('returned_bev_hash_mismatch')
                report=json.loads((output/'RUN.json').read_text(encoding='utf-8'))
                result=dict(manifest=manifest,image=image,frame_id=capture['public']['frame_id'],
                    inference_seconds=time.monotonic()-started,detection_count=report['detection_count'],
                    capture=capture['public'])
            with self.lock:job.update(state='completed',phase={'warmup':'四路持续取流已预热','capture':'照片已就绪','infer':'本次鸟瞰图已生成'}[job['action']],result=result,completed_at=time.time())
        except Exception as exc:
            with self.lock:job.update(state='failed',phase='本次任务未完成',error=str(exc)[:250],completed_at=time.time())
        finally:
            with self.app.arbiter.lock:self._busy=False

    def artifact(self,name):
        with self.lock:
            if name not in self.assets:raise ValueError('unknown_photo_artifact')
            path,descriptor=self.assets[name]
        with path.open('rb') as stream:raw=stream.read(16*1024*1024+1)
        if hashlib.sha256(raw).hexdigest()!=descriptor['sha256']:raise ValueError('photo_artifact_changed')
        return raw,'image/png' if path.suffix.lower()=='.png' else 'image/jpeg'

    def cancel(self):
        if self.capture_cancel_file is not None:self.capture_cancel_file.touch(exist_ok=True)
        if self._busy:self.app.voice.cancel('photo_cancel')

    def close(self):
        self.closing=True;self.cancel()
        if self.thread is not None:self.thread.join()
        if self.stream is not None:self.stream.close()
