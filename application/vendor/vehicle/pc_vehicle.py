"""Windows Bluetooth vehicle-status client. This CLI never sends a drive command."""
import argparse
import datetime
import json
import sys
import threading
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASELINE=Path('D:/FM/board_validation/20261028_bluetooth_voice_r2')
sys.path.insert(0,str(ROOT/'vendor_fvb1'))


class PCBluetoothLock:
    """Same nonblocking first-byte Windows lock as the local web bridge."""
    def __init__(self,path=Path('D:/FM/integration_20261028/pc_bluetooth.lock')):
        self.path=path;self.file=None
    def __enter__(self):
        import msvcrt
        self.file=self.path.open('a+b')
        try:
            if self.file.seek(0,2)==0:self.file.write(b'\0');self.file.flush()
            self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
        except BaseException:self.file.close();self.file=None;raise
        return self
    def __exit__(self,*unused):
        import msvcrt
        if self.file is not None:
            self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            self.file.close();self.file=None


class VehicleClient:
    """No queue, reconnect replay or implicit arm. Call from one owner only."""
    def __init__(self,commands):
        self.commands=commands;self.motion_id=None;self.ticket=None
        self.owner=threading.Lock()

    def _call(self,op,args):
        c=self.commands
        if c.sid is None:raise ConnectionError('FVB1_handshake_required')
        c.seq+=1
        try:
            timeout=2.0 if op in ('vehicle.status','vehicle.stop') else .9
            reply=c.exchange({'v':1,'sid':c.sid,'seq':c.seq,'op':op,'args':args},timeout=timeout)
            if not reply.get('ok'):raise RuntimeError(reply.get('error','vehicle_command_failed'))
            return reply['data']
        except BaseException:
            self.motion_id=None;self.ticket=None
            # Even a negative reply ends this client's motion attempt. Closing
            # also prevents callers continuing an outcome-unknown connection.
            c.abort()
            raise

    def status(self):
        with self.owner:return self._call('vehicle.status',{})

    def arm(self):
        with self.owner:
            r=self._call('vehicle.arm',{})
            self.motion_id=r['m'];self.ticket=r['t'];return r

    def drive(self,v):
        if not self.owner.acquire(blocking=False):raise RuntimeError('one_request_in_flight_no_motion_queue')
        try:
            if self.motion_id is None or self.ticket is None:raise RuntimeError('explicit_arm_required')
            r=self._call('vehicle.drive',{'m':self.motion_id,'t':self.ticket,'v':v})
            self.ticket=r['t'];return r
        finally:self.owner.release()

    def stop(self):
        with self.owner:
            self.motion_id=None;self.ticket=None
            return self._call('vehicle.stop',{})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir',type=Path,default=BASELINE)
    parser.add_argument('--status',action='store_true',required=True,
                        help='read status only; no arm or driving CLI is exposed')
    args=parser.parse_args()
    runtime=args.runtime_dir.resolve()
    sys.path.append(str(runtime))
    from command_client import CommandClient
    from pc_bluetooth import Link
    key=bytes.fromhex((runtime/'link.key').read_text().strip())
    if len(key)!=32:raise ValueError('invalid_link_key')
    run=ROOT/'client_runs'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    run.mkdir(parents=True)
    class Radio(Link):
        def __init__(self):
            self.mac='123459658872';self.name='WHEELTEC';self.sock=None
            self.log=(run/'radio.jsonl').open('x',encoding='utf-8')
    with PCBluetoothLock():
        radio=Radio();commands=CommandClient(radio,key)
        try:
            commands.connect();result=VehicleClient(commands).status()
            (run/'status.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:commands.close();radio.log.close()


if __name__=='__main__':main()
