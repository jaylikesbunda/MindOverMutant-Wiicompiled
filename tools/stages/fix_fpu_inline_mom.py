r"""Keep the FP-enabled check of require_fpu inside generated code; the lazy-FPU acquire stays out of line.

Marker: NATIVE_MOM_FPU_INLINE_V1. Port of the Crash of the Titans NATIVE_FPU_INLINE_GUARD_V1.

Every translated floating-point instruction is preceded by `if (ctx.require_fpu(PC)) { ... return; }`. MoM's
require_fpu was an ordinary member function that GCC did not inline (it shows as `require_fpu [clone .isra.0]` in the
guest profile), so each FP instruction paid a call to test one MSR bit. The body becomes an always-inline test of
MSR.FP that returns false when it is set (the common case) and otherwise calls native_require_fpu_slow - the unchanged
original body (acquire the lazy FP context of the current thread, set MSR.FP, return false).

Edits include/runtime/cpu_context.h in the vendored runtime and export/; every generated chunk includes it.
Idempotent; --restore is the exact inverse.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_FPU_INLINE_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/include/runtime/cpu_context.h',
         ROOT / 'export/nWiiRuntime/include/runtime/cpu_context.h']
OLD = ('  bool require_fpu(uint32_t fault_pc) {\n'
       '    if (msr & 0x2000u) return false;\n')
NEW = ('  // ' + MARK + ': the FP-enabled test stays in generated code; the lazy acquire is out of line.\n'
       '  __attribute__((always_inline)) inline bool require_fpu(uint32_t fault_pc) {\n'
       '    if (__builtin_expect(msr & 0x2000u, 1)) return false;\n'
       '    return native_require_fpu_slow(fault_pc);\n'
       '  }\n'
       '  __attribute__((noinline)) bool native_require_fpu_slow(uint32_t fault_pc) {\n'
       '    if (msr & 0x2000u) return false;\n')


def main():
    restore = '--restore' in sys.argv
    changed = 0
    for path in FILES:
        raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
        crlf = '\r\n' in raw
        t = raw.replace('\r\n', '\n')
        old, new = (NEW, OLD) if restore else (OLD, NEW)
        if (MARK in t) != restore:
            continue
        if t.count(old) != 1:
            print('fix_fpu_inline_mom: anchor not found once in ' + str(path))
            sys.exit(1)
        t = t.replace(old, new, 1)
        path.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
        changed += 1
    print('FPU inline %s: %d file(s) changed.' % ('removed' if restore else 'installed', changed))


if __name__ == '__main__':
    main()
