"""Short-lived request adapter for the board's bounded four-camera RAM stream."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


CAPTURE_LAYOUT=((0,'front','1-1.2.3'),(1,'right','1-1.1.4'),
                (2,'left','1-1.1.3'),(3,'back','1-1.2.4'))


def correct_direction_binding(manifest,mapping_binding=None):
    """Bind verified physical USB ports to the frozen LUT's camera slots.

    Keep the acquisition manifest and all JPEG bytes unchanged. The correction
    was confirmed from the four live views by the operator on 2026-11-04.
    """
    manifest=Path(manifest)
    raw=manifest.read_bytes();capture=json.loads(raw)
    if mapping_binding is not None:
        # A reboot changes boot_id and video node names, and therefore the file
        # hash. Verify this boot's exact file, then the stable physical layout.
        mapping_raw=Path(mapping_binding['path']).read_bytes()
        mapping_sha=hashlib.sha256(mapping_raw).hexdigest()
        if mapping_sha!=mapping_binding['sha256'] or mapping_sha!=capture.get('mapping_sha256'):
            raise ValueError('direction_binding_mapping_identity_mismatch')
        mapping=json.loads(mapping_raw)
        layout=tuple((c.get('slot'),c.get('physical_camera'),c.get('usb_port')) for c in mapping.get('cameras',[]))
        if (mapping.get('schema')!='FM-CAMERA-MAPPING/1' or
                mapping.get('camera_mapping_confirmed') is not True or not mapping.get('confirmed_by') or
                layout!=CAPTURE_LAYOUT):
            raise ValueError('direction_binding_physical_layout_changed')
    elif capture.get('mapping_sha256')!='0097f181760a7bcfc302c58d0fc9daee156a5a03f6c85b1fbf3d98abd4ead920':
        return manifest
    if capture.get('direction_binding_correction'):
        raise ValueError('direction_binding_already_corrected')
    binding={'1-1.2.3':(0,0,'front'),'1-1.1.4':(1,3,'back'),
             '1-1.1.3':(2,2,'left'),'1-1.2.4':(3,1,'right')}
    if capture.get('status')!='complete' or len(capture.get('frames',[]))!=6:
        raise ValueError('complete_capture_required_for_direction_binding')
    for frame in capture['frames']:
        sources=frame['sources']
        if len(sources)!=4 or {s.get('physical_usb_port') for s in sources}!=set(binding):
            raise ValueError('direction_binding_physical_ports_mismatch')
        for source in sources:
            port=source['physical_usb_port'];old_slot,slot,name=binding[port]
            if (source['slot']!=old_slot or port not in Path(source['sysfs']).parts or
                    source['physical_camera']!=CAPTURE_LAYOUT[old_slot][1]):
                raise ValueError('direction_binding_source_identity_mismatch')
            source['capture_slot']=old_slot
            source['capture_direction_label']=source['physical_camera']
            source['slot']=slot;source['physical_camera']=name
        frame['sources']=sorted(sources,key=lambda s:s['slot'])
    capture['direction_binding_correction']=dict(
        original_manifest=manifest.name,original_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        confirmed_by='operator confirmed displayed physical views front/back/left/right on 2026-11-04',
        physical_port_to_model_slot={port:values[1] for port,values in binding.items()},
        model_slot_directions=['front','right','left','back'],image_bytes_changed=False,
        mapping_validation='file_sha256_and_physical_USB_layout' if mapping_binding is not None else 'historical_file_sha256')
    output=manifest.with_name('capture_directions_20261104.json')
    with output.open('x',encoding='utf-8') as stream:
        json.dump(capture,stream,ensure_ascii=False,indent=2)
    return output


def main():
    p=argparse.ArgumentParser();p.add_argument('request',type=Path);args=p.parse_args()
    request=json.loads(args.request.read_text(encoding='utf-8'))
    strategy=request['config'].get('strategy','baseline')
    if strategy=='fast':
        camera_dir=Path(__file__).resolve().parent.parent/'camera_continuous'
        release=json.loads((camera_dir/'release.json').read_text())
        for name,sha in release['files'].items():
            p=(camera_dir/name).resolve()
            if p.parent!=camera_dir or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:
                raise ValueError('camera release integrity mismatch: '+name)
        sys.path.insert(0,str(camera_dir))
        from continuous_client import capture_burst
    elif strategy=='baseline':from recording_capture import capture_burst
    else:raise ValueError('unsupported_capture_strategy')
    def progress(value):
        if Path(request['cancel_file']).exists():raise RuntimeError('capture_cancelled_by_user')
        print(json.dumps(value,ensure_ascii=False),flush=True)
    manifest=capture_burst(request['output_dir'],request['boot_id'],config=request['config'],progress=progress)
    manifest=correct_direction_binding(manifest,request['config']['mapping'])
    print(json.dumps(dict(event='worker_complete',capture_manifest=str(manifest))),flush=True)


if __name__=='__main__':main()
