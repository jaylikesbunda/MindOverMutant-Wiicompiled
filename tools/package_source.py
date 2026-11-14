"""Create a reviewable GitHub source snapshot from the current working tree."""
from pathlib import Path
import argparse, json, subprocess, sys, zipfile
from builder_revision import ROOT

def main():
    version=json.loads((ROOT/'launcher/src-tauri/tauri.conf.json').read_text())['version']
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,default=ROOT/'dist'/f'MoM-WiiComp-{version}-source.zip');args=ap.parse_args()
    subprocess.run([sys.executable,ROOT/'tools/check_release.py'],cwd=ROOT,check=True)
    if args.out.exists():raise SystemExit('source archive already exists; choose a new --out')
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in sorted(set(names)):
            if name and (ROOT/name).is_file():archive.write(ROOT/name,name)
    print('Source candidate: '+str(args.out))

if __name__=='__main__':main()
