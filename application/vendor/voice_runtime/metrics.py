"""Linux/PS observations and sampler health; SDK/PL allocation stays separate."""
import json,os,threading,time
from pathlib import Path


def memory(include_pss=True):
    def fields(path):
        result={}
        for line in Path(path).read_text().splitlines():
            parts=line.split()
            if len(parts)>=2 and parts[1].isdigit():result[parts[0].rstrip(':')]=int(parts[1])
        return result
    status=fields('/proc/self/status');info=fields('/proc/meminfo')
    smaps={}
    if include_pss:
        try:smaps=fields('/proc/self/smaps_rollup')
        except OSError:pass
    times=os.times()
    return dict(rss_kib=status.get('VmRSS'),pss_kib=smaps.get('Pss'),hwm_kib=status.get('VmHWM'),
        rss_anon_kib=status.get('RssAnon'),rss_file_kib=status.get('RssFile'),rss_shmem_kib=status.get('RssShmem'),
        available_kib=info.get('MemAvailable'),free_kib=info.get('MemFree'),
        cma_free_kib=info.get('CmaFree'),cma_total_kib=info.get('CmaTotal'),
        # MemAvailable includes pages that cannot necessarily back malloc.
        # This subtraction is a conservative admission estimate, not a
        # guarantee about fragmentation, kernel reserves or future demand.
        non_cma_available_estimate_kib=max(0,info['MemAvailable']-info.get('CmaFree',0)),
        non_cma_free_kib=max(0,info.get('MemFree',0)-info.get('CmaFree',0)),
        threads=status.get('Threads'),
        cpu_user_seconds=times.user,cpu_system_seconds=times.system)


class Metrics:
    def __init__(self,folder,memory_reader=None,interval_seconds=.2,printer=print):
        if interval_seconds<=0:raise ValueError('Sampling interval must be positive')
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=False)
        self.reader=memory if memory_reader is None else memory_reader
        self.interval=interval_seconds;self.printer=printer
        self.events=(self.folder/'events.jsonl').open('w',encoding='utf-8')
        self.stop=threading.Event();self.log_lock=threading.Lock();self.status_lock=threading.Lock();self.close_lock=threading.Lock()
        self.closed=False;self.final_status=None;self.sample_count=0;self.event_count=0
        self.successful_reads=0;self.error_count=0;self.errors=[];self.read_seconds=0.
        self.peak_rss=None;self.peak_hwm=None;self.minimum_available=None
        self.thread=threading.Thread(target=self.sample,daemon=True)
        self.emit('started',pid=os.getpid())
        self.thread.start()

    def record_error(self,stage,error):
        with self.status_lock:
            self.error_count+=1
            if len(self.errors)<16:self.errors.append(dict(stage=stage,error=repr(error)))

    def snapshot(self):
        start=time.monotonic()
        values=self.reader()
        for field in ('rss_kib','hwm_kib','available_kib'):
            value=values.get(field)
            if isinstance(value,bool) or not isinstance(value,int) or value<0:
                raise ValueError('Missing/invalid required memory field '+field)
        elapsed=time.monotonic()-start
        with self.status_lock:
            self.successful_reads+=1;self.read_seconds+=elapsed
            self.peak_rss=max(values['rss_kib'],self.peak_rss or 0)
            self.peak_hwm=max(values['hwm_kib'],self.peak_hwm or 0)
            self.minimum_available=values['available_kib'] if self.minimum_available is None else min(
                self.minimum_available,values['available_kib'])
        return values,elapsed

    def sample(self):
        previous=None
        try:
            with (self.folder/'memory.jsonl').open('w',encoding='utf-8') as output:
                while not self.stop.is_set():
                    moment=time.monotonic();values,overhead=self.snapshot()
                    user,system=values.get('cpu_user_seconds'),values.get('cpu_system_seconds')
                    total=user+system if user is not None and system is not None else None
                    percent=None
                    if previous is not None and total is not None and moment>previous[0] and total>=previous[1]:
                        percent=100.*(total-previous[1])/(moment-previous[0])
                    if total is not None:previous=(moment,total)
                    output.write(json.dumps(dict(monotonic=moment,memory_read_seconds=overhead,
                        process_cpu_percent=percent,**values))+'\n');output.flush()
                    with self.status_lock:self.sample_count+=1
                    self.stop.wait(self.interval)
        except BaseException as exc:
            self.record_error('periodic_sampler',exc);self.stop.set()

    def emit(self,event,**data):
        with self.log_lock:
            if self.closed:raise RuntimeError('Metrics observer is closed')
            moment=time.monotonic()
            try:
                values,overhead=self.snapshot();measurement_error=None
            except BaseException as exc:
                self.record_error('event_snapshot',exc);measurement_error=repr(exc)
                values=dict(rss_kib=None,pss_kib=None,hwm_kib=None,available_kib=None,threads=None)
                overhead=None
            row=dict(event=event,monotonic=moment,memory=values,memory_read_seconds=overhead,**data)
            if measurement_error is not None:row['measurement_error']=measurement_error
            line=json.dumps(row,ensure_ascii=False)
            self.events.write(line+'\n');self.events.flush();self.event_count+=1
            self.printer(line,flush=True)

    def close(self):
        with self.close_lock:
            return self.finish()

    def finish(self):
        if self.final_status is not None:return self.final_status
        self.stop.set();self.thread.join()
        with self.log_lock:
            self.closed=True;self.events.close()
        with self.status_lock:
            status=dict(success=self.error_count==0 and self.sample_count>0 and self.successful_reads>0,
                scope='Linux/PS process observations only; no NPU allocation or hardware utilization inferred',
                periodic_samples=self.sample_count,event_snapshots=self.event_count,
                successful_reads=self.successful_reads,error_count=self.error_count,errors=list(self.errors),
                sampled_and_event_peak_rss_kib=self.peak_rss,process_hwm_kib=self.peak_hwm,
                minimum_available_kib=self.minimum_available,sampling_interval_seconds=self.interval,
                memory_read_seconds=self.read_seconds,
                process_cpu_percent_convention='100 percent per full CPU core; may exceed100; sampler/event overhead included',
                sampled_peaks_cover_all_allocations=False)
        (self.folder/'METRICS_STATUS.json').write_text(json.dumps(status,indent=2),encoding='utf-8')
        self.final_status=status
        return status
