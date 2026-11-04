"""Explicit boot-bound grants: bounded legacy tests or user-controlled sessions."""
import json
import hashlib
import time
import math
from pathlib import Path
from motion import Policy

EXPECTED_BOOT='63da329b16a246050035ca627ede8a8bc2916368b69ee76e29ec0770803a08ae'


def load_authorization(path,boot_id,clock=time.time):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    now=clock()
    required=('operator_authorized_start','wheels_raised_and_secured','operator_present',
              'physical_motor_stop_verified','wiring_verified','can_readback_verified',
              'chassis_identity_verified','axis_direction_verified')
    version=data.get('version')
    if version==7:
        required=('operator_authorized_start','user_controlled_session',
                  'explicit_no_wallclock_expiry','ground_motion_authorized',
                  'user_initiated_commands_only','metric_motion_authorized',
                  'charging_disconnected','wiring_verified','can_readback_verified',
                  'chassis_identity_verified')
        if (data.get('scope')!='ground_user_command_demo' or
                data.get('lifetime')!='until_explicit_stop' or
                data.get('wheels_raised_and_secured') is not False or
                data.get('agent_motion_authorized') is not False or
                data.get('autonomous_motion_authorized') is not False or
                data.get('ground_distance_calibrated') is not False or
                data.get('user_command_channels')!=['voice','bluetooth_text','llm_current_user_request','manual_hold'] or
                data.get('request_limits') not in ({'max_distance_m':.2,'max_angle_deg':30,'timeout_s':5},
                                                  {'max_distance_m':.2,'max_angle_deg':90,'timeout_s':5}) or
                not isinstance(data.get('session_id'),str) or not 8<=len(data['session_id'])<=96 or
                data.get('expires_unix_s') is not None):
            raise ValueError('explicit_ground_user_command_demo_required')
    if version==6:
        required=('operator_authorized_start','user_controlled_session',
                  'explicit_no_wallclock_expiry','ground_motion_authorized',
                  'user_web_controls_only','charging_disconnected',
                  'wiring_verified','can_readback_verified','chassis_identity_verified')
        if (data.get('scope')!='ground_manual_demo' or
                data.get('lifetime')!='until_explicit_stop' or
                data.get('wheels_raised_and_secured') is not False or
                data.get('agent_motion_authorized') is not False or
                data.get('autonomous_motion_authorized') is not False or
                data.get('metric_motion_authorized') is not False or
                data.get('ground_distance_calibrated') is not False or
                not isinstance(data.get('session_id'),str) or not 8<=len(data['session_id'])<=96 or
                data.get('expires_unix_s') is not None):
            raise ValueError('explicit_ground_manual_demo_required')
    if version==5:
        required=('operator_authorized_start','wheels_raised_and_secured','charging_disconnected',
                  'wiring_verified','can_readback_verified','chassis_identity_verified',
                  'user_controlled_session','explicit_no_wallclock_expiry')
        if (data.get('scope')!='user_controlled_raised_wheel_session' or
                data.get('lifetime')!='until_explicit_stop' or
                data.get('ground_motion_authorized') is not False or
                not isinstance(data.get('session_id'),str) or not 8<=len(data['session_id'])<=96 or
                data.get('expires_unix_s') is not None):
            raise ValueError('explicit_user_controlled_session_required')
    if version==2:
        # Records the current authorized bench scope honestly: encoder signs
        # do not prove ground geometry or physical emergency-stop performance.
        required=('operator_authorized_start','wheels_raised_and_secured',
                  'wiring_verified','can_readback_verified','chassis_identity_verified',
                  'encoder_response_verified')
        if data.get('ground_motion_authorized') is not False:
            raise ValueError('raised_wheel_scope_required')
    if version in (3,4):
        required=('operator_authorized_start','wheels_raised_and_secured','charging_disconnected',
                  'wiring_verified','can_readback_verified','chassis_identity_verified')
        if version==3:
            required+=('user_web_controls_only',)
            expected_scope='user_web_raised_wheel_revalidation'
        else:
            required+=('agent_interface_testing_authorized',)
            expected_scope='agent_interface_raised_wheel_revalidation'
            if data.get('user_web_controls_only') is not False or data.get('operator_present') is not False:
                raise ValueError('explicit_unattended_agent_interface_scope_required')
        if (data.get('scope')!=expected_scope or
            data.get('ground_motion_authorized') is not False or
            data.get('current_encoder_response_verified') is not False):
            raise ValueError('explicit_user_web_revalidation_scope_required')
        historical=data.get('historical_encoder_evidence',{})
        expected='49d57e2285c12f0b06fff254cc309699d0d6075b8a8b26b16771ff6cb729667d'
        evidence=Path(__file__).resolve().parents[2]/'evidence/recording_historical_vehicle.json'
        if historical.get('sha256')!=expected or hashlib.sha256(evidence.read_bytes()).hexdigest()!=expected:
            raise ValueError('bound_historical_encoder_evidence_required')
    if version not in (1,2,3,4,5,6,7) or any(data.get(k) is not True for k in required):
        raise ValueError('explicit_current_operator_checks_required')
    if data.get('boot_id')!=boot_id or data.get('boot_sha256')!=EXPECTED_BOOT:
        raise ValueError('authorization_boot_mismatch')
    if data.get('profile')!='r3_four_wheel_diff_gmr':
        raise ValueError('unsupported_or_unverified_chassis')
    issued=data.get('issued_unix_s'); until=data.get('expires_unix_s')
    # Default remains ten minutes. A one-hour window must be explicitly
    # recorded for the current supervised session, as authorized by the user.
    max_window=3600 if version==2 and data.get('operator_authorized_window_seconds')==3600 else 600
    if version in (5,6,7):
        if type(issued) not in (int,float) or not math.isfinite(issued) or issued>now:
            raise ValueError('invalid_user_session_issue_time')
    elif (type(issued) not in (int,float) or type(until) not in (int,float) or
            not issued<=now<until or not 0<until-issued<=max_window):
        raise ValueError('current_authorization_expired_or_invalid')
    if version in (6,7):
        if data.get('manual_limits')!={'linear_mm_s':100,'yaw_mrad_s':400} or data.get('bench_limits') is not None:
            raise ValueError('explicit_ground_manual_limits_required')
        return Policy(allow_motion=True,profile=data['profile'],max_linear_mm_s=100,
                      max_yaw_mrad_s=400,manual_only=version==6,user_commands_only=version==7,
                      session_s=5. if version==7 else 30.),None
    limits=data.get('bench_limits') if version in (2,3,4,5) else None
    if version in (3,4,5) and limits is None:raise ValueError('explicit_revalidation_limits_required')
    if limits is not None and limits!={'linear_mm_s':100,'yaw_mrad_s':800}:
        raise ValueError('only_current_validated_bench_limits_supported')
    return Policy(allow_motion=True,profile=data['profile'],
                  max_yaw_mrad_s=800 if limits else 200),None if version==5 else until-now
