"""Explicit OFFLINE ONLY ideal chassis for process and wire tests."""
import struct
import time
from functools import reduce
from operator import xor
from wheeltec import Frame, FEEDBACK_IDS


def feedback_frames(v=(0,0,0),disabled=0,battery_mv=24500,received_at=0):
    data=bytes([0x7b,disabled])+struct.pack('>hhh',*v)
    data+=struct.pack('>hhhhhhH',0,0,1672,0,0,0,battery_mv)
    data+=bytes([reduce(xor,data,0),0x7d])
    return [Frame(i,data[n*8:n*8+8],received_at+n*.001) for n,i in enumerate(FEEDBACK_IDS)]


class MockCAN:
    def __init__(self):
        self.tx=[]; self.velocity=(0,0,0); self.pending=[]; self.next_feedback=0
        self.feedback_enabled=True; self.disabled=0

    def send(self,frame):
        self.tx.append((time.monotonic(),frame.data.hex()))
        self.velocity=struct.unpack('>hhh',frame.data[:6])

    def receive(self):
        now=time.monotonic()
        if self.pending: return self.pending.pop(0)
        if self.feedback_enabled and now>=self.next_feedback:
            self.pending=feedback_frames(self.velocity,self.disabled,received_at=now-.003)
            self.next_feedback=now+.05
            return self.pending.pop(0)
        return None

    def close(self): pass
