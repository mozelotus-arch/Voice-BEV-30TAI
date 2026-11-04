"""Adapt the accepted persistent voice UI to the sole Gateway SPP worker."""
from pathlib import Path
import sys
from .dependencies import ROOT


def make_bridge(gateway, folder, mock=False):
    sys.path.insert(0, str(ROOT/'voice_proxy'))
    from bridge import Bridge
    from radio_client import CommandRejected

    class Client:
        def connect(self): return gateway.connect()
        def call(self, op, args=None):
            try:
                return gateway.application(op, args)
            except RuntimeError as exc:
                if gateway.status()['connected']:
                    raise CommandRejected(str(exc)) from None
                raise
        def close(self):
            gateway.disconnect()
            return dict(goodbye_acknowledged=False, disconnect_requested=True)

    class SharedBridge(Bridge):
        def request(self,action,data):
            getattr(gateway,'resume_application_polling',lambda:None)()
            return super().request(action,data)
        def _finish_stop(self):
            # Disconnecting the radio must not send global stop_all to a
            # microphone session explicitly owned by the independent LAN entry.
            with self.lock:
                epoch, disconnect = self.epoch, self.stop_disconnect
            if disconnect and self.client is not None:
                voice = self.client.call('voice_status')
                if voice.get('local_active'):
                    self._close_radio()
                    with self.lock:
                        if epoch != self.epoch: return
                        self.stop_pending=False; self.link='disconnected'
                        self.voice={}; self.last_reply=None; self.active=None
                        self._event('radio_disconnected', local_voice_preserved=True)
                    return
            return super()._finish_stop()

        def status(self):
            # The radio belongs to Gateway, including connections made by the
            # vehicle page. Adopt that transport without opening/reconnecting it.
            physical = gateway.status()
            with self.condition:
                if not self.closing and not self.stop_pending and self.pending is None:
                    if physical['connected'] and self.link in ('disconnected', 'error'):
                        self.client = Client()
                        self.link = 'connected'
                        self.error = None
                        self.wanted = False
                        self.condition.notify_all()
                    elif not physical['connected'] and self.link == 'connected':
                        self.client = None
                        self.link = 'disconnected'
                        self.wanted = False
                        self.epoch += 1
                        self.voice = {}
                        self.last_reply = None
                        self.stopped = True
            state = super().status()
            state['connection']['source'] = 'unique_pc_gateway'
            state['connection']['automatic_reconnect'] = False
            return state

        def _fault(self, exc):
            super()._fault(exc)
            # A disconnect from the vehicle UI also disconnects voice. The
            # voice polling worker must not undo it by reopening the radio.
            with self.condition:
                self.wanted = False

        def _poll(self):
            if gateway.status()['armed']:
                return  # preserve old voice last_reply; do not fabricate freshness
            with self.lock:
                latest=self.voice.get('latest')
                settled=(latest is None or isinstance(self.voice_job,dict)
                         and self.voice_job.get('id')==latest
                         and self.voice_job.get('state') in ('completed','failed','cancelled'))
                idle_text=(self.active is None and self.pending is None and not self.stop_pending
                           and self.voice.get('mode')=='text' and self.voice.get('active') is None
                           and not self.voice.get('pending') and not self.voice.get('playing') and settled)
            if idle_text and getattr(gateway,'manual_view_active',lambda:False)():
                return  # visible manual page needs the radio; voice status ages normally
            result=super()._poll()
            with self.lock:
                if not self.stop_pending:
                    self.stopped=bool(self.voice.get('stopped', True))
            return result
    return SharedBridge(Client, folder, mode='mock' if mock else 'bluetooth')


def make_voice_facade(bridge):
    sys.path.insert(0, str(ROOT/'frontend'))
    from board_gateway import BoardGateway

    class SharedVoiceFacade(BoardGateway):
        def __init__(self): self.bridge = bridge
        def status(self, fresh=False): return dict(available=True, state=self.bridge.status())
        def command(self, action, expected=None, **data):
            return self.bridge.request(action, dict(expected or {}, **data))
    return SharedVoiceFacade()
