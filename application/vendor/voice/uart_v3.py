"""UART v3 transport only. No board access occurs on import or with a fake backend."""
import ctypes
import json
import math
import os
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parent
ABI = 0x55415203


def require_trial_boot(identity_path=None, receipt_path=None, boot_id_path=None):
    identity_path = Path(identity_path or ROOT / 'candidate_identity.json')
    receipt_path = Path(receipt_path or ROOT / 'trial_boot.json')
    boot_id_path = Path(boot_id_path or '/proc/sys/kernel/random/boot_id')
    try:
        identity = json.loads(identity_path.read_text())
        receipt = json.loads(receipt_path.read_text())
        boot_id = boot_id_path.read_text().strip()
    except (OSError, ValueError) as exc:
        raise RuntimeError('MMIO blocked: verified candidate boot receipt is missing') from exc
    expected = identity.get('boot_sha256')
    if (not isinstance(expected, str) or len(expected) != 64 or
            not boot_id or boot_id != receipt.get('active_candidate_boot_id') or
            expected != receipt.get('candidate_boot_sha256') or
            identity.get('required_test_mode') != 'uart_only_pjtag_inactive' or
            receipt.get('stage') != 'uart_mmio' or receipt.get('idle_baseline_passed') is not True or
            receipt.get('pjtag_inactive_confirmed') is not True):
        raise RuntimeError('MMIO blocked: this boot has not passed the UART trial entry gate')
    return boot_id


class Registers:
    """One process owns this UART page; 32-bit volatile accesses live in C."""
    def __init__(self):
        require_trial_boot()  # Check before even opening /dev/mem.
        import fcntl
        self.pointer = None
        self.lock = open('/tmp/fm-register-400f0000.lock', 'a+')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lib = ctypes.CDLL(str(ROOT / 'libfm_uart_mmio.so'), use_errno=True)
            self.lib.fm_map.argtypes = [ctypes.c_uint64, ctypes.c_size_t]
            self.lib.fm_map.restype = ctypes.c_void_p
            self.lib.fm_read.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self.lib.fm_read.restype = ctypes.c_uint32
            self.lib.fm_write.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
            self.lib.fm_write.restype = None
            self.lib.fm_unmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            self.lib.fm_unmap.restype = ctypes.c_int
            self.pointer = self.lib.fm_map(0x400f0000, 4096)
            if not self.pointer:
                raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        except BaseException:
            self.lock.close()
            raise

    def _check(self, offset):
        if not self.pointer or type(offset) is not int or offset % 4 or not 0 <= offset < 4096:
            raise ValueError('Invalid UART register access')

    def read(self, offset):
        self._check(offset)
        return int(self.lib.fm_read(self.pointer, offset))

    def write(self, offset, value):
        self._check(offset)
        if offset not in (0, 0x10, 0x20):
            raise ValueError('Register is not writable in UART v3')
        self.lib.fm_write(self.pointer, offset, int(value) & 0xffffffff)

    def close(self):
        if self.pointer:
            self.lib.fm_unmap(self.pointer, 4096)
            self.pointer = None
        self.lock.close()


class Uart:
    def __init__(self, registers=None, clock=time.monotonic, sleep=time.sleep):
        self.reg = registers if registers is not None else Registers()
        self.clock, self.sleep = clock, sleep
        self.tx_lock, self.rx_lock = threading.Lock(), threading.Lock()
        try:
            self.version = self.reg.read(0x0c)
            if self.version != ABI:
                raise RuntimeError('UART v3 required; do not use this driver with old firmware')
            if (self.reg.read(0x28), self.reg.read(0x2c)) != (100000000, 9600):
                raise RuntimeError('Unexpected UART clock or baud')
            caps = self.reg.read(0x78)
            self.tx_depth, self.rx_depth = caps & 0xffff, caps >> 16
            if (self.tx_depth, self.rx_depth) != (128, 256):
                raise RuntimeError('Unexpected UART FIFO capacities')
        except BaseException:
            self.reg.close()
            raise

    def receive(self, limit=256):
        if type(limit) is not int or not 0 <= limit <= 4096:
            raise ValueError('Invalid receive limit')
        with self.rx_lock:
            result = bytearray()
            for _ in range(limit):
                word = self.reg.read(4)
                if not word & 0x100:
                    break
                result.append(word & 255)
            return bytes(result)

    def _check_tx_evidence(self, accepted, dropped, epoch):
        if (self.reg.read(0x68) != accepted or self.reg.read(0x6c) != dropped or
                self.reg.read(0x74) != epoch or self.reg.read(0x70) & 1):
            raise RuntimeError('UART TX evidence changed: partial transfer; do not silently retry')

    def send(self, data, timeout=3.0):
        data = bytes(data)
        if len(data) > 1200 or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Send needs <=1200 bytes and a positive timeout')
        with self.tx_lock:
            deadline = self.clock() + timeout
            accepted, dropped, epoch = (self.reg.read(0x68), self.reg.read(0x6c), self.reg.read(0x74))
            self._check_tx_evidence(accepted, dropped, epoch)
            for value in data:
                while self.reg.read(0x64) == 0:
                    self._check_tx_evidence(accepted, dropped, epoch)
                    if self.clock() >= deadline:
                        raise TimeoutError('UART queue space timeout; transfer may be partial')
                    self.sleep(0.0005)
                if self.clock() >= deadline:
                    raise TimeoutError('UART send deadline; transfer may be partial')
                self.reg.write(0, value)
                accepted = (accepted + 1) & 0xffffffff
                self._check_tx_evidence(accepted, dropped, epoch)
            return len(data)  # Accepted into FIFO, not proof of remote reception.

    def drain(self, timeout=3.0):
        if not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Drain needs a positive timeout')
        with self.tx_lock:
            deadline = self.clock() + timeout
            evidence = (self.reg.read(0x68), self.reg.read(0x6c), self.reg.read(0x74))
            while self.reg.read(8) & 4:
                self._check_tx_evidence(*evidence)
                if self.clock() >= deadline:
                    raise TimeoutError('UART line drain timeout')
                self.sleep(0.0005)
            self._check_tx_evidence(*evidence)

    def soft_reset(self):
        # Explicit user/service recovery action. Discards both FIFOs and aborts TX.
        with self.tx_lock, self.rx_lock:
            epoch = self.reg.read(0x74)
            self.reg.write(0x10, 2)
            if self.reg.read(0x74) != ((epoch + 1) & 0xffffffff):
                raise RuntimeError('UART reset epoch did not advance')
            # WAIT_HIGH needs one idle bit. Caller must keep the peer idle here;
            # elapsed time alone cannot make a continuously low wire ready.
            self.sleep(2.0 / 9600)

    def clear_stats(self):
        with self.tx_lock, self.rx_lock:
            self.reg.write(0x10, 1)

    def status(self):
        fields = {'flags':8,'levels':0x1c,'framing_errors':0x14,'rx_overruns':0x18,
                  'bad_writes':0x30,'bad_reads':0x34,'breaks':0x38,'invalid_stops':0x3c,
                  'tx_space':0x64,'tx_accepted':0x68,'tx_dropped':0x6c,
                  'tx_overflow':0x70,'reset_epoch':0x74}
        result = {name:self.reg.read(offset) for name,offset in fields.items()}
        result.update(abi=hex(self.version),tx_depth=self.tx_depth,rx_depth=self.rx_depth)
        return result

    def close(self):
        self.reg.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status','echo'])
    parser.add_argument('--seconds',type=float,default=30)
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error('--seconds must be in (0,300]')
    uart = Uart()
    try:
        print(json.dumps({'before':uart.status()}),flush=True)
        if args.action == 'echo':
            count = 0
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                chunk = uart.receive(128)
                if chunk:
                    uart.send(chunk); count += len(chunk)
                else:
                    time.sleep(.001)
            uart.drain()
            print(json.dumps({'echoed_bytes':count,'after':uart.status()}),flush=True)
    finally:
        uart.close()


if __name__ == '__main__':
    main()
