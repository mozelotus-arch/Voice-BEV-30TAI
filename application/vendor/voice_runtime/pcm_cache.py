"""Bounded, model-scoped PCM reuse; synthesis and cache mutation are serialized.

Budget counts immutable audio payload plus fixed-size digest keys, not Python
object overhead. A separate entry limit bounds metadata; measure process RSS
for the actual cost. This is not a cache of normalized text or model outputs.
"""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import threading


@dataclass(frozen=True)
class PCM:
    data: bytes
    sample_rate: int
    channels: int = 1

    def __post_init__(self):
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError('PCM payload must be nonempty immutable bytes')
        if type(self.sample_rate) is not int or not 1 <= self.sample_rate <= 384000:
            raise ValueError('Invalid PCM sample rate')
        if type(self.channels) is not int or not 1 <= self.channels <= 8:
            raise ValueError('Invalid PCM channel count')
        if len(self.data) % (2 * self.channels):
            raise ValueError('Expected complete interleaved S16_LE frames')


class PCMCache:
    def __init__(self, namespace, max_bytes, max_entries=32):
        if not isinstance(namespace, str) or not namespace:
            raise ValueError('Bind the cache to a model/frontend/config identity')
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('Byte budget must be a nonnegative integer')
        if type(max_entries) is not int or max_entries < 0:
            raise ValueError('Entry budget must be a nonnegative integer')
        self.namespace = namespace
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.items = OrderedDict()
        self.lock = threading.RLock()
        self.charged_bytes = self.peak_charged_bytes = 0
        self.hits = self.misses = self.evictions = self.bypasses = 0

    def get_or_create(self, text, settings, synthesize):
        if not isinstance(text, str) or not text:
            raise ValueError('Nonempty exact input text is required')
        # Exact text and all caller-provided synthesis settings are part of the
        # identity. Model/frontend assets belong in the immutable namespace.
        encoded = json.dumps([self.namespace, text, settings], ensure_ascii=False,
                             sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
        key = hashlib.sha256(encoded).digest()
        with self.lock:
            if key in self.items:
                self.hits += 1
                self.items.move_to_end(key)
                return self.items[key], True
            self.misses += 1
            value = synthesize()
            if not isinstance(value, PCM):
                raise TypeError('Synthesis must return an owned PCM value')
            cost = len(key) + len(value.data)
            if not self.max_entries or cost > self.max_bytes:
                self.bypasses += 1
                return value, False
            while self.items and (self.charged_bytes + cost > self.max_bytes or
                                  len(self.items) >= self.max_entries):
                old_key, old_value = self.items.popitem(last=False)
                self.charged_bytes -= len(old_key) + len(old_value.data)
                self.evictions += 1
            self.items[key] = value
            self.charged_bytes += cost
            self.peak_charged_bytes = max(self.peak_charged_bytes, self.charged_bytes)
            return value, False

    def clear(self):
        with self.lock:
            self.items.clear()
            self.charged_bytes = 0

    def stats(self):
        with self.lock:
            return dict(entries=len(self.items), charged_bytes=self.charged_bytes,
                        pcm_payload_bytes=sum(len(v.data) for v in self.items.values()),
                        peak_charged_bytes=self.peak_charged_bytes, max_bytes=self.max_bytes,
                        max_entries=self.max_entries, hits=self.hits, misses=self.misses,
                        evictions=self.evictions, bypasses=self.bypasses,
                        scope='PCM payload and digest keys; excludes Python metadata and caller-owned outputs')
