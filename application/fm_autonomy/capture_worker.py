"""Optional camera producer, co-located with the existing shared NPU hub."""
import sys
import threading
from .dependencies import ROOT

class CaptureWorker:
    def __init__(self,hub,config,output,event):
        self.stop=threading.Event()
        def work():
            try:
                path=str(ROOT/'fastbev_candidate/deploy/script/vehicle')
                if path not in sys.path:sys.path.insert(0,path)
                from run_board_mjpeg_bev import main
                main(['--run','--config',str(config),'--seconds','0','--output-dir',str(output)],
                     hub=hub,stop_event=self.stop)
            except Exception as exc:
                hub.cancel()
                event('perception_failed',dict(reason=str(exc)[:160]))
        self.thread=threading.Thread(target=work,name='six-camera-producer',daemon=True)
        self.thread.start()
    def close(self):
        self.stop.set()
        self.thread.join(1)
        return not self.thread.is_alive()
