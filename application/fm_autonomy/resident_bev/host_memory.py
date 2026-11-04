"""Host-only clean-file input staging, with no SDK or hardware ownership."""
import ctypes
import gc
import os
from pathlib import Path
import shutil
import numpy as np


def memory_snapshot():
    result={}
    for source,keys in (('/proc/self/status',('VmRSS','VmHWM','RssAnon','RssFile')),
                        ('/proc/meminfo',('MemAvailable','MemFree','CmaFree','CmaTotal','SwapFree'))):
        path=Path(source)
        if path.is_file():
            for line in path.read_text().splitlines():
                row=line.split()
                if row and row[0].rstrip(':') in keys:
                    result[row[0].rstrip(':')+'_kib']=int(row[1])
    if 'MemAvailable_kib' in result and 'CmaFree_kib' in result:
        result['available_minus_cma_free_kib']=max(0,result['MemAvailable_kib']-result['CmaFree_kib'])
    return result


def release_unused_heap():
    gc.collect()
    result=dict(memory_before=memory_snapshot())
    if os.name=='posix':
        libc=ctypes.CDLL(None,use_errno=True)
        if hasattr(libc,'malloc_trim'):
            libc.malloc_trim.argtypes=[ctypes.c_size_t]
            libc.malloc_trim.restype=ctypes.c_int
            result['malloc_trim_return']=int(libc.malloc_trim(0))
    result['memory_after']=memory_snapshot()
    return result


def write_clean_input(path,array):
    """One sequential pass; source ndarray is released by caller afterwards.

    The file is deliberately on ordinary disk, not /dev/shm/tmpfs. fsync makes
    its mapping clean and reclaimable before SDK creates its temporary buffers.
    """
    path=Path(path)
    if not array.flags.c_contiguous or array.dtype!=np.float32:
        raise ValueError('contiguous FP32 decoder input required')
    if shutil.disk_usage(str(path.parent)).free<array.nbytes+16*1024*1024:
        raise OSError('insufficient_space_for_clean_decoder_input')
    with path.open('xb') as stream:
        np.save(stream,array,allow_pickle=False)
        stream.flush();os.fsync(stream.fileno())
