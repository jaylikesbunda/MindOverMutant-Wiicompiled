#!/usr/bin/env python3
"""identify_pc.py - turn a guest address from a runtime log into a work item.

Given a PC from a heartbeat, an interrupt line or a crash, report:

  * whether the recompiler treats it as a function entry or an offset inside one
  * its SDK name, if the signature database matched it
  * its Crash of the Titans counterpart, if the decomp cross-match found one
    (which usually means a CotT stage script already knows this function's fix)

Usage:
    python tools/identify_pc.py 0x8017FC74
    python tools/identify_pc.py 0x8017FC74 --context 0x20
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNCTIONS_H = os.path.join(ROOT, "export", "functions.h")
SYMBOLS_CSV = os.path.join(ROOT, "export", "sdk_symbols.csv")
DECOMP = os.path.join(os.path.dirname(ROOT), "Crash - Mind over Mutant - DECOMP")
CROSSMATCH = os.path.join(DECOMP, "export", "crossmatch.tsv")
GHIDRA_INDEX = os.path.join(DECOMP, "export", "index.tsv")
EMITTED_RE = re.compile(r"\b(?:func_|sub_)([0-9a-fA-F]{8})\b")


def recompiler_functions():
    """Every function entry the recompiler emitted."""
    if not os.path.isfile(FUNCTIONS_H):
        return set()
    with open(FUNCTIONS_H, encoding="utf-8", errors="replace") as handle:
        return {int(value, 16) for value in EMITTED_RE.findall(handle.read())}


def ghidra_extents():
    extents = {}
    if os.path.isfile(GHIDRA_INDEX):
        with open(GHIDRA_INDEX, encoding="utf-8", errors="replace") as handle:
            handle.readline()
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3:
                    try:
                        extents[int(parts[0], 16)] = (int(parts[1], 0), parts[2])
                    except ValueError:
                        continue
    return extents


def sdk_names():
    names = {}
    if os.path.isfile(SYMBOLS_CSV):
        with open(SYMBOLS_CSV, encoding="utf-8", errors="replace") as handle:
            handle.readline()
            for line in handle:
                parts = line.split(",")
                if len(parts) >= 2:
                    try:
                        names[int(parts[1], 16)] = parts[0].strip().strip('"')
                    except ValueError:
                        continue
    return names


def cott_partner():
    """MoM address -> (cott address, kind) from the decomp cross-match."""
    partners = {}
    if os.path.isfile(CROSSMATCH):
        with open(CROSSMATCH, encoding="utf-8", errors="replace") as handle:
            handle.readline()
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 5 and parts[4]:
                    try:
                        partners[int(parts[0], 16)] = (int(parts[4], 16), parts[3])
                    except ValueError:
                        continue
    return partners


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("address", help="guest address, e.g. 0x8017FC74")
    parser.add_argument("--context", type=int, default=0,
                        help="also list this many nearby emitted function entries")
    args = parser.parse_args()

    pc = int(args.address, 16)
    functions = recompiler_functions()
    extents = ghidra_extents()
    names = sdk_names()
    partners = cott_partner()

    print("PC 0x%08X" % pc)
    print("  recompiler emitted it as a function entry: %s" % ("yes" if pc in functions else "no"))
    if functions:
        below = [a for a in functions if a <= pc]
        if below:
            start = max(below)
            print("  containing emitted function: 0x%08X (+0x%X into it)" % (start, pc - start))
    if pc in extents:
        size, name = extents[pc]
        print("  Ghidra extent: %d bytes, %s" % (size, name))
    else:
        containing = [a for a in extents if a <= pc]
        if containing:
            start = max(containing)
            size, name = extents[start]
            if pc < start + size:
                print("  inside Ghidra function 0x%08X +0x%X (%s, %d bytes)" % (
                    start, pc - start, name, size))
            else:
                print("  no Ghidra function contains it (data or an unanalysed gap)")
    print("  SDK name: %s" % names.get(pc, "(not matched by the signature database)"))
    # The cross-match is function-granular, so a PC inside a function resolves
    # through that function's entry point.
    host = None
    if pc in extents:
        host = pc
    else:
        containing = [a for a in extents if a <= pc]
        if containing:
            start = max(containing)
            if pc < start + extents[start][0]:
                host = start
    lookup = pc if pc in partners else host
    if lookup is not None and lookup in partners:
        cott, kind = partners[lookup]
        where = "this address" if lookup == pc else "via containing function 0x%08X" % host
        print("  CotT counterpart: 0x%08X (%s match, %s) - check for a stage script covering it" % (
            cott, kind, where))
    else:
        print("  CotT counterpart: none found (game-specific code, or the matcher missed it)")

    if args.context and functions:
        print("\nnearest emitted entries:")
        for address in sorted(functions):
            if abs(address - pc) <= args.context:
                print("  0x%08X %s" % (address, "<-- PC" if address == pc else ""))


if __name__ == "__main__":
    main()
