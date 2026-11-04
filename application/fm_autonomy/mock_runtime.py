"""Explicit offline runtime; no Bluetooth, UART, NPU, microphone or CAN devices."""
import time
from .dependencies import add_vendor
from .can_actor import VehicleActor
from .arbiter import Arbiter, ControlLoop
from .application import IntegratedApplication
from .perception import LatestPerception
from .planner import CruisePlanner
from .contracts import Chassis
add_vendor('vehicle', 'vehicle/vendor_fvb1')
from motion import Policy
from pc_vehicle import VehicleClient


class MockVoice:
    def __init__(self):
        self.epoch, self.ready, self.stopped, self.mode = 0, False, True, 'text'
        self.jobs = {}
    def status(self):
        return dict(api=2, epoch=self.epoch, ready=self.ready, stopped=self.stopped, mode=self.mode,
                    active=None, latest=max(self.jobs) if self.jobs else None, pending=False,
                    failed=None, playing=False, mic=dict(running=self.mode=='voice', open=False,
                        phase='offline_mock', error=None, level=0), backend='offline_mock', hardware_actions=False)
    def dispatch(self, op, args):
        if op == 'voice_status': return self.status()
        if op == 'job': return self.jobs[args['id']]
        if op == 'stop_all': self.cancel('stop'); return self.status()
        if args.get('epoch') != self.epoch: raise ValueError('stale_voice_epoch')
        if op == 'resume': self.stopped=False; self.epoch+=1; return self.status()
        if self.stopped: raise ValueError('resume_required')
        if op == 'set_mode': self.mode=args['mode']; self.epoch+=1; return self.status()
        if op == 'load_voice': self.ready=True
        if op in ('load_voice', 'speak'):
            import hashlib
            identifier=len(self.jobs)+1
            job=dict(id=identifier, op=op, state='completed', simulated=True,
                     playback_completed=op=='speak', acoustic_verified=False, hardware_executed=False)
            if op=='speak': job['text_sha256']=hashlib.sha256(args['text'].encode()).hexdigest()
            self.jobs[identifier]=job
            return job
        raise ValueError(op)
    def cancel(self, reason): self.stopped=True; self.mode='text'; self.epoch+=1
    def close(self): self.cancel('closed')


class MockRadio:
    simulated=True
    def __init__(self, unused):
        self.sid=None; self.seq=0
        self.commands=self
        self.actor=VehicleActor(backend='mock', enable_tx=True, authorization_seconds=3600,
            policy=Policy(allow_motion=True, profile='r3_four_wheel_diff_gmr', max_yaw_mrad_s=800))
        self.arbiter=Arbiter(self.actor, CruisePlanner(Chassis(.3,.2,.03,.02,.05,.1,.4,.25)),
                             LatestPerception('offline'), 'offline', allow_synthetic=True,
                             autonomy_enabled=True)
        self.app=IntegratedApplication(self.arbiter, MockVoice())
        self.vehicle=VehicleClient(self)
        self.loop=ControlLoop(self.arbiter)
        self.loop.start()
    def connect(self):
        self.sid='offline-session'
        self.app.connected()
        return dict(connected=True, simulated=True)
    def call(self, op, args=None): return self.app.dispatch(op, args or {})
    def exchange(self, request, timeout=2):
        try: return dict(ok=True, data=self.call(request['op'], request['args']))
        except Exception as exc: return dict(ok=False, error=str(exc))
    def abort(self): self.close()
    def close(self):
        self.sid=None
        self.loop.close()
        self.app.close()
        return dict(goodbye_acknowledged=True, simulated=True)
