"""Differential test for tools/stages/promote_regs_mom.py.

Builds tools/promote_diff_harness.cpp: for every function in
tools/hot_functions_mom.txt it puts the pristine emitter output and the
register-promoted version of that function side by side in one translation unit,
runs both from the same seeded CPUContext and compares everything guest-visible
(registers, CR, ctr, xer, pc, lr, inst_count, guest RAM).

The harness stubs the runtime and the guest callees; the stubs deliberately
scribble on the register file, and every fourth poll pretends to have run a
guest callback so the generated code returns to the dispatcher and is re-entered
at the poll pc.  That covers the barrier spill/reload protocol, the interior
entry reloads and the return paths, which is where a promotion bug lives.

The tree has to be in the un-promoted state (the stage is off by default), so
that the "original" text is the real emitter output.

Usage:
    python tools/promote_diff_harness.py            # build and run, 200 trials
    python tools/promote_diff_harness.py -t 50      # fewer trials
    python tools/promote_diff_harness.py --bench    # time both versions instead
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent
STAGE = ROOT / 'tools' / 'stages' / 'promote_regs_mom.py'
HARNESS = ROOT / 'tools' / 'promote_diff_harness.cpp'
BUILD = ROOT / 'logs' / '_promote_diff'

KEYWORDS = {'if', 'switch', 'while', 'for', 'return', 'sizeof', 'else', 'do',
            'catch', 'throw', 'decltype', 'static_cast', 'reinterpret_cast'}
CALL = re.compile(r'(?<![:.>\w])([A-Za-z_]\w*)\s*\(\s*ctx\b')
RUNTIME = {'process_pending_callbacks', 'interpret_step', 'micro_interpret',
           'trace_call', 'note_null_call', 'handle_syscall', 'require_fpu'}


def load_stage():
    spec = importlib.util.spec_from_file_location('promote_regs_mom', STAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', '--trials', type=int, default=200)
    parser.add_argument('-b', '--budget', type=int, default=3000,
                        help='polls per trial before the run is cut short')
    parser.add_argument('--bench', action='store_true',
                        help='time the two versions instead of comparing state')
    parser.add_argument('--bench-trials', type=int, default=20)
    parser.add_argument('-j', '--jobs', default='8')
    args = parser.parse_args()

    stage = load_stage()
    hot = [l.strip() for l in (ROOT / 'tools' / 'hot_functions_mom.txt').read_text().split('\n')
           if l.strip() and not l.strip().startswith('#')]
    want = set(hot)

    targets = []
    for name in sorted(os.listdir(ROOT / 'export')):
        if not (name.startswith('output_') and name.endswith('.cpp')):
            continue
        text = (ROOT / 'export' / name).read_text(errors='surrogateescape')
        for m in stage.FUNC_HEAD.finditer(text):
            func = m.group(1)
            if func not in want:
                continue
            a, b = stage.find_span(text, m.start())
            original = text[a:b]
            if stage.MARKER in original:
                raise SystemExit('%s in %s is already promoted; restore the '
                                 'un-promoted chunks first' % (func, name))
            promoted, stats = stage.promote(original)
            if promoted is None:
                print('skip %-46s %s' % (func, stats))
                continue
            targets.append((func, original, promoted))
    print('%d of %d hot functions promoted' % (len(targets), len(hot)))

    callees = set()
    for _, original, _ in targets:
        for m in CALL.finditer(original):
            if m.group(1) not in KEYWORDS:
                callees.add(m.group(1))
    callees -= RUNTIME
    print('%d callee stubs' % len(callees))

    def redirect(text):
        def rep(m):
            sym = m.group(1)
            if sym in callees:
                return 'stub_' + sym + ' (ctx'
            return m.group(0)
        return CALL.sub(rep, text)

    BUILD.mkdir(parents=True, exist_ok=True)
    with (BUILD / 'targets.inc').open('w', newline='\n') as fh:
        for func, original, promoted in targets:
            renamed = re.sub(r'\bvoid ' + func + r'\(', 'void orig_' + func + '(', original, count=1)
            fh.write(redirect(renamed) + '\n')
            fh.write(redirect(promoted) + '\n')
    with (BUILD / 'stubs.inc').open('w', newline='\n') as fh:
        fh.write('// Guest callees: deterministic, and they scribble on the register\n'
                 '// file so the spill/reload protocol around a barrier is exercised.\n')
        for i, sym in enumerate(sorted(callees)):
            # Returning through lr is what a real guest return does.
            fh.write('void stub_%s(CPUContext& c) { nwii::runtime::scribble(c, 0x%08Xu); '
                     'c.pc = c.lr; }\n' % (sym, 0x1000193 * (i + 1)))
    with (BUILD / 'table.inc').open('w', newline='\n') as fh:
        for func, original, promoted in targets:
            entry = stage.parse(original)[1]
            fh.write('  { "%s", orig_%s, %s, 0x%08Xu },\n' % (func, func, func, entry))

    source = (ROOT / 'tools' / 'promote_diff_harness.cpp').read_text()
    # The harness includes the generated files from its own directory.
    (BUILD / 'harness.cpp').write_text(source, newline='\n')

    exe = BUILD / 'harness.exe'
    cxx = os.environ.get('CXX')
    if not cxx:
        probe = subprocess.run(['g++', '--version'], capture_output=True, text=True)
        if probe.returncode == 0:
            cxx = 'g++'
        else:
            # The compiler CMake configured for export/build.
            cxx = ('C:/Users/deki/AppData/Local/Microsoft/WinGet/Packages/'
                   'BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/'
                   'mingw64/bin/c++.exe')
    compile_cmd = [
        cxx, '-std=gnu++20', '-Os', '-fwrapv', '-fno-strict-aliasing',
        '-ffp-contract=off',
        '-I', str(ROOT / 'export' / 'nWiiRuntime' / 'include'),
        '-I', str(ROOT / 'export' / 'nWiiRuntime' / 'src'),
        '-o', str(exe), str(BUILD / 'harness.cpp'),
    ]
    print('compiling...')
    result = subprocess.run(compile_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print((result.stderr or '').strip()[:4000])
        return result.returncode

    if args.bench:
        cmd = [str(exe), '2', str(args.budget), '-', 'time', str(args.bench_trials)]
    else:
        cmd = [str(exe), str(args.trials), str(args.budget)]
    print('running:', ' '.join(cmd))
    return subprocess.run(cmd).returncode


if __name__ == '__main__':
    raise SystemExit(main())
