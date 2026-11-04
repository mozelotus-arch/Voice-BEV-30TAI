"""Bounded reactive cruise: brake, turn/arc around an obstacle, then cruise again.

This is a local planner, not goal navigation. It requires measured free-space
coverage; a detector returning an empty list does not authorize motion.
"""
import math
from .contracts import Decision


class CruisePlanner:
    def __init__(self, chassis):
        self.chassis = chassis
        self.reset()

    def reset(self):
        self.state, self.turn = 'cruise', 0
        self.avoiding_since = None
        self.clear_frames = 0
        self.last_frame = None

    def _clearance(self, snapshot, v, w, age):
        p = self.chassis
        # Conservative circular envelope includes every orientation of the car,
        # measured stopping distance, data age, actuator delay and sample error.
        reserve = p.braking_distance_m + p.linear_m_s * (age + p.stop_delay_s)
        radius = p.radius_m + reserve + .005
        nearest = float('inf')
        for index in range(61):
            t = index * .05  # 3 s prediction, <=5 mm centre spacing at 0.1 m/s
            if abs(w) < 1e-9:
                x, y = v * t, 0
            else:
                x, y = v / w * math.sin(w * t), v / w * (1 - math.cos(w * t))
            coverage = snapshot.observed_radius_m - math.hypot(x, y) - radius
            if coverage <= 0:
                return None
            distance = min((o.distance(x, y) - radius for o in snapshot.obstacles), default=coverage)
            if distance <= 0:
                return None
            nearest = min(nearest, distance, coverage)
        return nearest

    def step(self, snapshot, now):
        p = self.chassis
        if snapshot is None or snapshot.observed_radius_m is None or not snapshot.valid:
            return Decision(0, 0, 'wait', 'perception_or_coverage_missing')
        age = now - snapshot.captured_at
        if not 0 <= age < p.max_frame_age_s or snapshot.completed_at > now:
            return Decision(0, 0, 'wait', 'perception_stale_or_future')
        fresh_frame = (snapshot.stream_id, snapshot.frame_id) != self.last_frame
        self.last_frame = (snapshot.stream_id, snapshot.frame_id)
        forward = self._clearance(snapshot, p.linear_m_s, 0, age)
        if forward is not None:
            if fresh_frame:
                self.clear_frames += 1
            if self.state == 'cruise' or self.clear_frames >= 3:
                self.reset()
                self.last_frame = (snapshot.stream_id, snapshot.frame_id)
                return Decision(round(p.linear_m_s * 1000), 0, 'cruise', 'forward_corridor_clear')
        else:
            self.clear_frames = 0
            if self.state == 'cruise':
                self.state = 'brake'
                self.avoiding_since = now
                return Decision(0, 0, 'brake', 'obstacle_ahead')
        if self.avoiding_since is None:
            self.avoiding_since = now
        if now - self.avoiding_since >= 8:
            self.state = 'wait'
            return Decision(0, 0, 'wait', 'no_route_within_turn_budget')
        candidates = []
        for sign in (1, -1):
            if self.turn and sign != self.turn:
                continue  # avoid oscillating between left and right every frame
            for v in (p.linear_m_s * .5, 0):
                w = sign * p.yaw_rad_s
                distance = self._clearance(snapshot, v, w, age)
                if distance is not None:
                    candidates.append((v * 10 + min(distance, 1), sign, v, w))
        if not candidates:
            self.state = 'wait'
            return Decision(0, 0, 'wait', 'no_observed_collision_free_path')
        _, self.turn, v, w = max(candidates)
        self.state = 'bypass' if v else 'turn'
        return Decision(round(v * 1000), round(w * 1000), self.state,
                        'avoid_left' if self.turn > 0 else 'avoid_right')
