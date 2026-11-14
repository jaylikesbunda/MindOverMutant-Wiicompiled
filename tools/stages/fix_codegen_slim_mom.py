r"""Poll-site console line out of line (CotT's fix_poll_spin_outline.py, ported to MoM's generated text).

Marker: NATIVE_MOM_POLL_SPIN_OUTLINE_V1. Source: CotT native-port/tools/fix_poll_spin_outline.py and
runtime-overlays/native_guest_fast.h (Spinning). Run after anything that regenerates export/output_*.cpp.

Every generated poll site in MoM ends with (9,753 sites in 66 of the 70 chunks, 1.58 MB of generated source)

    if (nwii::runtime::native_poll_due(ctx.pc)) { if (process_pending_callbacks(ctx)) return; if ((ctx.inst_count % 10000000) == 0) { std::cout << "Spinning at PC: 0x" << std::hex << 0x8000402C << " LR: 0x" << std::hex << ctx.lr << std::dec << std::endl; } }

so every poll site carries an inlined iostream chain. Here the inner statement becomes

    if ((ctx.inst_count % 10000000) == 0) nwii::runtime::native_poll_spinning(0x8000402C, ctx.lr); /* NATIVE_MOM_POLL_SPIN_OUTLINE_V1 */

and native_poll_spinning, a noinline cold function in the new header include/runtime/native_poll_spin.h (written to
vendor/NWiiRecomp/nWiiRuntime and export/nWiiRuntime), prints the same line. The condition, the printed text and the
poll order are unchanged. Only the inner statement is matched, so it is independent of the poll_due/credit/reload
stages that rewrite the enclosing block.

Edits (export only for generated code; the header is new in both runtime trees):
  * export/output_*.cpp: one `#include "runtime/native_poll_spin.h"` line after the cpu_context.h include, and each
    poll-site line above rewritten to the outlined form.
  * export/main_output.cpp is not touched.

Idempotent (a second run changes nothing). `--restore` removes the include line, reverses the site rewrite and deletes
the header (only if it carries the marker). `--dry-run` reports the counts without writing. Generated files are
CRLF; they are read with CRLF folded to LF and written back with CRLF, so a restore is byte-exact.
Rollback: `--restore`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_POLL_SPIN_OUTLINE_V1'
ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / 'export'
TREES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', EXPORT / 'nWiiRuntime']
HDR = 'include/runtime/native_poll_spin.h'
ANCHOR = '#include "runtime/cpu_context.h"\n'
INCLUDE = '#include "runtime/native_poll_spin.h" // ' + MARK + '\n'

ORIGINAL = re.compile(
    r'if \(\(ctx\.inst_count % 10000000\) == 0\) \{ std::cout << "Spinning at PC: 0x" << std::hex << (0x[0-9A-Fa-f]+) '
    r'<< " LR: 0x" << std::hex << ctx\.lr << std::dec << std::endl; \}')
OUTLINED = re.compile(
    r'if \(\(ctx\.inst_count % 10000000\) == 0\) nwii::runtime::native_poll_spinning\((0x[0-9A-Fa-f]+), ctx\.lr\); /\* '
    + MARK + r' \*/')

HDR_TEXT = (
    '// ' + MARK + ': the poll-site "Spinning at PC" console line, out of line.\n'
    '// Generated poll sites carry only their inst_count test; this prints the identical line.\n'
    '#pragma once\n'
    '#include <cstdint>\n'
    '#include <iostream>\n'
    'namespace nwii::runtime {\n'
    '__attribute__((noinline, cold)) inline void native_poll_spinning(uint32_t pc, uint32_t lr) {\n'
    '    std::cout << "Spinning at PC: 0x" << std::hex << pc << " LR: 0x" << std::hex << lr << std::dec << std::endl;\n'
    '}\n'
    '} // namespace nwii::runtime\n'
)


class Skip(Exception):
    pass


def read(path):
    return path.read_bytes().decode('utf-8', errors='surrogateescape')


def rw(path, fn, write=True):
    raw = read(path)
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    n = fn(t)
    if n == t:
        return False
    if write:
        path.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    return True


def apply_text(t):
    if INCLUDE not in t:
        t = t.replace(ANCHOR, ANCHOR + INCLUDE, 1)
    return ORIGINAL.sub(lambda m: 'if ((ctx.inst_count % 10000000) == 0) nwii::runtime::native_poll_spinning('
                        + m[1] + ', ctx.lr); /* ' + MARK + ' */', t)


def restore_text(t):
    t = t.replace(INCLUDE, '')
    return OUTLINED.sub(lambda m: 'if ((ctx.inst_count % 10000000) == 0) { std::cout << "Spinning at PC: 0x" << std::hex << '
                        + m[1] + ' << " LR: 0x" << std::hex << ctx.lr << std::dec << std::endl; }', t)


def main():
    args = sys.argv[1:]
    restore = '--restore' in args
    dry = '--dry-run' in args
    outputs = sorted(EXPORT.glob('output_*.cpp'))

    if restore:
        for tree in TREES:
            h = tree / HDR
            if h.exists() and MARK in read(h):
                if not dry:
                    h.unlink()
        n = sum(rw(p, restore_text, not dry) for p in outputs)
        print('Poll spin outline removed: %d generated file(s) restored.' % n)
        return

    # Preflight: every chunk needs exactly one cpu_context.h include (or already carry ours), and the headers
    # must be ours or absent, before anything is written.
    for tree in TREES:
        h = tree / HDR
        if h.exists() and MARK not in read(h):
            raise Skip(str(h) + ' exists and does not carry ' + MARK)
    for p in outputs:
        t = read(p).replace('\r\n', '\n')
        if INCLUDE not in t and t.count(ANCHOR) != 1:
            raise Skip(str(p) + ': cpu_context.h include anchor not found exactly once')

    if not dry:
        for tree in TREES:
            h = tree / HDR
            if not h.exists():
                h.parent.mkdir(parents=True, exist_ok=True)
                h.write_text(HDR_TEXT, encoding='utf-8')
    changed = sum(rw(p, apply_text, not dry) for p in outputs)
    counted = ORIGINAL if dry else OUTLINED
    sites = sum(len(counted.findall(read(p))) for p in outputs)
    print('Poll spin outlined: %d poll site(s) across %d generated file(s)%s.' % (sites, changed, ' (dry run)' if dry else ''))


if __name__ == '__main__':
    try:
        main()
    except Skip as reason:
        print('fix_codegen_slim_mom: ' + str(reason))
        sys.exit(1)
