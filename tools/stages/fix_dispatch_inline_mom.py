r"""Inline the dispatch-cache hit path (MoM counterpart of CotT fix_dispatch_abi.py, NATIVE_DISPATCH_ABI16_V1).

Marker: NATIVE_MOM_DISPATCH_INLINE_V1.

find_dispatch_target_hot returns DispatchChoice { uint32_t pc; func; bool hle; } (24 bytes). CotT shrank it to 16 bytes so
the SysV ABI returns it in registers, but this build uses the Windows x64 ABI, which returns any struct over 8 bytes
through a hidden pointer. It was 3.8% of the guest thread in profile prof5. Instead, the hit path (hash, tag compare,
copy) is forced inline into its callers (run_game, native_call_indirect, native_call_indirect_site), and the miss is an
out-of-line cold call to the unchanged resolver, so a hit is no call at all. The cache contents, tags and results are
unchanged.

Edits export/main_output.cpp. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_DISPATCH_INLINE_V1'
PATH = Path(__file__).resolve().parents[2] / 'export/main_output.cpp'
OLD = ('static DispatchChoice find_dispatch_target_hot(uint32_t target) {\n'
       '    static std::array<DispatchChoice, 4096> cache{};\n'
       '    DispatchChoice& slot = cache[((target >> 2) ^ (target >> 14)) & 4095u];\n'
       '    if (slot.pc != target) slot = find_dispatch_target_slow(target);\n'
       '    return slot;\n'
       '}\n')
NEW = ('// ' + MARK + ': the hit path is inlined into every caller; the miss refills out of line.\n'
       'static std::array<DispatchChoice, 16384> g_native_dispatch_cache{};\n'
       '__attribute__((noinline, cold)) static void native_dispatch_refill(DispatchChoice& slot, uint32_t target) {\n'
       '    slot = find_dispatch_target_slow(target);\n'
       '}\n'
       '__attribute__((always_inline)) static inline const DispatchChoice& find_dispatch_target_hot(uint32_t target) {\n'
       '    DispatchChoice& slot = g_native_dispatch_cache[((target >> 2) ^ (target >> 16)) & 16383u];\n'
       '    if (__builtin_expect(slot.pc != target, 0)) native_dispatch_refill(slot, target);\n'
       '    return slot;\n'
       '}\n')


def main():
    raw = PATH.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    if '--restore' in sys.argv:
        n = t.replace(NEW, OLD, 1)
    elif MARK in t:
        print('dispatch inline already installed.'); return
    elif t.count(OLD) != 1:
        print('fix_dispatch_inline_mom: anchor not found exactly once'); sys.exit(1)
    else:
        n = t.replace(OLD, NEW, 1)
    if n != t:
        PATH.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('dispatch inline %s.' % ('removed' if '--restore' in sys.argv else 'installed'))


if __name__ == '__main__':
    main()
