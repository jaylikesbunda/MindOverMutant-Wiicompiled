r"""One bounded copy per gather-pipe write (follows fix_fifo_append_mom.py; CotT fix_gather_span / fix_cheap_guest_hotspots).

Marker: NATIVE_MOM_FIFO_APPEND_V2.

GX_WGPIPE_Write8/16/32/F32/F64 were 4.6% of the guest thread in profile prof6 (the skinning loops write every vertex
through them). On the ordinary path native_wgp_bulk appended the 1-8 big-endian bytes with one push_back each, i.e. a
capacity test and a store per byte. They are now appended with a single vector insert of the same bytes in the same order
(the capacity is already reserved to 4104 on that path, so it never reallocates). Bytes, order and publish points are
unchanged.

Edits src/hle/gx.cpp in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx.cpp', ROOT / 'export/nWiiRuntime/src/hle/gx.cpp']
OLD = 'for (unsigned i = 0; i < bytes; ++i) g_cpu_fifo.push_back(be_bytes[i]); // NATIVE_MOM_FIFO_APPEND_V1: no zero-fill resize'
NEW = 'g_cpu_fifo.insert(g_cpu_fifo.end(), be_bytes, be_bytes + bytes); // NATIVE_MOM_FIFO_APPEND_V2: one bounded copy (was V1 push_back loop)'


def main():
    restore = '--restore' in sys.argv
    for p in FILES:
        raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
        a, b = (NEW, OLD) if restore else (OLD, NEW)
        if b in raw and a not in raw:
            continue
        if raw.count(a) != 1:
            print('fix_fifo_append_v2_mom: anchor not found once in %s' % p); sys.exit(1)
        p.write_bytes(raw.replace(a, b, 1).encode('utf-8', errors='surrogateescape'))
    print('fifo append v2 %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
