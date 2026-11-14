"""Assemble a launcher builder pack containing tools and source, never a game.

Use a new --out directory for each pack. The pinned upstream source is archived
from Git, so private working-tree files cannot enter the distribution.
"""
from pathlib import Path
import hashlib, urllib.request
import argparse, shutil, subprocess, sys, tempfile
from builder_revision import ROOT, validate
from clean_room_build import upstream, PIN

DEPS={'sdl2-src':('https://github.com/libsdl-org/SDL.git','release-2.28.5'),
      'tomlplusplus-src':('https://github.com/marzer/tomlplusplus.git','v3.4.0'),
      'glad-src':('https://github.com/Dav1dde/glad.git','v0.1.36')}
PRUNE={'sdl2-src':{'android-project','android-project-ant','Xcode','Xcode-iOS','VisualC','VisualC-GDK','VisualC-WinRT','visualtest','acinclude','mingw','docs','.github'},
       'tomlplusplus-src':{'docs','.github','examples'},'glad-src':{'.github','test','example'}}

def copy_dependency(src,dst,name):
    src=src.resolve()
    shutil.copytree(src,dst,ignore=lambda folder,names:
        set(shutil.ignore_patterns('.git','__pycache__')(folder,names)) |
        ((set(names)&PRUNE[name]) if Path(folder).resolve()==src else set()))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,default=ROOT/'launcher/src-tauri/builder-pack')
    ap.add_argument('--mingw',type=Path,required=True)
    ap.add_argument('--python',type=Path,default=Path(sys.base_prefix))
    ap.add_argument('--deps',type=Path,default=ROOT/'export/build/_deps')
    args=ap.parse_args();identity=validate();out=args.out.resolve()
    if out.exists():raise SystemExit('pack destination already exists; choose a new --out')
    nb=out/'native-builder';nb.mkdir(parents=True)
    for relative in identity['required_inputs']:
        src=ROOT/relative;dst=nb/relative;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
    shutil.copy2(ROOT/'builder-revision.json',nb/'builder-revision.json')
    for notice in ('LICENSE','THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT/notice,out/notice);shutil.copy2(ROOT/notice,nb/notice)
    # License files with extensionless names are also required redistribution inputs.
    for src in (ROOT/'reference').rglob('*'):
        if src.is_file() and 'user' not in src.relative_to(ROOT/'reference').parts and src.name in ('COPYING','LICENSE'):
            dst=nb/src.relative_to(ROOT);dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
    upstream(nb/'vendor/NWiiRecomp')
    (nb/'vendor/NWiiRecomp/PINNED_COMMIT').write_text(PIN+'\n')
    sig=ROOT/'vendor/NWiiRecomp/third_party/dolphin-sigdb/totaldb.dsy'
    data=sig.read_bytes() if sig.is_file() else urllib.request.urlopen('https://raw.githubusercontent.com/dolphin-emu/dolphin/master/Data/Sys/totaldb.dsy').read()
    if hashlib.sha256(data).hexdigest()!='09fb4a167946dbdfee5890c0f027e40b3eacbb6cc5ee9184adeb9d1a81c2c0aa':
        raise SystemExit('Dolphin signature database differs from pin')
    (nb/'vendor/NWiiRecomp/third_party/dolphin-sigdb/totaldb.dsy').write_bytes(data)
    for name,(url,tag) in DEPS.items():
        src=args.deps/name;dst=out/'deps'/name
        if src.is_dir():copy_dependency(src,dst,name)
        else:
            with tempfile.TemporaryDirectory(prefix='mom-dependency-') as folder:
                clone=Path(folder)/name
                subprocess.run(['git','clone','--depth','1','--branch',tag,url,str(clone)],check=True)
                copy_dependency(clone,dst,name)
    tc=out/'toolchain'
    shutil.copytree(args.mingw,tc/'mingw64',ignore=shutil.ignore_patterns('doc','man','info','gdb*','gfortran*','lto-dump.exe'))
    py=tc/'python';py.mkdir()
    for src in args.python.glob('*.dll'):shutil.copy2(src,py/src.name)
    for name in ('python.exe','python3.dll','LICENSE.txt'):
        src=args.python/name
        if src.is_file():shutil.copy2(src,py/name)
    for name in ('Lib','DLLs'):
        shutil.copytree(args.python/name,py/name,ignore=shutil.ignore_patterns('site-packages','test','tests','__pycache__','idlelib','tkinter','turtledemo','ensurepip','tcl*','tk*'))
    validate(nb)
    print('Source-only builder pack ready: '+str(out))

if __name__=='__main__':main()
