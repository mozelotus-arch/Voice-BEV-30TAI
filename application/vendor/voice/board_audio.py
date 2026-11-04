"""Bounded ALSA capture, with explicit half-duplex pause and input admission."""
from collections import deque
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from audio_timing import AudioTiming, Endpoint, ResumeGate, FRAME_MS


class Microphone:
    def __init__(self, service, device, threshold=512, timing=None):
        self.service, self.device, self.threshold = service, device, threshold
        self.timing = timing or AudioTiming()
        self.playback_finished_at = None
        self.input_quiet_required = False
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.thread = None
        self.phase = "stopped"
        self.last_error = None
        self.detected = self.completed = self.restarts = 0
        self.level = 0

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def status(self):
        return dict(running=self.running, phase=self.phase, last_error=self.last_error,
                    segments_detected=self.detected, segments_processed=self.completed,
                    capture_restarts=self.restarts, mean_abs_level=self.level,
                    threshold=self.threshold, capture_device=self.device,
                    microphone_open=self.process is not None, input_mode="board_microphone",
                    timing=asdict(self.timing), continuation_threshold=self.threshold * self.timing.continuation_ratio)

    def playback_finished(self):
        self.playback_finished_at = time.monotonic()

    def require_quiet(self):
        self.input_quiet_required = True

    def start(self):
        if self.running:
            raise RuntimeError("Microphone is already running")
        self.stop_event.clear()
        self.input_quiet_required = False
        self.last_error = None
        self.phase = "starting"
        self.thread = threading.Thread(target=self.run, name="board-microphone", daemon=True)
        self.thread.start()

    def close_capture(self):
        with self.lock:
            process, self.process = self.process, None
            if process is None:
                return
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def stop(self):
        self.stop_event.set()
        # The reader uses select with a short timeout, so it can close its own FD.
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        if self.running:
            raise RuntimeError("A spoken turn is still finishing; wait before unloading models")
        self.phase = "stopped"

    def request_stop(self):
        """Interrupt only this capture process; do not wait on neural inference."""
        self.stop_event.set()
        with self.lock:
            process = self.process
            if process is not None and process.poll() is None:
                try:
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                except ProcessLookupError:
                    pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=.25)
        if not self.running:
            self.phase = "stopped"

    def run(self):
        import numpy as np
        failures = 0
        try:
            while not self.stop_event.is_set():
                if self.service.paused.is_set() or self.service.state != "ready":
                    self.phase = "paused"
                    self.stop_event.wait(.1)
                    continue
                job = None
                try:
                    self.phase = "listening"
                    pre = deque(maxlen=9)
                    frames, pending = [], b""
                    endpoint = Endpoint(self.threshold, self.timing)
                    playback_at = self.playback_finished_at
                    gate = (ResumeGate(self.timing.resume_quiet_frames,
                                       endpoint.continuation_threshold)
                            if playback_at is not None and self.timing.resume_quiet_frames else None)
                    if self.input_quiet_required:
                        gate=ResumeGate(max(4,self.timing.resume_quiet_frames),endpoint.continuation_threshold)
                    capture_started = time.monotonic()
                    with self.lock:
                        if self.stop_event.is_set():
                            break
                        self.process = subprocess.Popen(
                            ["arecord", "-q", "-D", self.device, "-f", "S16_LE",
                             "-r", "16000", "-c", "1", "-t", "raw"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        process = self.process
                    first_frame, last_frame, level_at = False, time.monotonic(), 0.
                    while not self.stop_event.is_set():
                        if self.service.paused.is_set():
                            break
                        readable, _, _ = select.select([process.stdout], [], [], .2)
                        if not readable:
                            if time.monotonic() - last_frame > 3:
                                raise RuntimeError("Microphone provided no PCM for 3 seconds")
                            continue
                        data = os.read(process.stdout.fileno(), 1024 - len(pending))
                        if not data:
                            raise RuntimeError("Microphone stream ended")
                        pending += data
                        if len(pending) != 1024:
                            continue
                        frame = np.frombuffer(pending, dtype="<i2").copy()
                        pending = b""
                        last_frame = time.monotonic()
                        if not first_frame:
                            first_frame = True
                            failures = 0
                            self.last_error = None
                            self.service.emit(dict(stage="capture", state="pcm_open",
                                                   open_seconds=last_frame-capture_started))
                            if gate is None:
                                self.playback_finished_at = None
                                self.service.emit(dict(stage="capture", state="ready",
                                                       device=self.device, sample_rate=16000,
                                                       after_playback_seconds=(last_frame-playback_at if playback_at else None)))
                        energy = float(np.abs(frame.astype(np.int32)).mean())
                        self.level = round(energy, 1)
                        if last_frame - level_at >= 2:
                            level_at = last_frame
                            self.service.emit(dict(stage="capture", state="level",
                                                   mean_abs=self.level, threshold=self.threshold))
                        if gate is not None:
                            self.phase = "input_quiet" if self.input_quiet_required else "echo_guard"
                            if gate.push(energy):
                                self.service.emit(dict(stage="capture", state="ready",
                                    device=self.device, sample_rate=16000,
                                    after_playback_seconds=(last_frame-playback_at if playback_at is not None else None),
                                    guard_frames=gate.frames, quiet_frames=gate.quiet))
                                gate = None
                                self.playback_finished_at = None
                                self.input_quiet_required = False
                                self.phase = "listening"
                            # Never let the last quiet frame or earlier echo enter pre-roll.
                            continue
                        action = endpoint.push(energy)
                        if job is None:
                            pre.append(frame)
                            if action == 'start':
                                job = self.service.admit_microphone()
                                if job is None:
                                    break
                                self.detected += 1
                                self.phase = "recording"
                                frames = list(pre)
                        else:
                            frames.append(frame)
                            if action in ('end_silence', 'end_limit'):
                                self.service.emit(dict(stage="capture", state="endpoint",
                                    request_id=job['id'], reason=action,
                                    retained_frames=len(frames), trailing_silence_frames=endpoint.silent,
                                    endpoint_wait_ms=endpoint.silent * FRAME_MS,
                                    continuation_threshold=endpoint.continuation_threshold))
                                close_started = time.monotonic()
                                self.close_capture()
                                self.service.emit(dict(stage="capture", state="closed",
                                                       seconds=time.monotonic()-close_started))
                                self.phase = "processing"
                                self.service.finish_microphone(job, np.concatenate(frames))
                                job = None
                                self.completed += 1
                                break
                    if job is not None:
                        self.service.abort_microphone(job, "capture_stopped")
                        job = None
                except Exception as exc:
                    if job is not None:
                        self.service.abort_microphone(job, "capture_stopped" if self.stop_event.is_set() else "capture_fault")
                    if self.stop_event.is_set():
                        break
                    self.last_error = str(exc)
                    failures += 1
                    self.restarts += 1
                    self.phase = "recovering"
                    self.service.emit(dict(stage="capture", state="error", error=str(exc),
                                           consecutive_failures=failures))
                    if failures >= 3:
                        self.phase = "error"
                        break
                    self.stop_event.wait(1)
                finally:
                    self.close_capture()
                self.stop_event.wait(self.timing.capture_restart_seconds)
        finally:
            self.close_capture()
            if self.phase != "error":
                self.phase = "stopped"
            self.service.emit(dict(stage="capture", state=self.phase))
