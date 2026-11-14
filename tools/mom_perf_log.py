"""Shared parsers for the Mind over Mutant runtime log.

The runtime writes its diagnostics as plain text lines on stdout. This module
holds one parser per line kind, so the analysis tools agree on what a line
means. Nothing here knows the game: the formats are the runtime's, and the
guest addresses that appear in them are data, not constants.

Line kinds understood:

  [Sample] pc=0x... lr=0x... r3=0x... [msr=0x...] [cb=0|1] [inst=N] [fast=N]
      The runtime's sampled guest diagnostic (NWII_SAMPLE=1). Taken at a
      fixed cadence, so the number of samples at a PC is a proxy for the
      time the guest spends there. When the line carries ``inst=`` the guest's
      retired-instruction counter, the instructions executed between two
      samples can be attributed to the earlier sample's PC.

  [STAT] <n>s frames=... xfb_copies=... draws=... parse_ms=... render_ms=...
      A five-second window of host-side counters (window mode only).

  [GXPROF] parse+decode_ms=... (decode_ms=...) draws_snap=... ...
      A two-second window of GX work counters (printed in headless mode too).

  [DIAG] wall=... guest_tb_s=... vi=... tokens=... finishes=... dec_fires=...
      A five-second window of guest and hardware counters.

  [Heartbeat] PC: 0x... LR: 0x... intmr=0x... intsr=0x... msr=0x... dec_h=0x...
      A guest heartbeat, printed every 20 million guest instructions.

  [PERFPHASE] t=... window=... frames=... frame_ms=... <phase>=... ...
      The frame/phase timeline added by tools/stages/fix_perf_phase_trace.py
      (NWII_PERF_PHASE=1).

  [PERFCENSUS] t=... polls=... polls_s=... fast=... slow=... instr=...
      The guest poll census added by tools/stages/fix_perf_census.py
      (NWII_PERF_CENSUS=1).

  [PERFPCS] n=... pc=0x... polls=... instr=... exec_ms=... poll_ms=...
      One line per hot poll site of the census.

  [CurThread] / [Thread] ...
      Thread dumps that follow a heartbeat.
"""
from __future__ import annotations

import math
import re

HEX = r"(?:0x)?[0-9a-fA-F]+"
DEC = r"[0-9]+"


def _num(text, default=0.0):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _int(text, default=0):
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        return default


def _hex(text, default=0):
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return default


def _fields(text):
    """Split a whitespace-separated ``key=value`` list, keeping the last
    occurrence of a key. Values may contain spaces (the pad() group of
    [DIAG]), so only the first token of a value is taken."""
    out = {}
    for match in re.finditer(r"(\w+)=(\S+)", text):
        out[match.group(1)] = match.group(2)
    return out


def parse_sample(line):
    """A [Sample] line, or None."""
    if not line.startswith("[Sample]"):
        return None
    body = line[len("[Sample]"):]
    match = re.search(
        r"pc=(%s)\s+lr=(%s)\s+r3=(%s)" % (HEX, HEX, HEX), body)
    if not match:
        return None
    rest = body[match.end():]
    fields = _fields(rest)
    return {
        "pc": _hex(match.group(1)),
        "lr": _hex(match.group(2)),
        "r3": _hex(match.group(3)),
        "msr": _hex(fields.get("msr", "0")),
        "cb": _int(fields.get("cb", "0")),
        "inst": _int(fields["inst"]) if "inst" in fields else None,
        "fast": _int(fields["fast"]) if "fast" in fields else None,
    }


def parse_stat(line):
    """A [STAT] line, or None."""
    if not line.startswith("[STAT]"):
        return None
    fields = _fields(line)
    if "frames" not in fields:
        return None
    nonblack = fields.get("efb_nonblack", "0/0").split("/")
    seconds = re.match(r"\s*([0-9.]+)\s*s\b", line[len("[STAT]"):])
    return {
        "seconds": _num(seconds.group(1)) if seconds else 0.0,
        "frames": _int(fields.get("frames")),
        "xfb_copies": _int(fields.get("xfb_copies")),
        "draws": _int(fields.get("draws")),
        "parse_ms": _num(fields.get("parse_ms")),
        "render_ms": _num(fields.get("render_ms")),
        "efb_nonblack": _int(nonblack[0]),
        "efb_total": _int(nonblack[1]) if len(nonblack) > 1 else 0,
        "shaders": _int(fields.get("shaders")),
        "hash0": fields.get("hash0", ""),
    }


def parse_gxprof(line):
    """A [GXPROF] line, or None."""
    if not line.startswith("[GXPROF]"):
        return None
    fields = _fields(line)
    decode = re.search(r"\(decode_ms=([0-9.]+)\)", line)
    return {
        "parse_decode_ms": _num(fields.get("parse+decode_ms")),
        "decode_ms": _num(decode.group(1)) if decode else None,
        "draws": _int(fields.get("draws_snap")),
        "draw_bytes": _int(fields.get("draw_bytes")),
        "dl_calls": _int(fields.get("dl_calls")),
        "dl_bytes": _int(fields.get("dl_bytes")),
        "dl_ms": _num(fields.get("dl_ms")),
        "snap_ms": _num(fields.get("snap_ms")),
        "unknown": _int(fields.get("unknown")),
        "carry": _int(fields.get("carry")),
        "buf": _int(fields.get("buf")),
    }


def parse_diag(line):
    """A [DIAG] line, or None."""
    if not line.startswith("[DIAG]"):
        return None
    fields = _fields(line)
    if "wall" not in fields:
        return None
    return {
        "wall": _num(fields.get("wall")),
        "guest_tb_s": _num(fields.get("guest_tb_s")),
        "vi": _int(fields.get("vi")),
        "tokens": _int(fields.get("tokens")),
        "finishes": _int(fields.get("finishes")),
        "dec_fires": _int(fields.get("dec_fires")),
        "msr": _hex(fields.get("msr", "0")),
        "in_cb": _int(fields.get("in_cb")),
        "cbq": _int(fields.get("cbq")),
    }


def parse_heartbeat(line):
    """A [Heartbeat] line, or None."""
    if not line.startswith("[Heartbeat]"):
        return None
    match = re.search(
        r"PC:\s*(%s)\s+LR:\s*(%s)" % (HEX, HEX), line)
    if not match:
        return None
    fields = _fields(line)
    return {
        "pc": _hex(match.group(1)),
        "lr": _hex(match.group(2)),
        "intmr": _hex(fields.get("intmr", "0")),
        "intsr": _hex(fields.get("intsr", "0")),
        "msr": _hex(fields.get("msr", "0")),
        "dec_h": _hex(fields.get("dec_h", "0")),
    }


def parse_perfphase(line):
    """A [PERFPHASE] line, or None."""
    if not line.startswith("[PERFPHASE]"):
        return None
    fields = _fields(line)
    if "frames" not in fields:
        return None
    phases = {}
    for key, value in fields.items():
        if key.endswith("_ms") or key in (
                "gx_parse", "gx_decode", "gl_render", "xfb_copy", "present",
                "swap", "host_field", "host_drain_wait", "guest_idle"):
            phases[key] = _num(value)
    return {
        "t": _num(fields.get("t")),
        "window": _num(fields.get("window")),
        "frames": _int(fields.get("frames")),
        "frame_ms": _num(fields.get("frame_ms")),
        "p95": _num(fields.get("p95")),
        "max": _num(fields.get("max")),
        "host_sum": _num(fields.get("host_sum")),
        "guest_cpu_ms_s": _num(fields.get("guest_cpu_ms_s")),
        "phases": phases,
    }


def parse_perfcensus(line):
    """A [PERFCENSUS] line, or None."""
    if not line.startswith("[PERFCENSUS]"):
        return None
    fields = _fields(line)
    if "polls" not in fields:
        return None
    return {
        "t": _num(fields.get("t")),
        "polls": _int(fields.get("polls")),
        "polls_s": _num(fields.get("polls_s")),
        "fast": _int(fields.get("fast")),
        "slow": _int(fields.get("slow")),
        "instr": _int(fields.get("instr")),
        "instr_s": _num(fields.get("instr_s")),
        "polls_per_instr": _num(fields.get("polls_per_instr")),
        "poll_us": _num(fields.get("poll_us")),
        "poll_us_min": _num(fields.get("poll_us_min")),
        "poll_us_p10": _num(fields.get("poll_us_p10")),
        "poll_us_p50": _num(fields.get("poll_us_p50")),
        "poll_us_p90": _num(fields.get("poll_us_p90")),
        "poll_pct": _num(fields.get("poll_pct")),
    }


def parse_perfpcs(line):
    """A [PERFPCS] line, or None."""
    if not line.startswith("[PERFPCS]"):
        return None
    fields = _fields(line)
    if "pc" not in fields:
        return None
    return {
        "rank": _int(fields.get("n")),
        "pc": _hex(fields.get("pc")),
        "polls": _int(fields.get("polls")),
        "instr": _int(fields.get("instr")),
        "exec_ms": _num(fields.get("exec_ms")),
        "poll_us": _num(fields.get("poll_us")),
        "share_pct": _num(fields.get("share_pct")),
    }


def read_pcs_table(path):
    """Read the census per-site table (pc,polls,instr,exec_ns,poll_ns,poll_samples)."""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        header = handle.readline().strip().split(",")
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cells = line.split(",")
            if len(cells) < len(header):
                continue
            record = dict(zip(header, cells))
            rows.append({
                "pc": _hex(record.get("pc", "0")),
                "polls": _int(record.get("polls")),
                "instr": _int(record.get("instr")),
                "exec_ns": _int(record.get("exec_ns")),
                "poll_ns": _int(record.get("poll_ns", "0")),
                "poll_samples": _int(record.get("poll_samples", "0")),
            })
    return rows


def read_frames_csv(path):
    """Read the per-frame timeline CSV written by the phase trace."""
    frames = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        header = None
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cells = line.split(",")
            if header is None:
                header = cells
                continue
            record = {}
            for key, value in zip(header, cells):
                record[key] = _num(value)
            frames.append(record)
    return frames


def percentile(values, fraction):
    """The ``fraction`` percentile of a list, nearest-rank."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    if index >= len(ordered):
        index = len(ordered) - 1
    return float(ordered[index])


def mean(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / float(len(values))
