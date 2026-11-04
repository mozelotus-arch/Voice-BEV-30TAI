"""Linux classic SocketCAN, bounded nonblocking I/O; never configures a device."""
import json
import re
import socket
import struct
import subprocess
import time
from pathlib import Path
from wheeltec import Frame, FEEDBACK_IDS, CONTROL_ID

CAN_EFF_FLAG=0x80000000
CAN_RTR_FLAG=0x40000000
CAN_ERR_FLAG=0x20000000
CAN_SFF_MASK=0x7ff
CAN_FRAME=struct.Struct('=IB3x8s')


def inspect_interface(name):
    if not re.fullmatch(r'can[0-9]+',name): raise ValueError('physical_can_interface_required')
    p=subprocess.run(['ip','-details','-json','link','show','dev',name],
                     capture_output=True,text=True,timeout=3,check=True)
    rows=json.loads(p.stdout)
    if len(rows)!=1: raise RuntimeError('ambiguous_can_interface')
    row=rows[0]; info=row.get('linkinfo',{})
    data=info.get('info_data',{})
    if info.get('info_kind')!='can' or row.get('mtu')!=16:
        raise RuntimeError('classic_physical_can_required')
    if 'UP' not in row.get('flags',[]): raise RuntimeError('can_interface_down_no_automatic_enable')
    if data.get('bittiming',{}).get('bitrate')!=1000000:
        raise RuntimeError('verified_1mbps_configuration_required')
    if data.get('state')!='ERROR-ACTIVE': raise RuntimeError('can_not_error_active')
    if not (Path('/sys/class/net')/name/'device').exists():
        raise RuntimeError('physical_device_binding_required')
    return row


class SocketCAN:
    def __init__(self,name='can0',enable_tx=False):
        # Inspection precedes socket creation. No ip link set, module loading,
        # automatic bus-off recovery, CAN FD, or broadcast-manager cyclic job.
        self.interface=inspect_interface(name); self.enable_tx=enable_tx
        flags=self.interface['linkinfo']['info_data'].get('ctrlmode',[])
        if isinstance(flags,dict): flags=flags.get('flags',[])
        if isinstance(flags,str): flags=[flags]
        if enable_tx and any(str(f).upper() in ('LISTEN-ONLY','LOOPBACK') for f in flags):
            raise RuntimeError('can_control_mode_not_normal')
        self.lock=None; self.sock=None
        import fcntl
        try:
            self.lock=open('/tmp/fm_vehicle_'+name+'.lock','a+')
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.sock=socket.socket(socket.AF_CAN,socket.SOCK_RAW,socket.CAN_RAW)
            sol=getattr(socket,'SOL_CAN_RAW',101)
            mask=CAN_SFF_MASK|CAN_EFF_FLAG|CAN_RTR_FLAG
            self.sock.setsockopt(sol,1,b''.join(struct.pack('=II',i,mask) for i in FEEDBACK_IDS))
            self.sock.setsockopt(sol,2,struct.pack('=I',0x1fffffff))
            self.sock.setsockopt(sol,3,0)  # No local echo mistaken for chassis feedback.
            self.sock.setsockopt(sol,4,0)
            self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8192)
            # Target: Linux aarch64, old SO_TIMESTAMPNS ABI uses two longs.
            self.timestamp_option=getattr(socket,'SO_TIMESTAMPNS',35)
            self.sock.setsockopt(socket.SOL_SOCKET,self.timestamp_option,1)
            self.sock.setblocking(False); self.sock.bind((name,))
        except BaseException:
            self.close(); raise

    def send(self,frame):
        if not self.enable_tx: raise PermissionError('CAN_transmit_locked')
        if (frame.can_id!=CONTROL_ID or len(frame.data)!=8 or frame.extended or frame.remote or frame.error):
            raise ValueError('only_standard_181_control_allowed')
        if self.sock.send(CAN_FRAME.pack(frame.can_id,8,frame.data))!=CAN_FRAME.size:
            raise OSError('short_can_write')

    def receive(self):
        try: packet, ancillary, flags, address=self.sock.recvmsg(CAN_FRAME.size,128)
        except BlockingIOError: return None
        if flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC) or len(packet)!=CAN_FRAME.size:
            raise RuntimeError('malformed_socketcan_frame')
        can_id,dlc,data=CAN_FRAME.unpack(packet)
        if dlc!=8:
            return Frame(can_id&0x1fffffff,b'',time.monotonic(),
                         bool(can_id&CAN_EFF_FLAG),bool(can_id&CAN_RTR_FLAG),bool(can_id&CAN_ERR_FLAG))
        timestamp=None
        for level,kind,value in ancillary:
            if level==socket.SOL_SOCKET and kind==self.timestamp_option:
                sec,nsec=struct.unpack('@ll',value[:struct.calcsize('@ll')])
                timestamp=sec+nsec/1e9
        if timestamp is None: raise RuntimeError('kernel_receive_timestamp_missing')
        age=time.time()-timestamp
        if age<-.05: raise RuntimeError('realtime_clock_jump_or_future_frame')
        received_at=time.monotonic()-max(0,age)
        return Frame(can_id&0x1fffffff,data,received_at,
                     bool(can_id&CAN_EFF_FLAG),bool(can_id&CAN_RTR_FLAG),bool(can_id&CAN_ERR_FLAG))

    def close(self):
        if self.sock is not None: self.sock.close(); self.sock=None
        if self.lock is not None: self.lock.close(); self.lock=None
