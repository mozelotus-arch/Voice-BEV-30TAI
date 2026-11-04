"""Serial voice-turn orchestration with injected models, policy and PCM sink.

Capture calls begin_segment at utterance admission, then finish_segment after
endpoint detection. File replay uses the same boundary, without claiming live
capture or acoustic timing. Resources/models belong to the enclosing factory.
"""
from collections import deque
from contextlib import contextmanager
import hashlib
import math
from pathlib import Path
import threading
import time
import wave
import numpy as np
from pcm_cache import PCM


class RuntimeBusy(RuntimeError):
    pass


class VoiceTurnRuntime:
    def __init__(self, asr, session, tts, sink, *, sample_rate=16000,
                 max_audio_seconds=30, max_pcm_bytes=4*1024*1024,
                 max_reply_chars=512, health_check=None, clock=time.monotonic):
        if sample_rate != 16000 or not math.isfinite(max_audio_seconds) or not 0 < max_audio_seconds <= 30:
            raise ValueError('Require 16 kHz and a positive recording limit no greater than 30 seconds')
        if type(max_pcm_bytes) is not int or max_pcm_bytes < 2:
            raise ValueError('Require a positive PCM output byte budget')
        if type(max_reply_chars) is not int or not 1 <= max_reply_chars <= 4096:
            raise ValueError('Require a finite reply text length budget')
        self.asr, self.session, self.tts, self.sink = asr, session, tts, sink
        self.clock, self.health_check = clock, health_check
        self.sample_rate = sample_rate
        self.max_samples = int(max_audio_seconds * sample_rate)
        self.max_pcm_bytes = max_pcm_bytes
        self.max_reply_chars = max_reply_chars
        self.lock = threading.Lock()
        self.segment = None
        self.closed = False
        # Metadata only; no PCM arrays, logits or unbounded transcript history.
        self.events = deque(maxlen=128)

    @contextmanager
    def _exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise RuntimeBusy('A voice operation is already running; input is not queued')
        try:
            if self.closed:
                raise RuntimeError('Voice runtime is closed')
            yield
        finally:
            self.lock.release()

    def _healthy(self):
        if self.health_check is not None:
            self.health_check()

    def begin_segment(self):
        with self._exclusive():
            self._healthy()
            if self.segment is not None:
                raise RuntimeBusy('A recording is already admitted')
            status = self.session.begin()
            self.segment = dict(id=status['segment_id'], admitted_at=self.clock())
            self.events.append(dict(event='segment_started', segment_id=self.segment['id']))
            return status

    def abort_segment(self, segment_id):
        with self._exclusive():
            self._require_segment(segment_id)
            try:
                return self.session.abort(segment_id)
            finally:
                self.segment = None

    def _require_segment(self, segment_id):
        if self.segment is None or self.segment['id'] != segment_id:
            raise ValueError('Unknown or already consumed recording')

    def _validate_audio(self, audio):
        if (not isinstance(audio, np.ndarray) or audio.dtype != np.int16 or
                audio.ndim != 1 or not 400 <= len(audio) <= self.max_samples):
            raise ValueError('Require mono int16 PCM, 25 ms to the configured limit, at 16 kHz; no truncation')

    def finish_segment(self, segment_id, audio):
        with self._exclusive():
            # A stale completion must not reset a newer valid recording.
            self._require_segment(segment_id)
            start = self.clock()
            admitted = self.segment['admitted_at']
            stage = 'input_validation'
            try:
                self._healthy()
                self._validate_audio(audio)
                stage = 'asr'
                recognized = self.asr.transcribe(audio)
                asr_at = self.clock()
                if not isinstance(recognized, dict) or not isinstance(recognized.get('text'), str) or len(recognized['text']) > 512:
                    raise ValueError('ASR must provide a text string of at most 512 characters')
                stage = 'session_nlu_policy'
                response = self.session.finish(segment_id, recognized['text'])
                understood_at = self.clock()
                tts_result = sink_result = None
                pcm_at = sink_at = None
                if response.get('speech_offer_id'):
                    if not isinstance(response.get('message'), str) or not 0 < len(response['message']) <= self.max_reply_chars:
                        raise ValueError('Policy reply exceeds the configured text budget')
                    stage = 'speech_admission'
                    speech = self.session.speech_begin(response['speech_offer_id'])
                    if speech['text'] != response['message']:
                        raise ValueError('Speech offer and policy response differ')
                    stage = 'tts'
                    pcm, tts_result = self.tts.synthesize(speech['text'])
                    pcm_at = self.clock()
                    if not isinstance(pcm, PCM) or pcm.channels != 1 or len(pcm.data) > self.max_pcm_bytes:
                        raise ValueError('TTS output exceeds the mono PCM contract/budget')
                    stage = 'output_sink'
                    sink_result = self.sink.write(pcm, speech['speech_id'])
                    sink_at = self.clock()
                    if not isinstance(sink_result, dict) or sink_result.get('success') is not True:
                        raise RuntimeError('PCM output sink did not confirm completion')
                    stage = 'speech_completion'
                    ended = self.session.speech_end(speech['speech_id'], True)
                    if ended['event'] != 'speech_finished':
                        raise RuntimeError('Speech processing exceeded the session deadline')
                completed_at = self.clock()
                result = dict(success=True, segment_id=segment_id, asr=recognized, response=response,
                    tts=tts_result, sink=sink_result, state=self.session.status(),
                    timing=dict(capture_admission_to_finish_api_seconds=start-admitted,
                        asr_seconds=asr_at-start, session_nlu_policy_seconds=understood_at-asr_at,
                        tts_to_pcm_seconds=None if pcm_at is None else pcm_at-understood_at,
                        output_sink_seconds=None if sink_at is None else sink_at-pcm_at,
                        finish_api_to_pcm_seconds=None if pcm_at is None else pcm_at-start,
                        finish_api_to_sink_seconds=None if sink_at is None else sink_at-start,
                        finish_api_seconds=completed_at-start),
                    scope='Completed-utterance API; excludes endpoint silence wait and model loading; no measured acoustic first sound')
                self.events.append(dict(event='turn_finished', segment_id=segment_id,
                                        response_event=response['event'], seconds=completed_at-start))
                return result
            except BaseException as exc:
                # Discard pending dialogue and old playback offers after a partial turn.
                self.session.reset('turn_failed')
                self.events.append(dict(event='turn_failed', segment_id=segment_id,
                                        stage=stage, error=repr(exc)[:512]))
                raise
            finally:
                self.segment = None

    def reset(self, reason='audio_disconnected'):
        with self._exclusive():
            self.segment = None
            result = self.session.reset(reason)
            self.events.append(dict(event='session_reset', reason=reason))
            return result

    def close(self):
        # The caller must first stop the capture producer. Wait for an owned turn
        # to finish rather than unloading models that are still in use.
        with self.lock:
            if not self.closed:
                self.session.reset('runtime_closed')
                self.segment = None
                self.asr = self.session = self.tts = self.sink = None
                self.health_check = None
                self.closed = True


class WaveDirectorySink:
    """Writes owned PCM for inspection; never claims playback or actual sound."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(exist_ok=False)

    def write(self, pcm, speech_id):
        # Session-generated IDs only. Never allow a model/text result to choose paths.
        if not isinstance(speech_id, str) or not speech_id or any(c not in '0123456789abcdef' for c in speech_id):
            raise ValueError('Invalid speech identity')
        path = self.folder / (speech_id + '.wav')
        with path.open('xb') as handle:
            with wave.open(handle, 'wb') as output:
                output.setnchannels(pcm.channels)
                output.setsampwidth(2)
                output.setframerate(pcm.sample_rate)
                output.writeframes(pcm.data)
        return dict(success=True, kind='wav_file', path=str(path.absolute()),
                    pcm_bytes=len(pcm.data), pcm_sha256=hashlib.sha256(pcm.data).hexdigest(),
                    playback_attempted=False, acoustic_verified=False)


def read_input_wave(path, max_samples=480000):
    """Read bounded input before allocation; resampling is a separate explicit stage."""
    with wave.open(str(path), 'rb') as input_wave:
        if (input_wave.getnchannels(), input_wave.getsampwidth(), input_wave.getframerate(), input_wave.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError('Require an uncompressed mono 16-bit 16 kHz WAV')
        count = input_wave.getnframes()
        if not 400 <= count <= max_samples:
            raise ValueError('WAV is outside the configured duration bound')
        data = input_wave.readframes(count)
        if len(data) != count * 2:
            raise ValueError('Truncated WAV data')
    return np.frombuffer(data, dtype='<i2').astype(np.int16, copy=True)
