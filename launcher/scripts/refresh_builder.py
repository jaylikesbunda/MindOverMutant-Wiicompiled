"""Refresh source inputs in an assembled MoM builder without replacing tools."""
from pathlib import Path
import argparse, json, shutil, sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from builder_revision import validate

ap=argparse.ArgumentParser();ap.add_argument('--pack',type=Path,default=ROOT/'launcher/src-tauri/builder-pack');args=ap.parse_args()
pack=args.pack.resolve();dest=pack/'native-builder'
if not (pack/'toolchain/python/python.exe').is_file() or not (dest/'vendor/NWiiRecomp/PINNED_COMMIT').is_file():
    raise SystemExit('expected an assembled MoM builder pack')
value=validate(ROOT)
previous=json.loads((dest/'builder-revision.json').read_text(encoding='utf-8'))
for relative in previous['required_inputs'].keys()-value['required_inputs'].keys():
    target=(dest/relative).resolve()
    if not target.is_relative_to(dest):raise SystemExit('invalid prior builder input: '+relative)
    if target.is_file():target.unlink()
for relative in value['required_inputs']:
    target=dest/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/relative,target)
for name in ('builder-revision.json','LICENSE','THIRD_PARTY_NOTICES.md'):shutil.copy2(ROOT/name,dest/name)
validate(dest)
print('Refreshed builder source inputs: '+value['revision'])
