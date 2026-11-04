"""Keep Windows awake only while an active validation/client owns this context."""
import ctypes,datetime,json,os
from pathlib import Path

class KeepAwake:
    def __init__(self,path):self.path=Path(path);self.api=None
    def event(self,kind,result):
        from audit import Audit
        log=Audit(self.path)
        try:log.event(kind,{'result':result})
        finally:log.close()
    def __enter__(self):
        if os.name!='nt':return self
        self.api=ctypes.WinDLL('kernel32',use_last_error=True).SetThreadExecutionState
        self.api.argtypes=[ctypes.c_uint32];self.api.restype=ctypes.c_uint32
        result=self.api(0x80000001)
        if not result:raise ctypes.WinError(ctypes.get_last_error())
        self.event('system_required_set',int(result));return self
    def __exit__(self,*unused):
        if self.api is not None:
            result=self.api(0x80000000)
            self.event('system_required_cleared',int(result))
            if not result:raise ctypes.WinError(ctypes.get_last_error())
