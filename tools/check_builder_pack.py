"""Verify an assembled pack contains its source identity and no game payload."""
from pathlib import Path
import argparse, hashlib, json
from builder_revision import ROOT,validate
from clean_room_build import PIN

def main():
    ap=argparse.ArgumentParser();ap.add_argument('pack',type=Path,nargs='?',default=ROOT/'launcher/src-tauri/builder-pack');args=ap.parse_args()
    pack=args.pack.resolve();source=pack/'native-builder';validate(source)
    from release_pgo import validate_bundle
    validate_bundle(source/'reference/pgo/mom-pal.json')
    if json.loads((source/'builder-revision.json').read_text())!=json.loads((ROOT/'builder-revision.json').read_text()):
        raise SystemExit('pack source identity differs from this release')
    assert (source/'vendor/NWiiRecomp/PINNED_COMMIT').read_text().strip()==PIN
    for relative in ('toolchain/python/python.exe','toolchain/mingw64/bin/g++.exe','toolchain/mingw64/bin/cmake.exe','toolchain/mingw64/bin/ninja.exe',
                     'deps/sdl2-src/CMakeLists.txt','deps/tomlplusplus-src/CMakeLists.txt','deps/glad-src/CMakeLists.txt'):
        assert (pack/relative).is_file(),relative
    sig=source/'vendor/NWiiRecomp/third_party/dolphin-sigdb/totaldb.dsy'
    assert hashlib.sha256(sig.read_bytes()).hexdigest()=='09fb4a167946dbdfee5890c0f027e40b3eacbb6cc5ee9184adeb9d1a81c2c0aa'
    for name in ('android-project','android-project-ant','Xcode','Xcode-iOS','VisualC','VisualC-GDK','VisualC-WinRT'):
        if (pack/'deps/sdl2-src'/name).exists():raise SystemExit('unpruned SDL2 tree: '+name)
    forbidden={'.dol','.rel','.rcf','.iso','.wbfs','.rvz','.wia','.gcz','.ciso','.nwstate','.gcda','.gcno','.obj','.o','.a','.pdb'}
    for path in pack.rglob('*'):
        if not path.is_file():continue
        rel=path.relative_to(pack)
        if '.git' in rel.parts:raise SystemExit('Git metadata in pack: '+str(rel))
        # Compilers/Python and their standard libraries are intentionally binary;
        # everything under native-builder/deps must remain source-only.
        if rel.parts[0]!='toolchain' and (path.suffix.lower() in forbidden|{'.exe','.dll'} or path.name.startswith('output_')):
            raise SystemExit('game/build payload in pack: '+str(rel))
    print('Builder pack passed: pinned tools, source identity, offline dependencies, no game payload')

if __name__=='__main__':main()
