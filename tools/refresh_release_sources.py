"""Capture the tested NWiiRecomp source changes as a portable upstream patch.

No game executable, generated guest chunks, profiles, saves or build outputs
are read into the patch. Run after changing the runtime or recompiler.
"""
from pathlib import Path
import difflib, subprocess

ROOT=Path(__file__).resolve().parents[1]
PIN='595f176f1d24cc54ff2e8389feed12d7fb553cc2'
VENDOR=ROOT/'vendor/NWiiRecomp'

def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=VENDOR,text=True).strip()
    assert head==PIN, 'upstream checkout differs from tested pin'
    files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=VENDOR,text=True).splitlines()
    patch=[]
    for relative in sorted(set(files)):
        if not (relative.startswith(('nWiiRuntime/','nWiiRecomp/','nWiiAnalyzer/')) or relative=='CMakeLists.txt'):continue
        path=VENDOR/relative
        if path.suffix not in ('.h','.hpp','.cpp','.c','.inc','.txt','.cmake'):continue
        original=subprocess.run(['git','show',f'{PIN}:{relative}'],cwd=VENDOR,capture_output=True)
        before=original.stdout.decode('utf-8').replace('\r\n','\n') if original.returncode==0 else ''
        after=path.read_text(encoding='utf-8') if path.is_file() else ''
        if relative=='CMakeLists.txt':
            after=after.replace('add_subdirectory(nWiiStudio)', 'option(NWII_BUILD_STUDIO "Build the optional editor" OFF)\nif(NWII_BUILD_STUDIO)\n  add_subdirectory(nWiiStudio)\nendif()')
        after=after.replace('"I:/GhostESP2/Crash - Mind over Mutant - RECOMP/overlays/native_gl_raster_shadow.h"','"runtime/native_gl_raster_shadow.h"')
        if before==after:continue
        patch.append(f'diff --git a/{relative} b/{relative}\n')
        if not before:patch.append('new file mode 100644\n')
        if not after:patch.append('deleted file mode 100644\n')
        for line in difflib.unified_diff(before.splitlines(True),after.splitlines(True),
            fromfile='a/'+relative if before else '/dev/null',tofile='b/'+relative if after else '/dev/null'):
            patch.append(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n')
    relative='nWiiRuntime/include/runtime/native_gl_raster_shadow.h'
    if not (VENDOR/relative).exists():
        data=(ROOT/'overlays/native_gl_raster_shadow.h').read_text()
        patch.extend([f'diff --git a/{relative} b/{relative}\n','new file mode 100644\n'])
        patch.extend(difflib.unified_diff([],data.splitlines(True),fromfile='/dev/null',tofile='b/'+relative))
    target=ROOT/'patches/mom-release-runtime.patch'
    target.write_text(''.join(patch),encoding='utf-8',newline='\n')
    print(f'{target.name}: {target.stat().st_size:,} bytes; source changes against {PIN}')

if __name__=='__main__':main()
