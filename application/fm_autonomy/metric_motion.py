"""Feedback-integrated distance/angle controller, with explicit calibrated scales.

Only complete CAN group samples are integrated. No commanded-speed odometry.
Encoder displacement is an estimate unless ground calibration is declared.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class MetricCalibration:
    scope: str
    evidence_id: str
    linear_m_per_count_s: float
    yaw_rad_per_count_s: float
    max_distance_m: float
    max_angle_deg: float
    distance_tolerance_m: float
    angle_tolerance_deg: float
    linear_command_mm_s: int
    yaw_command_mrad_s: int
    timeout_s: float
    no_progress_s: float
    max_feedback_linear_count_s: float
    max_feedback_yaw_count_s: float
    feedback_timeout_s: float = .2

    def __post_init__(self):
        if self.scope not in ('encoder_bench','encoder_ground_demo','ground_calibrated') or not self.evidence_id:
            raise ValueError('metric_calibration_evidence_required')
        for key in ('linear_m_per_count_s','yaw_rad_per_count_s','max_distance_m','max_angle_deg',
                    'distance_tolerance_m','angle_tolerance_deg','timeout_s','no_progress_s','feedback_timeout_s',
                    'max_feedback_linear_count_s','max_feedback_yaw_count_s'):
            value=getattr(self,key)
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise ValueError('measured_metric_parameter_required:'+key)
        if (not 0<self.feedback_timeout_s<=.2 or not 0<self.no_progress_s<=self.timeout_s<=30 or
            not 0<self.distance_tolerance_m<self.max_distance_m or
            not 0<self.angle_tolerance_deg<self.max_angle_deg):
            raise ValueError('invalid_metric_bounds')
        if (type(self.linear_command_mm_s) is not int or not 0<self.linear_command_mm_s<=100 or
            type(self.yaw_command_mrad_s) is not int or not 0<self.yaw_command_mrad_s<=800):
            raise ValueError('metric_command_exceeds_existing_bench_limits')
        if self.scope=='encoder_ground_demo' and (self.max_distance_m>.2 or self.max_angle_deg>90 or
                self.timeout_s>5 or self.yaw_command_mrad_s>400):
            raise ValueError('ground_demo_short_request_limits_required')


def validate_ground_request_limits(calibration, authorization):
    """The loaded control envelope must stay within the current explicit grant."""
    if calibration.scope!='encoder_ground_demo':
        return
    if authorization.get('scope')!='ground_user_command_demo':
        raise ValueError('ground_metric_authorization_required')
    limits=authorization.get('request_limits') or {}
    for key in ('max_distance_m','max_angle_deg','timeout_s'):
        maximum=limits.get(key)
        if type(maximum) not in (int,float) or not math.isfinite(maximum) or not 0<getattr(calibration,key)<=maximum:
            raise ValueError('metric_config_exceeds_authorized_request:'+key)


class MetricMotion:
    def __init__(self, calibration):
        self.config = calibration
        self.reset()

    def reset(self):
        self.active=False
        self.progress=0.
        self.last=None
        self.reason='idle'

    def sample(self, status, now):
        telemetry=status.get('telemetry') or {}
        stamp=status.get('feedback_received_at')
        sequence=status.get('feedback_groups')
        velocity=telemetry.get('v')
        if (status.get('feedback_fresh') is not True or status.get('tx_fault') or telemetry.get('disabled')!=0 or
            type(stamp) not in (int,float) or not math.isfinite(stamp) or
            not 0<=now-stamp<self.config.feedback_timeout_s or type(sequence) is not int or
            sequence<1 or not isinstance(velocity,list) or len(velocity)!=3 or
            any(type(v) is not int or not -32768<=v<=32767 for v in velocity)):
            raise ValueError('metric_feedback_invalid_or_stale')
        if (max(abs(velocity[0]),abs(velocity[1]))>self.config.max_feedback_linear_count_s or
                abs(velocity[2])>self.config.max_feedback_yaw_count_s):
            raise ValueError('metric_feedback_exceeds_calibrated_range')
        return stamp,sequence,velocity

    def start(self, semantic, status, now):
        stamp,sequence,velocity=self.sample(status,now)
        kind=semantic['intent']
        if kind not in ('move','turn'):raise ValueError('metric_intent_required')
        expected_unit='m' if kind=='move' else 'deg'
        if semantic.get('normalized_unit')!=expected_unit:raise ValueError('metric_unit_mismatch')
        value=float(semantic['normalized_value'])
        maximum=self.config.max_distance_m if kind=='move' else self.config.max_angle_deg
        tolerance=self.config.distance_tolerance_m if kind=='move' else self.config.angle_tolerance_deg
        if not math.isfinite(value) or not tolerance<value<=maximum:
            raise ValueError('metric_target_outside_calibrated_range')
        if any(abs(v)>10 for v in velocity):raise ValueError('metric_start_requires_standstill')
        self.axis=0 if kind=='move' else 2
        positive='前进' if kind=='move' else '左转'
        negative='后退' if kind=='move' else '右转'
        if semantic['direction'] not in (positive,negative):raise ValueError('metric_direction_mismatch')
        self.sign=1 if semantic['direction']==positive else -1
        self.scale=self.config.linear_m_per_count_s if kind=='move' else self.config.yaw_rad_per_count_s
        self.target=value if kind=='move' else math.radians(value)
        self.tolerance=tolerance if kind=='move' else math.radians(tolerance)
        self.speed=self.config.linear_command_mm_s if kind=='move' else self.config.yaw_command_mrad_s
        self.progress=0.;self.started=now;self.last_progress=now
        self.last=(stamp,sequence,velocity[self.axis]);self.active=True;self.reason='tracking'

    def step(self, status, now):
        if not self.active:raise RuntimeError('metric_motion_not_active')
        stamp,sequence,velocity=self.sample(status,now)
        old_stamp,old_sequence,old_velocity=self.last
        if sequence<old_sequence or stamp<old_stamp:raise ValueError('metric_feedback_regressed')
        if sequence!=old_sequence:
            dt=stamp-old_stamp
            if not 0<dt<=self.config.feedback_timeout_s:raise ValueError('metric_feedback_gap')
            delta=self.sign*(old_velocity+velocity[self.axis])*.5*self.scale*dt
            if delta < -self.tolerance:raise ValueError('metric_feedback_wrong_direction')
            self.progress+=delta
            if delta>0:self.last_progress=now
            self.last=(stamp,sequence,velocity[self.axis])
        elif stamp!=old_stamp or velocity[self.axis]!=old_velocity:
            raise ValueError('metric_sample_identity_changed')
        if self.progress>=self.target-self.tolerance:
            self.active=False;self.reason='metric_target_reached_stop_requested'
            return [0,0,0],self.reason
        if now-self.started>=self.config.timeout_s:raise ValueError('metric_session_timeout')
        if now-self.last_progress>=self.config.no_progress_s:raise ValueError('metric_no_feedback_progress')
        command=[0,0,0];command[self.axis]=self.sign*self.speed
        return command,None

    def status(self):
        return dict(active=self.active,progress=self.progress,reason=self.reason,
                    scope=self.config.scope,calibration_evidence=self.config.evidence_id,
                    ground_calibration_declared=self.config.scope=='ground_calibrated')
