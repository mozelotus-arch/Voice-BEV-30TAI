"""CPU LUT bridge to the ORIGINAL full decoder input, no hardware access."""
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np

SELECTED = (5, 4, 2, 0)  # current, previous 1/3/5 actual capture batches
SLOTS = ('front', 'right', 'left', 'back', None, None)
LUT_SHA = '4d84bb0b918819263a39397881481d83118f84d2d9ecd7bbab1e5e661572d2b8'

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)

def validate_capture(path, boot_id):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('schema') != 'FM-CAMERA-BURST/1' or data.get('board_boot_id') != boot_id
            or data.get('stationary_camera_rig') is not True or data.get('real_capture') is not True
            or data.get('camera_mapping_confirmed') is not True or not data.get('run_id')):
        raise ValueError('real_current_boot_stationary_capture_and_confirmed_mapping_required')
    active = data['active_slots']
    if (not active or any(type(s) is not int or s not in range(4) for s in active)
            or len(set(active)) != len(active)):
        raise ValueError('candidate_supports_only_distinct_front_right_left_back_slots')
    frames = data['frames']
    if len(frames) != 6 or [f['frame_index'] for f in frames] != list(range(6)):
        raise ValueError('six_actual_capture_batches_required')
    seen = set()
    last = {s: -float('inf') for s in active}
    for frame in frames:
        sources = frame['sources']
        if sorted(s['slot'] for s in sources) != sorted(active):
            raise ValueError('every_batch_requires_same_actual_camera_set')
        for s in sources:
            slot = s['slot']
            if s.get('physical_camera') != SLOTS[slot] or not all(s.get(k) for k in ('device','sysfs','captured_at','sha256')):
                raise ValueError('explicit_physical_camera_and_source_metadata_required')
            stamp = s['captured_monotonic_s']
            if not isinstance(stamp, (float, int)) or not np.isfinite(stamp) or stamp <= last[slot]:
                raise ValueError('capture_time_must_increase_per_camera')
            last[slot] = stamp
            p = (path.parent/s['path']).resolve()
            if path.parent not in p.parents or p in seen:
                raise ValueError('each_capture_requires_distinct_file_within_burst_directory')
            seen.add(p)
            if sha(p) != s['sha256']:
                raise ValueError('capture_file_hash_mismatch')
            im = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if im is None or im.shape != (240,320,3):
                raise ValueError('candidate_requires_real_320x240_BGR_capture')
            s['_absolute_path'] = str(p)
    return data

def preprocess(sources):
    result = np.zeros((6,256,704,3), dtype=np.float32)
    for s in sources:
        im = cv2.imread(s['_absolute_path'], cv2.IMREAD_COLOR)
        if im is None or im.shape != (240,320,3):
            raise ValueError('capture_read_failed')
        result[s['slot'], :, 181:522, :] = cv2.resize(im, (341,256), interpolation=cv2.INTER_LINEAR)
    return result

def load_lut(path, active):
    if sha(path) != LUT_SHA:
        raise ValueError('frozen_candidate_LUT_required')
    lut = np.fromfile(path, dtype='<i2').reshape(4,200,200,4)
    # The candidate picks the first valid camera. Missing selected cameras become
    # invalid (conservative holes); never silently fall back to a wrong view.
    valid = np.isin(lut[...,0], active)
    if np.any(valid & ((lut[...,1] < 0) | (lut[...,1] >= 176) | (lut[...,2] < 0) | (lut[...,2] >= 64))):
        raise ValueError('LUT_feature_coordinate_out_of_bounds')
    return lut, valid

def project_into(feature, lut, valid, target, temporal_group):
    if feature.shape != (6,64,176,64) or feature.dtype != np.float32 or not np.isfinite(feature).all():
        raise ValueError('extractor_output_contract_mismatch')
    if target.shape != (1,200,200,4,256) or target.dtype != np.float32:
        raise ValueError('full_decoder_input_contract_mismatch')
    # LUT is [Z,Y,X]; decoder full input is [N,X,Y,Z,C]. The fourth
    # dimension is height Z; time is concatenated inside the 256 channels.
    for z in range(4):
        y, x = np.nonzero(valid[z])
        cam, u, v = (lut[z,y,x,k] for k in range(3))
        target[0,x,y,z,temporal_group*64:(temporal_group+1)*64] = feature[cam,v,u,:]

def public_sources(capture):
    return [dict({k:v for k,v in source.items() if not k.startswith('_')}, frame_index=i)
            for i in SELECTED for source in capture['frames'][i]['sources']]
