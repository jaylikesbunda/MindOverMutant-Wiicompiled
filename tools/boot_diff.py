#!/usr/bin/env python3
"""boot_diff.py - compare two runtime boot logs.

Every iteration of the port is "change something, boot 60 s, compare with the
previous run". Doing that by eye is how you miss a subsystem that quietly
stopped working, so this reports:

  * per-event-kind counts, before vs after, with deltas
  * event kinds that appeared or disappeared entirely
  * the unhandled-MMIO address histogram (a new address is a new hardware path)
  * the "frontier": the last real events before the log degenerates into
    dispatch/spin noise, which is usually where the guest is actually stuck

Usage:
    python tools/boot_diff.py logs/boot-before.log logs/boot-after.log
    python tools/boot_diff.py old.log new.log --frontier 25
"""
from __future__ import annotations

import argparse
import collections
import re

# Lines that mean "the guest is idling in the interrupt path" rather than work.
NOISE = (re.compile(r"^Dispatcher PC"), re.compile(r"^Spinning at PC"),
         re.compile(r"^\[Heartbeat\]"), re.compile(r"^\[DIAG\]"))
BRACKET = re.compile(r"^(\[[^\]]+\](?:\s+\[[^\]]+\])?)")
MMIO = re.compile(r"Unhandled (read|write)(\d+) at 0x([0-9a-fA-F]+)")


def kind(line):
    match = BRACKET.match(line)
    if match:
        return " ".join(match.group(1).split())
    head = line.split(":", 1)[0]
    return head if len(head) < 40 else line[:40]


def counts(path):
    per_kind = collections.Counter()
    mmio = collections.Counter()
    total = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            total += 1
            per_kind[kind(line)] += 1
            match = MMIO.search(line)
            if match:
                mmio["%s%s 0x%s" % (match.group(1), match.group(2),
                                    match.group(3).lower())] += 1
    return per_kind, mmio, total


def frontier(path, limit):
    """Last lines that are not idle noise, in order."""
    keep = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if any(pattern.match(line) for pattern in NOISE):
                continue
            keep.append(line)
    return keep[-limit:]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--frontier", type=int, default=15,
                        help="show this many of the last non-idle lines (0 = skip)")
    args = parser.parse_args()

    before, before_mmio, before_total = counts(args.before)
    after, after_mmio, after_total = counts(args.after)

    print("lines: before %d, after %d\n" % (before_total, after_total))
    keys = sorted(set(before) | set(after), key=lambda k: -max(before[k], after[k]))
    print("%-34s %8s %8s %8s" % ("event kind", "before", "after", "delta"))
    for key in keys:
        delta = after[key] - before[key]
        if before[key] == 0 or after[key] == 0 or abs(delta) > 2:
            flag = "  <-- new" if before[key] == 0 else ("  <-- gone" if after[key] == 0 else "")
            print("%-34s %8d %8d %+8d%s" % (key[:34], before[key], after[key], delta, flag))

    print("\nunhandled MMIO addresses:")
    for key in sorted(set(before_mmio) | set(after_mmio)):
        if before_mmio[key] or after_mmio[key]:
            print("  %-24s before %3d  after %3d" % (key, before_mmio[key], after_mmio[key]))

    if args.frontier:
        for label, path in (("before", args.before), ("after", args.after)):
            lines = frontier(path, args.frontier)
            print("\nfronterror: last %d non-idle lines (%s)" % (len(lines), label))
            for line in lines:
                print("  " + line[:150])


if __name__ == "__main__":
    main()
