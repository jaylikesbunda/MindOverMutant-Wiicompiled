r"""Train MoM's GCC profile over several save states, accumulating into one set of .gcda files.

Prerequisites: `python tools/pgo_mom.py gen`, then a build of the MindOverMutant target (ninja -C export/build
MindOverMutant), with the NWII_PGO_HOOK define and the dump hook from tools/stages/fix_pgo_dump_hook_mom.py.
Nothing here builds or changes the runtime; preflight only checks that build.ninja has the instrumented flags.

For each save state:
  1. launch MindOverMutant.exe dump config.toml with NWII_LOAD_STATE and a scripted stick path:
     --boot seconds of neutral stick (state load), then --seconds of alternating up/down stick movement;
  2. when the movement ends, create the trigger file (NWII_PGO_DUMP_TRIGGER); the hook writes the profile and
     creates <trigger>.done;
  3. terminate the process (TerminateProcess, not WM_CLOSE).

Why terminate instead of closing the window: a normal exit writes the profile a second time, and the counters are
cumulative since process start, so the dumped part would be counted twice. Killing after the dump writes it exactly
once. libgcov merges each process's counts into the existing .gcda, so the states accumulate across runs. Use
--clean to delete old profiles first.

Usage (from the repo root):
    python tools/pgo_train_mom.py                       # every C:\ctb\mom-states\2026111*.nwstate, 60 s each
    python tools/pgo_train_mom.py --seconds 90 --boot 12
    python tools/pgo_train_mom.py --states A.nwstate B.nwstate --clean
    python tools/pgo_train_mom.py --dry-run             # print the plan and the stick scripts, launch nothing
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'export/build/MindOverMutant.exe'
BUILD_NINJA = ROOT / 'export/build/build.ninja'
OBJ = ROOT / 'export/build/CMakeFiles/MindOverMutant.dir'
LOGS = ROOT / 'logs/pgo-train'
TRIGGER = LOGS / 'dump-trigger.txt'
STICK = LOGS / 'stick.txt'
PROGRAM_CACHE = ROOT / 'logs/pgo-program-cache'

# (stick x, stick y) in -128..127. Each movement segment is followed by a neutral pause.
DIRECTIONS = [(0, 100), (0, -100)]
SEGMENT_MS = 2000
PAUSE_MS = 400


def stick_script(boot_s: float, seconds: float, rotate: int) -> str:
    """Neutral for boot_s, then cycles through DIRECTIONS (starting at `rotate`) for `seconds`, ending neutral."""
    lines = ['0 %d 0 0' % int(boot_s * 1000)]
    budget = int(seconds * 1000)
    i = rotate
    while budget > 0:
        x, y = DIRECTIONS[i % len(DIRECTIONS)]
        hold = min(SEGMENT_MS, budget)
        lines.append('0 %d %d %d' % (hold, x, y))
        budget -= hold
        if budget > 0:
            pause = min(PAUSE_MS, budget)
            lines.append('0 %d 0 0' % pause)
            budget -= pause
        i += 1
    lines.append('0 %d 0 0' % PAUSE_MS)
    return '\n'.join(lines) + '\n'


def preflight() -> None:
    if not EXE.exists():
        sys.exit('missing ' + str(EXE) + ' (build the MindOverMutant target first)')
    if not BUILD_NINJA.exists():
        sys.exit('missing ' + str(BUILD_NINJA) + ' (configure export/build first)')
    ninja = BUILD_NINJA.read_text(errors='replace')
    if '-fprofile-generate' not in ninja:
        sys.exit('build.ninja has no -fprofile-generate: run `python tools/pgo_mom.py gen` and rebuild')
    if 'NWII_PGO_HOOK' not in ninja:
        sys.exit('build.ninja has no NWII_PGO_HOOK define: the gen block must add '
                 'target_compile_definitions(MindOverMutant PRIVATE NWII_PGO_HOOK=1), then rebuild')


def run_state(state: Path, index: int, args: argparse.Namespace) -> bool:
    """Runs one save state; returns True when the dump completed."""
    done = Path(str(TRIGGER) + '.done')
    for f in (TRIGGER, done):
        if f.exists():
            f.unlink()
    STICK.write_text(stick_script(args.boot, args.seconds, index))

    env = {k: v for k, v in os.environ.items() if not k.startswith('NWII_')}
    env.update(NWII_WALLTB='1', NWII_SINGLE_VI='1', NWII_FPS_LIMIT='120', NWII_NO_DIAG='1',
               NWII_LOAD_STATE=str(state), NWII_WPAD_LIVE=str(STICK), NWII_PROGRAM_CACHE_DIR=str(PROGRAM_CACHE),
               NWII_PGO_DUMP_TRIGGER=str(TRIGGER))
    log = (LOGS / (state.stem + '.log')).open('w')
    proc = subprocess.Popen([str(EXE), str(args.game_dir.resolve()), str(args.config.resolve())], cwd=args.profile_dir.resolve(), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    ok = False
    try:
        start = time.monotonic()
        deadline = start + args.boot + args.seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print('  the game exited early (code %s); see %s' % (proc.returncode, log.name))
                return False
            time.sleep(0.5)
        TRIGGER.write_text('dump\n')
        wait_until = time.monotonic() + args.dump_timeout
        while time.monotonic() < wait_until:
            if done.exists():
                ok = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if not ok:
            print('  no dump completion marker within %d s; see %s' % (args.dump_timeout, log.name))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        log.close()
    return ok


def main() -> None:
    global EXE, BUILD_NINJA, OBJ, LOGS, TRIGGER, STICK, PROGRAM_CACHE
    ap = argparse.ArgumentParser(description='Train MoM GCC profiles over several save states (see module docstring).')
    ap.add_argument('--states', nargs='+', help='explicit .nwstate paths (overrides --glob)')
    ap.add_argument('--glob', default=r'C:\ctb\mom-states\2026111*.nwstate', help='save-state glob')
    ap.add_argument('--seconds', type=float, default=60.0, help='stick-movement seconds per state')
    ap.add_argument('--boot', type=float, default=10.0, help='neutral seconds at launch (state load)')
    ap.add_argument('--dump-timeout', type=float, default=120.0, help='seconds to wait for the dump marker')
    ap.add_argument('--clean', action='store_true', help='delete existing .gcda files before training')
    ap.add_argument('--dry-run', action='store_true', help='print the plan and stick scripts; launch nothing')
    ap.add_argument('--exe', type=Path, default=EXE, help='instrumented game executable')
    ap.add_argument('--build-dir', type=Path, default=BUILD_NINJA.parent, help='CMake build directory for that executable')
    ap.add_argument('--game-dir', type=Path, default=ROOT / 'dump', help='prepared disc directory')
    ap.add_argument('--config', type=Path, default=ROOT / 'config.toml')
    ap.add_argument('--profile-dir', type=Path, default=ROOT, help='working directory containing the training NAND')
    ap.add_argument('--log-dir', type=Path, default=LOGS)
    args = ap.parse_args()
    EXE = args.exe.resolve()
    BUILD_NINJA = args.build_dir.resolve() / 'build.ninja'
    OBJ = args.build_dir.resolve() / 'CMakeFiles/MindOverMutant.dir'
    LOGS = args.log_dir.resolve()
    TRIGGER = LOGS / 'dump-trigger.txt'
    STICK = LOGS / 'stick.txt'
    PROGRAM_CACHE = LOGS / 'program-cache'

    states = [Path(s) for s in args.states] if args.states else [Path(p) for p in sorted(glob.glob(args.glob))]
    if not states:
        sys.exit('no save states matched')
    missing = [str(s) for s in states if not s.exists()]
    if missing:
        sys.exit('missing save state(s): ' + ', '.join(missing))

    print('states: %d | %.0f s movement + %.0f s boot each' % (len(states), args.seconds, args.boot))
    for i, s in enumerate(states):
        print('  %d. %s' % (i + 1, s))
    if args.dry_run:
        print('stick script for state 1:\n' + stick_script(args.boot, args.seconds, 0))
        return

    preflight()
    LOGS.mkdir(parents=True, exist_ok=True)
    PROGRAM_CACHE.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for p in OBJ.rglob('*.gcda'):
            p.unlink()
        print('cleaned existing .gcda files')

    for i, s in enumerate(states):
        print('[%d/%d] %s' % (i + 1, len(states), s.name))
        if not run_state(s, i, args):
            sys.exit('state %s did not produce a dump; stopping (profiles from earlier states are kept)' % s.name)
        print('  dumped')

    count = sum(1 for _ in OBJ.rglob('*.gcda'))
    size = sum(p.stat().st_size for p in OBJ.rglob('*.gcda'))
    print('.gcda files under %s: %d (%d bytes)' % (OBJ, count, size))


if __name__ == '__main__':
    main()
