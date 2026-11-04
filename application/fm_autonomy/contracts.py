"""All control geometry is metres/radians in x-forward, y-left vehicle frame."""
from dataclasses import dataclass
import math

CAMERAS = ('CAM_FRONT', 'CAM_FRONT_RIGHT', 'CAM_FRONT_LEFT',
           'CAM_BACK', 'CAM_BACK_LEFT', 'CAM_BACK_RIGHT')


def finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid_' + name)
    return float(value)


@dataclass(frozen=True)
class Obstacle:
    x_m: float
    y_m: float
    length_m: float
    width_m: float
    yaw_rad: float
    score: float
    class_id: int

    def __post_init__(self):
        for name in ('x_m', 'y_m', 'length_m', 'width_m', 'yaw_rad', 'score'):
            finite(getattr(self, name), name)
        if not (0 < self.length_m <= 100 and 0 < self.width_m <= 100 and 0 <= self.score <= 1):
            raise ValueError('invalid_obstacle_extent_or_score')
        if type(self.class_id) is not int:
            raise ValueError('invalid_class_id')

    def distance(self, x, y):
        # Distance to an oriented rectangle, including its interior.
        c, s = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        dx, dy = x - self.x_m, y - self.y_m
        u, v = c * dx + s * dy, -s * dx + c * dy
        return math.hypot(max(0, abs(u) - self.length_m / 2),
                          max(0, abs(v) - self.width_m / 2))


@dataclass(frozen=True)
class Snapshot:
    stream_id: str
    frame_id: int
    boot_id: str
    captured_at: float  # conservative lower bound in this board's monotonic clock
    completed_at: float
    obstacles: tuple
    valid: bool
    reason: str
    observed_radius_m: object = None  # independent evidence; empty detections are NOT free space
    synthetic: bool = False

    def __post_init__(self):
        if not self.stream_id or not self.boot_id or type(self.frame_id) is not int or self.frame_id < 1:
            raise ValueError('invalid_frame_identity')
        finite(self.captured_at, 'captured_at')
        finite(self.completed_at, 'completed_at')
        if self.captured_at < 0 or self.completed_at < self.captured_at:
            raise ValueError('invalid_frame_times')
        if not isinstance(self.obstacles, tuple) or len(self.obstacles) > 256 or any(not isinstance(o, Obstacle) for o in self.obstacles):
            raise ValueError('invalid_obstacles')
        if type(self.valid) is not bool or type(self.synthetic) is not bool:
            raise ValueError('invalid_validity')
        if self.observed_radius_m is not None and not 0 < finite(self.observed_radius_m, 'observed_radius') <= 100:
            raise ValueError('invalid_observed_radius')

    def rejection(self, now, boot_id, max_age_s, allow_synthetic=False):
        finite(now, 'clock')
        if self.boot_id != boot_id:
            return 'different_clock_domain'
        if self.synthetic and not allow_synthetic:
            return 'synthetic_input_cannot_drive_hardware'
        if not self.valid:
            return self.reason or 'invalid_perception'
        if not 0 <= now - self.captured_at < max_age_s or self.completed_at > now:
            return 'perception_stale_or_future'
        if self.observed_radius_m is None:
            return 'free_space_coverage_unverified'
        return None


@dataclass(frozen=True)
class Chassis:
    # No physical defaults: fill using measured envelope and braking tests.
    length_m: float
    width_m: float
    margin_m: float
    braking_distance_m: float
    stop_delay_s: float
    linear_m_s: float
    yaw_rad_s: float
    max_frame_age_s: float

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if finite(getattr(self, name), name) <= 0:
                raise ValueError('positive_chassis_values_required')
        if self.linear_m_s > .1 or self.yaw_rad_s > .8 or self.max_frame_age_s > .5:
            raise ValueError('first_version_motion_limits')

    @property
    def radius_m(self):
        return math.hypot(self.length_m, self.width_m) / 2 + self.margin_m


@dataclass(frozen=True)
class Decision:
    vx_mm_s: int
    wz_mrad_s: int
    state: str
    reason: str

    @property
    def velocity(self):
        return [self.vx_mm_s, 0, self.wz_mrad_s]
