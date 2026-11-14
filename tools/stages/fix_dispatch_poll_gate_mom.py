r"""Gate run_game's per-dispatch callback poll with native_poll_due (CotT fix_dispatch_hot_path / poll credit, for MoM).

Marker: NATIVE_MOM_DISPATCH_POLL_GATE_V1.

run_game called process_pending_callbacks on every pass through the dispatcher, while every generated poll site goes
through native_poll_due (NWII_POLL_CREDIT countdown, or at once when a producer raised the attention latch).
process_pending_callbacks was 3.8% of the guest thread in profile prof5. The dispatcher now uses the same gate.
native_poll_due returns true for pc 0, 0xFFFFFFFC (callback return) and the idle loop entry, so the callback-return
unwind below it and the idle path run exactly as before; a pending interrupt is taken at the next due poll, as at every
other poll site. Edits export/main_output.cpp. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_DISPATCH_POLL_GATE_V1'
PATH = Path(__file__).resolve().parents[2] / 'export/main_output.cpp'
OLD = '      if (true) {\n        process_pending_callbacks(ctx);\n        if (ctx.pc == 0xFFFFFFFC) {\n'
NEW = ('      if (true) {\n        if (nwii::runtime::native_poll_due(ctx.pc)) process_pending_callbacks(ctx); // ' + MARK + '\n'
       '        if (ctx.pc == 0xFFFFFFFC) {\n')


def main():
    raw = PATH.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    if '--restore' in sys.argv:
        n = t.replace(NEW, OLD, 1)
    elif MARK in t:
        print('dispatch poll gate already installed.'); return
    elif t.count(OLD) != 1:
        print('fix_dispatch_poll_gate_mom: anchor not found exactly once'); sys.exit(1)
    else:
        n = t.replace(OLD, NEW, 1)
    if n != t:
        PATH.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('dispatch poll gate %s.' % ('removed' if '--restore' in sys.argv else 'installed'))


if __name__ == '__main__':
    main()
