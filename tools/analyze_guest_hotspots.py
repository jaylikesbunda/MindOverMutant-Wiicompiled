"""Rank the guest's hot spots from a Mind over Mutant runtime log.

The runtime's sampled diagnostic ([Sample], NWII_SAMPLE=1) reports the guest
PC and link register every few hundred thousand polls. Because the samples
are taken at a fixed cadence, the number of samples at a PC is a proxy for the
time the guest spends there - the same estimator a sampling profiler uses, but
with a much denser sample than a usual 1 kHz profiler.

When the line carries ``inst=`` (the guest's retired-instruction counter) the
instructions executed between two consecutive samples can be attributed to the
earlier sample's PC, which is a sharper weight than the sample count.

If the run used the poll census (NWII_PERF_CENSUS=1, NWII_PERF_PC_HISTOGRAM=1)
the exact per-site table is used instead when it is available: the census
counts every poll and attributes the guest time and the guest instructions
between two polls to the poll site that started them.

Usage:
    python tools/analyze_guest_hotspots.py logs/boot-s11a.log
    python tools/analyze_guest_hotspots.py logs/boot-perf.log --pcs logs/boot-perf.pcs.csv

Writes <log>.hotspots.md and <log>.hotspots.json next to the log.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mom_perf_log as loglib


def collect(path):
    """Read the log, returning the samples, the heartbeats and the census."""
    samples = []
    heartbeats = []
    census = []
    pcs = []
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("[Sample]"):
            sample = loglib.parse_sample(line)
            if sample:
                samples.append(sample)
        elif line.startswith("[Heartbeat]"):
            beat = loglib.parse_heartbeat(line)
            if beat:
                heartbeats.append(beat)
        elif line.startswith("[PERFCENSUS]"):
            record = loglib.parse_perfcensus(line)
            if record:
                census.append(record)
        elif line.startswith("[PERFPCS]"):
            record = loglib.parse_perfpcs(line)
            if record:
                pcs.append(record)
    return samples, heartbeats, census, pcs


def from_samples(samples):
    """Rank the hot spots out of the sampled diagnostic."""
    by_pc = collections.Counter()
    by_pair = collections.Counter()
    callers = collections.defaultdict(collections.Counter)
    inst_by_pc = collections.Counter()
    inst_total = 0
    for index, sample in enumerate(samples):
        by_pc[sample["pc"]] += 1
        by_pair[(sample["pc"], sample["lr"])] += 1
        callers[sample["pc"]][sample["lr"]] += 1
        # The instructions executed between two samples belong to the region
        # that follows the earlier poll, so they are charged to the earlier
        # sample's PC.
        if index and samples[index - 1]["inst"] is not None and sample["inst"] is not None:
            delta = sample["inst"] - samples[index - 1]["inst"]
            if delta > 0:
                inst_by_pc[samples[index - 1]["pc"]] += delta
                inst_total += delta
    return {
        "by_pc": by_pc,
        "by_pair": by_pair,
        "callers": callers,
        "inst_by_pc": inst_by_pc,
        "inst_total": inst_total,
    }


def from_census_table(rows):
    """Rank the hot spots out of the census per-site table."""
    by_pc = collections.Counter()
    inst_by_pc = collections.Counter()
    exec_by_pc = collections.Counter()
    poll_by_pc = collections.Counter()
    samples_by_pc = collections.Counter()
    for row in rows:
        by_pc[row["pc"]] += row["polls"]
        inst_by_pc[row["pc"]] += row["instr"]
        exec_by_pc[row["pc"]] += row["exec_ns"]
        poll_by_pc[row["pc"]] += row["poll_ns"]
        samples_by_pc[row["pc"]] += row.get("poll_samples", 0)
    return {
        "by_pc": by_pc,
        "inst_by_pc": inst_by_pc,
        "exec_by_pc": exec_by_pc,
        "poll_by_pc": poll_by_pc,
        "samples_by_pc": samples_by_pc,
        "inst_total": sum(inst_by_pc.values()),
    }


def report(samples, heartbeats, census, pcs_rows, top):
    lines = []
    lines.append("# Guest hot spots")
    lines.append("")
    lines.append("Source: the runtime's sampled guest diagnostic and, when the")
    lines.append("run used the poll census, the census per-site table.")
    lines.append("")

    census_rows = from_census_table(pcs_rows) if pcs_rows else None
    sampled = from_samples(samples) if samples else None

    lines.append("## What was measured")
    lines.append("")
    lines.append("| Quantity | Value |")
    lines.append("|---|---:|")
    lines.append("| Sampled guest observations | %d |" % len(samples))
    lines.append("| Heartbeats | %d |" % len(heartbeats))
    lines.append("| Census reports | %d |" % len(census))
    if census:
        last = census[-1]
        lines.append("| Polls counted by the census | %d |" % last["polls"])
        lines.append("| Guest instructions counted | %d |" % last["instr"])
        lines.append("| Poll rate | %.0f /s |" % last["polls_s"])
        lines.append("| Instruction rate | %.0f /s |" % last["instr_s"])
    lines.append("")

    if census_rows:
        total_polls = sum(census_rows["by_pc"].values()) or 1
        total_exec = sum(census_rows["exec_by_pc"].values()) or 1
        total_inst = census_rows["inst_total"] or 1
        lines.append("## Hot poll sites (exact counts, from the census)")
        lines.append("")
        lines.append("The census counts *every* poll. The instructions and the wall")
        lines.append("clock between two consecutive polls belong to the loop whose")
        lines.append("backward branch is the earlier poll site, so they are charged")
        lines.append("to that site.")
        lines.append("")
        lines.append("| # | PC | Polls | Poll share | Instructions | Instr share | Guest ms | Time share |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|---:|")
        ranked = sorted(census_rows["by_pc"].items(),
                        key=lambda item: census_rows["exec_by_pc"][item[0]], reverse=True)
        for rank, (pc, polls) in enumerate(ranked[:top], 1):
            lines.append("| %d | 0x%08X | %d | %.2f%% | %d | %.2f%% | %.1f | %.2f%% |" % (
                rank, pc, polls, 100.0 * polls / total_polls,
                census_rows["inst_by_pc"][pc], 100.0 * census_rows["inst_by_pc"][pc] / total_inst,
                census_rows["exec_by_pc"][pc] / 1e6,
                100.0 * census_rows["exec_by_pc"][pc] / total_exec))
        lines.append("")

    if sampled and sampled["by_pc"]:
        total = sum(sampled["by_pc"].values()) or 1
        lines.append("## Hot guest PCs (sampled)")
        lines.append("")
        lines.append("The sample cadence is fixed, so the sample share is a proxy for")
        lines.append("the time share. The instruction share is the sharper number:")
        lines.append("it counts the guest instructions executed between two samples")
        lines.append("and charges them to the earlier sample's PC.")
        lines.append("")
        inst_total = sampled["inst_total"]
        lines.append("| # | PC | Samples | Sample share | Instructions | Instr share | Most frequent caller |")
        lines.append("|---:|---|---:|---:|---:|---:|---|")
        ranked = sampled["by_pc"].most_common(top)
        for rank, (pc, count) in enumerate(ranked, 1):
            caller = sampled["callers"][pc].most_common(1)
            caller_text = "0x%08X" % caller[0][0] if caller else "-"
            inst = sampled["inst_by_pc"][pc]
            inst_text = ("%d (%.2f%%)" % (inst, 100.0 * inst / inst_total)) if inst_total else "-"
            lines.append("| %d | 0x%08X | %d | %.2f%% | %s | %s |" % (
                rank, pc, count, 100.0 * count / total, inst_text, caller_text))
        lines.append("")

        lines.append("## Hot (PC, LR) pairs")
        lines.append("")
        lines.append("| # | PC | LR | Samples | Share |")
        lines.append("|---:|---|---|---:|---:|")
        for rank, ((pc, lr), count) in enumerate(sampled["by_pair"].most_common(top), 1):
            lines.append("| %d | 0x%08X | 0x%08X | %d | %.2f%% |" % (
                rank, pc, lr, count, 100.0 * count / total))
        lines.append("")

    if not samples and not pcs_rows:
        lines.append("No guest samples and no census table were found in this log.")
        lines.append("Run with NWII_SAMPLE=1 for the samples, or with")
        lines.append("NWII_PERF_CENSUS=1 NWII_PERF_PC_HISTOGRAM=1 and")
        lines.append("NWII_PERF_CENSUS_FILE=<path> for the exact table.")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- The sampled diagnostic observes the guest at poll points, not")
    lines.append("  at arbitrary program points, so a PC is a poll site and the")
    lines.append("  cost charged to it is the guest's work between two polls.")
    lines.append("- The census itself costs a hash lookup (and a clock read) per")
    lines.append("  poll, so a census run is slower than a plain run; compare the")
    lines.append("  frame times of the two before reading the numbers as absolute.")
    lines.append("- The guest's instruction counter is the runtime's own counter,")
    lines.append("  incremented by the generated code once per guest instruction")
    lines.append("  and once per dispatch; it is not a hardware retired count.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="runtime log (stdout of MindOverMutant.exe)")
    parser.add_argument("--pcs", help="census per-site table (NWII_PERF_CENSUS_FILE)")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", help="output prefix (default: <log>)")
    args = parser.parse_args()

    samples, heartbeats, census, pcs_lines = collect(args.log)
    rows = loglib.read_pcs_table(args.pcs) if args.pcs else []

    prefix = args.out or args.log
    md_path = prefix + ".hotspots.md"
    json_path = prefix + ".hotspots.json"

    text = report(samples, heartbeats, census, rows, args.top)
    with open(md_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    sampled = from_samples(samples) if samples else None
    payload = {
        "log": args.log,
        "samples": len(samples),
        "heartbeats": len(heartbeats),
        "census": census,
        "census_sites": rows,
        "hot_pcs": [
            {"pc": "0x%08X" % pc, "samples": count}
            for pc, count in (sampled["by_pc"].most_common(args.top) if sampled else [])
        ],
        "hot_pairs": [
            {"pc": "0x%08X" % pc, "lr": "0x%08X" % lr, "samples": count}
            for (pc, lr), count in (sampled["by_pair"].most_common(args.top) if sampled else [])
        ],
    }
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")

    print(text)
    print("wrote %s and %s" % (md_path, json_path))


if __name__ == "__main__":
    main()
