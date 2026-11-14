r"""Request the GX drain before the idle park, not after it (MoM; CotT fix_gx_idle_drain / fix_gx_drain_cv ordering).

Marker: NATIVE_MOM_DRAIN_BEFORE_PARK_V1.

In process_pending_callbacks the guest-idle park (NATIVE_OS_IDLE_WAIT_MOM_V1, up to NWII_IDLE_PARK_US = 1 ms) ran before the
idle-drain request (NATIVE_GX_IDLE_DRAIN). When the guest idles on a GXDrawDone / PE wait with GX bytes still staged, it
therefore parked for up to a millisecond before asking the host to drain the bytes that would release it; a frame has
several such waits. CotT requests the drain as soon as the guest is idle with bytes pending. The same request (same
condition, same call, NWII_NO_GX_IDLE_DRAIN still disables it) now also runs immediately before the park; the existing
request after the park is unchanged and finds nothing pending when the drain already happened. A shorter park measured
+4% (NWII_IDLE_PARK_US=100), which this addresses at the cause.

Edits src/hle/ios.cpp in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_DRAIN_BEFORE_PARK_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/ios.cpp', ROOT / 'export/nWiiRuntime/src/hle/ios.cpp']
ANCHOR = '  // NATIVE_OS_IDLE_WAIT_MOM_V1: the guest\'s own idle loop, verified by\n'
BLOCK = ('  { // ' + MARK + ': ask for the drain first, so the park below is ended by the PE wake, not by its timeout\n'
         '    static const bool idle_drain_first = std::getenv("NWII_NO_GX_IDLE_DRAIN") == nullptr;\n'
         '    if (idle_drain_first && ctx.pc == 0x80492408u && g_gx_pending_bytes.load(std::memory_order_relaxed) > 0)\n'
         '      nwii::runtime::native_gx_request_drain(); }\n')


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
        elif t.count(ANCHOR) != 1:
            print('fix_drain_before_park_mom: anchor not found once in %s' % p); sys.exit(1)
        else:
            n = t.replace(ANCHOR, BLOCK + ANCHOR, 1)
        if n != t:
            p.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('drain before park %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
