"""Reject game data, local state and private binaries in Git publication inputs."""
from pathlib import Path
import argparse, json, re, subprocess, tomllib
from builder_revision import ROOT, validate

FORBIDDEN={'.iso','.wbfs','.rvz','.wia','.ciso','.gcz','.dol','.rel','.rcf','.nwstate',
           '.sav','.dtm','.raw','.episodes','.direct','.prompts','.gcda','.gcno','.exe','.dll','.obj','.o','.a','.pdb','.dmp','.wav','.bmp','.ll','.bc'}
PRIVATE={'dump','orig','game','nand','savestates','export','build','logs','release-work','target','builder-pack','.git','reports','preservation'}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,default=ROOT);args=ap.parse_args()
    root=args.root.resolve();errors=[]
    if (root/'.git').exists():
        files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
        paths=[root/p for p in set(files) if p and (root/p).is_file()]
    else:paths=[p for p in root.rglob('*') if p.is_file()]
    for p in paths:
        rel=p.relative_to(root)
        if p.suffix.lower() in FORBIDDEN or any(part in PRIVATE for part in rel.parts):errors.append(str(rel))
        if rel.parts[:2]==('reference','user') or p.name.upper().startswith('HANDOFF') or '-before' in p.name or '_baseline.' in p.name:errors.append(str(rel))
        if p.name.startswith('.env') or p.suffix.lower() in ('.pem','.key','.pfx'):errors.append(str(rel))
        if p.is_file() and p.stat().st_size>50*1024*1024:errors.append(str(rel)+' exceeds 50 MiB')
    patch=(root/'patches/mom-release-runtime.patch').read_text(encoding='utf-8')
    if re.search(r'^[+]#include\s+"[A-Za-z]:[/\\]',patch,re.M):errors.append('runtime patch contains machine-specific includes')
    for name in ('LICENSE','THIRD_PARTY_NOTICES.md','README.md','data/main.dol.sha256','builder-revision.json'):
        if not (root/name).is_file():errors.append('missing '+name)
    version=json.loads((root/'launcher/src-tauri/tauri.conf.json').read_text())['version']
    cargo=tomllib.loads((root/'launcher/src-tauri/Cargo.toml').read_text())['package']['version']
    if version!=cargo:errors.append('launcher Cargo/Tauri versions differ')
    validate(root)
    from release_pgo import validate_bundle
    validate_bundle(root/'reference/pgo/mom-pal.json')
    if errors:raise SystemExit('Publication blocked:\n  '+'\n  '.join(sorted(set(errors))))
    print(f'Publication inputs passed: {len(paths)} files; no disc/runtime binaries or local play state')

if __name__=='__main__':main()
