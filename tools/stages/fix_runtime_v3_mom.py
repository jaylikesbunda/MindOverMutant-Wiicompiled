r"""x86-64-v3 for the runtime library (port of Crash of the Titans fix_movbe_flags.py, NATIVE_X86_64_V3_V1).

Marker: NATIVE_MOM_RUNTIME_V3_V1.

The generated guest chunks already build with -march=x86-64-v3 -mfma (Haswell baseline), but the nwiiruntime library
(callback poll, GX FIFO writes, psq slow paths, MMU slow paths, AX mixing, FIFO parser/decoder) built for the SSE2-only
x86-64 baseline. This adds -march=x86-64-v3 to that target, with -ffp-contract=off so GCC does not fuse separate
multiplies and adds into FMAs: float results stay bit-identical to the baseline build. Same hardware requirement as the
chunks already have.

Edits nWiiRuntime/CMakeLists.txt in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_RUNTIME_V3_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/CMakeLists.txt', ROOT / 'export/nWiiRuntime/CMakeLists.txt']
BLOCK = ('\n# ' + MARK + ': Haswell baseline for the runtime library, no FP contraction (tools/stages/fix_runtime_v3_mom.py)\n'
         'target_compile_options(nwiiruntime PRIVATE -march=x86-64-v3 -ffp-contract=off)\n')


def main():
    restore = '--restore' in sys.argv
    for p in FILES:
        raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
        crlf = '\r\n' in raw
        t = raw.replace('\r\n', '\n')
        if restore:
            n = t.replace(BLOCK, '', 1)
        elif MARK in t:
            continue
        else:
            if 'add_library(nwiiruntime STATIC' not in t:
                print('fix_runtime_v3_mom: nwiiruntime target not found in %s' % p); sys.exit(1)
            n = t + BLOCK if t.endswith('\n') else t + '\n' + BLOCK
        if n != t:
            p.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('runtime x86-64-v3 %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
