"""Evidence-bound candidate validation, separate from production quality acceptance."""
import json
import math
from pathlib import Path
import time


def validate_evidence(config, verified_file):
    receipt_path = verified_file(config['execution_evidence'])
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if receipt.get('schema') != 'fm-voice-board-evidence-v1':
        raise ValueError('voice_execution_evidence_required')
    def source(key):
        entry = dict(receipt[key])
        entry['path'] = str(receipt_path.parent / entry['path'])
        return json.loads(verified_file(entry).read_text(encoding='utf-8'))
    summary, inventory = source('summary'), source('asset_verification')
    for kind, name in (('asr','asr100_rebuilt'),('bert','vehicle_bert'),('tts','tts_decoder')):
        if summary[name].get('execution_success') is not True:
            raise ValueError('model_execution_not_verified:' + kind)
        if not summary[name]['release_checks'] or any(
                row.get('returned_to_baseline') is not True for row in summary[name]['release_checks']):
            raise ValueError('model_teardown_not_verified:' + kind)
        bundle = json.loads(verified_file(config[kind+'_manifest']).read_text(encoding='utf-8'))
        for role in ('network_json','network_raw'):
            inventory_name = 'asr' if kind=='asr' else name
            rows = [r for r in inventory['candidate_files'] if r['candidate']==inventory_name and r['role']==role]
            if len(rows)!=1 or rows[0]['match'] is not True or rows[0]['sha256']!=bundle['files'][role]['sha256']:
                raise ValueError('evidence_model_identity_mismatch:' + kind)
    if summary['serial'].get('success') is not True or summary['serial'].get('retained_outputs_unchanged') is not True:
        raise ValueError('shared_owner_execution_evidence_required')
    if summary.get('asr_production_replacement_allowed') is not False:
        raise ValueError('candidate_quality_state_changed_requires_review')
    if config.get('quality_acknowledgements') != {
            'asr_frontend_sensitive_word_difference': True,
            'asr_emotion_token_difference': True, 'tts_listening_not_accepted': True,
            'production_replacement_allowed': False}:
        raise ValueError('explicit_candidate_quality_acknowledgement_required')
    return summary


def validate_authorization(config, verified_file, *, boot_id=None, now=None):
    """Current boot/time grant supplied by the authorized board test owner."""
    grant = json.loads(verified_file(config['validation_authorization']).read_text(encoding='utf-8'))
    now = time.monotonic() if now is None else now
    issued, until = grant.get('issued_monotonic_s'), grant.get('expires_monotonic_s')
    user_session=grant.get('schema')=='fm-voice-user-session-authorization-v2'
    if user_session:
        if (grant.get('user_controlled_session') is not True or grant.get('explicit_no_wallclock_expiry') is not True or
                grant.get('lifetime')!='until_explicit_stop' or until is not None or
                not isinstance(grant.get('session_id'),str) or not 8<=len(grant['session_id'])<=96 or
                not isinstance(grant.get('boot_id'),str) or not grant['boot_id'] or
                type(issued) not in (int,float) or not math.isfinite(issued) or issued>now):
            raise ValueError('explicit_user_voice_session_required')
    bounded_time_valid=(all(type(v) in (int,float) and math.isfinite(v) for v in (issued,until)) and
                        issued<=now<until and 0<until-issued<=3600) if not user_session else True
    if (grant.get('schema') not in ('fm-voice-validation-authorization-v1','fm-voice-user-session-authorization-v2') or
            grant.get('operator_authorized_candidate_validation') is not True or
            grant.get('allow_old_model_fallback') is not False or
            grant.get('production_replacement_allowed') is not False or
            not isinstance(grant.get('authorization_reference'),str) or not grant['authorization_reference'] or
            not bounded_time_valid):
        raise ValueError('current_candidate_validation_authorization_required')
    if boot_id is not None and grant.get('boot_id') != boot_id:
        raise ValueError('voice_validation_boot_mismatch')
    for kind in ('asr','bert','tts'):
        if grant.get('model_manifest_sha256',{}).get(kind) != config[kind+'_manifest']['sha256']:
            raise ValueError('validation_authorization_model_mismatch')
    return grant


def require_selected_runtime(config):
    from importlib.metadata import version
    import icraft
    runtime = Path(config['runtime_root']).resolve()
    if version('icraft') != '3.39.0' or runtime not in Path(icraft.__file__).resolve().parents:
        raise RuntimeError('isolated_icraft339_runtime_required')
    # Imports load all libraries before a device is opened; reject mixed installations.
    from icraft import xir, xrt, host_backend
    from icraft.zg330backend import ZG330Backend
    for line in Path('/proc/self/maps').read_text().splitlines():
        path = line.split()[-1]
        if any(x in path for x in ('libicraft','libaxizg','/xrt.','/xir.','/zg330backend.')):
            if runtime not in Path(path).resolve().parents:
                raise RuntimeError('mixed_icraft_runtime_libraries')
