"""Prune the per-instruction entry switches of multi-entry generated functions (CotT's fix_prune_entry_cases).

Every generated guest function that has more than one possible entry starts with

    if (ctx.pc != START) {
        switch (ctx.pc) {
            case 0xA: goto loc_A;
            case 0xB: goto loc_B;
            ...
            case 0x0: return;                // idle: no runnable thread
            default: nwii::runtime::interpret_step(ctx); return;
        }
    }

with one case for EVERY instruction in the function.  Every label therefore has to
be treated as an entry point, so the compiler cannot keep any guest register in a
host register across an instruction boundary, every label becomes a jump-table
target, and the machine code is padded and aligned to match.  On MoM the switches
hold 1,332,575 cases (80% of them unreachable).

A guest pc can only be entered mid-function when it is one of:

  * a constant the translated code itself produces - ``ctx.pc = ADDR`` (calls,
    tail jumps, poll sites, fall-through, ``sc``), ``ctx.lr = ADDR`` (bl return
    addresses), ``require_fpu(ADDR)`` and any other ``0x8xxxxxxx`` literal in an
    executable statement;
  * a pointer-sized word in a DOL data section that lands inside the text range
    (switch jump tables, vtables, callback tables).  MoM ships the DOL at
    ``dump/sys/main.dol``; CotT's original read ``orig/main.dol``.

Function starts never need a case (the ``if (ctx.pc != START)`` guard covers them).
Everything else keeps ``default: interpret_step(ctx)``, so a missed entry still
executes correctly - slowly, and visibly in the [INTERP] histogram - rather than
wrongly.  This stage therefore cannot change guest-visible behaviour: it only
removes fast paths for addresses that the analysed image never names.

Measured on MoM: 1,066,406 of 1,332,575 cases removed (80.0%), every one of the 70
generated chunks rewritten, 266.6 MB -> 216.8 MB of generated C++, and .text on a
sampled chunk (output_62.cpp, -Os) 2,045,232 -> 1,451,648 bytes (-29.0%).

Run on the generated chunks (export/), after anything that regenerates them.
Idempotent: a second pass finds only already-pruned switches and removes nothing.
"""
from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

MARKER = 'NATIVE_MOM_PRUNE_ENTRY_CASES_V1'
# The generated image is the DOL's text plus the importer's extents; every guest
# address we care about is a 4-aligned word in this window.
LO, HI = 0x80000000, 0x81800000
CASE = re.compile(r'^( +)case 0x([0-9A-F]{8}): goto loc_([0-9A-F]{8});\n', re.M)
LITERAL = re.compile(r'0x(8[0-9A-Fa-f]{7})\b')


def executable_literals(text):
    out = set()
    for line in text.split('\n'):
        stripped = line.lstrip()
        if not stripped or stripped.startswith('//'):
            continue
        # A case line is what this stage removes; it must not keep itself alive.
        if stripped.startswith('case 0x') and ': goto loc_' in stripped:
            continue
        # The dispatcher guard and the entry trace name the function start, which
        # never needs a case.
        if stripped.startswith('if (ctx.pc != 0x') or 'trace_call(0x' in stripped:
            continue
        code = line.split('//', 1)[0]
        for match in LITERAL.finditer(code):
            value = int(match[1], 16)
            if LO <= value < HI and value % 4 == 0:
                out.add(value)
    return out


def dol_data_targets(dol):
    data = dol.read_bytes()
    offset = struct.unpack('>18I', data[0:0x48])
    address = struct.unpack('>18I', data[0x48:0x90])
    size = struct.unpack('>18I', data[0x90:0xD8])
    targets = set()
    # Sections 0..6 are text/bss-image; 7..17 are data and bss.
    for i in range(7, 18):
        if not size[i]:
            continue
        blob = data[offset[i]:offset[i] + size[i]]
        for off in range(0, len(blob) - 3, 4):
            value = struct.unpack_from('>I', blob, off)[0]
            if LO <= value < HI and value % 4 == 0:
                targets.add(value)
    return targets


def prune(text, targets):
    kept = removed = 0

    def sub(match):
        nonlocal kept, removed
        if int(match[2], 16) in targets:
            kept += 1
            return match[0]
        removed += 1
        return ''

    return CASE.sub(sub, text), kept, removed


def find_dol(root):
    for candidate in (root / 'dump/sys/main.dol', root / 'orig/main.dol',
                      root / 'dump/main.dol'):
        if candidate.is_file():
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=None)
    args = parser.parse_args()

    root = args.root
    if root is None:
        root = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
        if not (root / 'vendor').is_dir():
            root = root.parent
    gen = root / 'export'
    if not gen.is_dir():
        print('Export not generated; entry-case pruning deferred.')
        return 0
    dol = find_dol(root)
    if dol is None:
        print('No main.dol found (dump/sys/main.dol); entry-case pruning deferred.')
        return 0

    files = sorted(gen.glob('output_*.cpp')) + [gen / 'main_output.cpp']
    targets = set()
    texts = {}
    for path in files:
        if not path.is_file():
            continue
        texts[path] = path.read_text(errors='surrogateescape')
        targets |= executable_literals(texts[path])
    targets |= dol_data_targets(dol)

    total_kept = total_removed = 0
    changed = 0
    for path, text in texts.items():
        pruned, kept, removed = prune(text, targets)
        total_kept += kept
        total_removed += removed
        if pruned != text:
            path.write_text(pruned, errors='surrogateescape')
            changed += 1
    if not total_removed:
        print('Entry-case pruning: nothing to prune (%d cases already match the target set).'
              % total_kept)
    else:
        print('Entry-case pruning: %d possible entry addresses; %d switch cases removed, '
              '%d kept, %d chunks rewritten.' % (len(targets), total_removed, total_kept, changed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
