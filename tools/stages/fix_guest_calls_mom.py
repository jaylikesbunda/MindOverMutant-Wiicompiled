r"""Guest call overhead, ported from the Crash of the Titans port: inline indirect calls, one dispatcher recovery frame,
no per-call trace test.

Marker: NATIVE_MOM_GUEST_CALLS_V1. Sources: CotT tools/fix_native_call_indirect.py (NATIVE_CALL_INDIRECT_V1),
fix_dispatch_exception_scope.py and the trace half of fix_codegen_slim.py.

1. Indirect calls (bctrl, i.e. C++ virtual calls and function pointers - 10,767 sites in MoM's generated code) were
   `ctx.lr = RET; if (ctx.ctr) { ctx.pc = ctx.ctr; return; } ...`: the caller left, run_game dispatched the target, the
   callee returned to run_game, which dispatched RET and re-entered the caller through its entry switch. Now the site
   calls nwii::runtime::native_call_indirect(ctx) - one run_game loop iteration, verbatim (callback poll first, the
   C0/D0 -> 80/90 alias mapping, ++inst_count, the range-guarded WPAD hook, the dispatch table, HLE return) - and
   continues in place when the callee came back to RET; any other pc (thread switch, exception, callback, idle) still
   returns to run_game exactly as before. The `++ctx.inst_count` in front of RET counts the re-entry the loop used to
   count, so instruction accounting is unchanged.
2. run_game called _setjmp on every dispatch (a CRT call, visible as __intrinsic_setjmp on the guest thread). Only
   run_game arms ctx.exception_jmp_buf, and every longjmp to it comes from below run_game while run_game is active, so
   the buffer is armed once; a longjmp lands at that point and `continue`s the loop, as the old else branch did.
3. Every generated function began with `if (nwii::runtime::g_trace_calls && ctx.pc == ADDR) trace_call(ADDR, ctx);`
   (30,676 sites: a global load and two compares per call). The test is prefixed with NWII_MOM_TRACE_CALLS_ON, 0 unless
   the build defines it, so the compiler drops it; build with -DNWII_MOM_TRACE_CALLS_ON=1 to trace calls again.

Run after anything that regenerates or rewrites export/output_*.cpp and export/main_output.cpp. Idempotent;
--restore reverses every edit (regex inverse on the generated files, backups for cpu_context.h and main_output.cpp).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_GUEST_CALLS_V1'
ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / 'export'
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': EXPORT / 'nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_guest_calls_mom'
HDR = 'include/runtime/cpu_context.h'


class Skip(Exception):
    pass


SITE = re.compile(r'(    ctx\.lr = 0x([0-9A-F]{8}); // save return address\n\s*)'
                  r'if \(ctx\.ctr\) \{ ctx\.pc = ctx\.ctr; return; \}( else nwii::runtime::note_null_call\(0x[0-9A-F]{8}, ctx\.lr\);)')
SITE_BACK = re.compile(r'if \(ctx\.ctr\) \{ ctx\.pc = ctx\.ctr; nwii::runtime::native_call_indirect\(ctx\); '
                       r'if \(ctx\.pc != 0x[0-9A-F]{8}\) return; \}( else nwii::runtime::note_null_call\(0x[0-9A-F]{8}, ctx\.lr\);) /\* '
                       + MARK + r' \*/')
TRACE_OLD = 'if (nwii::runtime::g_trace_calls && ctx.pc == 0x'
TRACE_NEW = 'if (NWII_MOM_TRACE_CALLS_ON && nwii::runtime::g_trace_calls && ctx.pc == 0x'

HDR_OLD = 'bool process_pending_callbacks(CPUContext &ctx);\n'
HDR_NEW = (HDR_OLD + 'void native_call_indirect(CPUContext &ctx); // ' + MARK + '\n'
           '#ifndef NWII_MOM_TRACE_CALLS_ON // ' + MARK + ': per-call trace test compiled out unless the build defines it\n'
           '#define NWII_MOM_TRACE_CALLS_ON 0\n#endif\n')

CALL_INDIRECT = r'''// NATIVE_MOM_GUEST_CALLS_V1: one run_game iteration for an indirect call made from inside a translated function.
namespace nwii::runtime {
bool native_wpad_dispatch(CPUContext&, uint32_t);
void native_call_indirect(CPUContext& ctx) {
    if (!ctx.is_running || ctx.pc == 0) return;
    process_pending_callbacks(ctx);
    if (ctx.pc == 0 || ctx.pc == 0xFFFFFFFC) return;
    if ((ctx.pc & 0xF0000000) == 0xC0000000) ctx.pc = (ctx.pc & 0x0FFFFFFF) | 0x80000000;
    else if ((ctx.pc & 0xF0000000) == 0xD0000000) ctx.pc = (ctx.pc & 0x0FFFFFFF) | 0x90000000;
    else if (ctx.pc < 0x80000000) ctx.pc |= 0x80000000;
    const uint32_t target = ctx.pc;
    ++ctx.inst_count;
    if (target >= 0x804A4CE0u && target <= 0x804A788Cu && native_wpad_dispatch(ctx, target)) return;
    const auto dispatch = find_dispatch_target_hot(target);
    if (dispatch.func) {
        ctx.pc = target;
        dispatch.func(ctx);
        if (dispatch.hle) ctx.pc = ctx.lr;
    } else {
        interpret_step(ctx);
    }
}
}
'''
MAIN_ANCHOR = '// --- Entry Point Wrapper ---\n'
SETJMP_OLD = ('#if defined(__MINGW64__)\n      if (_setjmp(ctx.exception_jmp_buf, nullptr) == 0) {\n#else\n'
              '      if (_setjmp(ctx.exception_jmp_buf) == 0) {\n#endif\n')
SETJMP_NEW = ('      static bool native_recovery_armed = false; // ' + MARK + ': arm the recovery frame once\n'
              '      if (!native_recovery_armed) {\n        native_recovery_armed = true;\n'
              '#if defined(__MINGW64__)\n        if (_setjmp(ctx.exception_jmp_buf, nullptr) != 0) continue;\n#else\n'
              '        if (_setjmp(ctx.exception_jmp_buf) != 0) continue;\n#endif\n      }\n      if (true) {\n')


def rw(path, fn):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    n = fn(t)
    if n != t:
        path.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
        return True
    return False


def gen_apply(t):
    if MARK in t:
        return t
    t = SITE.sub(lambda m: m.group(1) + 'if (ctx.ctr) { ctx.pc = ctx.ctr; nwii::runtime::native_call_indirect(ctx); '
                 'if (ctx.pc != 0x%08X) return; }' % (int(m.group(2), 16)) + m.group(3) + ' /* ' + MARK + ' */', t)
    t = t.replace(TRACE_OLD, TRACE_NEW)
    return t


def gen_restore(t):
    if MARK not in t and TRACE_NEW not in t:
        return t
    t = SITE_BACK.sub(lambda m: 'if (ctx.ctr) { ctx.pc = ctx.ctr; return; }' + m.group(1), t)
    return t.replace(TRACE_NEW, TRACE_OLD)


def main():
    restore = '--restore' in sys.argv
    outputs = sorted(EXPORT.glob('output_*.cpp'))
    main_cpp = EXPORT / 'main_output.cpp'
    manifest_path = BACKUP / 'manifest.json'
    if restore:
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            for key, digest in manifest.items():
                path = ROOT / key
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise Skip(key + ' changed since this stage wrote it')
            for key in manifest:
                shutil.copyfile(BACKUP / key, ROOT / key)
            shutil.rmtree(BACKUP)
        n = sum(rw(p, gen_restore) for p in outputs)
        print('Guest calls removed: %d generated file(s) restored.' % n)
        return
    # check anchors before writing anything
    for base in TREES.values():
        if (base / HDR).read_text(errors='surrogateescape').count(HDR_OLD) != 1 and MARK not in (base / HDR).read_text(errors='surrogateescape'):
            raise Skip('cpu_context.h anchor not found in ' + str(base))
    m = main_cpp.read_text(errors='surrogateescape').replace('\r\n', '\n')
    if MARK not in m and (m.count(MAIN_ANCHOR) != 1 or m.count(SETJMP_OLD) != 1):
        raise Skip('main_output.cpp anchors not found')
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    targets = [(TREES[k] / HDR) for k in TREES] + [main_cpp]
    for path in targets:
        text = path.read_text(errors='surrogateescape')
        if MARK in text:
            continue
        key = str(path.relative_to(ROOT)).replace('\\', '/')
        dst = BACKUP / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(path.read_bytes())
        if path == main_cpp:
            rw(path, lambda t: gen_apply(t).replace(MAIN_ANCHOR, CALL_INDIRECT + MAIN_ANCHOR, 1).replace(SETJMP_OLD, SETJMP_NEW, 1))
        else:
            rw(path, lambda t: t.replace(HDR_OLD, HDR_NEW, 1))
        manifest[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    sites = 0
    for p in outputs:
        before = p.read_text(errors='surrogateescape')
        if rw(p, gen_apply):
            sites += p.read_text(errors='surrogateescape').count('native_call_indirect(ctx)')
    print('Guest calls installed: %d inline indirect-call sites; recovery frame armed once; trace test compiled out.' % sites)


if __name__ == '__main__':
    try:
        main()
    except Skip as reason:
        print('fix_guest_calls_mom: ' + str(reason))
        sys.exit(1)
