#!/usr/bin/env python3
"""compare_generated.py - diff two sets of recompiler output.

A change to NWiiRecomp (or to the patches in patches/) regenerates export/.
Before spending the compile time, compare the new generated C++ with the old and
look at the shape of the change:

  * same file set and same function count -> codegen change, expected small deltas
  * different function count -> the analyzer saw a different program
  * a file rewritten wholesale -> treat as suspicious, not as an improvement

Usage:
    python tools/compare_generated.py --before <dir> --after export
    python tools/compare_generated.py --before <dir> --after export --show 3
"""
from __future__ import annotations

import argparse
import difflib
import os
import re

FUNC_RE = re.compile(r"^\s*(?:static\s+)?void\s+(?:func_|sub_)([0-9a-fA-F]{8})", re.M)


def generated_files(directory):
    out = {}
    for name in os.listdir(directory):
        if name.endswith(".cpp") or name == "functions.h":
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as handle:
                    out[name] = handle.read()
    return out


def function_count(text):
    return len(set(FUNC_RE.findall(text)))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--show", type=int, default=2,
                        help="show this many example hunks per changed file")
    parser.add_argument("--show-files", type=int, default=6)
    args = parser.parse_args()

    before = generated_files(args.before)
    after = generated_files(args.after)

    only_before = sorted(set(before) - set(after))
    only_after = sorted(set(after) - set(before))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    identical = sorted(name for name in set(before) & set(after) if before[name] == after[name])

    print("files before=%d after=%d" % (len(before), len(after)))
    print("  identical      %d" % len(identical))
    print("  changed        %d" % len(changed))
    print("  only before    %s" % (only_before or "-"))
    print("  only after     %s" % (only_after or "-"))

    fb = sum(function_count(text) for text in before.values())
    fa = sum(function_count(text) for text in after.values())
    print("\nfunctions with a body: before %d, after %d (%+d)" % (fb, fa, fa - fb))

    if not changed:
        return

    print("\nbiggest changes:")
    scored = []
    for name in changed:
        a = before[name].splitlines()
        b = after[name].splitlines()
        added = sum(1 for line in difflib.ndiff(a, b) if line.startswith("+ "))
        removed = sum(1 for line in difflib.ndiff(a, b) if line.startswith("- "))
        scored.append((added + removed, name, added, removed))
    scored.sort(reverse=True)
    for total, name, added, removed in scored[:args.show_files]:
        print("  %-16s %+d/-%d lines (%d changed lines)" % (name, added, removed, total))

    for total, name, _, _ in scored[:args.show_files]:
        if total == 0:
            continue
        print("\n--- %s ---" % name)
        diff = list(difflib.unified_diff(before[name].splitlines(),
                                         after[name].splitlines(),
                                         fromfile="before/" + name, tofile="after/" + name,
                                         n=1, lineterm=""))
        for line in diff[:args.show * 14 + 2]:
            print(line[:200])


if __name__ == "__main__":
    main()
