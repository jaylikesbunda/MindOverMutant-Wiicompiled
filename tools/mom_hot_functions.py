"""Derive MoM's hot generated-function list from this port's own guest profile.

Input: a CSV written by the perf census (NWII_PERF_CENSUS=1,
NWII_PERF_PC_HISTOGRAM=1, NWII_PERF_CENSUS_FILE=...).  Each row is one guest poll
site - a backward branch - and the number of guest instructions executed by the
loop that ends there, so the file is an exact (not sampled) attribution of guest
work to poll sites.

The site is mapped to its generated function exactly the way run_game's
dispatcher would (exact g_dispatch_overrides entry first, else the first
g_func_bounds range whose prefix-maximum end passes the pc; both tables are read
out of export/main_output.cpp), and the per-function totals are ranked.

Writes tools/hot_functions_mom.txt, which tools/stages/promote_regs_mom.py reads.
Nothing else in the tree is touched.

Usage:
    python tools/mom_hot_functions.py logs/promote-profile.pcs.csv [--coverage 0.90]
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tools' / 'hot_functions_mom.txt'
BOUND = re.compile(r'^\s*\{(0x[0-9A-Fa-f]+),\s*(0x[0-9A-Fa-f]+),\s*(\w+)\},', re.M)
OVERRIDE = re.compile(r'^\s*\{(0x[0-9A-Fa-f]+),\s*(\w+),\s*(false|true)\},', re.M)

HEADER = """\
# NATIVE_MOM_HOT_FUNCTIONS_V1
# One generated guest function per line, hottest first.
#
# Derived from this port's own profile, not from CotT.  A 90 s run of the staged
# binary with the perf census on (NWII_PERF_CENSUS=1, NWII_PERF_PC_HISTOGRAM=1)
# records, per guest poll site (every backward branch), the exact number of guest
# instructions executed by the loop that ends there.  Each poll site is mapped to
# its function with the dispatcher's own lookup (exact g_dispatch_overrides entry
# first, else the first g_func_bounds range whose prefix-maximum end passes the
# pc, both read out of export/main_output.cpp), and the per-function totals are
# what is ranked here.  The list stops where the cumulative instruction share
# reaches the requested coverage (90% by default).
#
# Regenerate with: python tools/mom_hot_functions.py <census.csv>
"""


def parse_tables(main_text):
    bounds = [(int(a, 16), int(b, 16), n) for a, b, n in BOUND.findall(main_text)]
    overrides = {int(a, 16): n for a, n, _ in OVERRIDE.findall(main_text)}
    if len(bounds) < 1000 or len(overrides) < 10:
        raise SystemExit('dispatch tables not found in export/main_output.cpp')
    ends = []
    high = 0
    for _, end, _ in bounds:
        high = max(high, end)
        ends.append(high)
    return bounds, ends, overrides


def make_lookup(bounds, ends, overrides):
    def lookup(pc):
        if pc in overrides:
            return overrides[pc]
        lo = bisect.bisect_right(ends, pc)
        if lo >= len(bounds) or bounds[lo][0] > pc:
            return None
        return bounds[lo][2]
    return lookup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--coverage', type=float, default=0.90)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args()

    text = (ROOT / 'export' / 'main_output.cpp').read_text(errors='surrogateescape')
    lookup = make_lookup(*parse_tables(text))

    rows = []
    with args.csv.open() as handle:
        for row in csv.DictReader(handle):
            rows.append((int(row['instr']), int(row['pc'], 16)))
    total = sum(instr for instr, _ in rows)
    per = collections.Counter()
    unmapped = 0
    for instr, pc in rows:
        name = lookup(pc)
        if name is None:
            unmapped += instr
        else:
            per[name] += instr
    order = per.most_common()
    covered = 0
    keep = []
    for name, instr in order:
        keep.append(name)
        covered += instr
        if covered >= args.coverage * total:
            break

    lines = [HEADER]
    lines.extend(name + '\n' for name in keep)
    args.out.write_text(''.join(lines))
    print('%s: %d sites, %.3fB instructions attributed, %.2f%% unmapped'
          % (args.csv, len(rows), total / 1e9, 100.0 * unmapped / total))
    print('%d functions cover %.1f%% of the attributed instructions -> %s'
          % (len(keep), 100.0 * covered / total, args.out.relative_to(ROOT)))


if __name__ == '__main__':
    main()
