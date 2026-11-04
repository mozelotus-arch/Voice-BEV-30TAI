"""Authenticated LAN photo jobs and verified board artifacts; no local inference."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request
from fm_autonomy.local_voice_proxy import VoiceHTTPClient


class PhotoBridge:
    def __init__(self,config_path,folder):
        self.config=json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.folder=Path(folder).resolve();self.folder.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.generation=0;self.thread=None;self.current=None
        self.capture=None;self.sources={};self.client=None;self.board_instance=None
        self.closing=False;self.seen={};self.capture_request_id=None;self.camera=None;self.capture_valid=False

    def record_timing(self,report):
        """Browser-reported timing, bound to the current verified result.

        This is a browser rendering-opportunity receipt, not a screen sensor.
        """
        if not isinstance(report,dict):raise ValueError('timing_report_required')
        allowed={'capture_id','infer_id','frame_id','sha256','marks','hidden_during_wait','visible_at_completion','clock','boundary','one_shot'}
        if set(report)!=allowed:raise ValueError('invalid_timing_fields')
        with self.lock:
            job=self.current or {};result=job.get('result') or {}
            if (job.get('state')!='completed' or job.get('action')!='infer' or job.get('id')!=report['infer_id'] or
                self.capture_request_id!=report['capture_id'] or result.get('frame_id')!=report['frame_id'] or
                result.get('image',{}).get('sha256')!=report['sha256']):raise ValueError('timing_result_identity_mismatch')
            marks=report['marks']
            order=('trigger_ms','capture_received_ms','infer_trigger_ms','result_received_ms','decoded_ms','presentation_ready_ms')
            if not isinstance(marks,dict) or set(marks)!=set(order):raise ValueError('timing_marks_required')
            values=[marks[k] for k in order]
            if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=3600000 for v in values) or values!=sorted(values) or values[0]!=0:
                raise ValueError('invalid_timing_sequence')
            if (report['clock']!='browser.performance.now' or report['boundary']!='trigger_to_decoded_visible_two_animation_frames' or
                report['visible_at_completion'] is not True or type(report['one_shot']) is not bool or type(report['hidden_during_wait']) is not bool):
                raise ValueError('invalid_timing_boundary')
            record=dict(report,source='browser_report',board_capture_seconds=result['capture'].get('capture_seconds'),
                        board_inference_seconds=result.get('inference_seconds'),recorded_unix_s=time.time())
            with (self.folder/'browser_timing.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
            return dict(recorded=True,frame_id=report['frame_id'])

    def status(self):
        with self.lock:return dict(available=True,busy=self.current is not None and self.current['state']=='running',
            capture_ready=self.capture is not None and self.capture_valid,capture=copy.deepcopy(self.capture),job=copy.deepcopy(self.current),camera=copy.deepcopy(self.camera))

    def request(self,action,request_id,stationary_confirmed):
        if action not in ('warmup','capture','infer'):raise ValueError('photo_action_required')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):raise ValueError('photo_request_id_required')
        if stationary_confirmed is not True:raise ValueError('请确认小车与场景保持静止')
        with self.lock:
            if request_id in self.seen:
                if self.seen[request_id]!=action:raise ValueError('photo_request_id_reused')
                return self.status()
            if self.closing or self.status()['busy']:raise RuntimeError('视觉任务正在进行，请等待完成')
            if action=='infer' and not self.capture_valid:raise RuntimeError('请先成功采集本次照片')
            if action=='capture':self.capture_valid=False
            self.seen[request_id]=action
            self.generation+=1;generation=self.generation
            self.current=dict(id=request_id,action=action,state='running',phase='连接板端视觉入口')
            self.thread=threading.Thread(target=self._run,args=(action,request_id,generation),daemon=False)
            self.thread.start();return self.status()

    def _check(self,generation):
        if self.closing or generation!=self.generation:raise RuntimeError('视觉请求已取消')

    def _json(self,name,value):
        path=self.folder/name;temporary=path.with_suffix(path.suffix+'.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)

    def _download(self,client,descriptor,generation,filename=None):
        self._check(generation)
        name=descriptor['name'];expected=descriptor['sha256']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or not re.fullmatch(r'[0-9a-f]{64}',expected):raise ValueError('invalid_board_artifact')
        size=descriptor['bytes']
        if type(size) is not int or not 0<size<=16*1024*1024:raise ValueError('board_artifact_size')
        request=Request(client.base+'/photo/artifact?'+urlencode(dict(name=name)),headers={'X-Voice-Token':client.token})
        with client.opener.open(request,timeout=15) as response:raw=response.read(size+1)
        self._check(generation)
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('板端图像传输校验失败')
        filename=filename or name
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',filename):raise ValueError('invalid_board_filename')
        path=self.folder/filename;temporary=path.with_suffix(path.suffix+'.tmp')
        temporary.write_bytes(raw);temporary.replace(path)
        return path

    def _capture(self,client,capture,generation):
        rows=[]
        for descriptor in capture['images']:
            path=self._download(client,descriptor,generation)
            self.sources[descriptor['sha256']]=path
            rows.append(dict(descriptor,image_url='/api/bev/source?sha256='+descriptor['sha256']))
        with self.lock:self.capture=dict(capture,images=rows)
        self._json('capture_preview.json',self.capture)

    def _run(self,action,request_id,generation):
        try:
            client=VoiceHTTPClient(self.config);health=client.read('/health')
            self._check(generation)
            if not health.get('photo_available'):raise RuntimeError('板端尚未部署共驻视觉入口')
            instance=health['instance_id'];boot=health['boot_id']
            with self.lock:self.client=client;self.board_instance=instance
            self._check(generation)
            state=client.read('/photo/command',dict(instance_id=instance,action=action,id=request_id,stationary_confirmed=True))
            deadline=time.monotonic()+360
            while True:
                self._check(generation)
                if state.get('boot_id')!=boot:raise RuntimeError('板端已重新启动，请重新准备本次照片')
                job=state.get('job') or {}
                if job.get('id')!=request_id:raise RuntimeError('板端视觉任务已变化；未接收其他任务结果')
                with self.lock:
                    self._check(generation)
                    self.camera=copy.deepcopy(state.get('camera'))
                    self.current=copy.deepcopy(job)
                    if job['state']=='completed':self.current.update(state='running',phase='接收并校验板端图像')
                if job['state'] in ('completed','failed'):break
                if time.monotonic()>deadline:raise TimeoutError('等待板端视觉任务超时，没有重试')
                time.sleep(.5);state=client.read('/photo/status')
            if job['state']!='completed':raise RuntimeError(job.get('error','板端任务未完成'))
            with self.lock:self.current.update(state='running',phase='接收并校验板端图像')
            result=job['result']
            if action!='warmup':self._capture(client,result['capture'],generation)
            if action=='infer':
                manifest=result['manifest']
                if manifest.get('generated_on')!='board' or manifest['frame_id']!=result['capture']['frame_id']:
                    raise ValueError('板端图像来源与本次照片不一致')
                if manifest['sha256']!=result['image']['sha256']:raise ValueError('board_manifest_hash_mismatch')
                self._download(client,result['image'],generation,manifest['image'])
                self._check(generation)
                self._json('display_details.json',dict(image_sha256=manifest['sha256'],detection_count=result['detection_count']))
                self._json('latest.json',manifest)
            with self.lock:
                self._check(generation)
                self.current=copy.deepcopy(job)
                if action=='capture':self.capture_request_id=request_id;self.capture_valid=True
        except Exception as exc:
            with self.lock:
                if generation==self.generation:
                    message=str(exc)[:250]
                    if self.current.get('phase')=='连接板端视觉入口' and isinstance(exc,(OSError,TimeoutError)):
                        message='无法连接板端网线接口。请接回原网线，等待有线网络恢复后重新点击采集；无需重新加载模型。'
                    self.current.update(state='failed',phase='本次任务未完成',error=message)

    def source(self,sha):
        with self.lock:path=self.sources.get(sha)
        if path is None:raise ValueError('source_image_unavailable')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('source_image_changed')
        return raw

    def cancel(self):
        with self.lock:
            self.generation+=1
            if self.current and self.current['state']=='running':self.current.update(state='cancelled',phase='已取消视觉请求')
            client,instance=self.client,self.board_instance
        if client is not None and instance is not None:
            def notify():
                try:client.read('/photo/command',dict(instance_id=instance,action='cancel',id='cancel',stationary_confirmed=False))
                except Exception:pass
            threading.Thread(target=notify,daemon=True).start()

    def close(self):
        self.closing=True;self.cancel()
        if self.thread is not None:self.thread.join(timeout=20)
