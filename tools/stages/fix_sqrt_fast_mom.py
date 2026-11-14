r"""Host sqrt for the MSL software sqrt (port of Crash of the Titans fix_sqrt_fast_path.py, NATIVE_SQRT_FAST_V1).

Marker: NATIVE_MOM_SQRT_FAST_V1.

MoM's fdlibm __ieee754_sqrt starts at 0x80432934 (generated as func_80434F74 in export/output_57.cpp; its opcode sequence
matches CotT's func_803A35F0 at 96%, same length, differing only in addresses). It computes the root bit by bit in integer
registers because Broadway has no double-precision sqrt, and it is the hottest translated function on the guest thread
(5.6% in profile prof5). fdlibm's sqrt is correctly rounded in round-to-nearest, so for positive normal doubles it equals the
host's sqrt bit for bit (CotT data/sdk-sqrt-tests.json: 9971/9971).

Entry fast path at the top of the function, taken only when ctx.pc is the entry (0x80432934, or 0x80434F74: the
one-instruction `b` stub every caller actually calls, 288 sites), MSR.FP is set, NWII_EXACT_SQRT is unset and f1 is a positive normal
double: f1 = sqrt(f1), pc = lr, instruction count advanced by a typical 800. Zero, denormals, negatives, infinities and NaNs
run the original code unchanged (errno writes, special returns). Not reproduced on the fast path, as in CotT: the volatile
scratch registers (r0, r3-r12, cr0/cr1, XER.CA, CTR, f0) and the dead stack frame below r1, none of which are live across a
call under the PowerPC EABI.

Edits export/output_57.cpp only. Idempotent; --restore removes the block exactly.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_SQRT_FAST_V1'
ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'export/output_57.cpp'
ENTRY = 0x80432934
ANCHOR = '    if (NWII_MOM_TRACE_CALLS_ON && nwii::runtime::g_trace_calls && ctx.pc == 0x80432934) nwii::runtime::trace_call(0x80432934, ctx);\n'
BLOCK = '''    { // NATIVE_MOM_SQRT_FAST_V1: host sqrt on positive normal doubles (tools/stages/fix_sqrt_fast_mom.py)
        static const bool exact_sqrt = std::getenv("NWII_EXACT_SQRT") != nullptr;
        uint64_t bits_; std::memcpy(&bits_, &ctx.fpr[1], 8);
        const uint32_t exponent_ = uint32_t(bits_ >> 52);   // sign and exponent: positive normal means 1..0x7FE
        if (!exact_sqrt && (ctx.pc == 0x80432934 || ctx.pc == 0x80434F74) && (ctx.msr & 0x2000) && exponent_ - 1u < 0x7FEu) {
            ctx.fpr[1] = std::sqrt(ctx.fpr[1]);
            ctx.inst_count += 800;
            ctx.pc = ctx.lr; return;
        }
    }
'''
INCLUDE_OLD = '#include <cmath>\n'
INCLUDE_NEW = '#include <cmath>\n#include <cstring> // NATIVE_MOM_SQRT_FAST_V1\n'


def main():
    raw = PATH.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    if '--restore' in sys.argv:
        n = t.replace(BLOCK, '', 1).replace(INCLUDE_NEW, INCLUDE_OLD, 1)
        print('sqrt fast path removed.' if n != t else 'sqrt fast path: nothing to restore.')
    elif MARK in t:
        print('sqrt fast path already installed.')
        return
    else:
        i = t.find('void func_80434F74(')
        if i < 0 or t.count(ANCHOR) != 1 or t.find(ANCHOR) < i or t.count(INCLUDE_OLD) != 1:
            print('fix_sqrt_fast_mom: anchors not found exactly once in output_57.cpp')
            sys.exit(1)
        n = t.replace(ANCHOR, BLOCK + ANCHOR, 1).replace(INCLUDE_OLD, INCLUDE_NEW, 1)
        print('sqrt fast path installed at 0x%08X.' % ENTRY)
    if n != t:
        PATH.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))


if __name__ == '__main__':
    main()
