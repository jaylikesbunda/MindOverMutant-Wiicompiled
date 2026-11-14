r"""Per-call-site target cache for the inline indirect calls installed by fix_guest_calls_mom.py.

Marker: NATIVE_MOM_INDIRECT_SITE_V1. Port of the Crash of the Titans NATIVE_INDIRECT_SITE_CACHE_V1
(tools/fix_indirect_call_cache.py, runtime-overlays/native_guest_fast.h native_call_indirect_fast).

After fix_guest_calls_mom.py every bctrl site calls native_call_indirect(ctx), which resolves the target through
find_dispatch_target_hot: a 4,096 x 16-byte exact-tag memo (64 KiB, larger than L1D) in front of a binary search. The
guest profile after that stage put find_dispatch_target_hot at 3.8% of the guest thread. A virtual call site almost
always calls the same target, so each site now keeps its own last (pc, function, hle) triple:

    if (ctx.ctr) { ctx.pc = ctx.ctr; { static nwii::runtime::NativeIndirectSite s{}; nwii::runtime::native_call_indirect_site(ctx, s); } ... }

native_call_indirect_site does exactly what native_call_indirect does (callback poll first, the C0/D0 -> 80/90 alias map,
++inst_count, the range-guarded WPAD hook, HLE return), except that when the mapped target equals the site's cached pc it
calls the cached function instead of looking it up; a miss resolves through find_dispatch_target_hot and refills the
site. A cached entry can only hold what the resolver returned for that exact pc, and the dispatch table is immutable, so a
hit returns the same function the lookup would. Interpreter targets (no function) are never cached.

The site object is constant-initialised (zero), so the per-site static needs no guard. Run after fix_guest_calls_mom.py.
Edits cpu_context.h (vendor + export), main_output.cpp and the generated chunks in export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_INDIRECT_SITE_V1'
ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / 'export'
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': EXPORT / 'nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_indirect_site_cache_mom'
HDR = 'include/runtime/cpu_context.h'

HDR_OLD = 'void native_call_indirect(CPUContext &ctx); // NATIVE_MOM_GUEST_CALLS_V1\n'
HDR_NEW = (HDR_OLD +
           '// ' + MARK + ': one cached target per indirect call site (see tools/stages/fix_indirect_site_cache_mom.py).\n'
           'struct NativeIndirectSite { uint32_t pc; void (*func)(CPUContext&); bool hle; };\n'
           'void native_call_indirect_site(CPUContext &ctx, NativeIndirectSite &site);\n')
SITE_OLD = 'nwii::runtime::native_call_indirect(ctx); if (ctx.pc != '
SITE_NEW = '{ static nwii::runtime::NativeIndirectSite native_site{}; nwii::runtime::native_call_indirect_site(ctx, native_site); } /* ' + MARK + ' */ if (ctx.pc != '
MAIN_ANCHOR = '// --- Entry Point Wrapper ---\n'
MAIN_BLOCK = r'''// NATIVE_MOM_INDIRECT_SITE_V1: native_call_indirect with a per-site memo of the last resolved target.
namespace nwii::runtime {
void native_call_indirect_site(CPUContext& ctx, NativeIndirectSite& site) {
    if (!ctx.is_running || ctx.pc == 0) return;
    // Same poll gate as every generated poll site (native_poll_due: credit countdown or raised attention), so an
    // indirect call no longer runs the full callback poll every time.
    if (native_poll_due(ctx.pc)) process_pending_callbacks(ctx);
    if (ctx.pc == 0 || ctx.pc == 0xFFFFFFFC) return;
    if ((ctx.pc & 0xF0000000) == 0xC0000000) ctx.pc = (ctx.pc & 0x0FFFFFFF) | 0x80000000;
    else if ((ctx.pc & 0xF0000000) == 0xD0000000) ctx.pc = (ctx.pc & 0x0FFFFFFF) | 0x90000000;
    else if (ctx.pc < 0x80000000) ctx.pc |= 0x80000000;
    const uint32_t target = ctx.pc;
    ++ctx.inst_count;
    if (target >= 0x804A4CE0u && target <= 0x804A788Cu && native_wpad_dispatch(ctx, target)) return;
    if (__builtin_expect(site.pc != target || !site.func, 0)) {
        const auto dispatch = find_dispatch_target_hot(target);
        if (!dispatch.func) { interpret_step(ctx); return; }
        site.pc = target; site.func = dispatch.func; site.hle = dispatch.hle;
    }
    ctx.pc = target;
    site.func(ctx);
    if (site.hle) ctx.pc = ctx.lr;
}
}
'''


class Skip(Exception):
    pass


def rw(path, fn):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    n = fn(t)
    if n != t:
        path.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
        return True
    return False


def main():
    outputs = sorted(EXPORT.glob('output_*.cpp'))
    main_cpp = EXPORT / 'main_output.cpp'
    manifest_path = BACKUP / 'manifest.json'
    if '--restore' in sys.argv:
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            for key, digest in manifest.items():
                if hashlib.sha256((ROOT / key).read_bytes()).hexdigest() != digest:
                    raise Skip(key + ' changed since this stage wrote it')
            for key in manifest:
                shutil.copyfile(BACKUP / key, ROOT / key)
            shutil.rmtree(BACKUP)
        n = sum(rw(p, lambda t: t.replace(SITE_NEW, SITE_OLD)) for p in outputs)
        print('Indirect site cache removed: %d generated file(s) restored.' % n)
        return
    for base in TREES.values():
        t = (base / HDR).read_text(errors='surrogateescape')
        if MARK not in t and t.count(HDR_OLD) != 1:
            raise Skip('cpu_context.h: run fix_guest_calls_mom.py first (anchor not found in %s)' % base)
    m = main_cpp.read_text(errors='surrogateescape').replace('\r\n', '\n')
    if MARK not in m and (m.count(MAIN_ANCHOR) != 1 or 'NATIVE_MOM_GUEST_CALLS_V1' not in m):
        raise Skip('main_output.cpp: run fix_guest_calls_mom.py first')
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for path in [TREES[k] / HDR for k in TREES] + [main_cpp]:
        if MARK in path.read_text(errors='surrogateescape'):
            continue
        key = str(path.relative_to(ROOT)).replace('\\', '/')
        dst = BACKUP / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(path.read_bytes())
        if path == main_cpp:
            rw(path, lambda t: t.replace(MAIN_ANCHOR, MAIN_BLOCK + MAIN_ANCHOR, 1))
        else:
            rw(path, lambda t: t.replace(HDR_OLD, HDR_NEW, 1))
        manifest[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    sites = 0
    for p in outputs:
        if rw(p, lambda t: t if SITE_NEW in t else t.replace(SITE_OLD, SITE_NEW)):
            sites += p.read_text(errors='surrogateescape').count('native_call_indirect_site(ctx, native_site)')
    print('Indirect site cache installed: %d sites.' % sites)


if __name__ == '__main__':
    try:
        main()
    except Skip as reason:
        print('fix_indirect_site_cache_mom: ' + str(reason))
        sys.exit(1)
