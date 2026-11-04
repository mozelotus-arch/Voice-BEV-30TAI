"""Bounded, authenticated serial frames. Status text is never a command."""
import hashlib
import hmac
import json
import struct
import zlib

MAX_JSON = 1000
MAX_ENCODED = 1100


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def cobs_encode(data):
    out = bytearray([0]); start = 0; length = 1
    for byte in data:
        if byte == 0:
            out[start] = length; start = len(out); out.append(0); length = 1
        else:
            out.append(byte); length += 1
            if length == 255:
                out[start] = length; start = len(out); out.append(0); length = 1
    out[start] = length
    return bytes(out)


def cobs_decode(data):
    out = bytearray(); pos = 0
    while pos < len(data):
        length = data[pos]; pos += 1
        if length == 0 or pos + length - 1 > len(data):
            raise ValueError('invalid COBS')
        out.extend(data[pos:pos+length-1]); pos += length-1
        if length < 255 and pos < len(data): out.append(0)
    return bytes(out)


def encode(value, key):
    body = canonical(value)
    if len(body) > MAX_JSON: raise ValueError('message too long')
    packet = b'FVB1' + body
    packet += struct.pack('<I', zlib.crc32(packet) & 0xffffffff)
    packet += hmac.new(key, packet, hashlib.sha256).digest()[:16]
    return b'\0' + cobs_encode(packet) + b'\0'


def decode(encoded, key):
    packet = cobs_decode(encoded)
    if not 26 <= len(packet) <= MAX_JSON + 24: raise ValueError('packet size')
    data, tag = packet[:-16], packet[-16:]
    if not hmac.compare_digest(hmac.new(key, data, hashlib.sha256).digest()[:16], tag):
        raise ValueError('authentication failed')
    body, crc = data[:-4], data[-4:]
    if not body.startswith(b'FVB1') or struct.pack('<I', zlib.crc32(body)&0xffffffff) != crc:
        raise ValueError('packet CRC/magic')
    result = json.loads(body[4:])
    if not isinstance(result, dict): raise ValueError('expected object')
    return result


class Stream:
    def __init__(self, key):
        self.key = key; self.buffer = bytearray(); self.discard = False
        self.rejected = 0; self.accepted = 0
        self.notifications = []

    def reset(self):
        """Drop a partial frame without erasing diagnostic counters."""
        dropped = len(self.buffer)
        self.buffer.clear(); self.discard = False
        self.notifications.clear()
        return dropped

    def feed(self, data):
        self.notifications = []
        messages = []
        for byte in data:
            if byte == 0:
                if self.buffer and not self.discard:
                    try: messages.append(decode(bytes(self.buffer), self.key)); self.accepted += 1
                    except (ValueError, UnicodeError, struct.error): self.rejected += 1
                self.buffer.clear(); self.discard = False
            elif not self.discard:
                self.buffer.append(byte)
                if byte == 10:
                    line=bytes(self.buffer).strip(b'\r\n')
                    # A valid encoded FVB1 frame has FVB1 immediately after
                    # its first COBS code, so these anchored ASCII prefixes
                    # cannot match text embedded in a valid frame.
                    if not line or line == b'CONNECTED' or line.startswith((b'+CONNECTING<<',b'+CONNECTED',b'+DISC:',b'+VERSION=',b'+STAT=')) or line == b'+OK':
                        if line:self.notifications.append(line.decode('ascii',errors='replace'))
                        self.buffer.clear()
                if len(self.buffer) > MAX_ENCODED:
                    self.buffer.clear(); self.discard = True; self.rejected += 1
        return messages


class Endpoint:
    """A new service boot invalidates old requests; same sequence cannot act twice."""
    def __init__(self, session, dispatch):
        self.session = session; self.dispatch = dispatch; self.last = 0; self.cache = {}

    def handle(self, request):
        seq = request.get('seq'); action = request.get('op')
        result = {'seq': seq, 'sid': self.session}
        if request.get('v') != 1 or type(seq) is not int or not 0 <= seq < 2**53:
            return dict(result, ok=False, error='invalid_envelope')
        if action == 'hello':
            if not isinstance(request.get('args'), dict):
                return dict(result, ok=False, error='invalid_hello')
            nonce = request.get('args', {}).get('nonce')
            if seq != 0 or not isinstance(nonce, str) or len(nonce) != 32:
                return dict(result, ok=False, error='invalid_hello')
            return dict(result, ok=True, nonce=nonce, last=self.last, protocol='FVB1')
        if request.get('sid') != self.session:
            return dict(result, ok=False, error='stale_session')
        digest = hashlib.sha256(canonical(request)).hexdigest()
        if seq in self.cache:
            previous, reply = self.cache[seq]
            return reply if digest == previous else dict(result, ok=False, error='sequence_conflict')
        if seq <= self.last: return dict(result, ok=False, error='stale_sequence')
        self.last = seq
        try:
            if not isinstance(action, str) or not isinstance(request.get('args', {}), dict):
                raise ValueError('invalid command')
            response = self.dispatch(action, request.get('args', {}))
            result.update(ok=True, data=response)
        except Exception as exc:
            result.update(ok=False, error=str(exc)[:180])
        if len(canonical(result)) > MAX_JSON:
            result = {'sid': self.session, 'seq': seq, 'ok': False, 'error': 'response_too_large'}
        self.cache[seq] = (digest, result)
        if len(self.cache) > 64: del self.cache[min(self.cache)]
        return result
