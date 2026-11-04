"""XiaoYa v2: unchanged single-speaker weights, bounded text and timing controls."""
from dataclasses import asdict
import time
import numpy as np
from tts_engine import LocalTTS as BaselineTTS, Speech, MAX_AUDIO_SECONDS
from prosody_frontend import normalize, pronounce, ids_from_syllables, plan_chunks

STYLES = {
    # Model duration/noise controls, not learned emotional conditioning or speaker changes.
    "neutral": dict(scales=(.667, 1., .8), comma_ms=100, sentence_ms=210),
    "warm": dict(scales=(.55, 1.035, .65), comma_ms=130, sentence_ms=260),
    "clear": dict(scales=(.45, 1.08, .60), comma_ms=160, sentence_ms=300),
}


def edge_silence(audio, rate, from_end=False):
    """Conservative 10ms windows, capped at 500ms; never remove generated speech."""
    x = audio[::-1] if from_end else audio
    step = max(1, round(rate * .01))
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.
    threshold = max(1e-5, peak * .003)
    count = 0
    for start in range(0, min(len(x), round(rate * .5)), step):
        block = x[start:start + step]
        if not len(block) or np.max(np.abs(block)) > threshold:
            break
        count += len(block)
    return count


def join_pause(previous, following, rate, wanted_ms):
    # Existing model pauses count; do not blindly double them or trim word endings.
    present = edge_silence(previous, rate, True) + edge_silence(following, rate)
    return max(0, round(rate * wanted_ms / 1000) - present)


class LocalTTS(BaselineTTS):
    def __init__(self, folder=None, threads=2, memory_arena=False, style="warm"):
        if style not in STYLES:
            raise ValueError("Unknown speaking style")
        self.style = style
        super().__init__(folder, threads, memory_arena)

    def synthesize(self, text, style=None):
        started = time.perf_counter()
        style = self.style if style is None else style
        if style not in STYLES:
            raise ValueError("Unknown speaking style")
        settings = STYLES[style]
        normalized = normalize(text)
        syllables, overrides = pronounce(normalized)  # Whole-sentence context before chunking.
        chunks = plan_chunks(normalized)
        pieces, details = [], []
        total = ids_count = 0
        previous = None
        prior_text = ""
        for chunk in chunks:
            spoken = normalized[chunk.start:chunk.end]
            ids = ids_from_syllables(syllables[chunk.start:chunk.end], self.config["phoneme_id_map"])
            feed = {"input": np.array([ids], dtype=np.int64),
                    "input_lengths": np.array([len(ids)], dtype=np.int64),
                    "scales": np.array(settings["scales"], dtype=np.float32)}
            if "sid" in {i.name for i in self.session.get_inputs()}:
                feed["sid"] = np.array([0], dtype=np.int64)
            audio = np.asarray(self.session.run(None, feed)[0], dtype=np.float32).reshape(-1)
            if not len(audio) or not np.isfinite(audio).all():
                raise ValueError("Invalid synthesized audio")
            pause = 0
            if previous is not None:
                end = prior_text.rstrip()[-1]
                wanted = settings["sentence_ms"] if end in "。？！" else settings["comma_ms"]
                pause = join_pause(previous, audio, self.sample_rate, wanted)
            total += pause + len(audio)
            if total > MAX_AUDIO_SECONDS * self.sample_rate:
                raise ValueError("Speech exceeds 45 seconds")
            if pause:
                pieces.append(np.zeros(pause, dtype=np.float32))
            pieces.append(audio)
            details.append(dict(**asdict(chunk), text=spoken, added_pause_ms=1000 * pause / self.sample_rate,
                                phoneme_ids=len(ids)))
            previous, prior_text = audio, spoken
            ids_count += len(ids)
        waveform = np.concatenate(pieces)
        peak = float(np.max(np.abs(waveform)))
        if peak < 1e-5:
            raise ValueError("Synthesized speech is silent")
        waveform = waveform / max(1., peak) * .75
        pcm = np.round(np.clip(waveform, -1., 1.) * 32767).astype(np.int16)
        seconds = len(pcm) / self.sample_rate
        elapsed = time.perf_counter() - started
        return Speech(pcm, self.sample_rate, dict(version="xiaoya_v2", normalized_text=normalized,
            seconds=seconds, synth_seconds=elapsed, rtf=elapsed / seconds, phoneme_ids=ids_count,
            peak_before_gain=peak, frontend="pypinyin_domain_v2", model="xiao_ya_medium", speaker_count=1,
            style=style, learned_emotion_control=False, scales=list(settings["scales"]),
            chunks=details, pronunciation_overrides=overrides,
            length_fallbacks=sum(c.boundary == "length_fallback" for c in chunks)))
