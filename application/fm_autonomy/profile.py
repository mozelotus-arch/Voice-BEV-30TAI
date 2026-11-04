"""Resolve bundle-relative paths and reject unsupported accelerator bindings."""
import copy
import hashlib
import json
from pathlib import Path
from .asset_binding import inside, verify_assets, runtime_blockers

def load_profile(path):
    path = Path(path).resolve()
    profile = json.loads(path.read_text(encoding='utf-8'))
    root = (path.parent / profile.pop('bundle_root', '..')).resolve()
    profile = copy.deepcopy(profile)
    for entry in profile['models'].values():
        for key in ('executable', 'config', 'cwd', 'run_dir', 'model_json', 'model_raw','pre_json','pre_raw','post_json','post_raw','metrics_dir','bundle_manifest'):
            if key in entry and entry[key] is not None:
                entry[key] = str(inside(root, entry[key]))
    profile['_bundle_root'] = str(root)
    return profile

def require_supported_hardware(profile):
    if profile.get('authorized_voice_validation') is not None:
        from .candidate_voice import verified_file,validate_config
        from .accelerator import Budget
        config=json.loads(verified_file(profile['authorized_voice_validation']).read_text())
        if config.get('release_track')!='all_icraft339_final':raise ValueError('all339_validation_profile_required')
        validate_config(config)
        entry=profile['models'].get('voice',{})
        if (set(profile['models'])!={'voice'} or entry.get('pin_resident_for_validation') is not True
            or entry.get('budget_evidence_kind')!='supervised_capacity_ceiling_not_measurement'
            or entry.get('budget',{}).get('load_timeout_s') is None):
            raise ValueError('voice_validation_only_bounded_residence_required')
        limit=profile['memory_limit_bytes']
        if type(limit)!=int or limit<=0:raise ValueError('explicit_validation_capacity_ceiling_required')
        Budget(**entry['budget']).validate(limit,hardware=False)
        if config.get('resident_bev'):
            # Only planning data is needed in this parent. The child verifies
            # every companion file before DeviceOwner construction.
            companion=json.loads(verified_file(config['resident_bev']).read_text(encoding='utf-8'))
            if companion.get('schema')!='fm-voice-bev-resident-v1':raise ValueError('resident_bev_configuration_required')
            capacity=companion['capacity']
            if (limit!=capacity['pl_limit_bytes'] or
                entry['budget']['resident_bytes']<capacity['expected_voice_pl_bytes']+capacity['expected_bev_pl_bytes']):
                raise ValueError('profile_must_budget_all_five_resident_graphs')
        return config
    if profile.get('asset_manifest') != 'config/teammate_assets.json':
        raise ValueError('explicit_verified_asset_binding_required')
    root=Path(profile['_bundle_root'])
    report = verify_assets(root)
    if profile['models'].get('bev',{}).get('backend_family')!='nuscenes_pro_sa_managed_static':
        raise ValueError('nuscenes_pro_models_incompatible_with_Vehicle_live_FP16_backend')
    receipt_name=profile.get('hardware_receipt')
    if not receipt_name:raise ValueError('bound_hardware_receipt_required')
    receipt=json.loads(inside(root,receipt_name).read_text())
    if receipt.get('schema')!='FM-ALL339-HARDWARE-RECEIPT/1':raise ValueError('hardware_receipt_schema')
    scope=profile.get('validation_scope')
    if receipt.get('scope')!=scope:raise ValueError('hardware_receipt_scope_mismatch')
    def sha(path):
        h=hashlib.sha256()
        with Path(path).open('rb') as f:
            for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
        return h.hexdigest()
    if receipt.get('asset_manifest_sha256')!=sha(root/'config/teammate_assets.json'):
        raise ValueError('hardware_receipt_model_binding_changed')
    bound_models=copy.deepcopy(profile['models'])
    for entry in bound_models.values():
        for key in ('executable','config','cwd','run_dir','model_json','model_raw','pre_json','pre_raw','post_json','post_raw','metrics_dir','bundle_manifest'):
            if entry.get(key) is not None:entry[key]=Path(entry[key]).relative_to(root).as_posix()
    if receipt.get('models')!=bound_models or receipt.get('memory_limit_bytes')!=profile['memory_limit_bytes']:
        raise ValueError('hardware_receipt_budget_or_backend_changed')
    for name,expected in receipt.get('files',{}).items():
        if sha(inside(root,name))!=expected:raise ValueError('hardware_receipt_file_changed:'+name)
    executable=Path(profile['models']['bev']['executable']).relative_to(root).as_posix()
    if executable not in receipt.get('files',{}):raise ValueError('hardware_receipt_native_unbound')
    for model,manifest in [('voice','config/voice339_assets.json'),('campp','config/campp339_assets.json')]:
        if model not in profile['models']:continue
        if receipt.get('additional_asset_manifests',{}).get(manifest)!=sha(root/manifest):
            raise ValueError('hardware_receipt_additional_models_unbound:'+model)
        for name,record in json.loads((root/manifest).read_text())['files'].items():
            if sha(inside(root,name))!=record['sha256']:raise ValueError('model_asset_changed:'+name)
    if 'campp' in profile['models']:
        from .campp_split import audit_bundle
        audit_bundle(profile['models']['campp'])
    boot=receipt.get('boot',{})
    if boot.get('path')!='/sdcard/BOOT.bin' or sha(boot['path'])!=boot.get('sha256'):
        raise ValueError('hardware_receipt_boot_changed')
    blockers = runtime_blockers(report,receipt.get('checks'),scope=scope)
    if blockers:
        raise ValueError('accelerator_profile_blocked:' + ';'.join(blockers))
