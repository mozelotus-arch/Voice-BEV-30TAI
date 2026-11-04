"""VEH1 result adapter. Arrival/finish times never renew an old camera frame."""
from dataclasses import dataclass
from .contracts import CAMERAS, Obstacle, Snapshot, finite


@dataclass(frozen=True)
class ResultContract:
    source_units: str
    axes: str
    calibration_sha256: str
    lut_sha256: str
    camera_latency_bound_s: object = None
    max_arrival_spread_s: float = .075

    def __post_init__(self):
        if self.source_units not in ('cm', 'm') or self.axes != 'x_forward_y_left_z_up':
            raise ValueError('explicit_units_and_vehicle_axes_required')
        for digest in (self.calibration_sha256, self.lut_sha256):
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('asset_hash_required')
        if self.camera_latency_bound_s is not None and not 0 <= finite(self.camera_latency_bound_s, 'camera_latency') <= 2:
            raise ValueError('camera_latency_bound_invalid')
        if not 0 <= finite(self.max_arrival_spread_s, 'max_spread') <= .5:
            raise ValueError('camera_spread_invalid')


class FastBEVAdapter:
    def __init__(self, contract, boot_id):
        self.contract, self.boot_id = contract, boot_id

    def convert(self, envelope):
        c = self.contract
        if envelope.get('schema') != 'FM-BEV/1' or envelope.get('boot_id') != self.boot_id:
            raise ValueError('schema_or_boot_mismatch')
        if envelope.get('calibration_sha256') != c.calibration_sha256 or envelope.get('lut_sha256') != c.lut_sha256:
            raise ValueError('perception_asset_mismatch')
        frame_id = envelope['frame_id']
        result = envelope['result']
        if type(result.get('frame_id')) is not int or result['frame_id'] != frame_id:
            raise ValueError('backend_frame_mismatch')
        if result.get('input_capture_ts_ns') != envelope.get('input_capture_ts_ns'):
            raise ValueError('backend_capture_echo_mismatch')
        frames = envelope['cameras']
        if set(frames) != set(CAMERAS):
            raise ValueError('all_calibrated_cameras_required')
        times = []
        for frame in frames.values():
            if frame.get('timestamp_kind') != 'usb_frame_complete':
                raise ValueError('unsupported_timestamp_kind')
            times.append(finite(frame['received_mono_s'], 'camera_time'))
        completed = finite(envelope['completed_mono_s'], 'completed_time')
        if min(times) < 0 or max(times) > completed:
            raise ValueError('camera_time_out_of_order')
        reasons = []
        if max(times) - min(times) > c.max_arrival_spread_s:
            reasons.append('camera_arrival_spread')
        if c.camera_latency_bound_s is None:
            reasons.append('camera_upstream_latency_unmeasured')
        if envelope.get('backend_ok') is not True:
            reasons.append('backend_failed')
        captured = min(times) - (c.camera_latency_bound_s or 0)
        scale = .01 if c.source_units == 'cm' else 1.0
        objects = result.get('objects')
        if not isinstance(objects, list) or len(objects) > 256:
            raise ValueError('bounded_object_list_required')
        obstacles = tuple(Obstacle(finite(o['x'], 'x') * scale, finite(o['y'], 'y') * scale,
                                   finite(o['length'], 'length') * scale, finite(o['width'], 'width') * scale,
                                   finite(o['yaw'], 'yaw'), finite(o['score'], 'score'), o['class_id'])
                          for o in objects)
        # This adapter does NOT infer free space from FastBEV detections.
        return Snapshot(envelope['stream_id'], frame_id, self.boot_id, captured, completed,
                        obstacles, not reasons, ','.join(reasons), observed_radius_m=None,
                        synthetic=envelope.get('synthetic', False))


class LatestPerception:
    """One item only, with explicit stream changes and a monotonic sequence fence."""
    def __init__(self, boot_id):
        import threading
        self.lock = threading.Lock()
        self.boot_id, self.stream_id = boot_id, None
        self.snapshot = None
        self.last_id = 0
        self.last_capture = -1.0

    def begin_stream(self, stream_id):
        with self.lock:
            self.stream_id = stream_id
            self.snapshot, self.last_id, self.last_capture = None, 0, -1.0

    def put(self, snapshot):
        with self.lock:
            if snapshot.boot_id != self.boot_id or snapshot.stream_id != self.stream_id:
                raise ValueError('unregistered_perception_stream')
            if snapshot.frame_id <= self.last_id or snapshot.captured_at <= self.last_capture:
                raise ValueError('replayed_or_out_of_order_frame')
            self.snapshot = snapshot
            self.last_id, self.last_capture = snapshot.frame_id, snapshot.captured_at

    def get(self):
        with self.lock:
            return self.snapshot
