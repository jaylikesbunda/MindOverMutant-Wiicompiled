r"""Guest GX FIFO stores append with inline push_back instead of an out-of-line zero-filling resize.

Marker: NATIVE_MOM_FIFO_APPEND_V1. Port of item 1 of the Crash of the Titans stage fix_cheap_guest_hotspots.py:
"the staging vector already has capacity >= 4104 on the fast path, so each guest FIFO store zero-filled via an
out-of-line resize and then overwrote the same bytes. Now each byte is appended with an inlined push_back. Bytes and
order are identical." MoM's native_wgp_bulk had the same resize + memcpy; every GX_WGPIPE_Write8/16/32/F32 store from
the guest thread goes through it (GX_WGPIPE_* ~3% of the guest thread in gameplay).

Edits src/hle/gx.cpp in the vendored runtime and in export/. Idempotent; --restore is the exact inverse.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_FIFO_APPEND_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx.cpp', ROOT / 'export/nWiiRuntime/src/hle/gx.cpp']
OLD = ('        g_cpu_fifo.resize(first + bytes);\n'
       '        const uint64_t be = __builtin_bswap64(value << (8 * (8 - bytes)));\n'
       '        std::memcpy(g_cpu_fifo.data() + first, &be, bytes);\n')
NEW = ('        const uint64_t be = __builtin_bswap64(value << (8 * (8 - bytes)));\n'
       '        const uint8_t* be_bytes = reinterpret_cast<const uint8_t*>(&be);\n'
       '        for (unsigned i = 0; i < bytes; ++i) g_cpu_fifo.push_back(be_bytes[i]); // ' + MARK + ': no zero-fill resize\n')


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
            print('fix_fifo_append_mom: anchor not found once in ' + str(path))
            sys.exit(1)
        t = t.replace(old, new, 1)
        path.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
        changed += 1
    print('FIFO append %s: %d file(s) changed.' % ('removed' if restore else 'installed', changed))


if __name__ == '__main__':
    main()
