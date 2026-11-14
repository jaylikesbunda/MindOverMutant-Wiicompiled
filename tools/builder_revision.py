"""Content identity shared by the launcher and isolated source builder."""
from pathlib import Path
import argparse, hashlib, json
ROOT=Path(__file__).resolve().parents[1]
REVISION='2026.11.14-3'

def paths(root):
    for name in ('recomp_config.toml','config.toml','patches/mom-release-runtime.patch','data/main.dol.sha256'):
        yield root/name
    for folder in ('tools','overlays','reference'):
        for path in sorted((root/folder).rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or 'user' in path.relative_to(root/folder).parts:continue
            if path.suffix in ('.py','.cpp','.h','.hpp','.inc','.json','.tsv','.bin','.txt','.md'):
                if '-before' not in path.name and '_baseline.' not in path.name:yield path

def identity(root=ROOT):
    return dict(schema_version=1,revision=REVISION,required_inputs={
        p.relative_to(root).as_posix():hash_input(p) for p in paths(root)})

def hash_input(path):
    data=path.read_bytes()
    if path.suffix!='.bin':data=data.replace(b'\r\n',b'\n')
    return hashlib.sha256(data).hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def validate(root=ROOT):
    expected=json.loads((root/'builder-revision.json').read_text())
    actual=identity(root)
    if actual!=expected:raise SystemExit('builder inputs changed; run python tools/builder_revision.py --write')
    return actual

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--write',action='store_true');args=ap.parse_args()
    if args.write:(ROOT/'builder-revision.json').write_text(json.dumps(identity(),indent=2)+'\n')
    value=validate();print(value['revision']+' '+digest(value))
