"""SenseVoice feature adapter without importing torch or training packages.

Explicit model contract: 16 kHz, Hamming/25ms/10ms, Kaldi 80 bins,
LFR 7/6, model-supplied CMVN. Dither is a configurable model parameter.
"""
import math
from pathlib import Path
import numpy as np


def read_cmvn(path):
    lines = Path(path).read_text().splitlines()
    values = []
    for i, line in enumerate(lines):
        if line.split() and line.split()[0] in ('<AddShift>', '<Rescale>'):
            values.append(np.asarray(lines[i+1].split()[3:-1], dtype=np.float32))
    if len(values) != 2 or any(v.shape != (560,) for v in values):
        raise ValueError('Expected 560-dimensional SenseVoice CMVN')
    return values


def read_wav(path):
    import wave
    with wave.open(str(path), 'rb') as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        if channels != 1 or width != 2:
            raise ValueError('Require mono signed 16-bit PCM')
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32)
    if rate != 16000:
        from scipy.signal import resample_poly
        divisor = math.gcd(rate, 16000)
        samples = resample_poly(samples, 16000//divisor, rate//divisor).astype(np.float32)
    if len(samples) < 400:
        raise ValueError('Audio shorter than the 25ms analysis window')
    return samples


def lfr_cmvn(fbank, cmvn):
    count = len(fbank)
    if count == 0:
        raise ValueError('No audio frames')
    indices = np.arange((count + 5)//6)[:, None]*6 + np.arange(7)[None, :] - 3
    features = fbank[np.clip(indices, 0, count-1)].reshape(-1, 560).copy()
    features += cmvn[0]
    features *= cmvn[1]
    return features


class NativeFrontend:
    def __init__(self, cmvn, dither=0.):
        import kaldi_native_fbank as knf
        self.knf = knf
        self.options = knf.FbankOptions()
        self.options.frame_opts.samp_freq = 16000
        self.options.frame_opts.frame_length_ms = 25
        self.options.frame_opts.frame_shift_ms = 10
        self.options.frame_opts.window_type = 'hamming'
        self.options.frame_opts.dither = dither
        self.options.frame_opts.snip_edges = True
        self.options.mel_opts.num_bins = 80
        self.options.energy_floor = 0.
        self.cmvn = read_cmvn(cmvn)

    def extract(self, pcm):
        instance = self.knf.OnlineFbank(self.options)
        instance.accept_waveform(16000, pcm.tolist())
        instance.input_finished()
        fbank = np.asarray([instance.get_frame(i) for i in range(instance.num_frames_ready)], dtype=np.float32)
        return fbank, lfr_cmvn(fbank, self.cmvn)


def fixed_input(features, length_mode='effective', max_frames=100):
    if len(features) > max_frames:
        raise ValueError('Fixed graph cannot accept this many frames; reject rather than silently truncate')
    padded = np.zeros((1, max_frames, 560), dtype=np.float32)
    padded[0, :len(features)] = features
    length = max_frames if length_mode == 'legacy' else len(features)
    return padded, np.asarray([length], dtype=np.float32)
