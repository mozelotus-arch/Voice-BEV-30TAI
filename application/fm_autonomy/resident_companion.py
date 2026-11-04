"""Hash-bound BEV companion injected into the existing voice DeviceOwner.

This is a joint-residency candidate, never a claim of measured joint capacity.
No alternate device/process or arbitrary browser-selected module is opened.
"""
import importlib
import json
import shutil
from pathlib import Path


def check_output_storage(root):
    """Fail before expensive model loads; never delete to create space."""
    path=Path(root).resolve()
    while not path.exists() and path!=path.parent:path=path.parent
    minimum=180*1024*1024  # 163,840,000-byte input plus header and write margin.
    free=shutil.disk_usage(path).free
    if free<minimum:
        raise OSError('joint_output_disk_space_insufficient: free={} required={}'.format(free,minimum))
    return dict(free_bytes=free,required_bytes=minimum,filesystem_path=str(path))


def prepare(entry, verify):
    config=json.loads(verify(entry).read_text(encoding='utf-8'))
    if config.get('schema')!='fm-voice-bev-resident-v1':
        raise ValueError('resident_bev_configuration_required')
    if config.get('input_storage','sd_spool') not in ('sd_spool','sdk_host_input'):
        raise ValueError('resident_bev_input_storage_required')
    if type(config.get('audit_decoder_input',False)) is not bool:
        raise ValueError('boolean_decoder_input_audit_required')
    check_output_storage(config['output_root'])
    files={name:verify(config[name]) for name in ('adapter_manifest','lut','postprocess')}
    package=Path(__file__).parent/'resident_bev'
    if files['adapter_manifest'].resolve()!=(package/'MANIFEST.json').resolve():
        raise ValueError('bundled_resident_adapter_required')
    manifest=json.loads(files['adapter_manifest'].read_text(encoding='utf-8'))
    if manifest.get('schema')!='FM-BEV-RESIDENT-PACKAGE/1':raise ValueError('resident_package_schema')
    for name,sha in manifest['files'].items():
        if Path(name).name!=name:raise ValueError('resident_package_path')
        verify(dict(path=str(package/name),sha256=sha))
    models=config.get('models', {})
    if set(models)!={'extractor','decoder'}:raise ValueError('two_bound_bev_graphs_required')
    specs={}
    for name,graph in models.items():
        specs[name]=(str(verify(graph['network_json'])),str(verify(graph['network_raw'])))
    for name in ('capture_root','output_root'):
        if not Path(config[name]).is_absolute():raise ValueError('absolute_workspace_root_required')
    limits=config['capacity']
    if any(type(limits.get(k)) is not int or limits[k]<=0 for k in
           ('pl_limit_bytes','expected_voice_pl_bytes','expected_bev_pl_bytes','ps_min_available_kib')):
        raise ValueError('explicit_joint_capacity_required')
    if limits['expected_voice_pl_bytes']+limits['expected_bev_pl_bytes']>limits['pl_limit_bytes']:
        raise ValueError('nominal_joint_pl_exceeds_capacity')
    if 'ps_min_non_cma_available_kib' in limits and (type(limits['ps_min_non_cma_available_kib']) is not int or limits['ps_min_non_cma_available_kib']<=0):
        raise ValueError('positive_non_cma_reserve_required')
    replay=config.get('capture_replay')
    if replay is not None:
        if not isinstance(replay,dict) or set(replay)!={'manifest_sha256','source_boot_id','run_id'}:
            raise ValueError('hash_bound_capture_replay_required')
        if any(not isinstance(v,str) or not v for v in replay.values()):
            raise ValueError('capture_replay_identity_required')
        if len(replay['manifest_sha256'])!=64 or any(c not in '0123456789abcdef' for c in replay['manifest_sha256']):
            raise ValueError('capture_replay_sha256_required')
    return config,files,specs


class ResidentCompanion:
    def check_host_margin(self,stage):
        observed=self.memory_reader()
        limits=self.config['capacity']
        if observed['available_kib']<limits['ps_min_available_kib']:
            raise MemoryError('joint_'+stage+'_host_margin_unavailable')
        minimum=limits.get('ps_min_non_cma_available_kib',0)
        estimate=observed.get('non_cma_available_estimate_kib')
        if minimum and (estimate is None or estimate<minimum):
            raise MemoryError('joint_'+stage+'_non_cma_host_margin_unavailable')
        return observed

    def __init__(self, owner, prepared, *, factory=None, memory_reader=None):
        config,files,paths=prepared
        self.config,self.owner=config,owner
        if memory_reader is None:
            from metrics import memory
            memory_reader=memory
        self.memory_reader=memory_reader
        self.check_host_margin('residence')
        if factory is None:
            factory=importlib.import_module('fm_autonomy.resident_bev.owner_bev').ResidentBev
        from graph_runtime import ModelSpec
        specs={name:ModelSpec('resident_bev_'+name,*pair) for name,pair in paths.items()}
        options={} if config.get('capture_replay') is None else dict(capture_replay=config['capture_replay'])
        if config.get('input_storage') is not None:options['input_storage']=config['input_storage']
        if config.get('audit_decoder_input') is not None:options['audit_decoder_input']=config['audit_decoder_input']
        self.backend=factory(owner,specs,str(files['lut']),str(files['postprocess']),**options)
        self.closed=False
        try:
            self.backend.load()
            used=owner.allocations()['sdk_allocated_bytes']
            if used>config['capacity']['pl_limit_bytes']:
                raise MemoryError('actual_joint_pl_exceeds_capacity')
            self.evidence=dict(model_count=len(owner.graphs),sdk_pl_bytes=used,
                model_process_memory=memory_reader(),same_owner=True,
                joint_quality_accepted=False)
        except BaseException:
            # Preserve the original SDK failure. A poisoned owner retains its
            # native roots; its existing parent cleanup/quarantine owns recovery.
            if not (getattr(owner,'failed',False) or getattr(owner,'quarantined',False)):
                self.backend.close()
            raise

    def infer(self, payload):
        if self.closed:raise RuntimeError('resident_bev_closed')
        if not isinstance(payload,dict) or set(payload)!={'capture_manifest','output_dir','boot_id','save_intermediates'}:
            raise ValueError('photo_manifest_request_required')
        for key,rootkey in (('capture_manifest','capture_root'),('output_dir','output_root')):
            path=Path(payload[key]).resolve();root=Path(self.config[rootkey]).resolve()
            if path==root or root not in path.parents:raise ValueError('photo_request_outside_bound_workspace')
        if type(payload['save_intermediates']) is not bool:raise ValueError('explicit_intermediate_policy_required')
        if not isinstance(payload['boot_id'],str) or not payload['boot_id']:raise ValueError('boot_identity_required')
        self.check_host_margin('inference')
        return self.backend.infer(**payload)

    def close(self):
        if not self.closed:
            self.backend.close()  # companion releases only its own graphs
            self.closed=True
