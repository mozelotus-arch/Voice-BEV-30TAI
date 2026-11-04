"""Integrated board candidate. Default prints a plan; no hardware is opened.

Observe, raised-wheel bench and bounded user ground commands are supported.
Ground autonomy remains locked until the combined FPGA/perception is accepted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from fm_autonomy.dependencies import ROOT, add_vendor


def verify_manifest(root, name, nested=False):
    values=json.loads((root/name).read_text(encoding='utf-8'))
    entries=values['files'] if nested else values
    for relative,digest in entries.items():
        path=(root/relative).resolve()
        if root.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('release_hash_mismatch:' + relative)


def check(baseline, mode, authorization, platform_receipt=None):
    if sys.platform!='linux':raise RuntimeError('board_Linux_required')
    verify_manifest(ROOT,'bundle_manifest.json',True)
    verify_manifest(baseline,'release_manifest.json')
    add_vendor('vehicle','voice','vehicle/vendor_fvb1')
    import uart_v3
    import commissioning
    EXPECTED_BOOT=commissioning.EXPECTED_BOOT
    load_authorization=commissioning.load_authorization
    # Point at the verified runtime directory; retain every original boot/ABI check.
    uart_v3.ROOT=baseline
    if platform_receipt is not None:
        from fm_autonomy.platform_receipt import verify_platform
        boot_id,EXPECTED_BOOT=verify_platform(platform_receipt)
        # Keep the vendor transport intact; every Registers open repeats the new
        # image's receipt check, instead of impersonating an old UART-only trial.
        uart_v3.require_trial_boot=lambda:verify_platform(platform_receipt)[0]
        commissioning.EXPECTED_BOOT=EXPECTED_BOOT
    else:
        boot_id=uart_v3.require_trial_boot()
    mounts=[line.split() for line in Path('/proc/mounts').read_text().splitlines()
            if line.split()[0]=='/dev/mmcblk0p1']
    if len(mounts)!=1 or 'ro' not in mounts[0][3].split(','):
        raise RuntimeError('existing_readonly_boot_mount_required')
    digest=hashlib.sha256((Path(mounts[0][1])/'BOOT.bin').read_bytes()).hexdigest()
    if digest!=EXPECTED_BOOT:raise RuntimeError('baseline_BOOT_identity_mismatch')
    from motion import Policy
    policy,seconds=Policy(),0
    if mode in ('bench','ground-user'):
        if authorization is None:raise RuntimeError('current_motion_authorization_required')
        policy,seconds=load_authorization(authorization,boot_id)
        if mode=='ground-user' and not policy.user_commands_only:
            raise RuntimeError('ground_user_command_authorization_required')
        if mode=='bench' and (policy.user_commands_only or policy.manual_only):
            raise RuntimeError('ground_authorization_requires_ground_entry')
    return boot_id,policy,seconds


class NoAutonomy:
    chassis=None
    def reset(self):pass


class UnavailableVehicle:
    def __init__(self,error):self.error=str(error)[:120]
    def dispatch(self,op,args):
        if op=='vehicle.status':return dict(state='unavailable',reason=self.error,motion_allowed=False,
            feedback_fresh=False,feedback_age_ms=None,zero_enqueued=False,standstill_observed=False)
        raise RuntimeError('CAN_unavailable')
    def cancel(self,reason):pass
    def close(self):pass


def start(args,boot_id,policy,authorization_seconds):
    import fcntl
    from uart_v3 import Uart
    from control_wire import ControlLinkSession,encode_reply
    from fm_autonomy.can_actor import VehicleActor
    from fm_autonomy.arbiter import Arbiter,ControlLoop
    from fm_autonomy.perception import LatestPerception
    from fm_autonomy.application import IntegratedApplication
    from fm_autonomy.voice_adapter import make_voice
    from service import debug
    owner=open('/tmp/r6_validation.lock','a+')
    fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
    run=ROOT/'runs'/str(time.time_ns())
    run.mkdir(parents=True)
    # Copy the pinned voice source contract, never edit the original runtime.
    (run/'tts_source_contract.json').write_bytes((ROOT/'vendor/voice/tts_source_contract.json').read_bytes())
    (run/'npu_owner.json').write_text(json.dumps(dict(lock_path='/tmp/fm-npu-residence.lock')),encoding='utf-8')
    if args.voice_candidate_config is not None:
        (run/'voice_candidate.json').write_bytes(args.voice_candidate_config.read_bytes())
    log=(run/'events.jsonl').open('x',encoding='utf-8',buffering=1)
    log_lock=threading.Lock()
    from collections import deque
    from fm_autonomy.voice_feedback import terminal_failure
    motion_notices = deque(maxlen=8)
    application=None;uart=None;session=None;loop=None;last_sid=None;hub=None;capture=None;voice_http=None;photo=None
    def event(kind,value):
        nonlocal last_sid
        if kind=='handshake' and application is not None and value['sid']!=last_sid:
            application.connected();last_sid=value['sid']
        with log_lock:log.write(json.dumps(dict(t=time.monotonic(),event=kind,value=value),ensure_ascii=False)+'\n')
        if kind == 'metric_terminal':
            message = terminal_failure(value)
            if message and application is not None and application.voice is not None:
                motion_notices.append((dict(value), message, application.voice.generation.value))
    done=threading.Event()
    signal.signal(signal.SIGTERM,lambda *a:done.set())
    signal.signal(signal.SIGINT,lambda *a:done.set())
    try:
        key=bytes.fromhex((args.baseline/'link.key').read_text().strip())
        if len(key)!=32:raise ValueError('invalid_link_key')
        try:
            vehicle=VehicleActor(policy=policy,enable_tx=args.mode in ('bench','ground-user'),authorization_seconds=authorization_seconds)
        except Exception as exc:
            if args.mode in ('bench','ground-user'):raise
            vehicle=UnavailableVehicle(exc)
        metric_calibration=None
        if args.metric_calibration is not None:
            metric_calibration=json.loads(args.metric_calibration.read_text(encoding='utf-8'))
            if metric_calibration.get('scope')!={'bench':'encoder_bench','ground-user':'encoder_ground_demo'}.get(args.mode):
                raise ValueError('metric_scope_must_match_entry')
        arbiter=Arbiter(vehicle,NoAutonomy(),LatestPerception(boot_id),boot_id,autonomy_enabled=False,
                        metric_calibration=metric_calibration,event=event)
        application=IntegratedApplication(arbiter,None, command_contract=(
            'confirmed_vehicle_v1' if args.voice_candidate_config is not None else 'legacy_seconds'),
            voice_motion_enabled=not args.manual_vehicle_only)
        if args.accelerator_profile is not None:
            from fm_autonomy.npu_hub import NpuHub
            hub=NpuHub.from_file(args.accelerator_profile)
        application.voice=make_voice(run,event,lambda:application,hub=hub)
        if args.photo_config is not None:
            if hub is None:raise RuntimeError('photo_requires_shared_accelerator_profile')
            from fm_autonomy.board_photo_service import BoardPhotoService
            photo=BoardPhotoService(application,hub,json.loads(args.photo_config.read_text(encoding='utf-8')),boot_id)
            application.photo_service=photo
        if args.voice_http_config is not None:
            from fm_autonomy.local_voice_http import make_server as make_voice_http
            config=json.loads(args.voice_http_config.read_text(encoding='utf-8'))
            token=Path(config['token_file']).read_text(encoding='utf-8').strip()
            voice_http=make_voice_http(application,config['host'],config['port'],token,config['client_ip'],boot_id)
            threading.Thread(target=voice_http.serve_forever,kwargs={'poll_interval':.1},daemon=True).start()
            event('local_voice_http_ready',dict(host=config['host'],port=voice_http.server_port,voice_only=True))
        if args.capture_config is not None:
            if hub is None:raise RuntimeError('capture_requires_shared_accelerator_profile')
            from fm_autonomy.capture_worker import CaptureWorker
            capture=CaptureWorker(hub,args.capture_config,run/'perception',event)
        loop=ControlLoop(arbiter);loop.start()
        uart=Uart()
        session=ControlLinkSession(key,application.dispatch,application.cancel,event)
        for command in ('AT+VERSION','AT+STAT'):
            uart.send((command+'\r\n').encode());uart.drain()
            began=time.monotonic();last=began;reply=bytearray()
            while time.monotonic()-began<2:
                part=uart.receive()
                if part:reply.extend(part);last=time.monotonic()
                if reply and time.monotonic()-last>.15:break
                time.sleep(.005)
            event('startup_query',dict(command=command,reply_hex=reply.hex()))
            if command=='AT+STAT':
                if b'+STAT=00' in reply:session.module_connected=False
                elif b'+STAT=02' in reply or b'+STAT=01' in reply:session.module_connected=True
        began=time.monotonic();next_wake=began+2
        event('opened',dict(boot_id=boot_id,mode=args.mode,autonomy_enabled=False))
        print('FM_INTEGRATED_CANDIDATE_READY '+args.mode,flush=True)
        while not done.is_set() and not (run/'stop.request').exists() and (args.until_stopped or time.monotonic()-began<args.seconds):
            if motion_notices and application.voice.notify_motion_failure(*motion_notices[0]):
                motion_notices.popleft()
            session.observe(uart.status(),debug(uart))
            for reply in session.feed(uart.receive(256)):
                uart.send(encode_reply(reply,key));uart.drain()
            if session.module_connected is False and session.state=='await_hello' and time.monotonic()>=next_wake and time.monotonic()-session.last_rx>.25:
                uart.send(b'AT+VERSION\r\n');uart.drain();next_wake=time.monotonic()+2
            done.wait(.002)
    finally:
        try:
            if voice_http is not None:voice_http.shutdown();voice_http.server_close()
            if photo is not None:photo.close()
            if loop is not None:loop.close()
            if session is not None:session.close()
            if application is not None:application.close()
        finally:
            if capture is not None:event('capture_closed',dict(stopped=capture.close()))
            if hub is not None:
                release=hub.close(timeout=None)
                event('accelerators_closed',release)
                if not release['quiescent']:
                    raise RuntimeError('accelerator_release_unconfirmed_owner_retained')
            if uart is not None:uart.close()
            event('closed',dict(boot_written=False,rebooted=False))
            log.close();owner.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path)
    p.add_argument('--voice-http-config',type=Path,help='Private voice-only HTTP config; no automatic load or mic')
    p.add_argument('--mode',choices=('observe','bench','ground-user'),default='observe')
    p.add_argument('--authorization',type=Path)
    p.add_argument('--platform-receipt',type=Path,help='Current boot-bound merged C/D/E/F acceptance record')
    p.add_argument('--accelerator-profile',type=Path)
    p.add_argument('--voice-candidate-config',type=Path,
                   help='Explicit version-bound voice candidate; no silent legacy ASR fallback')
    p.add_argument('--metric-calibration',type=Path,help='Explicit measured feedback scales and bounded targets')
    p.add_argument('--capture-config',type=Path)
    p.add_argument('--photo-config',type=Path,help='Explicit photo capture/output roots; on-demand jobs reuse the voice owner')
    p.add_argument('--manual-vehicle-only',action='store_true',help='Voice recognition/readback only; vehicle motion requires manual controls')
    lifetime=p.add_mutually_exclusive_group()
    lifetime.add_argument('--seconds',type=int,default=120)
    lifetime.add_argument('--until-stopped',action='store_true',help='Explicit v5 user session: run until signal or stop.request')
    action=p.add_mutually_exclusive_group()
    action.add_argument('--check',action='store_true')
    action.add_argument('--start',action='store_true')
    args=p.parse_args()
    if not 1<=args.seconds<=3600:p.error('seconds must be 1..3600')
    if not (args.start or args.check):
        print(json.dumps(dict(plan_only=True,hardware_access=False,autonomy_enabled=False,
            requires=['current_board_state','verified_baseline_directory','current_boot_receipt',
                      'read_only_BOOT_hash','exclusive_UART_CAN_NPU_owners',
                      'fresh_raised_wheels_authorization_for_bench']),ensure_ascii=False,indent=2))
        return
    if args.baseline is None:p.error('--baseline is required for check/start')
    if args.voice_candidate_config is None:p.error('final all-3.39 entry requires --voice-candidate-config; legacy fallback is not selected')
    if args.metric_calibration is not None:
        from fm_autonomy.metric_motion import MetricCalibration, validate_ground_request_limits
        metric=MetricCalibration(**json.loads(args.metric_calibration.read_text(encoding='utf-8')))
        if metric.scope!={'bench':'encoder_bench','ground-user':'encoder_ground_demo'}.get(args.mode):
            p.error('metric scope must match the selected entry')
        if args.mode=='ground-user':
            if args.authorization is None:p.error('ground-user requires a matching request authorization')
            validate_ground_request_limits(metric,json.loads(args.authorization.read_text(encoding='utf-8')))
    if args.mode=='ground-user' and (args.manual_vehicle_only or args.metric_calibration is None):
        p.error('ground-user requires bounded encoder_ground_demo and all explicit user command channels')
    if args.voice_candidate_config is not None:
        if args.accelerator_profile is None:p.error('voice candidate requires shared accelerator profile')
        from fm_autonomy.candidate_voice import validate_config
        voice_config=json.loads(args.voice_candidate_config.read_text(encoding='utf-8'))
        if voice_config.get('release_track')!='all_icraft339_final':p.error('final entry requires all_icraft339_final')
        validate_config(voice_config)
        if args.photo_config is not None:
            from fm_autonomy.candidate_voice import verified_file
            companion=json.loads(verified_file(voice_config['resident_bev']).read_text(encoding='utf-8'))
            photo_config=json.loads(args.photo_config.read_text(encoding='utf-8'))
            if companion.get('capture_replay') is not None:p.error('recording_photo_requires_current_capture_not_replay')
            if any(Path(photo_config[key]).resolve()!=Path(companion[key]).resolve() for key in ('capture_root','output_root')):
                p.error('photo_roots_must_match_resident_companion')
    # Validate candidate model bindings before UART/CAN/device checks or owners.
    if args.accelerator_profile is not None:
        from fm_autonomy.profile import load_profile,require_supported_hardware
        require_supported_hardware(load_profile(args.accelerator_profile))
    args.baseline=args.baseline.resolve()
    boot_id,policy,seconds=check(args.baseline,args.mode,args.authorization,args.platform_receipt)
    if args.until_stopped and (args.mode not in ('bench','ground-user') or seconds is not None):
        p.error('--until-stopped requires explicit user-controlled lifetime authorization')
    print(json.dumps(dict(checks='passed',boot_id=boot_id,mode=args.mode,will_start=args.start)))
    if args.start:start(args,boot_id,policy,seconds)


if __name__=='__main__':main()
