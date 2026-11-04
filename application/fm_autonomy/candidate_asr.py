"""Held three-input ASR: explicit diagnostic adapter, never a production selector."""
from .dependencies import add_vendor


class DiagnosticASR:
    production_replacement_allowed = False

    def __init__(self, graph, bundle, *, purpose, frontend=None, tokenizer=None):
        if purpose != 'offline_diagnostic':
            raise ValueError('asr_equivalence_hold_requires_offline_diagnostic')
        if bundle.metadata.get('production_replacement_allowed') is not False:
            raise ValueError('unexpected_asr_candidate_hold_contract')
        if graph.spec != bundle.spec:
            raise ValueError('asr_graph_bundle_mismatch')
        add_vendor('voice_runtime')
        from asr_bucket_adapter import AsrBucketAdapter
        self.adapter = AsrBucketAdapter(graph, bundle.folder, frontend, tokenizer)

    def transcribe(self, pcm):
        import numpy as np
        if not isinstance(pcm, np.ndarray) or pcm.dtype != np.int16 or pcm.ndim != 1:
            raise ValueError('pcm16_mono_required')
        # Explicit conversion: adapter expects int16 amplitude scale, not [-1,1].
        result = self.adapter.transcribe(pcm.astype(np.float32), language='zh', use_itn=True)
        return dict(result, production_replacement_allowed=False, diagnostic_only=True)

    def close(self):
        self.adapter = None


class ValidationASR(DiagnosticASR):
    """Real ASR adapter reached only through evidence+authorization-gated binding.

    No textual substitution, no fallback, no production acceptance promotion.
    """
    def __init__(self, graph, bundle, *, frontend=None, tokenizer=None):
        super().__init__(graph, bundle, purpose='offline_diagnostic', frontend=frontend, tokenizer=tokenizer)

    def transcribe(self, pcm):
        result = super().transcribe(pcm)
        result.update(diagnostic_only=False, execution_mode='authorized_candidate_validation',
                      production_replacement_allowed=False)
        return result
