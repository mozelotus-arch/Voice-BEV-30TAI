"""One SDK owner in a separate process; cancellation crosses the process boundary."""
from contextlib import ExitStack
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time


class LoadMetrics:
    def emit(self, event_name, **kwargs):
        # Model events legitimately carry a `name` field for graph identity.
        if event_name == 'initialization_phase':
            print(json.dumps(dict(event='model_load_phase', **kwargs)), flush=True)


def native_quarantined():
    module=sys.modules.get('graph_runtime')
    return bool(module and getattr(module,'has_uncertain_native_call',lambda:False)())


def hold_quarantined_process(pipe):
    """Keep native owners, mappings and locks alive until external recovery.

    No protocol operation, including close, resumes or destroys this owner.
    Parent EOF is not proof of DMA completion either.
    """
    while True:
        try:
            pipe.send(('quarantined','native_completion_unknown_platform_recovery_required'))
            pipe.recv()
        except (OSError,EOFError):
            time.sleep(1)


def serve(pipe, root, generation, backend_factory):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Only the communication parent asks us to close, after native calls return.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    backend = None
    try:
        backend = (backend_factory or BoardModels)(root, generation)
        pipe.send(('ready', {}))
        while True:
            op, epoch, payload = pipe.recv()
            if op == 'close':
                break
            try:
                result = backend.run(op, epoch, payload)
                pipe.send(('result', result))
            except Exception as exc:
                if native_quarantined():
                    hold_quarantined_process(pipe)
                elif generation.value != epoch or isinstance(exc, Cancelled):
                    pipe.send(('cancelled', {}))
                elif getattr(backend,'quiescent_after_error',lambda:False)():
                    # Explicit owner proof, never inferred from exception text.
                    pipe.send(('quiescent_error',str(exc)[:250]))
                else:
                    pipe.send(('error', str(exc)[:250]))
    except BaseException as exc:
        if native_quarantined():
            hold_quarantined_process(pipe)
        try: pipe.send(('fatal', str(exc)[:250]))
        except (OSError, EOFError): pass
    finally:
        if native_quarantined():
            hold_quarantined_process(pipe)
        try:
            if backend is not None: backend.close()
            pipe.send(('closed', {}))
        except BaseException:
            if native_quarantined():
                hold_quarantined_process(pipe)
        pipe.close()


class Cancelled(RuntimeError):
    pass

class InitializationClosedError(RuntimeError):
    """Initialization failed; explicit child close receipt and clean exit observed."""


class QuiescentOperationError(RuntimeError):
    """Child attested idle healthy owner after an operation failed."""


class BoardModels:
    def __init__(self, root, generation):
        self.generation = generation
        self.epoch = generation.value
        self.session_epoch = None
        self.audio = None
        self.stack = ExitStack()
        contract = json.loads((Path(root)/'tts_source_contract.json').read_text())
        for name, digest in contract.items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('Installed voice source changed: ' + name)
        deploy = Path('/opt/voice-web-stopall-20261021')
        config = json.loads((deploy/'board_config.json').read_text())
        for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[name] = '1'
        for p in config['dependency_roots']:
            if p not in sys.path: sys.path.append(p)
        sys.path.insert(0, str(deploy/'vendor/runtime'))
        sys.path.insert(0, str(deploy/'vendor/policy'))
        from voice_runtime_factory import configured_runtime
        from interaction_control import GuardedCall
        try:
            self.runtime = self.stack.enter_context(configured_runtime(
                deploy/'board_config.json', self, backend='board_hybrid', metrics=LoadMetrics()))
            for name, methods in (('asr', {'transcribe'}), ('tts', {'synthesize'}), ('sink', {'write'})):
                setattr(self.runtime, name, GuardedCall(getattr(self.runtime, name), methods, self.check))
            session = self.runtime.session
            session.nlu = GuardedCall(session.nlu, {'understand'}, self.check)
            # This is the original in-memory household simulator, never CAN.
            session.dialogue.executor = GuardedCall(session.dialogue.executor, {'apply', 'query'}, self.check)
        except BaseException:
            self.stack.close()
            raise

    def check(self):
        if self.generation.value != self.epoch: raise Cancelled('Interaction cancelled')

    def write(self, pcm, speech_id):
        self.check()
        if pcm.channels != 1 or len(pcm.data) > 4194304: raise ValueError('PCM budget exceeded')
        self.audio = dict(data=pcm.data, sample_rate=pcm.sample_rate, channels=pcm.channels)
        # Only synthesis delivery: parent alone confirms physical playback.
        return dict(success=True, output='parent_playback_pending')

    def run(self, op, epoch, payload):
        self.epoch = epoch
        self.check()
        if self.session_epoch != epoch:
            self.runtime.reset('interaction_generation_changed')
            self.session_epoch = epoch
        self.audio = None
        if op == 'speak':
            pcm, _ = self.runtime.tts.synthesize(payload)
            self.write(pcm, '')
            result = dict(text=payload, message=payload, intent='text_playback')
        elif op == 'audio':
            import numpy as np
            samples = np.frombuffer(payload, dtype='<i2').copy()
            lease = self.runtime.begin_segment()
            details = self.runtime.finish_segment(lease['segment_id'], samples)
            response = details['response']
            result = dict(text=details['asr']['text'], message=response.get('display_message', response.get('message', '')),
                          intent=response.get('nlu', {}).get('raw', {}).get('intent', response.get('event', '')))
        else: raise ValueError('Unknown model operation')
        self.check()
        return dict(result, audio=self.audio, hardware_executed=False)

    def close(self): self.stack.close()


class ProcessModels:
    def __init__(self, root, generation, backend_factory=None):
        self.root = Path(root)
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=serve, args=(child, str(root), generation, backend_factory),
                                       name='voice-models-sdk')
        self.process.start()
        child.close()
        self.closed = False
        self.fatal_received = False
        try: self.receive('ready')
        except BaseException as error:
            if self.fatal_received:
                # serve() is already closing after fatal; sending close races
                # its pipe exit and can hide the actual initialization error.
                self.wait_closed()
                self.closed=True
                self.pipe.close()
                raise InitializationClosedError(str(error)) from error
            try:self.close()
            except BaseException as close_error:raise close_error from error
            raise

    def receive(self, expected):
        # No forced SDK termination. A stuck child leaves the service busy and
        # stopped; it is never replaced by a competing accelerator owner.
        while True:
            if not self.pipe.poll(.2):
                if not self.process.is_alive(): raise RuntimeError('Model process exited without receipt')
                continue
            kind, value = self.pipe.recv()
            if kind == expected: return value
            if kind == 'cancelled': raise Cancelled('Cancelled at model stage boundary')
            if kind == 'quiescent_error':raise QuiescentOperationError(str(value))
            if kind == 'fatal':self.fatal_received=True
            raise RuntimeError('Model process %s: %s' % (kind, value))

    def run(self, op, epoch, payload):
        self.pipe.send((op, epoch, payload))
        return self.receive('result')

    def wait_closed(self):
        received=False
        while True:
            try:pending=self.pipe.poll(.2)
            except (EOFError,OSError):break
            if pending:
                try:kind,value=self.pipe.recv()
                except (EOFError,OSError):break
                if kind=='closed':received=True;break
                if kind=='quarantined':
                    raise RuntimeError('native_completion_unknown_child_retained_platform_recovery_required')
                if kind not in ('fatal','error','quiescent_error','cancelled','result'):
                    raise RuntimeError('unexpected_model_close_response:'+str(kind))
            elif not self.process.is_alive():break
        self.process.join()
        if not received or self.process.exitcode!=0:
            raise RuntimeError('model_child_close_receipt_or_clean_exit_missing')
        receipt=dict(pid=self.process.pid, closed_receipt=received, exitcode=self.process.exitcode,
                     observed_at=time.time(), native_close_returned=True)
        path=self.root/'model_child_exit.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt),encoding='utf-8')
        os.replace(temporary,path)

    def close(self):
        if self.closed:return
        if self.process.is_alive():
            try:self.pipe.send(('close',0,None))
            except (BrokenPipeError,EOFError,OSError):
                pass  # A buffered receipt plus exit 0, never the pipe error, proves close.
        self.wait_closed()
        self.closed=True
        self.pipe.close()
