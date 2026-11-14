"""One-shot performance summary for a Mind over Mutant runtime log.

Runs the three detailed analyses over one log and writes a single headline
report:

  * the guest hot spots        (tools/analyze_guest_hotspots.py)
  * the frame time breakdown   (tools/analyze_frame_phases.py)
  * the poll and instruction rate (tools/analyze_poll_rate.py)

The detailed reports are written next to the log as well, so the summary is
the index and not the only output.

Usage:
    python tools/analyze_mom_perf.py logs/boot-perf.log
    python tools/analyze_mom_perf.py logs/boot-perf.log --pcs logs/boot-perf.pcs.csv \\
        --frames logs/boot-perf.frames.csv
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mom_perf_log as loglib
import analyze_guest_hotspots as hotspots
import analyze_frame_phases as phases
import analyze_poll_rate as pollrate


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="runtime log (stdout of MindOverMutant.exe)")
    parser.add_argument("--pcs", help="census per-site table (NWII_PERF_CENSUS_FILE)")
    parser.add_argument("--frames", help="per-frame timeline CSV (NWII_PERF_PHASE_FILE)")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--out", help="output prefix (default: <log>)")
    args = parser.parse_args()

    prefix = os.path.abspath(args.out or args.log)
    log_path = os.path.abspath(args.log)
    pcs_path = os.path.abspath(args.pcs) if args.pcs else None
    frames_path = os.path.abspath(args.frames) if args.frames else None

    # The detailed tools, so their reports exist next to the summary.
    for name, extra in (("analyze_guest_hotspots.py", ["--pcs", pcs_path] if pcs_path else []),
                        ("analyze_frame_phases.py", ["--frames", frames_path] if frames_path else []),
                        ("analyze_poll_rate.py", [])):
        command = [sys.executable, os.path.join(HERE, name), log_path, "--out", prefix]
        command += extra
        result = subprocess.run(command, cwd=HERE, capture_output=True, text=True)
        if result.returncode:
            print("warning: %s failed: %s" % (name, (result.stderr or "").strip()[-300:]))

    samples, heartbeats, census, pcs_lines = hotspots.collect(log_path)
    stats, gxprofs, diags, phase_lines = phases.collect(log_path)
    rows = loglib.read_pcs_table(pcs_path) if pcs_path else []
    frames = loglib.read_frames_csv(frames_path) if frames_path else []

    stat_rows = phases.stat_windows(stats)
    diag_rows = phases.diag_windows(diags)
    phase_rows = phases.phase_rows(phase_lines)
    sampled = pollrate.sampled_rates(samples, diags)
    clock = pollrate.guest_clock(diags)
    census_rows = hotspots.from_census_table(rows) if rows else None

    lines = []
    lines.append("# Mind over Mutant - performance summary")
    lines.append("")
    lines.append("Log: `%s`" % args.log)
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append("| Measurement | Value |")
    lines.append("|---|---:|")
    if census:
        last = census[-1]
        total_wall = clock["wall_s"] if clock else 0.0
        lines.append("| Guest poll rate | %.0f polls/s (whole run) |" % (
            last["polls"] / total_wall if total_wall else last["polls_s"]))
        lines.append("| Guest instruction rate | %.0f instructions/s (whole run) |" % (
            last["instr"] / total_wall if total_wall else last["instr_s"]))
        lines.append("| Polls per guest instruction | %.2f (one poll per %.1f instructions) |" % (
            last["polls"] / last["instr"] if last["instr"] else 0.0,
            last["instr"] / last["polls"] if last["polls"] else 0.0))
        lines.append("| Cost of one poll | %.0f ns (median of the sampled polls) |" % (
            last["poll_us_p50"] * 1000.0))
    if sampled:
        lines.append("| Guest poll rate (sampled estimate) | %.0f polls/s |" % sampled["polls_s"])
        lines.append("| Guest instruction rate (sampled estimate) | %.0f instructions/s |" % sampled["instr_s"])
    if phase_rows:
        last_phase = phase_rows[-1]
        lines.append("| Frame time | %.2f ms (p95 %.2f ms, max %.2f ms) |" % (
            last_phase["frame_ms"], last_phase["p95"], last_phase["max"]))
        lines.append("| Host render work per frame | %.2f ms |" % last_phase["host_sum"])
        lines.append("| Guest thread CPU | %.0f ms/s |" % last_phase["guest_cpu_ms_s"])
    if stat_rows:
        last_stat = stat_rows[-1]
        lines.append("| Field drains | %.1f /s |" % last_stat["drains_per_s"])
        lines.append("| XFB copies (completed images) | %.1f /s |" % last_stat["xfb_per_s"])
        lines.append("| Draws | %.0f /s (%.1f per frame) |" % (
            last_stat["draws_per_s"], last_stat["draws_per_frame"]))
        lines.append("| GX parse per frame | %.3f ms |" % last_stat["parse_ms_per_frame"])
        lines.append("| GL render per frame | %.3f ms |" % last_stat["render_ms_per_frame"])
    if clock:
        lines.append("| Guest clock drift | %+.3f s over %.1f s (%+.2f%%) |" % (
            clock["drift_s"], clock["wall_s"], clock["drift_pct"]))
    lines.append("")

    if phase_rows:
        lines.append("## Frame phases (last window, ms per frame)")
        lines.append("")
        lines.append("| Phase | ms |")
        lines.append("|---|---:|")
        for key in ("gx_parse", "gx_decode", "gl_render", "xfb_copy", "present",
                    "swap", "host_field", "host_drain_wait", "guest_idle"):
            lines.append("| %s | %.3f |" % (key, phase_rows[-1].get(key, 0.0)))
        lines.append("")

    if census_rows:
        total_exec = sum(census_rows["exec_by_pc"].values()) or 1
        lines.append("## Top guest hot spots (exact, from the poll census)")
        lines.append("")
        lines.append("| # | PC | Polls | Instructions | Guest ms | Time share |")
        lines.append("|---:|---|---:|---:|---:|---:|")
        ranked = sorted(census_rows["by_pc"].items(),
                        key=lambda item: census_rows["exec_by_pc"][item[0]], reverse=True)
        for rank, (pc, polls) in enumerate(ranked[:args.top], 1):
            lines.append("| %d | 0x%08X | %d | %d | %.1f | %.2f%% |" % (
                rank, pc, polls, census_rows["inst_by_pc"][pc],
                census_rows["exec_by_pc"][pc] / 1e6,
                100.0 * census_rows["exec_by_pc"][pc] / total_exec))
        lines.append("")
    elif samples:
        sampled_data = hotspots.from_samples(samples)
        total = sum(sampled_data["by_pc"].values()) or 1
        lines.append("## Top guest hot spots (sampled)")
        lines.append("")
        lines.append("| # | PC | Samples | Share | Most frequent caller |")
        lines.append("|---:|---|---:|---:|---|")
        for rank, (pc, count) in enumerate(sampled_data["by_pc"].most_common(args.top), 1):
            caller = sampled_data["callers"][pc].most_common(1)
            lines.append("| %d | 0x%08X | %d | %.2f%% | %s |" % (
                rank, pc, count, 100.0 * count / total,
                ("0x%08X" % caller[0][0]) if caller else "-"))
        lines.append("")

    lines.append("## Detailed reports")
    lines.append("")
    for suffix, name in ((".hotspots.md", "guest hot spots"),
                         (".phases.md", "frame time and phases"),
                         (".pollrate.md", "poll and instruction rate")):
        lines.append("- `%s%s` - %s" % (os.path.basename(prefix), suffix, name))
    lines.append("")

    md_path = prefix + ".perf.md"
    json_path = prefix + ".perf.json"
    text = "\n".join(lines) + "\n"
    with open(md_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    payload = {
        "log": args.log,
        "census": census,
        "sampled": sampled,
        "guest_clock": clock,
        "phase_windows": phase_rows,
        "stat_windows": stat_rows,
        "diag_windows": diag_rows,
        "census_sites": rows,
    }
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")

    print(text)
    print("wrote %s and %s" % (md_path, json_path))


if __name__ == "__main__":
    main()
