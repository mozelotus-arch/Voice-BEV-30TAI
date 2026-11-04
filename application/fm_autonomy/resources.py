"""Reviewable register/DMA allocation checks; this file does not program hardware."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RegisterWindow:
    name: str
    base: int
    size: int
    abi: int


def validate_register_windows(windows):
    if len({w.name for w in windows}) != len(windows):
        raise ValueError('duplicate_module')
    for w in windows:
        if type(w.base) is not int or type(w.size) is not int or w.size != 0x10000 or w.base % w.size:
            raise ValueError('64KiB_page_required')
        if not 0x400C0000 <= w.base < w.base + w.size <= 0x40100000:
            raise ValueError('outside_vendor_user_register_window')
        if w.name == 'uart' and (w.base != 0x400F0000 or w.abi != 0x55415203):
            raise ValueError('validated_UART_must_remain_at_400F')
        if w.name != 'uart' and w.base == 0x400F0000:
            raise ValueError('UART_page_reserved')
    for i, a in enumerate(windows):
        for b in windows[i+1:]:
            if a.base < b.base+b.size and b.base < a.base+a.size:
                raise ValueError('register_collision:' + a.name + ':' + b.name)
    return True


def validate_dma_ranges(ranges):
    for name, (address, length) in ranges.items():
        if (type(address) is not int or type(length) is not int or address < 0 or
            length <= 0 or address % 64 or length % 64 or address + length >= 2**32):
            raise ValueError('invalid_32bit_aligned_DMA_range:' + name)
    items=list(ranges.items())
    for i,(a,(base,size)) in enumerate(items):
        for b,(other,length) in items[i+1:]:
            if base < other+length and other < base+size:
                raise ValueError('DMA_overlap:' + a + ':' + b)
    return True
