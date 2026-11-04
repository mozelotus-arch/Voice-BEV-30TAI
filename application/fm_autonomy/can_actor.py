"""Derived from pinned VehicleActor; adds a CAN-worker-enforced input expiry.

Baseline source hash is recorded in vendor/manifest.json. No hardware imports
are used by the mock backend. Original board commissioning remains required.
"""
import math
import multiprocessing
import signal
import threading
import time
from dataclasses import asdict
from fm_autonomy.dependencies import add_vendor
add_vendor("vehicle")
from motion import MotionController,Policy
from wheeltec import FeedbackAssembler,is_arbitration_notification


def _serve(pipe,backend,interface,policy_dict,enable_tx,authorization_seconds):
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    transport=None; controller=None; close_until=None; parent_gone=False
    stopping=[False]
    signal.signal(signal.SIGTERM,lambda *args:stopping.__setitem__(0,True))
    try:
        if backend=='mock':
            from mock_transport import MockCAN
            transport=MockCAN()
        elif backend=='socketcan':
            from socketcan_transport import SocketCAN
            transport=SocketCAN(interface,enable_tx=enable_tx)
        else: raise ValueError('unsupported_backend')
        controller=MotionController(transport.send,Policy(**policy_dict),tx_enabled=enable_tx)
        assembler=FeedbackAssembler();arbitration_notifications=0
        authorization_until=None if authorization_seconds is None else time.monotonic()+authorization_seconds
        def snapshot():
            result=controller.status()
            result.update(backend=backend,hardware_executed=backend=='socketcan',
                          can_tx_enabled=enable_tx,feedback_groups=assembler.completed,
                          feedback_rejected=assembler.rejected,
                          arbitration_notifications=arbitration_notifications)
            result['feedback_received_at'] = (None if controller.last_telemetry is None
                                               else controller.last_telemetry.received_at)
            if backend=='mock':result.update(mock_tx_count=len(transport.tx),mock_last_tx=transport.tx[-1:] or None)
            return result
        pipe.send({'ok':True,'data':snapshot()})
        while True:
            controller.tick()
            now=time.monotonic()
            if stopping[0] and close_until is None:
                controller.stop('service_signal');close_until=now+.65
            if authorization_until is not None and now>=authorization_until and controller.policy.allow_motion:
                controller.stop('operator_authorization_expired')
                controller.policy=Policy()
            for unused in range(64):
                try:
                    frame=transport.receive()
                    if frame is None:break
                    if is_arbitration_notification(frame):
                        arbitration_notifications+=1
                        continue
                    telemetry=assembler.feed(frame)
                    if telemetry:controller.observe(telemetry)
                except Exception as exc:
                    controller.tx_fault='receive_fault: '+str(exc)[:100]
                    controller.stop('can_receive_fault')
                    if close_until is None:close_until=now+.65
                    break
            controller.tick()
            if close_until is not None:
                if now>=close_until:break
                # Surface an actual terminal fault instead of making a waiting
                # parent see only a generic IPC timeout during zero retries.
                if not parent_gone:
                    try:
                        if pipe.poll(0):
                            op,args=pipe.recv()
                            if op=='vehicle.status':pipe.send({'ok':True,'data':snapshot()})
                            elif op!='close':pipe.send({'ok':False,'error':'CAN_worker_stopping: '+controller.reason})
                    except (EOFError,OSError):parent_gone=True
                time.sleep(.005);continue
            try:
                has_request=pipe.poll(.005)
                if not has_request:continue
                op,args=pipe.recv()
            except (EOFError,OSError):
                parent_gone=True;controller.stop('parent_disconnected');close_until=now+.65
                continue
            if op=='close':
                controller.stop('service_shutdown');close_until=now+.65
                continue
            try:
                if op=='vehicle.status':
                    if args:raise ValueError('status_takes_no_arguments')
                    result=snapshot()
                elif op=='vehicle.arm':
                    if args:raise ValueError('arm_takes_no_arguments')
                    result=controller.arm()
                elif op=='vehicle.drive':result=controller.drive(args)
                elif op in ('autonomy.drive','metric.drive','command.drive'):
                    if controller.policy.manual_only or (op=='autonomy.drive' and controller.policy.user_commands_only):
                        reason='ground_manual_controls_only' if controller.policy.manual_only else 'ground_user_commands_only'
                        controller.stop(reason)
                        raise ValueError(reason)
                    # Enforced INSIDE the independent CAN process, even if the
                    # planner/voice/IPC parent stops making progress.
                    valid_until=args.get('valid_until')
                    now=time.monotonic()
                    if (set(args)!={'m','t','v','valid_until'} or
                        type(valid_until) not in (int,float) or
                        not math.isfinite(valid_until) or not now<valid_until<=now+.5):
                        controller.stop('invalid_perception_or_command_deadline')
                        raise ValueError('invalid_perception_or_command_deadline')
                    result=controller.drive({k:args[k] for k in ('m','t','v')})
                    controller.deadline=min(controller.deadline,valid_until)
                    result['d']=max(0,int((controller.deadline-time.monotonic())*1000))

                elif op=='vehicle.stop':
                    controller.stop('operator_stop');result=snapshot()
                elif op=='_cancel':
                    controller.stop(str(args['reason'])[:80]);result=snapshot()
                elif op=='mock.feedback' and backend=='mock':
                    transport.feedback_enabled=args['enabled'];result={}
                else:raise ValueError('unknown_vehicle_operation')
                reply={'ok':True,'data':result}
            except Exception as exc:reply={'ok':False,'error':str(exc)[:180]}
            try:pipe.send(reply)
            except (BrokenPipeError,EOFError,OSError):
                parent_gone=True;controller.stop('parent_disconnected');close_until=time.monotonic()+.65
        if not parent_gone:
            try:pipe.send({'ok':True,'data':snapshot(),'closed':True})
            except (BrokenPipeError,EOFError,OSError):pass
    except BaseException as exc:
        if controller is not None:
            controller.stop('worker_failure')
            until=time.monotonic()+.65
            while time.monotonic()<until:controller.tick();time.sleep(.005)
        try:pipe.send({'ok':False,'error':repr(exc)[:180]})
        except (BrokenPipeError,EOFError,OSError):pass
    finally:
        if transport is not None:transport.close()
        pipe.close()


class VehicleActor:
    def __init__(self,backend='socketcan',interface='can0',policy=None,enable_tx=False,
                 authorization_seconds=0):
        self.backend=backend; self.closed=False; self.lock=threading.Lock()
        self.close_receipt=None
        context=multiprocessing.get_context('spawn')
        self.pipe,child=context.Pipe()
        self.process=context.Process(target=_serve,
            args=(child,backend,interface,asdict(policy or Policy()),enable_tx,authorization_seconds),
            name='vehicle-can-deadline')
        self.process.start();child.close()
        try:self.startup=self._receive(5)
        except BaseException:
            self.pipe.close();self.process.join(2);self.closed=True
            raise

    def _receive(self,timeout):
        if not self.pipe.poll(timeout):
            # Closing the pipe makes the independent worker stop; never resend.
            self.pipe.close();self.closed=True
            raise TimeoutError('CAN_worker_timeout_no_command_replay')
        reply=self.pipe.recv()
        if not reply.get('ok'): raise RuntimeError(reply.get('error','CAN_worker_failed'))
        return reply['data']

    def dispatch(self,op,args):
        if self.closed:raise RuntimeError('vehicle_worker_closed')
        with self.lock:
            self.pipe.send((op,args))
            # Parent IPC may be delayed by Linux scheduling. This wait does
            # not extend the independent worker's 500 ms motion deadline.
            return self._receive(.3)

    def cancel(self,reason):
        if self.closed:return
        try:self.dispatch('_cancel',{'reason':reason})
        except Exception:
            self.pipe.close();self.closed=True

    def close(self):
        if not self.closed:
            with self.lock:
                try:
                    self.pipe.send(('close',{}))
                    self.close_receipt=self._receive(2)
                finally:self.pipe.close();self.closed=True
        self.process.join(3)
        if self.process.is_alive():raise RuntimeError('CAN_worker_not_closed_physical_stop_unconfirmed')
        if self.process.exitcode!=0:raise RuntimeError('CAN_worker_abnormal_exit')
        return self.close_receipt
