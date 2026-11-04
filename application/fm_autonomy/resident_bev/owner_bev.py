"""Five-model integration building block: borrows the caller's existing owner.

No SDK import, Device.Open/Close, board connection, camera control or NPU lease.
The combined model child owns lifecycle/lease/quarantine and serial request queue.
Do not call from a second process; owner and ModelSpec come from the voice runtime.
"""
import gc
import hashlib
import io
import json
from pathlib import Path
import subprocess
import threading
import time
from datetime import datetime, timezone
import numpy as np
from .cpu_part2 import SELECTED, load_lut, preprocess, public_sources, sha, validate_capture, write_json
from .projection import Projection
from .render_bev import render
from .host_memory import memory_snapshot, release_unused_heap, write_clean_input

MODELS = {
    'extractor': ('71c6b7e516597f7b68750853f71ee2ccf97ef35241ea9c2bb6aeb311d69acb0e',
                  '96138f1979de581df44710bb67e27a94af1fe6512c330d226e2ec3f1cb6d6f5b'),
    'decoder': ('f17006c3ad87c86473775f358380a234de8a1c77ec8daf67f338885a7d73ac22',
                '95354998749efdcd100fd46f11ba9e26e55b2dacfb889c7f91134e70ff5a2b79'),
}

def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical_npy_sha256(array):
    """Hash NumPy v1 header + existing contiguous bytes, without a large copy."""
    header=io.BytesIO()
    np.lib.format.write_array_header_1_0(header,np.lib.format.header_data_from_array_1_0(array))
    h=hashlib.sha256(header.getvalue())
    h.update(memoryview(array).cast('B'))
    return h.hexdigest()


class ResidentBev:
    def __init__(self, owner, model_specs, lut_path, postprocess_path, *, capture_replay=None,
                 input_storage='sd_spool',audit_decoder_input=False):
        if input_storage not in ('sd_spool','sdk_host_input'):
            raise ValueError('Unknown decoder input storage mode')
        if type(audit_decoder_input) is not bool:
            raise TypeError('audit_decoder_input must be boolean')
        self.input_storage=input_storage
        self.audit_decoder_input=audit_decoder_input
        self.owner = owner
        self.specs = dict(model_specs)
        if set(self.specs) != set(MODELS):
            raise ValueError('extractor and full decoder specs required')
        self.lut_path = Path(lut_path)
        self.postprocess = Path(postprocess_path).resolve()
        self.graphs = {}
        self.mutex = threading.RLock()
        self.closed = False
        self.capture_replay = dict(capture_replay) if capture_replay is not None else None
        if self.capture_replay is not None:
            pin=self.capture_replay
            if (set(pin)!={'manifest_sha256','source_boot_id','run_id'}
                    or not all(isinstance(v,str) and v for v in pin.values())
                    or len(pin['manifest_sha256'])!=64
                    or any(c not in '0123456789abcdef' for c in pin['manifest_sha256'])):
                raise ValueError('explicit immutable capture replay identity required')

    def load(self):
        with self.mutex, self.owner.mutex:
            self.owner.check_ready()
            self.owner.check_idle()
            if self.closed:
                raise RuntimeError('BEV adapter is closed')
            if self.graphs:
                if set(self.graphs) != set(MODELS):
                    raise RuntimeError('partial load; caller must close owner')
                return self.status()
            if not self.postprocess.is_file():
                raise ValueError('compiled CPU postprocess required')
            for name, identities in MODELS.items():
                spec = self.specs[name]
                if tuple(sha(p) for p in (spec.json_path,spec.raw_path)) != identities:
                    raise ValueError('frozen 3.39 model mismatch: '+name)
            # Each graph remains in owner's registry. A failed SDK call is
            # handled by the parent's existing poisoned-owner/quarantine path.
            for name in ('extractor','decoder'):
                self.graphs[name] = self.owner.load(self.specs[name])
            return self.status()

    def status(self):
        return dict(ready=not (self.closed or getattr(self.owner,'closed',False)
                              or getattr(self.owner,'failed',False)) and set(self.graphs)==set(MODELS),
                    resident_graphs=list(self.graphs),device_owner='borrowed',
                    allocation=self.owner.allocations(),model_hashes=MODELS)

    def infer(self, capture_manifest, output_dir, boot_id, save_intermediates=False):
        # One logical BEV job (4 extractor forwards + 1 decoder). Holding the
        # existing RLock forbids another caller closing/interleaving the owner.
        with self.mutex, self.owner.mutex:
            self.owner.check_ready()
            self.owner.check_idle()
            if self.closed or set(self.graphs)!=set(MODELS):
                raise RuntimeError('load BEV graphs first')
            source_boot=boot_id
            if self.capture_replay is not None:
                if sha(capture_manifest)!=self.capture_replay['manifest_sha256']:
                    raise ValueError('capture replay manifest hash mismatch')
                source_boot=self.capture_replay['source_boot_id']
            capture=validate_capture(capture_manifest,source_boot)
            if self.capture_replay is not None and capture['run_id']!=self.capture_replay['run_id']:
                raise ValueError('capture replay frame identity mismatch')
            lut,valid=load_lut(self.lut_path,capture['active_slots'])
            projection=Projection(lut,valid)
            out=Path(output_dir).resolve()
            out.mkdir(parents=True,exist_ok=False)
            frame=capture['run_id']
            sources=public_sources(capture)
            report=dict(schema='FM-FASTBEV-RESIDENT-RUN/1',success=False,frame_id=frame,
                board_boot_id=boot_id,models=MODELS,device_owner='borrowed',
                source_capture_boot_id=source_boot,capture_mode='replay' if self.capture_replay else 'current_boot_capture',
                models_retained=True,device_closed=False,quiescent=False,
                active_slots=capture['active_slots'],selected_capture_indices=list(SELECTED),
                capture_manifest_sha256=sha(capture_manifest),
                geometry_status='candidate_virtual_lidar_unvalidated',
                stationary_assumption=True,synchronized_cameras=False,
                decoder_input_layout='N,X,Y,Z,temporal_group*64+channel',
                valid_voxels=int(valid.sum()),total_voxels=int(valid.size),
                save_intermediates=bool(save_intermediates),stages=[])
            report['input_storage_mode']=self.input_storage
            report['input_storage']=('one SDK Host Tensor allocation; projection writes its buffer view; forward reuses it'
                if self.input_storage=='sdk_host_input' else
                'RAM projection then one sequential fsynced NPY; decoder reads clean readonly mmap')
            report['memory_before_job']=memory_snapshot()
            write_json(out/'sources.json',sources)
            write_json(out/'RUN.json',report)
            def checkpoint(stage):
                report['current_stage']=stage
                report['memory_at_checkpoint']=memory_snapshot()
                write_json(out/'RUN.json',report)
            target=None;prepared_input=None
            spool=out/'decoder_input.npy'
            try:
                checkpoint('allocate_decoder_input')
                t=time.monotonic()
                if self.input_storage=='sdk_host_input':
                    prepared_input=self.graphs['decoder'].allocate_host_input(0)
                    target=prepared_input.array
                    if target.shape!=(1,200,200,4,256) or target.dtype!=np.float32:
                        raise ValueError('frozen BEV decoder input contract mismatch')
                    target.fill(0.)
                else:
                    target=np.zeros((1,200,200,4,256),dtype=np.float32)
                report['stages'].append(dict(stage='allocate_decoder_input',seconds=time.monotonic()-t))
                for group,index in enumerate(SELECTED):
                    checkpoint('extractor_%d'%index)
                    stage=dict(stage='extractor_and_cpu_projection',capture_index=index)
                    t=time.monotonic();x=preprocess(capture['frames'][index]['sources'])
                    stage['preprocess_seconds']=time.monotonic()-t
                    t=time.monotonic();values=self.graphs['extractor'].forward([x])
                    stage['forward_seconds']=time.monotonic()-t
                    del x
                    if len(values)!=1:raise ValueError('extractor output count mismatch')
                    feature=values[0]
                    t=time.monotonic();projection.into(feature,target,group)
                    stage['projection_seconds']=time.monotonic()-t
                    if save_intermediates:
                        t=time.monotonic();np.save(out/('feature_%d.npy'%index),feature,allow_pickle=False)
                        stage['save_seconds']=time.monotonic()-t
                        stage['feature_sha256']=sha(out/('feature_%d.npy'%index))
                    del feature,values
                    stage['seconds']=sum(v for k,v in stage.items() if k.endswith('_seconds'))
                    report['stages'].append(stage)
                    checkpoint('projected_%d'%index)
                if self.input_storage=='sdk_host_input':
                    # Projection already filled the SDK allocation. Keep both
                    # explicit owner and view; Graph retains them if uncertain.
                    if self.audit_decoder_input:
                        t=time.monotonic()
                        report['decoder_input_sha256']=canonical_npy_sha256(target)
                        report['stages'].append(dict(stage='audit_decoder_input_hash',seconds=time.monotonic()-t))
                    if save_intermediates:
                        t=time.monotonic();write_clean_input(spool,target)
                        report['stages'].append(dict(stage='optional_debug_input_snapshot',seconds=time.monotonic()-t,
                                                      bytes=spool.stat().st_size))
                        report['decoder_input_sha256']=sha(spool)
                    report['heap_release_before_decoder']=release_unused_heap()
                else:
                    # Frozen v3 fallback: clean disk pages can be reclaimed
                    # after Tensor(buffer,type) makes its SDK-owned copy.
                    checkpoint('sequential_input_spool')
                    t=time.monotonic();write_clean_input(spool,target)
                    report['stages'].append(dict(stage='sequential_input_spool',seconds=time.monotonic()-t,
                                                  bytes=spool.stat().st_size))
                    target=None
                    report['heap_release_before_decoder']=release_unused_heap()
                    if save_intermediates:report['decoder_input_sha256']=sha(spool)
                    target=np.load(spool,mmap_mode='r',allow_pickle=False)
                checkpoint('decoder_forward_begin')
                t=time.monotonic();values=self.graphs['decoder'].forward(
                    [prepared_input if prepared_input is not None else target])
                report['stages'].append(dict(stage='decoder_forward',seconds=time.monotonic()-t))
                del target;target=None;prepared_input=None
                report['heap_release_after_decoder']=release_unused_heap()
                checkpoint('decoder_forward_complete')
                # Only remove our known temporary source after native success.
                # Unknown native completion preserves mmap roots and this file.
                if self.input_storage=='sd_spool' and not save_intermediates:spool.unlink()
                if len(values)!=3:raise ValueError('decoder output count mismatch')
                for i,(value,ch) in enumerate(zip(values,(80,72,16))):
                    if value.shape!=(1,100,100,ch) or value.dtype!=np.float32 or not np.isfinite(value).all():
                        raise ValueError('decoder output contract mismatch')
                    value.tofile(out/('output_%d.fp32'%i))
                report['max_class_score']=float(values[0].max())
                del value,values
                report['quiescent']=True
                report['allocation_after_forward']=self.owner.allocations()
                t=time.monotonic()
                subprocess.run([str(self.postprocess)]+[str(out/('output_%d.fp32'%i)) for i in range(3)]
                               +[str(out/'detections.json')],check=True)
                detections=json.loads((out/'detections.json').read_text())
                png=out/('bev_'+hashlib.sha256(frame.encode()).hexdigest()[:16]+'.png')
                render(png,detections,frame,capture['active_slots'])
                report['stages'].append(dict(stage='CPU_postprocess_and_PNG',seconds=time.monotonic()-t))
                report.update(success=True,completed_at=stamp(),detection_count=len(detections['boxes']),
                    image_sha256=sha(png),detections_sha256=sha(out/'detections.json'),
                    outputs=[dict(file='output_%d.fp32'%i,sha256=sha(out/('output_%d.fp32'%i))) for i in range(3)])
                checkpoint('completed')
                manifest=dict(schema='FM-BOARD-BEV/1',generated_on='board',image=png.name,
                    sha256=sha(png),frame_id=frame,input_sources=sources,board_boot_id=boot_id,
                    source_capture_boot_id=source_boot,capture_mode=report['capture_mode'],
                    generated_at=report['completed_at'],run_evidence_sha256=sha(out/'RUN.json'),
                    message=('回放已保存的实拍照片；' if self.capture_replay else '')+
                    '板端FastBEV检测俯视图；复用现有设备所有者，模型保持加载；候选标定；0框不代表无障碍。')
                write_json(out/'latest.json',manifest)
                return manifest
            except BaseException as exc:
                report.update(error=repr(exc),success=False,
                    quiescent=not (getattr(self.owner,'quarantined',False) or getattr(self.owner,'busy',False)))
                write_json(out/'RUN.json',report)
                raise
            finally:
                # SDK uncertainty retains inputs in owner flight roots. Do not
                # close/replace the owner, swallow the exception or retry SDK.
                target=None;prepared_input=None
                gc.collect()

    def close(self):
        with self.mutex, self.owner.mutex:
            if self.closed:return
            self.owner.check_ready()
            self.owner.check_idle()
            for name in ('decoder','extractor'):
                if name in self.graphs:
                    self.owner.unload(self.graphs[name])
                    del self.graphs[name]
            self.closed=True
