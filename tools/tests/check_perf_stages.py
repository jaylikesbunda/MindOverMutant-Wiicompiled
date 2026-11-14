"""Focused checks for the perf-ready runtime stages (MoM).

Covers, without a full build:

* fix_poll_due_mom.py / fix_fifo_handoff_mom.py / fix_guest_runtime_trim_mom.py
  are idempotent on the live tree (second run is byte-identical) and
  restore->reapply is byte-identical on a scratch copy.
* The generated poll-site rewrite: every ``process_pending_callbacks`` call on
  a backward branch is wrapped in exactly one ``native_poll_due(ctx.pc)`` guard,
  the spin diagnostic is folded into the taken branch, and no site writes a
  guest field from inside the guard.
* ios.cpp shape: the credit reload + attention consume run before processing,
  the publish call is preserved, and IPC/DI delays still force every-site
  polling (native_poll_reload's contract, exercised by poll_due.cpp).
* gx.cpp shape: the publish counts bytes before the (possibly swapping)
  append, the pending counter is updated under the lock, and the host drain
  swaps under the lock but appends outside it.
* poll_due.exe is run across the NWII_POLL_CREDIT / NWII_NO_POLL_THROTTLE
  matrix (default 64, 1, 64, 0, -5, 99999, abc, throttle); fifo_handoff.exe
  runs once (it has no env switches).

Usage: python tools/tests/check_perf_stages.py [--skip-fixtures]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'vendor/NWiiRecomp/nWiiRuntime'
STAGES = ['fix_poll_due_mom.py', 'fix_fifo_handoff_mom.py',
          'fix_guest_runtime_trim_mom.py', 'fix_shader_data_uniforms_mom.py',
          'fix_shader_cache_io_mom.py', 'fix_shader_preload_mom.py',
          'fix_gpu_vertex_mom.py', 'fix_gx_overlap_mom.py',
          'fix_copy_snapshot_mom.py',
          'fix_texture_budget_mom.py', 'fix_texture_budget_safety_mom.py',
          'fix_packed_upload_mom.py', 'fix_persistent_upload_mom.py',
          'fix_guest_frame_gate_mom.py', 'fix_mipmap_state_trace_mom.py',
          'fix_used_texture_binding_mom.py']
FAILED = []


def check(cond, msg):
    if not cond:
        FAILED.append(msg)
        print('FAIL', msg)
    return cond


def read(path):
    return Path(path).read_text(encoding='utf-8', errors='surrogateescape')


def run_stage(name, *args):
    r = subprocess.run([sys.executable, str(ROOT / 'tools/stages' / name), *args],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        check(False, f'{name} exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}')
    return r


def snap(paths):
    return {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest()
            for p in paths if Path(p).is_file()}


def tracked_files():
    files = [RUNTIME / 'src/hle/ios.cpp', RUNTIME / 'src/hle/gx.cpp',
             RUNTIME / 'include/runtime/cpu_context.h',
             RUNTIME / 'include/runtime/native_fifo_lock.h',
             ROOT / 'export/main_output.cpp']
    files += sorted((ROOT / 'export').glob('output_*.cpp'))
    files += [RUNTIME / 'src/hle/gx/renderer_gl.cpp',
              ROOT / 'export/nWiiRuntime/src/hle/gx/renderer_gl.cpp']
    for tree in (RUNTIME, ROOT / 'export/nWiiRuntime'):
        files += [tree / 'src/hle/texture_cache_gl.cpp',
                  tree / 'include/runtime/native_used_texture_mask_mom.h']
    return files


def check_idempotency():
    # the live tree is fully staged; a later stage's edits can sit inside an
    # earlier stage's pair span, so per-stage re-apply can't see its own `new`.
    # The runner's reverse-undo + forward replay is the semantic that has to be
    # byte-stable - exercise it directly.
    before = snap(tracked_files())
    r = subprocess.run([sys.executable, str(ROOT / 'tools/apply_perf_stages.py')],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        check(False, 'apply_perf_stages exited %d: %s' % (r.returncode,
              (r.stderr or r.stdout)[-300:]))
    after = snap(tracked_files())
    check(before == after, 'a second pass over the live tree changed bytes')

    scratch = Path(tempfile.mkdtemp(prefix='mom_perf_stage_'))
    try:
        (scratch / 'vendor/NWiiRecomp').mkdir(parents=True)
        shutil.copytree(RUNTIME, scratch / 'vendor/NWiiRecomp/nWiiRuntime')
        (scratch / 'export').mkdir()
        for src in ('output_0.cpp', 'output_35.cpp', 'output_63.cpp',
                    'output_gap_0.cpp', 'main_output.cpp'):
            if (ROOT / 'export' / src).is_file():
                shutil.copy(ROOT / 'export' / src, scratch / 'export' / src)
        (scratch / 'overlays').mkdir()
        for o in (ROOT / 'overlays').glob('*.h'):
            shutil.copy(o, scratch / 'overlays' / o.name)
        if (ROOT / 'reference/mom-frame-gate.json').is_file():
            (scratch / 'reference').mkdir(exist_ok=True)
            shutil.copy(ROOT / 'reference/mom-frame-gate.json',
                        scratch / 'reference/mom-frame-gate.json')
        dol = ROOT / 'dump/sys/main.dol'
        if dol.is_file():
            (scratch / 'dump/sys').mkdir(parents=True, exist_ok=True)
            shutil.copy(dol, scratch / 'dump/sys/main.dol')
        # stages interleave edits (the overlap/tail lines can sit inside an
        # earlier stage's pair span), so a forward pass on a staged tree cannot
        # see its own `new`.  Undo in reverse first - the runner's order -
        # then apply forward to reach the 'applied' state.
        for name in reversed(STAGES):
            run_stage(name, '--root', str(scratch), '--restore')
        for name in STAGES:
            run_stage(name, '--root', str(scratch))
        applied = snap(p for p in scratch.rglob('*') if p.is_file())
        for name in reversed(STAGES):
            run_stage(name, '--root', str(scratch), '--restore')
        for name in STAGES:
            run_stage(name, '--root', str(scratch))
        reapplied = snap(p for p in scratch.rglob('*') if p.is_file())
        check(applied == reapplied, 'restore->reapply differed on the scratch tree')
        t = read(scratch / 'export/output_0.cpp')
        check(t.count('native_poll_due(ctx.pc)') > 0 and
              '\n    if (process_pending_callbacks(ctx)) return;' not in t,
              'scratch rewrite incomplete')
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_generated():
    sites = folded = files = 0
    bare = re.compile(r'\n    if \(process_pending_callbacks\(ctx\)\) return;')
    fold_re = re.compile(
        r'if \(nwii::runtime::native_poll_due\(ctx\.pc\)\) '
        r'\{ if \(process_pending_callbacks\(ctx\)\) return; '
        r'if \(\(ctx\.inst_count % 10000000\) == 0\) \{')
    for path in sorted((ROOT / 'export').glob('output_*.cpp')):
        t = read(path)
        check(not bare.search(t), f'{path.name}: ungated poll site remains')
        check('native_poll_due(ctx.pc) && process_pending_callbacks' not in t
              or 'if (nwii::runtime::native_poll_due(ctx.pc)) {' in t,
              f'{path.name}: diagnostic not folded')
        check('native_poll_due(native_poll_due' not in t and
              'native_poll_due(ctx.pc)) { if (nwii::runtime::native_poll_due' not in t,
              f'{path.name}: duplicate guard')
        sites += t.count('native_poll_due(ctx.pc)')
        folded += len(fold_re.findall(t))
        files += 'native_poll_due(ctx.pc)' in t
    print(f'generated: {sites} guarded site(s) in {files} chunk(s), '
          f'{folded} folded diagnostic(s)')
    check(sites > 9000, f'only {sites} guarded sites')
    check(sites == folded, f'{sites - folded} sites did not fold their diagnostic')
    t63 = read(ROOT / 'export/output_63.cpp')
    check('ctx.pc = 0x80492408;\n    if (nwii::runtime::native_poll_due(ctx.pc))' in t63,
          'idle-loop site lost its (pc-exempt) guard')
    # The guard never writes guest state: every rewritten line only wraps the
    # original two statements in `if (native_poll_due(ctx.pc)) { ... }`.
    bad = []
    for path in sorted((ROOT / 'export').glob('output_*.cpp')):
        for line in read(path).splitlines():
            if 'native_poll_due(ctx.pc)' in line and re.search(
                    r'native_poll_due\(ctx\.pc\)[^;]*ctx\.(?!pc)', line):
                bad.append((path.name, line.strip()[:90]))
    check(not bad, f'guard lines write guest state: {bad[:3]}')


def check_ios():
    t = read(RUNTIME / 'src/hle/ios.cpp')
    check(t.count('int32_t g_native_poll_credit = 1;') == 1,
          'g_native_poll_credit definition missing or duplicated')
    block = ('g_native_poll_credit = native_poll_reload(g_ipc_interrupt_delay != 0,\n'
             '                                              hw::g_di_interrupt_delay != 0);\n'
             '    native_idle::attention.store(0, std::memory_order_relaxed);')
    check(t.count(block) == 1, 'credit reload/attention-consume block missing or duplicated')
    i_block = t.index(block)
    i_probe = t.index('native_fp_context_probe(ctx,"poll");')
    i_fn = t.index('bool process_pending_callbacks(CPUContext &ctx) {')
    check(i_fn < i_block < i_probe,
          'reload/consume does not run first inside process_pending_callbacks')
    check('if (g_gx_publish_always || !g_cpu_fifo.empty()) native_gx_publish();' in t,
          'publish call (still inside every real poll) missing')
    check('if (native_diag_report_on && poll_clock_due && ((++diag_phase) & 255u) == 0)' in t,
          'diagnostics clock throttle missing')
    h = read(RUNTIME / 'include/runtime/cpu_context.h')
    check('native_poll_due(uint32_t pc)' in h and
          'pc == 0x80492408u || pc == 0u || pc == 0xFFFFFFFCu' in h,
          'idle/sentinel pc exemption missing from native_poll_due')
    check('if (ipc_delay || di_delay) return 1;' in h,
          'IPC/DI every-site reload missing')
    check('v < 1 ? 1 : (v > 1024 ? 1024 : v)' in h and 'strtol' in h,
          'NWII_POLL_CREDIT clamp missing')
    check('std::getenv("NWII_NO_POLL_THROTTLE")) return int32_t(1);' in h,
          'NWII_NO_POLL_THROTTLE every-site switch missing')


def check_gx():
    t = read(RUNTIME / 'src/hle/gx.cpp')
    check('const size_t published = g_cpu_fifo.size();\n'
          '        nwii::runtime::NativeAppendFifo(g_hw_fifo, g_cpu_fifo);\n'
          '        g_gx_pending_bytes.fetch_add(published, std::memory_order_relaxed);' in t,
          'publish no longer counts before the swapping append')
    drain = ('native_drain_buf.swap(g_hw_fifo);\n'
             '        g_gx_pending_bytes.store(0, std::memory_order_relaxed);\n'
             '    }\n')
    check(drain in t, 'drain swap/pending-reset under the lock missing')
    check('if (!native_drain_buf.empty())\n'
          '        nwii::runtime::NativeAppendFifo(s_parse_buf, native_drain_buf);' in t,
          'drain append outside the lock missing')
    lock = read(RUNTIME / 'include/runtime/native_fifo_lock.h')
    check('inline void NativeAppendFifo' in lock and
          'if (destination.empty()) destination.swap(source);' in lock,
          'NativeAppendFifo helper missing from the lock header')
    renderer = read(RUNTIME / 'src/hle/gx/renderer_gl.cpp')
    check(renderer.count('static const bool s_gx_no_copy_snapshot =') == 1,
          'copy snapshot switch missing/duplicated after stage replay')
    for consumer in ('uint32_t dest = copy_dest;',
                     'uint32_t w = copy_w, h = copy_h;',
                     'NativeCopyEfbTexture(tex,copy_x,copy_y,w,h,',
                     'NativeLatchXfb(copy_dest,copy_x,copy_y,copy_w,copy_h,',
                     'glScissor(copy_x*NativeResolutionScale(),'):
        check(consumer in renderer, 'copy descriptor consumer reverted: ' + consumer)


def check_copy_comment_migration():
    path = ROOT / 'tools/stages/fix_gx_overlap_mom.py'
    spec = importlib.util.spec_from_file_location('copy_migration_fixture', path)
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    anchor = '// original copy branch\n'
    prefix = '// ' + stage.MARK + ': copy locals\n'
    expected = prefix + stage.COPY_CLEAR_COMMENT + anchor
    with tempfile.TemporaryDirectory(prefix='mom_copy_comment_') as scratch:
        renderer = Path(scratch) / 'renderer_gl.cpp'
        for comment in (stage.COPY_CLEAR_LEGACY, stage.COPY_CLEAR_PARTIAL,
                        stage.COPY_CLEAR_COMMENT):
            renderer.write_text(prefix + comment + anchor)
            stage.RESTORE = True
            source = stage.Source(renderer, False)
            source.sub(anchor, expected, 'copy locals')
            check(source.text == anchor and stage.MARK not in source.text,
                  'legacy copy comment left a partial inverse span')
            source.save()
            stage.RESTORE = False
            source = stage.Source(renderer, False)
            check(not source.installed(), 'restored marker incorrectly suppresses reapply')
            source.sub(anchor, expected, 'copy locals')
            check(source.text == expected, 'copy comment restore/reapply changed bytes')
        ios = Path(scratch) / 'ios.cpp'
        old = ('extern std::atomic<bool> g_gx_drain_request;\n'
               'extern std::condition_variable g_gx_drain_cv;\n')
        declaration = 'extern void native_gx_request_drain(); // ' + stage.MARK + '\n'
        credit = 'int32_t g_native_poll_credit = 1; // NATIVE_MOM_POLL_DUE_V1\n'
        ios.write_text(old + credit + declaration)
        stage.RESTORE = True
        source = stage.Source(ios, False)
        source.sub(old, old + declaration, 'drain request declaration')
        check(source.text == old + credit,
              'foreign poll-credit insert stranded the drain declaration on restore')


def check_fixtures():
    exe = ROOT / 'logs/perf-ready/poll_due_test.exe'
    envs = [(None, None, '64'), ('1', None, '1'), ('64', None, '64'),
            ('0', None, '1'), ('-5', None, '1'), ('99999', None, '1024'),
            ('abc', None, '1'), ('200', '1', '1')]
    for credit, throttle, want in envs:
        env = dict(os.environ)
        env.pop('NWII_POLL_CREDIT', None)
        env.pop('NWII_NO_POLL_THROTTLE', None)
        if credit is not None:
            env['NWII_POLL_CREDIT'] = credit
        if throttle is not None:
            env['NWII_NO_POLL_THROTTLE'] = throttle
        r = subprocess.run([str(exe)], capture_output=True, text=True, env=env)
        first = r.stdout.splitlines()[0] if r.stdout else ''
        got = first.split('reload=')[-1].split()[0] if 'reload=' in first else '?'
        check(r.returncode == 0 and got == want,
              f'poll_due credit={credit} throttle={throttle}: want {want} got {got} '
              f'(rc={r.returncode})')
    r = subprocess.run([str(ROOT / 'logs/perf-ready/fifo_handoff_test.exe')],
                       capture_output=True, text=True)
    check(r.returncode == 0,
          f'fifo_handoff fixture failed: {r.stdout[-200:]}')


def check_mip_stage_preflight():
    path = ROOT / 'tools/stages/fix_mipmap_state_trace_mom.py'
    spec = importlib.util.spec_from_file_location('mip_preflight', path)
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    with tempfile.TemporaryDirectory(prefix='mom_mip_preflight_') as directory:
        root = Path(directory)
        tex, gl = root / stage.TEX, root / stage.GL
        gl.parent.mkdir(parents=True)
        original = read(ROOT / stage.TEX)
        include = stage.OWNED_INCLUDE + '\n'
        for blanks in (0, 1, 3):
            padded = original.replace(include, include + '\n' * blanks, 1)
            tex.write_bytes(padded.encode())
            _, _, applied = stage._prepare(tex, stage.TEX_PAIRS, True, True)
            check(applied == padded, 'mip stage consumed existing include whitespace')
            _, _, restored = stage._prepare(tex, stage.TEX_PAIRS, True, False)
            tex.write_bytes(restored.encode())
            _, _, reapplied = stage._prepare(tex, stage.TEX_PAIRS, True, True)
            check(reapplied == padded, 'mip stage round-trip changed whitespace')
        # A broken second target must not leave the first target patched.
        tex.write_bytes(restored.encode())
        before = tex.read_bytes()
        gl.write_text('// intentionally missing anchors\n')
        try:
            stage.apply(root)
        except SystemExit:
            pass
        else:
            check(False, 'mip preflight accepted a missing renderer anchor')
        check(tex.read_bytes() == before, 'mip preflight wrote a partial patch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-fixtures', action='store_true')
    args = parser.parse_args()
    check_idempotency()
    check_generated()
    check_ios()
    check_gx()
    check_copy_comment_migration()
    check_mip_stage_preflight()
    if not args.skip_fixtures:
        check_fixtures()
    print('\n%d check failure(s)' % len(FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == '__main__':
    main()
