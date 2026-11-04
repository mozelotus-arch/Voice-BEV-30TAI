"""Data-only checks. A valid asset hash does not establish hardware readiness."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import struct

def inside(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError('relative_bundle_path_required')
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError('asset_path_escapes_bundle')
    return path

def verify_assets(root, manifest=None):
    root = Path(root).resolve()
    if manifest is None:
        manifest = json.loads((root / 'config/teammate_assets.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 'FM-ASSET-BINDING/1':
        raise ValueError('unsupported_asset_manifest')
    for name, record in manifest['files'].items():
        path = inside(root, name)
        raw = path.read_bytes()
        if len(raw) != record['bytes'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('asset_hash_mismatch:' + name)
    def bound(name):
        if name not in manifest['files']:
            raise ValueError('unbound_asset:' + name)
        return inside(root, name)
    geometry = json.loads(bound(manifest['geometry']).read_text())
    calibration_raw = bound(manifest['calibration']).read_bytes()
    calibration = json.loads(calibration_raw)
    lut = bound(manifest['lut']).read_bytes()
    if (hashlib.sha256(calibration_raw).hexdigest() != geometry['calibration_sha256'] or
            hashlib.sha256(lut).hexdigest() != geometry['lut_sha256']):
        raise ValueError('geometry_asset_binding_mismatch')
    if (geometry['slot_to_physical_camera'] != ['front', 'right', 'left', 'back', None, None]
            or geometry['inactive_slots'] != [4, 5] or geometry['inactive_slot_value'] != 0
            or geometry['feature_shape_nhwc'] != [6, 64, 176, 64]
            or len(lut) != 1280000 or geometry['lut']['entries'] != 160000):
        raise ValueError('unsupported_four_camera_contract')
    counts = dict.fromkeys((-1, 0, 1, 2, 3), 0)
    for camera, u, v, pad in struct.iter_unpack('<hhhh', lut):
        if (camera not in counts or pad != 0 or
                (camera == -1 and (u != 0 or v != 0)) or
                (camera >= 0 and not (0 <= u < 176 and 0 <= v < 64))):
            raise ValueError('LUT_index_outside_contract')
        counts[camera] += 1
    if sum(counts[c] for c in (0, 1, 2, 3)) == 0:
        raise ValueError('empty_LUT')
    models = {}
    for part, name in manifest['models'].items():
        model = json.loads(bound(name).read_text())
        raw = bound(str(Path(name).with_suffix('.raw')).replace('\\', '/')).read_bytes()
        if model['params_bytes'] != len(raw) or hashlib.md5(raw).hexdigest() != model['params_md5']:
            raise ValueError('model_JSON_RAW_mismatch')
        inputs = [v['dtype'] for op in model['ops'] if op['_type_key'].endswith('::Input') for v in op['outputs']]
        outputs = [v['dtype'] for op in model['ops'] if op['_type_key'].endswith('::Output') for v in op['inputs']]
        if (model['icraft_version'] != 'v3.39.0' or model.get('icraft_xir_version') != '3.39.0.0'
                or model['ai_target'] != '@zhuget(330)'):
            raise ValueError('unexpected_model_compiler_or_target')
        expected = [6, 256, 704, 3] if part == 'extractor' else [1, 200, 200, 4, 256]
        if len(inputs) != 1 or inputs[0]['shape'] != expected or inputs[0]['element_dtype'] != '@fp(32)':
            raise ValueError('model_input_contract_mismatch')
        expected_outputs = [[6,64,176,64]] if part == 'extractor' else [[1,100,100,80],[1,100,100,72],[1,100,100,16]]
        if ([v['shape'] for v in outputs] != expected_outputs or
                any(v['element_dtype'] != '@fp(32)' for v in outputs)):
            raise ValueError('model_output_contract_mismatch')
        models[part] = dict(version=model['icraft_version'], inputs=inputs, outputs=outputs,
                            raw_bytes=len(raw), params_md5_verified=True)
    return dict(assets_verified=True, models=models, lut_entries=160000, lut_camera_counts=counts,
                calibration_ready=calibration.get('ready_for_pro_runtime') is True,
                calibration_schema=calibration.get('schema'),
                frame_mapping=geometry['grid']['vehicle_frame_mapping_status'],
                physical_cameras=4, model_slots=6, inactive_slots=[4, 5])

def runtime_blockers(report, evidence=None, *, scope='autonomy'):
    """Missing measured evidence, never an unconditional implementation veto."""
    evidence=evidence or {}
    required=['icraft_runtime_339','native_cleanup','model_switching','measured_budgets','combined_fpga']
    if scope=='autonomy':required+=['synchronized_live_capture','temporal_pose','free_space_and_braking']
    elif scope!='diagnostic_model_chain':raise ValueError('unknown_validation_scope')
    reasons=[name+'_not_board_verified' for name in required if evidence.get(name) is not True]
    if scope=='autonomy' and not report['calibration_ready']:
        reasons.append('calibration_not_ready_for_pro_runtime')
    if scope=='autonomy' and report['frame_mapping'] != 'board_validated':
        reasons.append('provisional_vehicle_frame_mapping')
    return reasons
