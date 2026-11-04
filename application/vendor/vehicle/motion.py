"""Deterministic fail-closed motion state machine, driven by a monotonic clock.

The deadline bounds a software request to send zero. It does not guarantee a
physical stop across power loss, process/OS hangs, or a failed/disconnected bus.
"""
import math
import secrets
import time
from dataclasses import dataclass
from wheeltec import Frame, CONTROL_ID, ZERO, velocity_frame


@dataclass(frozen=True)
class Policy:
    allow_motion: bool = False
    profile: str = 'unverified'
    max_linear_mm_s: int = 100
    max_yaw_mrad_s: int = 200
    horizon_s: float = .5
    feedback_timeout_s: float = .2
    session_s: float = 30.0
    send_period_s: float = .05
    stop_retry_s: float = .6
    manual_only: bool = False
    user_commands_only: bool = False

    def __post_init__(self):
        if type(self.manual_only) is not bool or type(self.user_commands_only) is not bool:
            raise ValueError('command_scope_must_be_boolean')
        if self.manual_only and self.user_commands_only:
            raise ValueError('conflicting_command_scopes')
        if not (0 < self.horizon_s <= .5 and 0 < self.feedback_timeout_s <= .2
                and 0 < self.session_s <= 30 and .005 <= self.send_period_s <= .05
                and 0 <= self.max_linear_mm_s <= 200
                and 0 <= self.max_yaw_mrad_s <= 1000):
            raise ValueError('unsafe_policy')


class MotionController:
    def __init__(self, send, policy=None, clock=time.monotonic, tx_enabled=True):
        self.send=send; self.policy=policy or Policy(); self.clock=clock
        self.tx_enabled=tx_enabled
        self.state='disarmed'; self.reason='startup'; self.target=(0,0,0)
        self.motion_id=None; self.ticket=None; self.ticket_until=0.0
        self.deadline=0.0; self.session_until=0.0; self.next_send=0.0
        self.stop_until=0.0; self.stop_at=None; self.zero_sent_at=None
        self.last_telemetry=None; self.tx_fault=None; self.zero_count=0
        self.last_tick=clock()

    def observe(self, telemetry):
        now=self.clock()
        if not math.isfinite(telemetry.received_at) or telemetry.received_at>now+.001:
            self.stop('invalid_feedback_timestamp')
            return
        if self.last_telemetry and telemetry.received_at<=self.last_telemetry.received_at:
            return
        self.last_telemetry=telemetry
        if self.motion_id and telemetry.disabled:
            self.stop('chassis_disabled')

    def fresh(self, now):
        t=self.last_telemetry
        return t is not None and 0 <= now-t.received_at < self.policy.feedback_timeout_s

    def _transmit(self, frame):
        if not self.tx_enabled: return False
        try:
            self.send(frame)
            if frame.data==ZERO:
                self.zero_sent_at=self.clock(); self.zero_count+=1
            return True
        except Exception as exc:
            first_fault=self.tx_fault is None
            self.tx_fault=type(exc).__name__+': '+str(exc)[:100]
            self.state='fault'; self.reason='can_tx_failed'; self.target=(0,0,0)
            self.motion_id=None; self.ticket=None
            if first_fault:
                self.stop_at=self.clock(); self.stop_until=self.clock()+self.policy.stop_retry_s
            return False

    def stop(self, reason='operator_stop'):
        now=self.clock()
        self.zero_sent_at=None
        self.target=(0,0,0); self.motion_id=None; self.ticket=None
        self.deadline=0.0; self.ticket_until=0.0
        self.state='fault' if self.tx_fault else 'stopped'; self.reason=reason
        self.stop_at=now; self.stop_until=now+self.policy.stop_retry_s
        self.next_send=now+self.policy.send_period_s
        self._transmit(Frame(CONTROL_ID,ZERO))
        return self.status()

    def _new_ticket(self, now):
        self.ticket=secrets.token_hex(8)
        self.ticket_until=min(now+self.policy.horizon_s,self.session_until)
        return {'t':self.ticket, 'h':max(0,int((self.ticket_until-now)*1000))}

    def arm(self):
        now=self.clock(); self.tick()
        p=self.policy
        if not p.allow_motion or not self.tx_enabled: raise ValueError('motion_locked_pending_operator_authorization')
        if p.profile!='r3_four_wheel_diff_gmr': raise ValueError('chassis_profile_unverified')
        if self.tx_fault: raise ValueError('can_fault_requires_service_restart')
        if self.motion_id: raise ValueError('already_armed')
        if not self.fresh(now): raise ValueError('fresh_feedback_required')
        if self.last_telemetry.disabled: raise ValueError('chassis_disabled')
        if any(abs(v)>10 for v in self.last_telemetry.velocity):
            raise ValueError('standstill_feedback_required')
        if not self._transmit(Frame(CONTROL_ID,ZERO)): raise ValueError('can_tx_failed')
        self.motion_id=secrets.token_hex(8); self.state='armed'; self.reason='explicit_arm'
        self.target=(0,0,0); self.session_until=now+p.session_s
        result=self._new_ticket(now); self.deadline=self.ticket_until
        self.next_send=now+p.send_period_s
        result['m']=self.motion_id
        # This is an actual CAN observation, not freshness inferred from an ACK.
        result['feedback']=self.status()
        return result

    def drive(self, args):
        self.tick(); now=self.clock()
        try:
            if not self.motion_id: raise ValueError('explicit_arm_required')
            if set(args)!={'m','t','v'}: raise ValueError('invalid_drive_fields')
            if args['m']!=self.motion_id: raise ValueError('stale_motion_session')
            if not isinstance(args['t'],str) or not secrets.compare_digest(args['t'],self.ticket):
                raise ValueError('invalid_or_consumed_ticket')
            if now>=self.ticket_until: raise ValueError('expired_ticket')
            if not self.fresh(now): raise ValueError('feedback_stale')
            if self.last_telemetry.disabled: raise ValueError('chassis_disabled')
            frame=velocity_frame(args['v'])
            x,y,z=args['v']
            if y!=0 or abs(x)>self.policy.max_linear_mm_s or abs(z)>self.policy.max_yaw_mrad_s:
                raise ValueError('velocity_limit_or_lateral_motion')
        except (ValueError,TypeError,KeyError):
            self.stop('rejected_drive')
            raise
        # The old board-issued ticket bounds the command; receiving a delayed
        # request cannot grant it a fresh full lease.
        self.deadline=self.ticket_until; self.target=(x,y,z); self.state='active'
        self.ticket=None
        if not self._transmit(frame): raise ValueError('can_tx_failed')
        self.next_send=now+self.policy.send_period_s
        result=self._new_ticket(now)
        result['d']=max(0,int((self.deadline-now)*1000))
        t=self.last_telemetry
        result['s']=[t.disabled,*t.velocity,t.battery_mv]
        return result

    def tick(self):
        now=self.clock()
        if now<self.last_tick:
            self.stop('monotonic_clock_regressed')
        self.last_tick=now
        if self.motion_id:
            reason=None
            if now>=self.deadline: reason='motion_deadline'
            elif now>=self.session_until: reason='motion_session_expired'
            elif not self.fresh(now): reason='feedback_stale'
            elif self.last_telemetry.disabled: reason='chassis_disabled'
            if reason: self.stop(reason)
        if now>=self.next_send:
            if self.motion_id:
                self._transmit(velocity_frame(self.target))
            elif now<self.stop_until:
                self._transmit(Frame(CONTROL_ID,ZERO))
            self.next_send=now+self.policy.send_period_s

    def status(self):
        now=self.clock(); t=self.last_telemetry
        fresh=self.fresh(now)
        stopped=(fresh and self.motion_id is None and self.stop_at is not None and
                 t.received_at>self.stop_at and all(abs(v)<=10 for v in t.velocity))
        return {'version':1, 'state':self.state, 'reason':self.reason,
                'continuous_drive_validated':False,
                'motion_allowed':self.policy.allow_motion, 'target':list(self.target),
                'manual_only':self.policy.manual_only,
                'user_commands_only':self.policy.user_commands_only,
                'max_linear_mm_s':self.policy.max_linear_mm_s,
                'max_yaw_mrad_s':self.policy.max_yaw_mrad_s,
                'deadline_ms':max(0,int((self.deadline-now)*1000)),
                'feedback_fresh':fresh,
                'feedback_age_ms':None if t is None else max(0,int((now-t.received_at)*1000)),
                'feedback_received_at':None if t is None else t.received_at,
                'telemetry':None if t is None else t.compact(),
                'zero_enqueued':self.zero_sent_at is not None,
                'standstill_observed':bool(stopped), 'tx_fault':self.tx_fault}
