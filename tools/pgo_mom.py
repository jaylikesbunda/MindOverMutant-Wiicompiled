r"""GCC profile-guided optimisation of MoM's generated guest code (the 70 chunks, the gap chunks and main_output.cpp).

Marker: NATIVE_MOM_PGO_V1. The method follows the Crash of the Titans port's fix_hot_pgo.py: profile only the generated
code, train on a real gameplay scene, then compile with the profile. MoM builds in place in export/, so the source and
object paths are the same in every build and GCC's path-keyed profiles match without the #line pin CotT needed.

    python tools/pgo_mom.py gen     # instrument: -fprofile-generate on generated code, link with it
    python tools/pgo_mom.py train   # load a save state, alternate up/down, quit cleanly so .gcda files are written
    python tools/pgo_mom.py use     # compile with the profile (-fprofile-use ...)
    python tools/pgo_mom.py off     # back to plain flags
    python tools/pgo_mom.py status

gen/use/off edit only the NATIVE_MOM_PGO_V1 block appended to export/CMakeLists.txt (re-run after regenerating export/,
like the compile-flags stage). The profile lives next to the objects (export/build/CMakeFiles/MindOverMutant.dir/*.gcda);
`use` refuses to run without it. Build with: ninja -C export/build MindOverMutant
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CML = ROOT / 'export/CMakeLists.txt'
OBJ = ROOT / 'export/build/CMakeFiles/MindOverMutant.dir'
MARK = '# NATIVE_MOM_PGO_V1'
GEN = '-fprofile-generate;-fprofile-update=single'
USE = '-fprofile-use;-fprofile-correction;-fprofile-partial-training;-Wno-missing-profile;-Wno-coverage-mismatch'


def units():
    gen = ROOT / 'export'
    return sorted(p.name for p in gen.glob('output_*.cpp')) + ['main_output.cpp']


def block(mode):
    if mode == 'off':
        return ''
    flags = GEN if mode == 'gen' else USE
    lines = [MARK + ' (' + mode + '; tools/pgo_mom.py)', 'set_property(SOURCE']
    lines += ['    "%s"' % u for u in units()]
    lines += ['    APPEND PROPERTY COMPILE_OPTIONS "%s")' % flags]
    if mode == 'gen':
        lines += ['target_link_options(MindOverMutant PRIVATE -fprofile-generate)']
        lines += ['target_compile_definitions(MindOverMutant PRIVATE NWII_PGO_HOOK=1)']  # dump trigger (fix_pgo_dump_hook_mom.py)
    lines += [MARK + ' END']
    return '\n'.join(lines) + '\n'


def set_mode(mode):
    text = CML.read_text()
    if MARK in text:
        a = text.index(MARK)
        b = text.index(MARK + ' END', a) + len(MARK + ' END') + 1
        text = text[:a].rstrip('\n') + '\n' + text[b:].lstrip('\n')
    if mode != 'off':
        if mode == 'use' and not list(OBJ.glob('*.gcda')):
            raise SystemExit('no .gcda profile in ' + str(OBJ) + '; run gen, build, train first')
        text = text.rstrip('\n') + '\n\n' + block(mode)
    CML.write_text(text)
    print('PGO mode:', mode)


def status():
    text = CML.read_text()
    mode = 'off'
    if MARK in text:
        mode = text[text.index(MARK):].split('(', 1)[1].split(';', 1)[0]
    gcda = list(OBJ.glob('*.gcda'))
    print('mode:', mode, '| profile files:', len(gcda), '| bytes:', sum(p.stat().st_size for p in gcda))


def train(state, seconds):
    """Loads `state`, alternates the stick up/down, and exits so libgcov writes."""
    exe = ROOT / 'export/build/MindOverMutant.exe'
    stick = ROOT / 'logs/pgo-stick.txt'
    strokes = (seconds + 31) // 2
    stick.write_text(''.join('0 2000 0 %d\n' % (100 if i % 2 == 0 else -100) for i in range(strokes)))
    env = {k: v for k, v in os.environ.items() if not k.startswith('NWII_')}
    env.update(NWII_WALLTB='1', NWII_SINGLE_VI='1', NWII_FPS_LIMIT='120', NWII_NO_DIAG='1', NWII_LOAD_STATE=state,
               NWII_WPAD_LIVE=str(stick), NWII_PROGRAM_CACHE_DIR=str(ROOT / 'logs/pgo-program-cache'))
    for p in OBJ.glob('*.gcda'):
        p.unlink()
    p = subprocess.Popen([str(exe), 'dump', 'config.toml'], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(seconds)
    # WM_CLOSE to the game's window: SDL_QUIT ends the main loop and the process exits normally.
    ps = ('$p=Get-Process -Id %d; $h=$p.MainWindowHandle; Add-Type -Name W -Namespace N -MemberDefinition '
          '\'[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l);\'; '
          '[N.W]::PostMessage($h,0x10,[IntPtr]::Zero,[IntPtr]::Zero)' % p.pid)
    subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True)
    try:
        p.wait(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        raise SystemExit('the game did not exit after WM_CLOSE; no profile written')
    status()


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd in ('gen', 'use', 'off'):
        set_mode(cmd)
    elif cmd == 'train':
        state = sys.argv[2] if len(sys.argv) > 2 else r'C:\ctb\mom-states\20261112-133439-089.nwstate'
        train(state, int(sys.argv[3]) if len(sys.argv) > 3 else 60)
    else:
        status()
