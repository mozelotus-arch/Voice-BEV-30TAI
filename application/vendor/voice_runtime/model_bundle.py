"""Read model packages with explicit roles and file integrity, no SDK calls."""
import hashlib,json
from pathlib import Path,PurePosixPath

from graph_runtime import ModelSpec


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(1024**2),b''):value.update(data)
    return value.hexdigest()


class ModelBundle:
    def __init__(self,folder):
        self.folder=Path(folder).absolute()
        self.metadata=json.loads((self.folder/'BUNDLE.json').read_text(encoding='utf-8'))
        if self.metadata.get('schema')!='voice-model-bundle-v1':raise ValueError('Unsupported model package schema')
        if not isinstance(self.metadata.get('name'),str) or not self.metadata['name'].strip():
            raise ValueError('Model package name must be nonempty')
        if not isinstance(self.metadata.get('files'),dict):raise ValueError('Model package files must be a role map')
        self.files={}
        for role,info in self.metadata['files'].items():
            if (not isinstance(role,str) or not role or not isinstance(info,dict) or
                not isinstance(info.get('path'),str) or type(info.get('bytes')) is not int or info['bytes']<0 or
                not isinstance(info.get('sha256'),str) or len(info['sha256'])!=64 or
                any(c not in '0123456789abcdef' for c in info['sha256'])):
                raise ValueError('Invalid model package file descriptor')
            relative=PurePosixPath(info['path'])
            if (not info['path'] or relative.is_absolute() or '..' in relative.parts or
                '\\' in info['path'] or ':' in info['path']):
                raise ValueError('Model role must refer to a package-relative file')
            path=self.folder.joinpath(*relative.parts)
            if not path.is_file() or path.stat().st_size!=info['bytes'] or digest(path)!=info['sha256']:
                raise ValueError('Model package file integrity mismatch: '+role)
            self.files[role]=path
        if not {'network_json','network_raw'}.issubset(self.files):
            raise ValueError('Model package is missing compiled network files')
        self.spec=ModelSpec(self.metadata['name'],str(self.files['network_json']),str(self.files['network_raw']))

    def role(self,name):
        if name not in self.files:raise KeyError('Model package role missing: '+name)
        return self.files[name]
