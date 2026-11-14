"""Measure the guest's poll rate and instruction rate from a runtime log.

The guest polls the host at every backward branch, so the poll rate is this
runtime's closest thing to a "guest loop iterations per second" meter. The
generated code increments the guest's retired-instruction counter once per
guest instruction, so the same log gives an instruction rate.

Two sources are combined:

  [Sample] lines  carry the guest PC, the guest's instruction counter and the
                  number of fast polls so far. Consecutive samples give the
                  polls and the instructions executed between them; the wall
                  clock comes from the [DIAG] lines, which are printed every
                  five seconds.
  [PERFCENSUS]    the exact counts from the poll census
                  (NWII_PERF_CENSUS=1): polls, fast polls, slow polls,
                  instructions, and the cost of the poll itself.

The census is preferred when the log has it: it counts every poll, with no
sampling and no window-edge error.

Usage:
    python tools/analyze_poll_rate.py logs/boot-perf.log

Writes <log>.pollrate.md and <log>.pollrate.json next to the log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mom_perf_log as loglib


def collect(path):
    samples = []
    diags = []
    census = []
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("[Sample]"):
            record = loglib.parse_sample(line)
            if record:
                samples.append(record)
        elif line.startswith("[DIAG]"):
            record = loglib.parse_diag(line)
            if record:
                diags.append(record)
        elif line.startswith("[PERFCENSUS]"):
            record = loglib.parse_perfcensus(line)
            if record:
                census.append(record)
    return samples, diags, census


def sampled_rates(samples, diags):
    """Poll and instruction rates out of the sampled diagnostic.

    The [DIAG] lines carry the wall clock but the [Sample] lines do not, so
    the samples are bucketed by the [DIAG] window they fall into and the
    window's length is used as the time base. The edges of a window are
    inexact; the totals over the whole log are exact apart from the first and
    last sample.
    """
    if not samples or len(diags) < 2:
        return None
    wall = [record["wall"] for record in diags]
    total_wall = wall[-1] - wall[0]
    if total_wall <= 0:
        return None

    # Assign each sample to the [DIAG] window it follows.
    buckets = [[] for _ in range(len(diags))]
    index = 0
    for sample in samples:
        while index + 1 < len(diags) and wall[index + 1] <= 0:
            index += 1
        buckets[index].append(sample)

    windows = []
    for position, bucket in enumerate(buckets):
        if len(bucket) < 2 or position + 1 >= len(wall):
            continue
        span = wall[position + 1] - wall[position]
        if span <= 0:
            continue
        if bucket[0]["fast"] is None or bucket[-1]["fast"] is None:
            continue
        if bucket[0]["inst"] is None or bucket[-1]["inst"] is None:
            continue
        polls = bucket[-1]["fast"] - bucket[0]["fast"]
        instr = bucket[-1]["inst"] - bucket[0]["inst"]
        if polls < 0 or instr < 0:
            continue
        windows.append({
            "window_s": span,
            "polls": polls,
            "polls_s": polls / span,
            "instr": instr,
            "instr_s": instr / span,
            "polls_per_instr": polls / instr if instr else 0.0,
            "samples": len(bucket),
        })

    first = samples[0]
    last = samples[-1]
    have_fast = first["fast"] is not None and last["fast"] is not None
    have_inst = first["inst"] is not None and last["inst"] is not None
    total_polls = (last["fast"] - first["fast"]) if have_fast else 0
    total_instr = (last["inst"] - first["inst"]) if have_inst else 0
    if not have_fast and not have_inst:
        return None
    return {
        "total_wall_s": total_wall,
        "total_polls": total_polls,
        "total_instr": total_instr,
        "polls_s": total_polls / total_wall if have_fast else 0.0,
        "instr_s": total_instr / total_wall if have_inst else 0.0,
        "polls_per_instr": total_polls / total_instr if total_instr else 0.0,
        "samples": len(samples),
        "counters_present": have_fast and have_inst,
        "windows": windows,
    }


def guest_clock(diags):
    """The guest time base against the wall clock."""
    if len(diags) < 2:
        return None
    wall = diags[-1]["wall"] - diags[0]["wall"]
    guest = diags[-1]["guest_tb_s"] - diags[0]["guest_tb_s"]
    if wall <= 0:
        return None
    return {
        "wall_s": wall,
        "guest_s": guest,
        "drift_s": guest - wall,
        "drift_pct": 100.0 * (guest - wall) / wall,
        "guest_rate": guest / wall,
    }


def report(sampled, clock, census, top):
    lines = []
    lines.append("# Poll rate and instruction rate")
    lines.append("")
    lines.append("The guest polls the host at every backward branch. The poll")
    lines.append("rate is therefore this runtime's \"guest loop iterations per")
    lines.append("second\" meter, and the instruction rate is the guest's own")
    lines.append("retired-instruction counter, incremented by the generated code")
    lines.append("once per guest instruction.")
    lines.append("")

    if census:
        lines.append("## Exact counts (poll census)")
        lines.append("")
        lines.append("| t s | Polls | Polls/s | Fast | Slow | Instructions | Instr/s | Polls/instr | Poll us p10 | p50 | p90 |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for record in census:
            lines.append("| %.0f | %d | %.0f | %d | %d | %d | %.0f | %.3f | %.2f | %.2f | %.2f |" % (
                record["t"], record["polls"], record["polls_s"], record["fast"],
                record["slow"], record["instr"], record["instr_s"],
                record["polls_per_instr"], record["poll_us_p10"],
                record["poll_us_p50"], record["poll_us_p90"]))
        lines.append("")
        last = census[-1]
        lines.append("The census ran for %.0f s of wall clock. Its last window:" % last["t"])
        lines.append("")
        lines.append("- **%.0f polls/s** (%.0f fast, %.0f slow)" % (
            last["polls_s"], last["fast"], last["slow"]))
        lines.append("- **%.0f guest instructions/s**" % last["instr_s"])
        lines.append("- **%.2f polls per guest instruction** (one poll every %.1f instructions)" % (
            last["polls_per_instr"],
            1.0 / last["polls_per_instr"] if last["polls_per_instr"] else 0.0))
        lines.append("- one poll costs **%.0f ns at the 10th percentile, %.0f ns at the"
                     " median and %.0f ns at the 90th**" % (
                         last["poll_us_p10"] * 1000.0, last["poll_us_p50"] * 1000.0,
                         last["poll_us_p90"] * 1000.0))
        lines.append("  (the distribution, not the mean: a poll that the scheduler")
        lines.append("  interrupts would drag the mean up)")
        lines.append("")
        if clock:
            total = clock["wall_s"]
            lines.append("Over the whole run (%.0f s of wall clock) the census counted" % total)
            lines.append("%d polls and %d guest instructions:" % (last["polls"], last["instr"]))
            lines.append("")
            lines.append("| Quantity | Whole run | Last window |")
            lines.append("|---|---:|---:|")
            lines.append("| Poll rate | %.0f /s | %.0f /s |" % (
                last["polls"] / total if total else 0.0, last["polls_s"]))
            lines.append("| Instruction rate | %.0f /s | %.0f /s |" % (
                last["instr"] / total if total else 0.0, last["instr_s"]))
            lines.append("| Polls per guest instruction | %.3f | %.3f |" % (
                last["polls"] / last["instr"] if last["instr"] else 0.0,
                last["polls_per_instr"]))
            lines.append("")
            lines.append("The two differ because the run is not homogeneous: the")
            lines.append("game's menu and its scene cost very different amounts.")
            lines.append("")

    if sampled:
        lines.append("## Sampled estimate")
        lines.append("")
        lines.append("| Quantity | Value |")
        lines.append("|---|---:|")
        lines.append("| Samples | %d |" % sampled["samples"])
        lines.append("| Wall clock covered | %.1f s |" % sampled["total_wall_s"])
        lines.append("| Polls between the first and last sample | %d |" % sampled["total_polls"])
        lines.append("| Guest instructions between them | %d |" % sampled["total_instr"])
        lines.append("| Poll rate | %.0f /s |" % sampled["polls_s"])
        lines.append("| Instruction rate | %.0f /s |" % sampled["instr_s"])
        lines.append("| Polls per guest instruction | %.3f |" % sampled["polls_per_instr"])
        lines.append("")
        if sampled["windows"]:
            lines.append("### Per window")
            lines.append("")
            lines.append("| Window s | Polls | Polls/s | Instructions | Instr/s | Polls/instr | Samples |")
            lines.append("|---:|---:|---:|---:|---:|---:|---:|")
            for row in sampled["windows"]:
                lines.append("| %.0f | %d | %.0f | %d | %.0f | %.3f | %d |" % (
                    row["window_s"], row["polls"], row["polls_s"], row["instr"],
                    row["instr_s"], row["polls_per_instr"], row["samples"]))
            lines.append("")

    if clock:
        lines.append("## Guest clock")
        lines.append("")
        lines.append("| Quantity | Value |")
        lines.append("|---|---:|")
        lines.append("| Wall clock | %.2f s |" % clock["wall_s"])
        lines.append("| Guest time base | %.2f s |" % clock["guest_s"])
        lines.append("| Drift (guest - wall) | %+.3f s (%+.2f%%) |" % (
            clock["drift_s"], clock["drift_pct"]))
        lines.append("| Guest clock rate | %.6f |" % clock["guest_rate"])
        lines.append("")
        if clock["drift_pct"] < -0.5:
            lines.append("The guest clock is falling behind the wall clock: the")
            lines.append("guest is not keeping up with real time.")
        else:
            lines.append("The guest clock keeps up with the wall clock.")
        lines.append("")

    if not census and not sampled:
        lines.append("No poll or instruction data found in this log.")
        lines.append("Run with NWII_SAMPLE=1 (samples) or NWII_PERF_CENSUS=1 (exact).")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- The sampled estimate buckets the samples by the five-second")
    lines.append("  [DIAG] windows, so a window's rate is exact only in its")
    lines.append("  middle; the whole-log total is the reliable number.")
    lines.append("- The instruction counter is the runtime's own: the generated")
    lines.append("  code increments it once per guest instruction and once per")
    lines.append("  dispatch, so it slightly over-counts the instructions.")
    lines.append("- The census costs a hash lookup and a clock read per poll, so")
    lines.append("  a census run is slower than a plain run. The census's own")
    lines.append("  poll cost is reported separately above.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="runtime log (stdout of MindOverMutant.exe)")
    parser.add_argument("--out", help="output prefix (default: <log>)")
    args = parser.parse_args()

    samples, diags, census = collect(args.log)
    sampled = sampled_rates(samples, diags)
    clock = guest_clock(diags)

    prefix = args.out or args.log
    md_path = prefix + ".pollrate.md"
    json_path = prefix + ".pollrate.json"

    text = report(sampled, clock, census, 20)
    with open(md_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    payload = {
        "log": args.log,
        "census": census,
        "sampled": sampled,
        "guest_clock": clock,
    }
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")

    print(text)
    print("wrote %s and %s" % (md_path, json_path))


if __name__ == "__main__":
    main()
