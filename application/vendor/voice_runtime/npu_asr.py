"""Isolated ZG330 ASR, no torch/FunASR, microphone, actions, or legacy startup."""
import argparse, ctypes, datetime as dt, fcntl, hashlib, json, time
from pathlib import Path
import numpy as np
from metrics import Metrics, memory
from frontend import NativeFrontend, read_wav, fixed_input

BASE = Path('/root/embodied_assistant_submit/models/asr_sensevoice')
COMPILED = BASE/'2_compile/imodel/SenseVoiceSmall_36/bf16/SenseVoiceSmall_1_i36_ZG'
ASSETS = BASE/'pyrt/iic/SenseVoiceSmall'


def parameter_maps():
    entries=[]
    for line in Path('/proc/self/maps').read_text().splitlines():
        fields=line.split(maxsplit=5)
        if len(fields)==6 and fields[5]==str(COMPILED)+'.raw':
            begin,end=[int(x,16) for x in fields[0].split('-')]
            entries.append(dict(begin=begin,end=end,permissions=fields[1],bytes=end-begin))
    return entries


def drop_parameter_pages():
    # Retain the mapping and backing file. MADV_DONTNEED only discards clean
    # resident pages; any later CPU access faults the unchanged data back in.
    # Never operate on anonymous, writable, driver, or device memory mappings.
    libc=ctypes.CDLL(None,use_errno=True)
    results=[]
    for entry in parameter_maps():
        if entry['permissions'] not in ('r--p','r--s'):
            results.append(dict(**entry,skipped='not a readonly regular model-file mapping'))
            continue
        result=libc.madvise(ctypes.c_void_p(entry['begin']),ctypes.c_size_t(entry['bytes']),ctypes.c_int(4))
        results.append(dict(**entry,result=int(result),errno=ctypes.get_errno() if result else 0))
    return results


def region_stats(region):
    manager = region.memManager()
    chunks = manager.getAllMemChunk()
    sizes = []
    for chunk in chunks:
        size = getattr(chunk, 'byte_size', None)
        if callable(size):
            size = size()
        sizes.append(int(size) if size is not None else None)
    # This SDK binds these values as opaque ObjectRef. Downcasting an
    # unchecked ObjectRef can crash in native code; keep unknown values unknown.
    info = {key: 'opaque ObjectRef; numeric value not queried'
            for key in manager.getMemRegionInfo()}
    return dict(region=region.typeKey(), info=info, chunks=len(chunks),
        chunk_bytes=sizes, sdk_allocated_bytes=sum(sizes) if all(s is not None for s in sizes) else None,
        scope='This process SDK region allocations, not total hardware utilization')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--length-mode', choices=['legacy','effective'], default='legacy')
    parser.add_argument('--feature-backend', choices=['native','saved'], default='native')
    parser.add_argument('--feature-dir')
    parser.add_argument('--dither', type=float, default=0.)
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--drop-param-pages',action='store_true')
    parser.add_argument('wav', nargs='+')
    args = parser.parse_args()
    lock = open('/opt/voice-lab-20261008/npu.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    metrics = Metrics(args.out)
    device = None
    try:
        start = time.perf_counter()
        import sentencepiece as spm
        from icraft import xir,xrt,host_backend
        from icraft.zg330backend import ZG330Backend,OcmOpt
        frontend = NativeFrontend(ASSETS/'am.mvn',args.dither)
        tokenizer = spm.SentencePieceProcessor(model_file=str(ASSETS/'chn_jpn_yue_eng_ko_spectok.bpe.model'))
        metrics.emit('runtime_imported',seconds=time.perf_counter()-start)
        start=time.perf_counter()
        network=xir.Network.CreateFromJsonFile(str(COMPILED)+'.json')
        network.lazyLoadParamsFromFile(str(COMPILED)+'.raw')
        values=list(network.inputs())
        if len(values)!=2:
            raise ValueError('Expected speech and speech_lengths')
        metrics.emit('network_loaded',seconds=time.perf_counter()-start,inputs=[str(v.tensorType()) for v in values])
        Path(args.out,'smaps-network-loaded.txt').write_text(Path('/proc/self/smaps').read_text())
        device=xrt.Device.Open('axi://zg330aiu?npu=0x40000000&dma=0x80000000')
        region=device.defaultMemRegion()
        metrics.emit('device_opened',plddr=region_stats(region))
        start=time.perf_counter()
        session=xrt.Session.Create([ZG330Backend,host_backend.HostBackend],network.view(0),[device,xrt.HostDevice.Default()])
        backend=ZG330Backend(session.backends[0])
        backend.ocmOptimize(OcmOpt.OPTION1)
        session.enableTimeProfile(True)
        metrics.emit('before_apply',plddr=region_stats(region))
        session.apply()
        metrics.emit('applied',seconds=time.perf_counter()-start,plddr=region_stats(region))
        ctypes.CDLL(None).malloc_trim(0)
        metrics.emit('trimmed',plddr=region_stats(region))
        Path(args.out,'smaps-applied.txt').write_text(Path('/proc/self/smaps').read_text())
        metrics.emit('parameter_mappings',mappings=parameter_maps())
        if args.drop_param_pages:
            dropped=drop_parameter_pages()
            metrics.emit('parameter_pages_advised',results=dropped,plddr=region_stats(region),
                note='RSS change is not automatically the same as extra MemAvailable; file pages were reclaimable already')
            Path(args.out,'smaps-advised.txt').write_text(Path('/proc/self/smaps').read_text())
        if memory()['available_kib']<64*1024:
            raise MemoryError('Insufficient host memory margin')
        for index,path in enumerate(args.wav):
            pcm=read_wav(path)
            start=time.perf_counter()
            if args.feature_backend=='saved':
                features=np.load(Path(args.feature_dir)/('{}-features.npy'.format(index)))
            else:
                _,features=frontend.extract(pcm)
            prepared=fixed_input(features,args.length_mode)
            feature_seconds=time.perf_counter()-start
            metrics.emit('prepared',wav=path,audio_seconds=len(pcm)/16000.,feature_frames=len(features),
                input_length=float(prepared[1][0]),seconds=feature_seconds,dither=args.dither,
                feature_backend=args.feature_backend,input_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
            for repeat in range(args.repeat):
                start=time.perf_counter()
                tensors=[]
                for array,value in zip(prepared,values):
                    typ=value.tensorType()
                    typ.setShape(list(array.shape))
                    tensors.append(xrt.Tensor(np.ascontiguousarray(array),typ))
                tensor_seconds=time.perf_counter()-start
                start=time.perf_counter()
                outputs=session.forward(tensors)
                forward_return=time.perf_counter()-start
                metrics.emit('forward_returned',seconds=forward_return,plddr=region_stats(region))
                arrays=[]
                for output in outputs:
                    output.waitForReady(dt.timedelta(seconds=30))
                    host=output.to(xrt.HostDevice.MemRegion())
                    host.waitForReady(dt.timedelta(seconds=30))
                    arrays.append(np.array(host,copy=True))
                inference_seconds=time.perf_counter()-start
                # Consume and own host outputs before resetting NPU.
                if len(arrays)<2 or arrays[1].ndim!=3:
                    raise ValueError('Unexpected SenseVoice output contract')
                start=time.perf_counter()
                yseq=arrays[1][0,:100,:].argmax(axis=-1)
                yseq=yseq[np.r_[True,yseq[1:]!=yseq[:-1]]]
                ids=yseq[yseq!=0].tolist()
                text=tokenizer.decode(ids)
                if '|>' in text:
                    text=text.split('|>')[-1]
                profile={str(k):list(v) for k,v in session.timeProfileResults().items()}
                Path(args.out,'profile-{}-{}.json'.format(index,repeat)).write_text(json.dumps(profile))
                decode_seconds=time.perf_counter()-start
                metrics.emit('transcribed',wav=path,repeat=repeat,text=text.strip(),ids=ids,
                    output_shapes=[list(a.shape) for a in arrays],tensor_seconds=tensor_seconds,
                    npu_and_host_transfer_seconds=inference_seconds,decode_seconds=decode_seconds,
                    pipeline_seconds=feature_seconds+tensor_seconds+inference_seconds+decode_seconds,
                    plddr=region_stats(region))
                device.reset(1)
                del arrays,outputs,tensors,host
                metrics.emit('after_reset',plddr=region_stats(region))
        metrics.emit('completed')
    except BaseException as exc:
        metrics.emit('failed',error=repr(exc))
        raise
    finally:
        if device is not None:
            xrt.Device.Close(device)
        metrics.close()


if __name__=='__main__':
    main()
