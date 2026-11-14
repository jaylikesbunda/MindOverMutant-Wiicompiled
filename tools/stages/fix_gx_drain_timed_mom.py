r"""Time-based early GX drain (MoM; extends CotT fix_gx_early_drain / fix_gx_early_drain_default).

Marker: NATIVE_MOM_GX_DRAIN_TIMED_V1.

Why: with NWII_IDLE_CAUSE the busy scene showed the guest idle ~30% of wall time in episodes that end with the PE finish /
token interrupt, i.e. waiting for the host to drain, parse and decode the frame's GX work. The early drain only fires once
NWII_GX_DRAIN_BYTES (8192) new FIFO bytes are pending, but Mind over Mutant draws mostly through display lists (GXPROF:
15 MB of display-list bytes against 17 MB of direct draw bytes): a CallDisplayList is a few FIFO bytes that expand into a
large draw on the host. Display-list-heavy stretches therefore never reach the byte threshold, and their whole parse and
decode lands at the frame's sync point while the guest waits.

Change: native_gx_publish also requests a drain when any published bytes are still pending and no drain was requested in
the last NWII_GX_DRAIN_US microseconds (default 1000; 0 disables the timer). The byte trigger is unchanged. The request is
the same coalescing native_gx_request_drain the byte trigger uses, so the host simply drains more often, in smaller pieces,
overlapping the guest; FIFO bytes, order and every PE signal are unchanged.

Edits src/hle/gx.cpp in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_GX_DRAIN_TIMED_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx.cpp', ROOT / 'export/nWiiRuntime/src/hle/gx.cpp']
OLD = ('    if (pending >= g_gx_requested_at + trigger) {\n'
       '        g_gx_requested_at = pending;\n'
       '        nwii::runtime::native_gx_request_drain();\n'
       '    }\n')
NEW = ('    // ' + MARK + ': also drain on a timer, so display-list work (few FIFO bytes, large host work) overlaps the guest.\n'
       '    static const int64_t timed_ns = [] {\n'
       '        const char* e = std::getenv("NWII_GX_DRAIN_US");\n'
       '        const long us = e ? std::strtol(e, nullptr, 10) : 1000;\n'
       '        return int64_t(us > 0 ? us : 0) * 1000;\n'
       '    }();\n'
       '    static std::chrono::steady_clock::time_point last_request{};\n'
       '    bool timed = false;\n'
       '    if (timed_ns && pending > g_gx_requested_at) {\n'
       '        const auto now = std::chrono::steady_clock::now();\n'
       '        timed = std::chrono::duration_cast<std::chrono::nanoseconds>(now - last_request).count() >= timed_ns;\n'
       '    }\n'
       '    if (pending >= g_gx_requested_at + trigger || timed) {\n'
       '        g_gx_requested_at = pending;\n'
       '        last_request = std::chrono::steady_clock::now();\n'
       '        nwii::runtime::native_gx_request_drain();\n'
       '    }\n')


def main():
    restore = '--restore' in sys.argv
    for p in FILES:
        raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
        crlf = '\r\n' in raw
        t = raw.replace('\r\n', '\n')
        a, b = (NEW, OLD) if restore else (OLD, NEW)
        if b in t:
            continue
        if t.count(a) != 1:
            print('fix_gx_drain_timed_mom: anchor not found once in %s' % p); sys.exit(1)
        t = t.replace(a, b, 1)
        if not restore and '#include <chrono>' not in t:
            t = '#include <chrono> // ' + MARK + '\n' + t
        p.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
    print('gx timed drain %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
