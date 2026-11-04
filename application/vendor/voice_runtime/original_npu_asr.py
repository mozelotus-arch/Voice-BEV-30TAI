"""Original fixed-100 SenseVoice graph, shared device, native CPU features.

No Torch, FunASR or simultaneous CPU ASR model. Lengths are legacy 100 by
default. Reject oversized utterances before inference; never clip a command.
"""
import time
import numpy as np
from frontend import NativeFrontend, fixed_input


class OriginalNPUASR:
    def __init__(self, graph, cmvn, tokenizer, length_mode='legacy'):
        expected = [((1, 100, 560), 'float32'), ((1,), 'float32')]
        if [(c.shape, c.dtype) for c in graph.contracts] != expected:
            raise ValueError('Expected original fixed-100 SenseVoice contract')
        if length_mode not in ('legacy', 'effective'):
            raise ValueError('Unknown ASR length mode')
        import sentencepiece as spm
        self.graph = graph
        self.frontend = NativeFrontend(cmvn, dither=0.)
        self.tokenizer = spm.SentencePieceProcessor(model_file=str(tokenizer))
        self.length_mode = length_mode

    @staticmethod
    def reduce_logits(outputs):
        if len(outputs) != 2 or outputs[1].shape != (1, 100, 25055):
            raise ValueError('Original ASR output contract changed')
        logits = outputs[1][0]
        if not np.isfinite(logits).all():
            raise ValueError('Nonfinite ASR logits')
        ids = np.argmax(logits, axis=-1)
        ids = ids[np.r_[True, ids[1:] != ids[:-1]]]
        return ids[ids != 0].tolist()

    def transcribe(self, pcm):
        if self.graph is None:
            raise RuntimeError('ASR is closed')
        if not isinstance(pcm, np.ndarray) or pcm.dtype != np.int16 or pcm.ndim != 1 or not 400 <= len(pcm) <= 480000:
            raise ValueError('ASR expects bounded mono int16 PCM at 16 kHz')
        # Kaldi snip_edges: 400-sample windows and 160-sample hop, LFR stride 6.
        frames = (1 + (len(pcm) - 400) // 160 + 5) // 6
        if frames > 100:
            raise ValueError('语音超过当前 NPU 模型容量（100 帧，约 6 秒），未执行，请缩短指令。')
        started = time.perf_counter()
        _, features = self.frontend.extract(pcm.astype(np.float32))
        inputs = fixed_input(features, self.length_mode, 100)
        prepared = time.perf_counter()
        ids = self.graph.forward(list(inputs), reducer=self.reduce_logits)
        inferred = time.perf_counter()
        text = self.tokenizer.decode(ids).split('|>')[-1].strip()
        done = time.perf_counter()
        return dict(text=text, backend='original_npu100_native', processing_seconds=done-started,
                    frontend_seconds=prepared-started, graph_seconds=inferred-prepared,
                    decode_seconds=done-inferred, feature_frames=len(features),
                    length_mode=self.length_mode, dither=0., truncated=False)

    def close(self):
        self.graph = self.frontend = self.tokenizer = None
