"""WHEELTEC C30D CAN codec. No hardware access and no implicit unit conversion."""
import struct
from dataclasses import dataclass
from functools import reduce
from operator import xor

CONTROL_ID = 0x181
FEEDBACK_IDS = (0x101, 0x102, 0x103)
ZERO = bytes(8)


@dataclass(frozen=True)
class Frame:
    can_id: int
    data: bytes
    received_at: float = 0.0
    extended: bool = False
    remote: bool = False
    error: bool = False


def is_arbitration_notification(frame):
    """A pure SocketCAN LOSTARB report is normal CAN contention, not bus failure.

    Combined errors and malformed reports remain faults. A pending transmit is
    still bounded by the independent controller's ordinary deadline.
    """
    return (frame.error and frame.can_id==0x2 and len(frame.data)==8
            and not frame.extended and not frame.remote)


def velocity_frame(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError('velocity_requires_three_integers')
    if any(type(v) is not int or not -32768 <= v <= 32767 for v in values):
        raise ValueError('velocity_requires_int16_mm_s_and_mrad_s')
    return Frame(CONTROL_ID, struct.pack('>hhhxx', *values))


@dataclass(frozen=True)
class Telemetry:
    received_at: float
    disabled: int
    velocity: tuple
    accel_raw: tuple
    gyro_raw: tuple
    battery_mv: int
    raw: bytes

    def compact(self):
        return {'disabled':self.disabled, 'v':list(self.velocity),
                'accel_raw':list(self.accel_raw), 'gyro_raw':list(self.gyro_raw),
                'battery_mv':self.battery_mv}


def decode_feedback(data, received_at):
    if len(data) != 24 or data[0] != 0x7b or data[23] != 0x7d:
        raise ValueError('feedback_header_tail_or_length')
    if reduce(xor, data[:22], 0) != data[22]:
        raise ValueError('feedback_bcc')
    return Telemetry(received_at, data[1], struct.unpack('>hhh', data[2:8]),
                     struct.unpack('>hhh', data[8:14]),
                     struct.unpack('>hhh', data[14:20]),
                     struct.unpack('>H', data[20:22])[0], bytes(data))


class FeedbackAssembler:
    """Ordered 101/102/103 groups with a bounded time span and whole-packet BCC.

    The vendor protocol has no cycle sequence number. These checks reduce mixed
    cycles but cannot prove atomicity; XOR collisions remain possible.
    """
    def __init__(self, max_span=.025):
        self.max_span=max_span
        self.parts=[]
        self.started=0.0
        self.last=0.0
        self.rejected=0
        self.completed=0

    def reset(self):
        self.parts=[]

    def feed(self, frame):
        if frame.error:
            self.reset(); self.rejected+=1
            raise ValueError('can_error_frame')
        if frame.can_id not in FEEDBACK_IDS:
            return None
        if frame.extended or frame.remote or len(frame.data)!=8:
            self.reset(); self.rejected+=1
            return None
        now=frame.received_at
        if frame.can_id==0x101:
            if self.parts: self.rejected+=1
            self.parts=[frame.data]; self.started=now; self.last=now
            return None
        if (not self.parts or frame.can_id != FEEDBACK_IDS[len(self.parts)] or
                now<self.last or now-self.started>self.max_span):
            self.reset(); self.rejected+=1
            return None
        self.parts.append(frame.data); self.last=now
        if len(self.parts)<3:
            return None
        data=b''.join(self.parts)
        # Use the oldest part, not the last arrival, for freshness checks.
        received_at=self.started
        self.reset()
        try: result=decode_feedback(data,received_at)
        except ValueError:
            self.rejected+=1
            return None
        self.completed+=1
        return result
