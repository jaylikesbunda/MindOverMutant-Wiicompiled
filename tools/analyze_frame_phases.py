"""Break a Mind over Mutant runtime log down into frame time and its phases.

Three sources are combined, and each is optional:

  [STAT] lines    five-second windows of the host's own counters: how many
                  field drains happened, how many XFB copies were latched, how
                  many draws were decoded, and how many milliseconds went into
                  the FIFO parse and into the GL render.
  [GXPROF] lines  two-second windows of the GX work: parse, decode, display
                  list calls, snapshot time.
  [PERFPHASE]     the frame/phase timeline added by
                  tools/stages/fix_perf_phase_trace.py (NWII_PERF_PHASE=1),
                  plus the per-frame CSV it writes
                  (NWII_PERF_PHASE_FILE=<path>).

The [DIAG] lines are used for the guest's own clock: the difference between
the wall clock and the guest time base is the guest's time drift, which is
the cheapest available check that the guest is not falling behind.

Usage:
    python tools/analyze_frame_phases.py logs/boot-perf.log
    python tools/analyze_frame_phases.py logs/boot-perf.log --frames logs/boot-perf.frames.csv

Writes <log>.phases.md and <log>.phases.json next to the log.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mom_perf_log as loglib

# The field budget: the runtime paces the host loop to the display field
# period (NATIVE_FIELD_PACING_V1). 20 ms for PAL, 16.67 ms for NTSC.
PAL_FIELD_MS = 20.0
NTSC_FIELD_MS = 1000.0 / 60.0


def collect(path):
    stats = []
    gxprofs = []
    diags = []
    phases = []
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("[STAT]"):
            record = loglib.parse_stat(line)
            if record:
                stats.append(record)
        elif line.startswith("[GXPROF]"):
            record = loglib.parse_gxprof(line)
            if record:
                gxprofs.append(record)
        elif line.startswith("[DIAG]"):
            record = loglib.parse_diag(line)
            if record:
                diags.append(record)
        elif line.startswith("[PERFPHASE]"):
            record = loglib.parse_perfphase(line)
            if record:
                phases.append(record)
    return stats, gxprofs, diags, phases


def stat_windows(stats):
    """Per-window frame numbers out of the [STAT] lines.

    The runtime resets its parse and render accumulators after every [STAT]
    line, so those two are already per-window; the frame, copy and draw
    counters are cumulative and are differenced here.
    """
    rows = []
    previous = None
    for record in stats:
        if previous is None:
            previous = record
            continue
        window_s = record["seconds"] - previous["seconds"]
        if window_s <= 0:
            previous = record
            continue
        frames = record["frames"] - previous["frames"]
        copies = record["xfb_copies"] - previous["xfb_copies"]
        draws = record["draws"] - previous["draws"]
        parse_ms = record["parse_ms"]
        render_ms = record["render_ms"]
        rows.append({
            "window_s": window_s,
            "frames": frames,
            "xfb_copies": copies,
            "draws": draws,
            "parse_ms": parse_ms,
            "render_ms": render_ms,
            "drains_per_s": frames / window_s,
            "xfb_per_s": copies / window_s,
            "draws_per_s": draws / window_s,
            "parse_ms_per_frame": parse_ms / frames if frames else 0.0,
            "render_ms_per_frame": render_ms / frames if frames else 0.0,
            "draws_per_frame": draws / frames if frames else 0.0,
        })
        previous = record
    return rows


def diag_windows(diags):
    """Per-window guest and hardware counters out of the [DIAG] lines."""
    rows = []
    previous = None
    for record in diags:
        if previous is None:
            previous = record
            continue
        window_s = record["wall"] - previous["wall"]
        if window_s <= 0:
            previous = record
            continue
        rows.append({
            "window_s": window_s,
            "guest_tb_s": record["guest_tb_s"] - previous["guest_tb_s"],
            "drift_s": (record["guest_tb_s"] - previous["guest_tb_s"]) - window_s,
            "vi_per_s": (record["vi"] - previous["vi"]) / window_s,
            "finishes_per_s": (record["finishes"] - previous["finishes"]) / window_s,
            "tokens_per_s": (record["tokens"] - previous["tokens"]) / window_s,
            "dec_fires_per_s": (record["dec_fires"] - previous["dec_fires"]) / window_s,
        })
        previous = record
    return rows


def phase_rows(phases):
    """The [PERFPHASE] reports."""
    rows = []
    for record in phases:
        row = {
            "t": record["t"],
            "window": record["window"],
            "frames": record["frames"],
            "frame_ms": record["frame_ms"],
            "p95": record["p95"],
            "max": record["max"],
            "host_sum": record["host_sum"],
            "guest_cpu_ms_s": record["guest_cpu_ms_s"],
        }
        row.update(record["phases"])
        rows.append(row)
    return rows


def frame_stats(frames):
    """The distribution of the per-frame timeline CSV."""
    if not frames:
        return None
    wall = [f.get("wall_ms", 0.0) for f in frames]
    stats = {
        "frames": len(frames),
        "wall_ms_mean": loglib.mean(wall),
        "wall_ms_p50": loglib.percentile(wall, 0.50),
        "wall_ms_p95": loglib.percentile(wall, 0.95),
        "wall_ms_p99": loglib.percentile(wall, 0.99),
        "wall_ms_max": max(wall),
    }
    phases = [key for key in frames[0] if key.endswith("_ms") and key != "wall_ms"]
    for key in phases:
        values = [f.get(key, 0.0) for f in frames]
        stats[key + "_mean"] = loglib.mean(values)
        stats[key + "_p95"] = loglib.percentile(values, 0.95)
        stats[key + "_max"] = max(values)
    return stats


def report(stats_rows, gxprofs, diag_rows, phase_list, frames, budget_ms, windows=20):
    lines = []
    lines.append("# Frame time and phase breakdown")
    lines.append("")
    lines.append("The field budget is %.2f ms (%s). The host loop is field-paced," % (
        budget_ms, "PAL" if budget_ms > 18.0 else "NTSC"))
    lines.append("so a frame that costs more than the budget cannot keep up.")
    lines.append("")

    if stats_rows:
        lines.append("## Host counters per window ([STAT])")
        lines.append("")
        lines.append("`frames` counts the host's field drains, not presented images:")
        lines.append("the guest decides how much of each field it fills.")
        lines.append("")
        lines.append("| Window s | Drains/s | XFB/s | Draws/s | Draws/frame | Parse ms/frame | Render ms/frame | Parse+render % of budget |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in stats_rows[-windows:]:
            busy = row["parse_ms_per_frame"] + row["render_ms_per_frame"]
            lines.append("| %.0f | %.1f | %.1f | %.0f | %.1f | %.3f | %.3f | %.1f%% |" % (
                row["window_s"], row["drains_per_s"], row["xfb_per_s"],
                row["draws_per_s"], row["draws_per_frame"],
                row["parse_ms_per_frame"], row["render_ms_per_frame"],
                100.0 * busy / budget_ms if budget_ms else 0.0))
        lines.append("")

    if gxprofs:
        lines.append("## GX work per window ([GXPROF])")
        lines.append("")
        lines.append("| Parse+decode ms | Decode ms | DL calls | DL bytes | DL ms | Snapshot ms | Unknown | Carry |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in gxprofs[-windows:]:
            lines.append("| %.1f | %s | %d | %d | %.1f | %.1f | %d | %d |" % (
                row["parse_decode_ms"],
                ("%.1f" % row["decode_ms"]) if row["decode_ms"] is not None else "-",
                row["dl_calls"], row["dl_bytes"], row["dl_ms"], row["snap_ms"],
                row["unknown"], row["carry"]))
        lines.append("")

    if phase_list:
        lines.append("## Phase timeline ([PERFPHASE])")
        lines.append("")
        lines.append("All values are milliseconds per frame over the report window.")
        lines.append("`host_sum` is the host's render work; the guest thread runs")
        lines.append("concurrently and is not part of it.")
        lines.append("")
        lines.append("| t s | Frames | Frame ms | p95 | Max | gx_parse | gx_decode | gl_render | xfb_copy | present | swap | host_field | drain_wait | guest_idle | host_sum | Guest CPU ms/s |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in phase_list:
            lines.append("| %.0f | %d | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f | %.0f |" % (
                row["t"], row["frames"], row["frame_ms"], row["p95"], row["max"],
                row.get("gx_parse", 0.0), row.get("gx_decode", 0.0),
                row.get("gl_render", 0.0), row.get("xfb_copy", 0.0),
                row.get("present", 0.0), row.get("swap", 0.0),
                row.get("host_field", 0.0), row.get("host_drain_wait", 0.0),
                row.get("guest_idle", 0.0), row["host_sum"], row["guest_cpu_ms_s"]))
        lines.append("")

    if frames:
        stats = frame_stats(frames)
        lines.append("## Per-frame timeline (CSV)")
        lines.append("")
        lines.append("| Measurement | Mean | p50 | p95 | p99 | Max |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        lines.append("| Frame wall ms | %.3f | %.3f | %.3f | %.3f | %.3f |" % (
            stats["wall_ms_mean"], stats["wall_ms_p50"], stats["wall_ms_p95"],
            stats["wall_ms_p99"], stats["wall_ms_max"]))
        for key in sorted(k for k in stats if k.endswith("_mean")):
            base = key[:-len("_mean")]
            lines.append("| %s | %.3f | | %.3f | | %.3f |" % (
                base, stats[key], stats.get(base + "_p95", 0.0), stats.get(base + "_max", 0.0)))
        lines.append("")

    if diag_rows:
        lines.append("## Guest clock and hardware ([DIAG])")
        lines.append("")
        lines.append("`drift` is the guest time base minus the wall clock over the")
        lines.append("window: a guest that falls behind shows a growing negative")
        lines.append("drift.")
        lines.append("")
        lines.append("| Window s | Guest s | Drift s | VI/s | PE finishes/s | Tokens/s | Dec fires/s |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|")
        for row in diag_rows[-windows:]:
            lines.append("| %.1f | %.3f | %+.3f | %.1f | %.1f | %.1f | %.0f |" % (
                row["window_s"], row["guest_tb_s"], row["drift_s"],
                row["vi_per_s"], row["finishes_per_s"], row["tokens_per_s"],
                row["dec_fires_per_s"]))
        lines.append("")

    if not stats_rows and not phase_list and not frames:
        lines.append("No frame data found in this log.")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- The phase timers are wall clock on the host thread, so a")
    lines.append("  preempted host thread inflates the phase it was in.")
    lines.append("- The guest thread runs concurrently with the host loop; its")
    lines.append("  execution time is not part of any frame phase, so the")
    lines.append("  difference between the frame wall time and host_sum is the")
    lines.append("  host's own overhead plus whatever the guest did meanwhile.")
    lines.append("- A field drain is not a presented image: the guest decides how")
    lines.append("  much of each field it fills, and the number of XFB copies is")
    lines.append("  the number of images the guest actually completed.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="runtime log (stdout of MindOverMutant.exe)")
    parser.add_argument("--frames", help="per-frame timeline CSV (NWII_PERF_PHASE_FILE)")
    parser.add_argument("--field-ms", type=float, default=NTSC_FIELD_MS,
                        help="the field period in ms (default %.3f)" % NTSC_FIELD_MS)
    parser.add_argument("--out", help="output prefix (default: <log>)")
    args = parser.parse_args()

    stats, gxprofs, diags, phases = collect(args.log)
    frames = loglib.read_frames_csv(args.frames) if args.frames else []

    budget = args.field_ms
    # A PAL run paces to 20 ms; the runtime prints the nominal field period.
    for line in open(args.log, "r", encoding="utf-8", errors="replace"):
        if line.startswith("[VI cadence]"):
            try:
                budget = float(line.split("field_ns=")[1].split()[0]) / 1e6
            except (IndexError, ValueError):
                pass
            break

    prefix = args.out or args.log
    md_path = prefix + ".phases.md"
    json_path = prefix + ".phases.json"

    stats_rows = stat_windows(stats)
    diag_rows = diag_windows(diags)
    phase_list = phase_rows(phases)

    text = report(stats_rows, gxprofs, diag_rows, phase_list, frames, budget)
    with open(md_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    payload = {
        "log": args.log,
        "field_budget_ms": budget,
        "stat_windows": stats_rows,
        "gxprof_windows": gxprofs,
        "diag_windows": diag_rows,
        "phase_windows": phase_list,
        "frame_stats": frame_stats(frames) if frames else None,
    }
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")

    print(text)
    print("wrote %s and %s" % (md_path, json_path))


if __name__ == "__main__":
    main()
