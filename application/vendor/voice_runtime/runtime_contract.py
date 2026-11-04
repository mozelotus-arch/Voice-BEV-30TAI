"""Hardware-independent validation at the compiled-model boundary."""
import json
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class InputContract:
    shape: tuple
    dtype: str
    layout: str

    @classmethod
    def from_sdk_json(cls, description):
        info=json.loads(description)
        dtypes={'@fp(32)':'float32','@fp(16)':'float16',
            '@sint(64)':'int64','@sint(32)':'int32','@sint(16)':'int16',
            '@sint(8)':'int8','@uint(64)':'uint64','@uint(32)':'uint32',
            '@uint(16)':'uint16','@uint(8)':'uint8'}
        if info['element_dtype'] not in dtypes:
            raise ValueError('No input adapter for SDK dtype '+info['element_dtype'])
        shape=tuple(info['shape'])
        if not shape or any(not isinstance(x,int) or x<=0 for x in shape):
            raise ValueError('This runner requires a compiled static input shape')
        return cls(shape,dtypes[info['element_dtype']],info.get('layout',''))


def validate_inputs(arrays,contracts):
    if len(arrays)!=len(contracts):
        raise ValueError('Incorrect compiled graph input count')
    checked=[]
    for index,(array,contract) in enumerate(zip(arrays,contracts)):
        if not isinstance(array,np.ndarray):
            raise TypeError('Input {} must be a NumPy array'.format(index))
        if array.shape!=contract.shape:
            raise ValueError('Input {} shape {} differs from compiled {}'.format(index,array.shape,contract.shape))
        if array.dtype!=np.dtype(contract.dtype):
            raise TypeError('Input {} dtype {} differs from compiled {}'.format(index,array.dtype,contract.dtype))
        array=np.ascontiguousarray(array)
        if np.issubdtype(array.dtype,np.floating):
            # The BEV input contains 40,960,000 floats. Check every value,
            # but bound the boolean temporary to 256 KiB instead of 41 MB.
            flat=array.reshape(-1)
            for start in range(0,flat.size,262144):
                if not np.isfinite(flat[start:start+262144]).all():
                    raise ValueError('Input {} contains non-finite values'.format(index))
        checked.append(array)
    return checked


def detach_result(result,temporary_buffers):
    """Prevent SDK-backed views escaping into the next device inference/reset."""
    if isinstance(result,np.ndarray):
        return result.copy() if any(np.may_share_memory(result,b) for b in temporary_buffers) else result
    if isinstance(result,np.generic):
        return result.item()
    if result is None or isinstance(result,(str,int,float,bool)):
        return result
    if isinstance(result,list):
        return [detach_result(x,temporary_buffers) for x in result]
    if isinstance(result,tuple):
        return tuple(detach_result(x,temporary_buffers) for x in result)
    if isinstance(result,dict):
        if any(not isinstance(k,(str,int,float,bool)) for k in result):
            raise TypeError('Reducer dictionary keys must be scalar values')
        return {k:detach_result(v,temporary_buffers) for k,v in result.items()}
    raise TypeError('Reducer must return eager scalar/list/tuple/dict/NumPy data')
