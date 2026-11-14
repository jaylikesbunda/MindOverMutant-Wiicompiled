"""Replay ONLY the perf-ready stages, in order, onto this tree.

Unlike apply_stages.py this never touches the rest of the stage list (an old
stage cannot overwrite these fixes), it does not preflight-copy the whole
vendor tree, and it is safe on a dirty worktree: every stage is idempotent,
so a second run is a byte-identical no-op.

    python tools/apply_perf_stages.py [--restore]
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ORDER = [
    # Runtime lane: poll credit gate, FIFO ownership handoff, runtime trim
    # (trim last of the runtime trio - it edits export/main_output.cpp).
    'fix_poll_due_mom.py',
    'fix_fifo_handoff_mom.py',
    'fix_guest_runtime_trim_mom.py',
    # Shader lane: data uniforms -> cache io -> preload (each anchors on the
    # previous one's include), then the GPU vertex path.
    'fix_shader_data_uniforms_mom.py',
    'fix_shader_cache_io_mom.py',
    'fix_shader_preload_mom.py',
    'fix_gpu_vertex_mom.py',
    # Per-command EFB-copy snapshots + hysteresis early drain + exact CV wake,
    # reanchored to MoM's GXCommand/fifo handoff.  Early PE stays off.
    'fix_gx_overlap_mom.py',
    # Per-command EFB-copy snapshot: derives clear/depth/mask/geometry state
    # from parse BP registers (lead-authored; early PE stays off).
    'fix_copy_snapshot_mom.py',
    # Byte-budgeted texture LRU + open-batch name protection.
    'fix_texture_budget_mom.py',
    'fix_texture_budget_safety_mom.py',
    # Compact post-decode upload (136-byte packed rows, opt-in).  FlushBatch
    # only; the float path stays the default and the fallback.
    'fix_packed_upload_mom.py',
    # Persistent coherently-mapped segmented upload rings (opt-in).
    'fix_persistent_upload_mom.py',
    # Guest frame gate (validates reference/mom-frame-gate.json + the DOL).
    'fix_guest_frame_gate_mom.py',
    # Preserve the current texture unit while generating complete mip chains;
    # expose opt-in renderer subscopes through the existing frame recorder.
    'fix_mipmap_state_trace_mom.py',
    'fix_used_texture_binding_mom.py',
    'fix_live_prompts_mom.py',
]

# After staging, these vendored runtime files must be mirrored into the export
# build tree (regeneration is not run; nothing is deleted).
SYNC = [
    'src/hle/ios.cpp', 'src/hle/gx.cpp',
    'src/hle/gx/renderer_gl.cpp', 'src/hle/gx/tev_shader_gen.cpp',
    'src/hle/gx/fifo_parser.cpp', 'src/hle/texture_cache_gl.cpp',
    'src/hw/hw_ipc.cpp', 'src/hw/hw_di.cpp', 'src/core/main.cpp',
    'include/runtime/cpu_context.h', 'include/runtime/native_fifo_lock.h',
    'include/runtime/native_verbose_log.h', 'include/runtime/native_frame_limit.h',
    'include/runtime/native_shader_data.h', 'include/runtime/native_shader_cache_io.h',
    'include/runtime/native_shader_preload.h', 'include/runtime/gx_command.h',
    'include/runtime/texture_cache_gl.h',
    'include/runtime/native_gpu_vertex_mom.h',
    'include/runtime/native_packed_vertices_mom.h',
    'include/runtime/native_persistent_upload_mom.h',
    'include/runtime/native_copy_snapshot_mom.h',
    'include/runtime/native_used_texture_mask_mom.h',
    'include/runtime/native_prompt_textures_mom.h',
]


def replay(restore):
    # Reverse our anchored edits before replaying dependent stages.
    steps = [(name, True) for name in reversed(ORDER)]
    if not restore:
        steps += [(name, False) for name in ORDER]
    for name, undo in steps:
        cmd = [sys.executable, str(ROOT / 'tools/stages' / name)]
        if undo:
            cmd.append('--restore')
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        out = (r.stdout or r.stderr or '').strip()
        print('%-42s %s' % (name, out.splitlines()[-1][:120] if out else ''))
        if r.returncode != 0:
            sys.exit(r.returncode)
    if True:  # sync applies equally after a restore (export mirrors vendor)
        vendor = ROOT / 'vendor/NWiiRecomp/nWiiRuntime'
        export = ROOT / 'export/nWiiRuntime'
        synced = []
        for rel in SYNC:
            src = vendor / rel
            if not src.is_file():
                raise SystemExit('missing staged runtime file: ' + str(src))
            dst = export / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                shutil.copy2(src, dst)
                synced.append(rel)
            if dst.read_bytes() != src.read_bytes():
                raise SystemExit('runtime sync mismatch: ' + rel)
        print('synced %d runtime file(s) into export/nWiiRuntime' % len(synced))


def preserve_unchanged_times(before):
    for path, (digest, atime, mtime) in before.items():
        if path.is_file() and hashlib.sha256(path.read_bytes()).digest() == digest:
            os.utime(path, ns=(atime, mtime))


def main():
    # Intermediate reverse/reapply writes must not force a full guest rebuild
    # when the final bytes are unchanged. Changed inputs retain their new times.
    paths = [ROOT / tree / rel for tree in (
        'vendor/NWiiRecomp/nWiiRuntime', 'export/nWiiRuntime') for rel in SYNC]
    paths += list((ROOT / 'export').glob('output_*.cpp'))
    paths += [ROOT / 'export/main_output.cpp']
    before = {}
    for path in paths:
        if path.is_file():
            stat = path.stat()
            before[path] = (hashlib.sha256(path.read_bytes()).digest(),
                            stat.st_atime_ns, stat.st_mtime_ns)
    try:
        replay('--restore' in sys.argv)
    finally:
        preserve_unchanged_times(before)


if __name__ == '__main__':
    main()
