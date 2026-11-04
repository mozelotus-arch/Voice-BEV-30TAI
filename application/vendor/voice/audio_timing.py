"""Frame-counted endpointing and post-playback admission; no model/ALSA imports."""
from dataclasses import dataclass

FRAME_MS = 32


@dataclass(frozen=True)
class AudioTiming:
    end_silence_frames: int = 20
    continuation_ratio: float = 1.0
    playback_tail_seconds: float = .5
    capture_restart_seconds: float = .15
    resume_quiet_frames: int = 0

    def __post_init__(self):
        if type(self.end_silence_frames) is not int or not 10 <= self.end_silence_frames <= 40:
            raise ValueError('Endpoint must be 10..40 complete 32 ms frames')
        if not .25 <= self.continuation_ratio <= 1:
            raise ValueError('Invalid continuation threshold ratio')
        if not .1 <= self.playback_tail_seconds <= 1:
            raise ValueError('Playback guard must be 0.1..1 seconds')
        if not .01 <= self.capture_restart_seconds <= .5:
            raise ValueError('Invalid capture restart interval')
        if type(self.resume_quiet_frames) is not int or not 0 <= self.resume_quiet_frames <= 16:
            raise ValueError('Invalid post-playback quiet interval')

    @classmethod
    def from_config(cls, config):
        return cls(**config.get('audio_timing', {}))


class Endpoint:
    """Keep the existing 160 ms onset and 288 ms pre-roll; count ALL tail PCM."""
    def __init__(self, threshold, timing):
        if threshold <= 0:
            raise ValueError('Positive onset threshold required')
        self.threshold, self.timing = threshold, timing
        self.continuation_threshold = threshold * timing.continuation_ratio
        self.above = self.silent = self.total = self.frames = 0
        self.recording = False

    def push(self, energy):
        self.total += 1
        if not self.recording:
            self.above = self.above + 1 if energy >= self.threshold else 0
            if self.above >= 5:
                self.recording = True
                self.frames = min(9, self.total)
                return 'start'
        else:
            self.frames += 1
            self.silent = self.silent + 1 if energy < self.continuation_threshold else 0
            if self.silent >= self.timing.end_silence_frames:
                return 'end_silence'
            if self.frames >= 250:
                return 'end_limit'
        return None


class ResumeGate:
    """After aplay drain and fixed guard, require consecutive quiet PCM frames.

    All guarded frames are discarded, including pre-roll. No noisy timeout may
    force admission: the speaker tail must not become a new utterance. This is
    half-duplex protection, not echo cancellation or support for barge-in.
    """
    def __init__(self, frames, threshold):
        self.required, self.threshold = frames, threshold
        self.quiet = 0
        self.frames = 0

    def push(self, energy):
        self.frames += 1
        self.quiet = self.quiet + 1 if energy < self.threshold else 0
        return self.quiet >= self.required
