"""Sole microphone/player arbiter; transport stays responsive during SDK work."""
from collections import OrderedDict
import hashlib
import math
import multiprocessing
from pathlib import Path
import subprocess
import threading
import time

from board_audio import Microphone
from audio_timing import AudioTiming
from model_process import ProcessModels, Cancelled


def short(value, size=160):
    return str(value).encode('utf-8')[:size].decode('utf-8', errors='ignore')


class Controller:
    def __init__(self, root, event, models_factory=ProcessModels, mic_factory=Microphone,
                 player_factory=subprocess.Popen):
        self.root, self.event = Path(root), event
        self.lock = threading.RLock()
        self.generation = multiprocessing.get_context('spawn').Value('Q', 0)
        self.models_factory, self.player_factory = models_factory, player_factory
        self.models = None
        self.stopped = True
        self.mode = 'text'
        self.failed = None
        self.active = None
        self.jobs = OrderedDict()
        self.count = 0
        self.threads = set()
        self.player = None
        self.cleanup = False
        self.closing = False
        self.paused = threading.Event()
        self.paused.set()
        self.microphone = mic_factory(self, 'plughw:CARD=UACDemoV10,DEV=0', timing=AudioTiming(end_silence_frames=15, continuation_ratio=.5,
                    playback_tail_seconds=.2, capture_restart_seconds=.02, resume_quiet_frames=4))

    @property
    def state(self): return 'ready' if self.models is not None and not self.failed else 'unloaded'

    def emit(self, value): self.event('microphone', value)

    def status(self):
        with self.lock:
            mic = self.microphone.status()
            return dict(api=2, ready=self.state == 'ready', stopped=self.stopped,
                        mode=self.mode, epoch=self.generation.value,
                        active=self.active, latest=self.count or None, pending=self.cleanup,
                        failed=short(self.failed, 120) if self.failed else None,
                        mic=dict(running=mic['running'], open=mic['microphone_open'], phase=mic['phase'],
                                 error=short(mic['last_error'], 100) if mic['last_error'] else None,
                                 level=mic['mean_abs_level']), playing=self.player is not None,
                        backend='board_asr_bert_tts_shared_owner', hardware_actions=False)

    def _spawn(self, target, *args):
        def run():
            try: target(*args)
            finally:
                with self.lock: self.threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=False)
        self.threads.add(thread)
        thread.start()

    def _check(self, job):
        if self.closing or self.stopped or job['epoch'] != self.generation.value:
            raise Cancelled('Stale interaction')

    def _new_job(self, op):
        if self.stopped or self.closing or self.active is not None or self.cleanup or self.failed:
            raise RuntimeError('Stopped or busy; no input queued')
        self.count += 1
        job = dict(id=self.count, op=op, epoch=self.generation.value, state='queued')
        self.jobs[self.count] = job
        self.active = self.count
        while len(self.jobs) > 64: self.jobs.popitem(last=False)
        return job

    def _public(self, job):
        return {k:v for k,v in job.items() if k != 'epoch'}

    def dispatch(self, op, args):
        if op == 'voice_status': return self.status()
        if op == 'stop_all':
            self.cancel('explicit_stop')
            return self.status()
        with self.lock:
            if op == 'job':
                if type(args.get('id')) is not int or args['id'] not in self.jobs:
                    raise ValueError('Job expired or unknown')
                return self._public(self.jobs[args['id']])
            if op not in ('resume', 'set_mode', 'load_voice', 'speak', 'command_text'):
                raise ValueError('Unsupported operation')
            if args.get('epoch') != self.generation.value:
                raise RuntimeError('Stale board epoch; refresh status')
            if op == 'resume':
                if self.active is not None or self.cleanup or self.microphone.running or self.failed:
                    raise RuntimeError('Prior work is still draining or failed')
                self.generation.value += 1
                self.stopped = False
                self.mode = 'text'
                return self.status()
            if op == 'set_mode':
                desired = args.get('mode')
                if desired not in ('text', 'voice'): raise ValueError('Unknown interaction mode')
                if self.stopped: raise RuntimeError('Resume explicitly before choosing a mode')
                if desired == 'text':
                    self._cancel_locked('text_mode', stopped=False)
                else:
                    if self.state != 'ready' or self.active is not None or self.cleanup:
                        raise RuntimeError('Load models and wait for idle before listening')
                    if self.microphone.running:
                        if self.mode == 'voice': return self.status()
                        raise RuntimeError('Microphone still closing')
                    self.generation.value += 1
                    self.mode = 'voice'
                    self.paused.clear()
                    self.microphone.start()
                return self.status()
            if op == 'load_voice' and self.models is not None: return self.status()
            if op in ('speak','command_text'):
                text = args.get('text')
                if not isinstance(text, str) or not text.strip() or len(text) > 120 or '\0' in text or len(text.encode()) > 600:
                    raise ValueError('Text requires 1..120 characters, <=600 bytes')
                if self.state != 'ready': raise RuntimeError('Models not loaded')
                if op=='command_text' and self.mode!='text':raise RuntimeError('text_mode_required')
                if op=='command_text' and (type(args.get('control_epoch')) is not int or
                        type(args.get('submitted_at')) not in (int,float)):
                    raise ValueError('captured_control_context_required')
            job = self._new_job(op)
            if op == 'load_voice': job['announce'] = args.get('announce') is True
            if op in ('speak','command_text'): job['text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
            if op=='command_text':
                job['control_epoch']=args['control_epoch']
                job['capture_completed_at']=args['submitted_at']
            self.paused.set()
            self._spawn(self._execute, job, args.get('text'))
            return self._public(job)

    def _cancel_locked(self, reason, stopped=True):
        self.generation.value += 1
        self.stopped = stopped
        self.mode = 'text'
        self.paused.set()
        self.microphone.stop_event.set()
        if self.active is not None: self.jobs[self.active]['state'] = 'cancelling'
        if not self.cleanup:
            self.cleanup = True
            self._spawn(self._cleanup_audio)
        self.event('interaction_cancel', dict(reason=reason, epoch=self.generation.value))

    def cancel(self, reason):
        with self.lock: self._cancel_locked(reason)

    def _cleanup_audio(self):
        try:
            self.microphone.request_stop()
            with self.lock: player = self.player
            if player is not None and player.poll() is None:
                try:
                    player.terminate()
                    player.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    player.kill(); player.wait(timeout=1)
                except ProcessLookupError: pass
        finally:
            with self.lock: self.cleanup = False

    def _wait_capture_closed(self, job):
        deadline = time.monotonic() + 4
        while True:
            # close_capture clears its reference while holding this lock until
            # arecord exits. A reference alone is not a device-release receipt.
            if self.microphone.lock.acquire(blocking=False):
                try: closed = not self.microphone.status()['microphone_open']
                finally: self.microphone.lock.release()
                if closed: return
            self._check(job)
            if time.monotonic() > deadline: raise RuntimeError('Capture did not close before playback')
            time.sleep(.02)

    def _play(self, job, audio):
        if audio is None: return False
        if (audio['channels'] != 1 or type(audio['sample_rate']) is not int or
                not 8000 <= audio['sample_rate'] <= 48000 or len(audio['data']) > 4194304):
            raise ValueError('Invalid model PCM')
        self._wait_capture_closed(job)
        with self.lock:
            self._check(job)
            player = self.player_factory(['aplay', '-q', '-D', self.microphone.device,
                      '-t', 'raw', '-f', 'S16_LE', '-r', str(audio['sample_rate']), '-c', '1'],
                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.player = player
            job['state'] = 'playing'
        try:
            player.communicate(audio['data'], timeout=len(audio['data'])/(audio['sample_rate']*2)+10)
            self._check(job)
            if player.returncode != 0: raise RuntimeError('ALSA playback failed')
            return True
        finally:
            if player.poll() is None:
                player.kill(); player.wait()
            with self.lock:
                self.player = None
                self.microphone.playback_finished()
            # Retain the previous half-duplex acoustic tail guard.
            time.sleep(self.microphone.timing.playback_tail_seconds)

    def _execute(self, job, payload):
        began=time.monotonic()
        job['timing']={}
        try:
            with self.lock:
                self._check(job)
                job['state'] = 'running'
            if job['op'] == 'load_voice':
                models = self.models_factory(self.root, self.generation)
                with self.lock: self.models = models
                self._check(job)
                if job.get('announce'):
                    announcement = models.run('speak', job['epoch'], '语音模型加载完成。')
                    self._check(job)
                    job['playback_completed'] = self._play(job, announcement['audio'])
                    job['message'] = '语音模型加载完成'
            else:
                self._wait_capture_closed(job)
                model_op={'listen':'audio','command_text':'command_text'}.get(job['op'],'speak')
                result = self.models.run(model_op, job['epoch'], payload)
                job['timing'].update(result.pop('timing',{}))
                validation_started=time.monotonic()
                self._check(job)
                if result.get('input_rejected'):
                    if job['op']!='listen' or result.get('audio') is not None or 'vehicle_nlu' in result:
                        raise ValueError('invalid_input_rejection_envelope')
                    job.update(input_rejected=True,rejection_reason=result['rejection_reason'],
                               feature_frames=result.get('feature_frames'),max_feature_frames=result.get('max_feature_frames'))
                    # Retain all resident models. Do not turn the rest of a long
                    # utterance into a new command until a quiet gap is observed.
                    self.microphone.require_quiet()
                if job['op']=='command_text' and not isinstance(result.get('vehicle_nlu'),dict):
                    raise ValueError('model_command_envelope_required')
                if job['op']=='command_text' and (result.get('text')!=payload or
                        result['vehicle_nlu'].get('source_text')!=payload or
                        hashlib.sha256(payload.encode()).hexdigest()!=job['text_sha256']):
                    raise ValueError('model_command_request_binding_mismatch')
                if job['op'] in ('listen','command_text') and 'vehicle_nlu' in result:
                    # Deliver complete text/slots before playback; display truncation
                    # must never turn an utterance prefix into a vehicle command.
                    with self.lock:
                        self._check(job)
                        job['vehicle_nlu_delivered'] = True
                        receipt = self.event('vehicle_nlu_ready', dict(text=result['text'],
                            nlu=result['vehicle_nlu'], control_epoch=job.get('control_epoch'),
                            capture_completed_at=job.get('capture_completed_at', float('-inf'))))
                        if isinstance(receipt, dict):
                            result['message'] = receipt.get('message', '指令未执行，请查看控制回执')
                            job['control_receipt'] = receipt
                    if job['op'] in ('listen', 'command_text'):
                        self._check(job)
                        if not isinstance(receipt,dict):
                            raise ValueError('parent_control_receipt_required')
                        # Both microphone and text commands return audio=None
                        # until the parent has checked the result. Synthesize
                        # that receipt, never a model claim of vehicle motion.
                        from fm_autonomy.voice_feedback import speech_for
                        announcement = speech_for(receipt)
                        job['speech_policy'] = 'wake_or_error_only'
                        job['spoken_message'] = announcement
                        result['audio'] = None
                        if announcement:
                            speech=self.models.run('speak',job['epoch'],announcement)
                            job['timing'].update(speech.get('timing',{}))
                            self._check(job)
                            result['audio']=speech['audio']
                playback_started=time.monotonic()
                capture_at=job.get('capture_completed_at')
                if type(capture_at) in (int,float) and math.isfinite(capture_at):
                    job['timing']['capture_to_play_request_seconds']=playback_started-capture_at
                audio = result.pop('audio')
                played = self._play(job, audio) if audio is not None else False
                job['timing']['playback_and_tail_seconds']=time.monotonic()-playback_started
                with self.lock:
                    self._check(job)
                    job.update(text=short(result['text']), message=short(result['message']),
                               intent=short(result['intent'], 80), playback_completed=played,
                               hardware_executed=False, acoustic_verified=False,
                               display_truncated=any(len(str(result[k]).encode()) > 160 for k in ('text','message')))
            with self.lock:
                self._check(job)
                job['state'] = 'completed'
        except Exception as exc:
            with self.lock:
                stale = job['epoch'] != self.generation.value or self.stopped
                job['state'] = 'cancelled' if stale or isinstance(exc, Cancelled) else 'failed'
                if job['state'] == 'failed':
                    job['error'] = short(exc, 140)
                    self.failed = short(exc, 140)
                    self._cancel_locked('model_or_audio_failure')
                    job['state'] = 'failed'
        finally:
            with self.lock:
                job['timing']['total_processing_seconds']=time.monotonic()-began
                if self.active == job['id']: self.active = None
                if self.mode == 'voice' and not self.stopped and not self.failed:
                    self.paused.clear()
                self.event('job_finished', self._public(job))

    def admit_microphone(self):
        with self.lock:
            if self.mode != 'voice' or self.microphone.stop_event.is_set(): return None
            try: return self._new_job('listen')
            except RuntimeError: return None

    def notify_motion_failure(self, report, message, epoch):
        """Called outside the control lock; never interrupt an active utterance."""
        if not self.lock.acquire(blocking=False): return False
        try:
            if self.closing or self.stopped or self.failed or self.models is None or epoch != self.generation.value:
                return True  # cancellation fences late announcements too
            if self.active is not None or self.cleanup: return False
            job = self._new_job('speak')
            job['notification'] = 'motion_failure'
            job['terminal_receipt'] = report
            self.paused.set()  # microphone loop closes capture before _play
            self._spawn(self._execute, job, message)
            return True
        finally:
            self.lock.release()

    def finish_microphone(self, job, pcm):
        self.paused.set()
        self._execute(job, pcm.astype('<i2', copy=False).tobytes())

    def abort_microphone(self, job, reason):
        with self.lock:
            job['state'] = 'cancelled'
            if self.active == job['id']: self.active = None
            self.event('capture_aborted', dict(id=job['id'], reason=reason))

    def close(self):
        with self.lock:
            self.closing = True
            self._cancel_locked('service_shutdown')
        # Wait for native SDK cleanup. Never start a replacement owner in parallel.
        self.microphone.stop_event.set()
        if self.microphone.thread: self.microphone.thread.join()
        while True:
            with self.lock: threads = list(self.threads)
            if not threads: break
            for thread in threads: thread.join()
        if self.models is not None: self.models.close()
