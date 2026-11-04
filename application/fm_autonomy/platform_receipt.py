"""Bind a combined-image UART entry to its actual boot and recorded MMIO checks."""
import hashlib
import json
from pathlib import Path

def verify_platform(path,*,boot_id_path='/proc/sys/kernel/random/boot_id',mounts_path='/proc/mounts'):
    record=json.loads(Path(path).read_text())
    if record.get('schema')!='FM-MERGED-PLATFORM-RECEIPT/1':raise ValueError('combined_platform_receipt_schema')
    boot_id=Path(boot_id_path).read_text().strip()
    if not boot_id or record.get('boot_id')!=boot_id:raise ValueError('combined_platform_boot_id_changed')
    mounts=[line.split() for line in Path(mounts_path).read_text().splitlines() if line.split()[0]=='/dev/mmcblk0p1']
    if len(mounts)!=1 or 'ro' not in mounts[0][3].split(','):raise ValueError('readonly_boot_mount_required')
    image=Path(mounts[0][1])/'BOOT.bin'
    if hashlib.sha256(image.read_bytes()).hexdigest()!=record.get('boot_sha256'):raise ValueError('combined_platform_BOOT_changed')
    required={'uart_abi':'0x55415203','pro_abi':'0x20260707','managed_fence':'MRG1',
        'sqrt_base':'0x400D0000','adder_base':'0x400E0000','uart_base':'0x400F0000'}
    if record.get('contract')!=required:raise ValueError('combined_platform_contract_mismatch')
    for key in ('uart_idle_readback','pjtag_inactive','C_D_E_F_address_isolation','managed_fence_idle'):
        if record.get('checks',{}).get(key) is not True:raise ValueError('combined_platform_check_missing:'+key)
    return boot_id,record['boot_sha256']
