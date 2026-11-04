"""Recreate the isolated ARM64 iCraft runtime as files; never open a device."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def digest(blob):
    return hashlib.sha256(blob).hexdigest()

def ar_members(blob):
    if not blob.startswith(b'!<arch>\n'):
        raise ValueError('Expected Debian ar archive')
    offset=8
    while offset<len(blob):
        header=blob[offset:offset+60]
        if len(header)!=60 or header[58:]!=b'`\n':
            raise ValueError('Invalid ar member header')
        name=header[:16].decode('ascii').strip().rstrip('/')
        size=int(header[48:58]);offset+=60
        value=blob[offset:offset+size]
        if len(value)!=size:
            raise ValueError('Truncated ar member')
        yield name,value
        offset+=size+(size%2)

def destination(root,name):
    parts=PurePosixPath(name).parts
    if not parts or name.startswith('/') or any(p in ('..','') or ':' in p for p in parts):
        raise ValueError('Unsafe archive path')
    target=root.joinpath(*[p for p in parts if p!='.'])
    if root.resolve() not in target.resolve().parents:
        raise ValueError('Archive path escapes output')
    return target

def write_file(root,name,blob):
    path=destination(root,name)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as stream:
        stream.write(blob)

def build(deb,wheel,output):
    manifest=json.loads((ROOT/'运行环境恢复清单.json').read_text(encoding='utf-8'))
    expected={v['name']:v for v in manifest['sdk_archives']}
    archives={}
    for path in (deb,wheel):
        if path.name not in expected:
            raise ValueError('Unrecognized SDK archive name')
        blob=path.read_bytes();entry=expected[path.name]
        if len(blob)!=entry['bytes'] or digest(blob)!=entry['sha256']:
            raise ValueError('SDK archive identity mismatch: '+path.name)
        archives[path.name]=blob
    output=output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    members=[blob for name,blob in ar_members(archives[deb.name]) if name.startswith('data.tar')]
    if len(members)!=1:
        raise ValueError('Expected exactly one Debian data archive')
    with tarfile.open(fileobj=io.BytesIO(members[0]),mode='r:*') as archive:
        for member in archive:
            name=member.name[2:] if member.name.startswith('./') else member.name
            if not name.startswith('usr/lib/aarch64-linux-gnu/') or member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Unexpected nonregular runtime member: '+name)
            with archive.extractfile(member) as stream:
                write_file(output,name,stream.read())
    with zipfile.ZipFile(io.BytesIO(archives[wheel.name])) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            if member.external_attr>>16 & 0o170000 == 0o120000:
                raise ValueError('Unexpected wheel symbolic link')
            write_file(output/'python',member.filename,archive.read(member))
    patch=json.loads((ROOT/'runtime_dependencies/allocator2g.json').read_text(encoding='utf-8'))
    target=output/patch['original']['runtime_relative_path']
    before=target.read_bytes()
    if len(before)!=patch['original']['bytes'] or digest(before)!=patch['original']['sha256']:
        raise ValueError('Allocator original identity mismatch')
    after=bytearray(before)
    if after[168516:168520].hex()!='e24f74b2':
        raise ValueError('Allocator instruction mismatch')
    after[168517]=0x4b
    if digest(after)!=patch['candidate']['sha256']:
        raise ValueError('Allocator reconstructed identity mismatch')
    target.write_bytes(after)
    files={p.relative_to(output).as_posix():{'bytes':p.stat().st_size,'sha256':digest(p.read_bytes())}
           for p in sorted(output.rglob('*')) if p.is_file()}
    receipt={'schema':'FM-ISOLATED-RUNTIME-FILES/1','hardware_opened':False,
             'models_loaded':False,'allocator_sha256':digest(after),'files':files}
    (output/'runtime_files.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'files':len(files),'allocator_sha256':digest(after),'hardware_opened':False}))
    return receipt

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deb',type=Path,required=True)
    parser.add_argument('--wheel',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='New directory; existing output is rejected')
    args=parser.parse_args()
    build(args.deb,args.wheel,args.output)

if __name__=='__main__':
    main()
