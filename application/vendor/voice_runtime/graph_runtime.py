"""One ZG330 owner; explicit model descriptors and bounded host-output lifetime."""
import ctypes,datetime as dt,mmap,threading,time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from metrics import memory
from runtime_contract import InputContract,validate_inputs,detach_result

# Strong process-lifetime roots. Only a platform recovery outside this process
# may resolve uncertain DMA completion; there is deliberately no clear API.
QUARANTINED_OWNERS=[]


def has_uncertain_native_call():
    return bool(QUARANTINED_OWNERS)


def discard_clean_input_pages(arrays):
    """After SDK Tensor(buffer,type) COPIES input, evict only readonly maps.

    The mapping and all lifetime roots remain valid. A later read faults the
    identical file bytes back in; ordinary arrays and writable maps are skipped.
    """
    advised=[];seen=set()
    for array in arrays:
        root=array
        while isinstance(root,np.ndarray):
            if isinstance(root,np.memmap) and root.mode=='r' and not root.flags.writeable:
                mapping=getattr(root,'_mmap',None)
                if mapping is not None and id(mapping) not in seen and hasattr(mapping,'madvise') and hasattr(mmap,'MADV_DONTNEED'):
                    seen.add(id(mapping))
                    try:
                        mapping.madvise(mmap.MADV_DONTNEED)
                        advised.append(dict(bytes=root.nbytes,discarded=True))
                    except (OSError,ValueError) as exc:
                        advised.append(dict(bytes=root.nbytes,discarded=False,error=str(exc)))
                break
            root=getattr(root,'base',None)
    return advised


@dataclass(frozen=True)
class ModelSpec:
    name:str
    json_path:str
    raw_path:str


@dataclass(frozen=True)
class PreparedHostInput:
    """Graph-bound SDK Host allocation plus its writable buffer-protocol view.

    Allocate through Graph.allocate_host_input(). Keep this object until the
    forward returns. Never write array concurrently with a forward. Graph's
    inflight roots also retain this object after uncertain native completion.
    """
    graph:object
    input_index:int
    tensor:object
    array:np.ndarray


class DeviceOwner:
    def __init__(self,metrics,shared_ftmp_bytes=0,*,debug_disable_merge=False):
        if type(debug_disable_merge) is not bool:
            raise ValueError('Debug merge control must be a boolean')
        self.debug_disable_merge=debug_disable_merge
        import fcntl
        from icraft import xrt
        self.xrt=xrt
        self.metrics=metrics
        self.locks=[]
        self.mutex=threading.RLock()
        self.device=None
        self.workspace=None
        self.graphs=[]
        self.closed=False
        self.failed=False
        self.quarantined=False
        self.retained=[]
        self.busy=False
        self.close_error=None
        self.pins={}
        if shared_ftmp_bytes<0:
            raise ValueError('Workspace size must be nonnegative')
        try:
            for path in ['/opt/voice-lab-20261008/npu.lock','/opt/voice-assistant-20261008/.assistant.lock']:
                lock=open(path,'a')
                self.locks.append(lock)
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            from fm_autonomy.pl_allocator import require_loaded,require_empty_region
            allocator_identity=require_loaded()
            self.metrics.emit('allocator2g_verified_before_open',**allocator_identity)
            self.device=xrt.Device.Open('axi://zg330aiu?npu=0x40000000&dma=0x80000000')
            self.metrics.emit('allocator2g_region_verified',**require_empty_region(self.device))
            self.workspace=self.device.malloc(shared_ftmp_bytes) if shared_ftmp_bytes else None
        except BaseException as initialization_error:
            try:
                self.close()
            except BaseException as close_error:
                # Construction never returned to its caller; only this process
                # root can retain the device/locks when close is also uncertain.
                self.quarantine((initialization_error,close_error))
                raise
            raise

    def check_ready(self):
        self.check_not_quarantined()
        if self.closed:
            raise RuntimeError('Device owner is closed')
        if self.failed:
            raise RuntimeError('Prior SDK inference failed; close this owner before reopening')

    def check_not_quarantined(self):
        if self.quarantined:
            raise RuntimeError('native_completion_unknown_platform_recovery_required')

    def quarantine(self, retained):
        self.failed=True
        self.quarantined=True
        self.retained.append(retained)
        if not any(value is self for value in QUARANTINED_OWNERS):
            QUARANTINED_OWNERS.append(self)

    def check_idle(self):
        if self.busy:
            raise RuntimeError('Model/device mutation or nested inference during an active forward is forbidden')

    def load(self,spec):
        with self.mutex:
            self.check_ready()
            self.check_idle()
            if memory()['available_kib']<80*1024:
                raise MemoryError('Host memory margin below 80 MiB')
            try:
                graph=Graph(self,spec)
            except BaseException:
                self.failed=True
                raise
            self.graphs.append(graph)
            return graph

    def unload(self,graph):
        with self.mutex:
            self.check_ready()
            self.check_idle()
            if graph.owner is not self or graph not in self.graphs:
                raise ValueError('Graph is not resident on this owner')
            if self.pins.get(graph,0):
                raise RuntimeError('Cannot unload a graph held by an active lease')
            try:
                graph.release()
            except BaseException:
                self.failed=True
                raise
            self.graphs.remove(graph)
            self.metrics.emit('graph_unloaded',name=graph.spec.name,plddr=self.allocations())

    @contextmanager
    def lease(self,graph):
        with self.mutex:
            self.check_ready()
            self.check_idle()
            if graph.owner is not self or graph not in self.graphs or graph.released:
                raise ValueError('Cannot lease a nonresident graph')
            self.pins[graph]=self.pins.get(graph,0)+1
        try:
            yield graph
        finally:
            # Releasing a pin changes host bookkeeping only, even after an SDK
            # failure. The caller can then close the poisoned owner explicitly.
            with self.mutex:
                remaining=self.pins[graph]-1
                if remaining:self.pins[graph]=remaining
                else:del self.pins[graph]

    def allocations(self):
        from npu_asr import region_stats
        return region_stats(self.device.defaultMemRegion())

    def close(self):
        with self.mutex:
            self.check_not_quarantined()
            self.check_idle()
            if self.pins:
                raise RuntimeError('Cannot close a device held by active graph leases')
            if self.closed:
                if self.close_error is not None:
                    raise RuntimeError('Prior device cleanup failed; no automatic SDK close retry') from self.close_error
                return
            self.closed=True
            try:
                errors=[]
                # Attempt each release. A failed release retains its references,
                # device and locks; do not close the device underneath it.
                for graph in list(self.graphs):
                    try:
                        graph.release()
                    except BaseException as exc:
                        errors.append(exc)
                    else:
                        self.graphs.remove(graph)
                if errors:
                    raise RuntimeError('One or more graph releases failed') from errors[0]
                self.workspace=None
                if self.device is not None:
                    self.xrt.Device.Close(self.device)
                    self.device=None
            except BaseException as exc:
                self.failed=True
                self.close_error=exc
                self.quarantine(exc)
                raise
            else:
                lock_errors=[]
                for lock in self.locks:
                    try:lock.close()
                    except BaseException as exc:lock_errors.append(exc)
                if lock_errors:
                    self.failed=True
                    self.close_error=lock_errors[0]
                    raise RuntimeError('Device closed but lock cleanup failed') from lock_errors[0]
                self.locks.clear()


class Graph:
    def __init__(self,owner,spec):
        self.owner=owner
        self.spec=spec
        self.released=False
        self.network=None
        self.values=[]
        self.session=None
        self.backend=None
        self.workspace_view=None
        self.inflight=None
        try:
            self.initialize(owner,spec)
        except BaseException:
            # apply/load failure can also leave SDK-owned work in flight.
            owner.quarantine(self)
            raise

    def initialize(self,owner,spec):
        from icraft import xir,xrt,host_backend
        from icraft.zg330backend import ZG330Backend,OcmOpt,SegmentType
        self.owner=owner
        self.spec=spec
        started=time.perf_counter()
        self.network=xir.Network.CreateFromJsonFile(spec.json_path)
        json_done=time.perf_counter()
        self.network.lazyLoadParamsFromFile(spec.raw_path)
        params_done=time.perf_counter()
        self.values=list(self.network.inputs())
        self.contracts=[InputContract.from_sdk_json(str(value.tensorType())) for value in self.values]
        self.session=xrt.Session.Create([ZG330Backend,host_backend.HostBackend],self.network.view(0),[owner.device,xrt.HostDevice.Default()])
        self.backend=ZG330Backend(self.session.backends[0])
        session_done=time.perf_counter()
        self.backend.ocmOptimize(OcmOpt.OPTION1)
        if owner.debug_disable_merge:
            # SDK requires this before apply; diagnostic timings are separate.
            self.backend.disableMergeHardOp()
        if owner.workspace is not None:
            from workspace_view import prefix
            needed=int(self.backend.logic_segment_map[SegmentType.FTMP].byte_size)
            owner.metrics.emit('workspace_required',name=spec.name,bytes=needed,
                owner_bytes=int(owner.workspace.byte_size))
            if needed:
                # iCraft 3.36.1 requires exact size, not merely enough capacity.
                # This view has no allocator/deleter ownership; the owner retains
                # the full allocation throughout every graph's lifetime.
                self.workspace_view=prefix(owner.workspace,needed)
                self.backend.userReuseSegment(self.workspace_view,SegmentType.FTMP)
        apply_started=time.perf_counter()
        self.session.apply()
        apply_done=time.perf_counter()
        self.segments={str(kind):dict(bytes=int(segment.byte_size),address=int(segment.phy_addr))
            for kind,segment in self.backend.phy_segment_map.items()}
        # Clean readonly mapped parameter pages can be discarded without
        # unmapping or modifying weights. Later accesses reload the same data.
        advised=[]
        libc=ctypes.CDLL(None,use_errno=True)
        for line in Path('/proc/self/maps').read_text().splitlines():
            fields=line.split(maxsplit=5)
            if len(fields)==6 and fields[5]==spec.raw_path and fields[1] in ('r--p','r--s'):
                begin,end=[int(x,16) for x in fields[0].split('-')]
                code=libc.madvise(ctypes.c_void_p(begin),ctypes.c_size_t(end-begin),ctypes.c_int(4))
                advised.append(dict(bytes=end-begin,result=int(code)))
        libc.malloc_trim(0)
        owner.metrics.emit('graph_loaded',name=spec.name,seconds=time.perf_counter()-started,
            segments=self.segments,parameter_page_advice=advised,plddr=owner.allocations(),
            load_phases_seconds=dict(network_json=json_done-started,parameter_mapping=params_done-json_done,
                create_session=session_done-params_done,configure_backend=apply_started-session_done,
                apply=apply_done-apply_started,cleanup_advice=time.perf_counter()-apply_done),
            debug_disable_merge=owner.debug_disable_merge)

    def release(self):
        with self.owner.mutex:
            self.owner.check_not_quarantined()
            self.owner.check_idle()
            if self.owner.pins.get(self,0):
                raise RuntimeError('Cannot release a graph held by an active lease')
            if self.released:
                return
            self.released=True
            self.session=None
            self.backend=None
            self.values=[]
            self.network=None
            self.workspace_view=None

    def allocate_host_input(self,index=0):
        """Allocate once in SDK Host memory; no physical-device operation.

        Caller fills result.array and passes result itself to forward(). The
        ordinary NumPy forward path remains unchanged. No pointer borrowing,
        tmpfs, source mmap, or second full-size Tensor(buffer,type) copy.
        """
        with self.owner.mutex:
            self.owner.check_ready()
            self.owner.check_idle()
            if self.released:
                raise RuntimeError('Graph was released; acquire a new resident graph')
            if type(index) is not int or not 0<=index<len(self.values):
                raise ValueError('Compiled graph input index out of range')
            tensor=self.owner.xrt.Tensor(self.values[index].tensorType()).mallocOn(
                self.owner.xrt.HostDevice.MemRegion())
            prepared=PreparedHostInput(self,index,tensor,np.asarray(tensor))
            self._prepared_array(prepared,index)
            return prepared

    def _prepared_array(self,prepared,index):
        if prepared.graph is not self or prepared.input_index!=index:
            raise ValueError('Prepared Host input belongs to another graph/input')
        array=prepared.array
        contract=self.contracts[index]
        if (not isinstance(array,np.ndarray) or array.shape!=contract.shape
                or array.dtype!=np.dtype(contract.dtype)):
            raise ValueError('Prepared Host input shape/dtype mismatch')
        if (not array.flags.c_contiguous or not array.flags.writeable
                or array.flags.owndata or not isinstance(array.base,memoryview)
                or array.base.obj is not prepared.tensor):
            raise ValueError('SDK does not expose the required writable owning buffer view')
        if (str(prepared.tensor.dtype())!=str(self.values[index].tensorType())
                or not prepared.tensor.isOn(self.owner.xrt.HostDevice.MemRegion())):
            raise ValueError('Prepared input must retain the exact compiled Host tensor')
        return array

    def forward(self,arrays,reducer=None):
        # Sharing FTMP is valid only with serialized inference, completion of
        # all host transfers, and owned outputs before the next graph runs.
        requested=time.perf_counter()
        with self.owner.mutex:
            acquired=time.perf_counter()
            self.owner.check_ready()
            self.owner.check_idle()
            if self.released:
                raise RuntimeError('Graph was released; acquire a new resident graph')
            inputs=list(arrays)
            prepared_inputs=[a if isinstance(a,PreparedHostInput) else None for a in inputs]
            arrays=[self._prepared_array(a,index) if isinstance(a,PreparedHostInput) else a
                    for index,a in enumerate(inputs)]
            reused_host_inputs=sum(a is not None for a in prepared_inputs)
            trace_large_input=any(isinstance(a,np.ndarray) and a.nbytes>=16*1024*1024 for a in arrays)
            def phase(stage):
                if trace_large_input:
                    self.owner.metrics.emit('graph_forward_phase',name=self.spec.name,phase=stage,
                        input_bytes=sum(a.nbytes for a in arrays if isinstance(a,np.ndarray)),
                        model_process_memory=memory(include_pss=False))
            phase('before_validation')
            arrays=validate_inputs(arrays,self.contracts)
            for array,prepared_input in zip(arrays,prepared_inputs):
                if prepared_input is not None and array is not prepared_input.array:
                    raise ValueError('Validation unexpectedly copied a prepared Host input')
            phase('after_validation_before_tensor_copy')
            validated=time.perf_counter()
            self.owner.busy=True
            # Register every externally owned object BEFORE another native call.
            flight=dict(arrays=arrays,prepared_inputs=prepared_inputs,tensors=[],outputs=[],hosts=[])
            self.inflight=flight
            try:
                started=validated
                tensors=flight['tensors']
                for array,value,prepared_input in zip(arrays,self.values,prepared_inputs):
                    if prepared_input is None:
                        tensors.append(self.owner.xrt.Tensor(array,value.tensorType()))
                    else:
                        tensors.append(prepared_input.tensor)
                phase('after_tensor_copy_before_submit')
                if trace_large_input:
                    advice=discard_clean_input_pages(arrays)
                    self.owner.metrics.emit('graph_input_page_advice',name=self.spec.name,advice=advice)
                    phase('source_pages_reclaimable_before_submit')
                prepared_at=time.perf_counter()
                prepared=prepared_at-started
                started=prepared_at
                device_wait=0.;host_transfer=0.
                try:
                    outputs=self.session.forward(tensors)
                    flight['outputs']=outputs
                    phase('submit_returned')
                    submitted=time.perf_counter()
                    hosts=flight['hosts']
                    host=None;output=None
                    for output in outputs:
                        waiting=time.perf_counter()
                        if output.waitForReady(dt.timedelta(seconds=30)) is False:
                            raise TimeoutError('device_output_not_ready')
                        ready=time.perf_counter()
                        device_wait+=ready-waiting
                        host=output.to(self.owner.xrt.HostDevice.MemRegion())
                        hosts.append(host)
                        if host.waitForReady(dt.timedelta(seconds=30)) is False:
                            raise TimeoutError('host_output_not_ready')
                        host_transfer+=time.perf_counter()-ready
                except BaseException:
                    # Do not issue another graph or reset after unknown completion.
                    self.owner.quarantine(self)
                    raise
                transferred_at=time.perf_counter()
                transferred=transferred_at-started
                started=transferred_at
                # Views and reducer are both within the completed-transfer reset
                # boundary. Conversion failure must not leave a live unreset turn.
                try:
                    views=[np.asarray(host) for host in hosts]
                    result=detach_result(reducer(views),views) if reducer else [view.copy() for view in views]
                    consumed_at=time.perf_counter()
                    consumed=consumed_at-started
                finally:
                    try:
                        self.owner.device.reset(1)
                    except BaseException:
                        self.owner.quarantine(self)
                        raise
                reset_at=time.perf_counter()
                output_bytes=sum(v.nbytes for v in views)
                # Detached results no longer need these SDK tensor owners.
                # Include their disposal in the result-ready timing boundary.
                del outputs,hosts,views,tensors,host,output
                self.inflight=None
                flight.clear()
                ready_at=time.perf_counter()
                self.owner.metrics.emit('graph_forward',name=self.spec.name,prepare_seconds=prepared,
                    inference_transfer_seconds=transferred,consume_seconds=consumed,
                    queued_seconds=acquired-requested,validation_seconds=validated-acquired,
                    submit_seconds=submitted-prepared_at,device_ready_wait_seconds=device_wait,
                    host_transfer_seconds=host_transfer,reset_seconds=reset_at-consumed_at,
                    output_cleanup_seconds=ready_at-reset_at,graph_ready_seconds=ready_at-requested,
                    inference_unclassified_seconds=max(0.,transferred-(submitted-prepared_at)-device_wait-host_transfer),
                    timing_scope='Graph API entry to detached result ready; excludes event logging and wrapper return',
                    device_wait_is_compute_time=False,
                    output_bytes=output_bytes,reduced=reducer is not None,
                    reused_host_inputs=reused_host_inputs)
                return result
            finally:
                self.owner.busy=False
                # Validation/preparation or reducer exceptions before any
                # uncertain native outcome do not require retained SDK objects.
                if not self.owner.quarantined:
                    self.inflight=None
                    flight.clear()
