"""Add the guest poll census to the runtime (ported from the CotT measurement tooling).

The Crash of the Titans port measures its work with a runtime census header
(``runtime/native_perf_census.h``) that counts the operations of its own
recompiler: the dispatch switch, the quantised gather, the packed-vertex
allocator, the texture cache. None of those exist in this runtime, so a
verbatim copy would count nothing.

What does exist here, and what the port actually needs, is the *guest poll*.
The guest polls the host at every backward branch, and the generated code
increments the guest's retired-instruction counter once per guest instruction.
That gives two exact measurements this port can use:

  * the poll rate - the closest thing this runtime has to a "guest loop
    iterations per second" meter;
  * a hot-spot profile with exact counts, not a sample: the instructions and
    the wall-clock time between two consecutive polls belong to the loop whose
    backward branch is the *previous* poll site, so the interval can be
    attributed to that site.

The header is adapted from the CotT original, keeps the CotT API surface the
overlay headers use (``Enabled()``, ``Frame()``) and adds the poll scope the
adapter below installs. It is game-independent: it names no guest address and
reads no guest RAM.

Env knobs (all new, all opt-in, none of them change the existing diagnostics):
    NWII_PERF_CENSUS=1        enable the census
    NWII_PERF_PC_HISTOGRAM=1  also record the per-poll-site histogram
    NWII_PERF_CENSUS_FAST=1   drop the per-poll clock read
    NWII_PERF_CENSUS_FILE     write the whole per-site table as CSV
    NWII_PERF_CENSUS_PERIOD   seconds between [PERFCENSUS] reports (default 5)

Output:
    [PERFCENSUS] t=12.0s polls=... polls_s=... fast=... slow=... instr=...
                 instr_s=... polls_per_instr=... poll_ms=... poll_pct=...
    [PERFPCS] n=1 pc=0x... polls=... instr=... exec_ms=... poll_ms=... share_pct=...

Idempotent: every edit is guarded by its own marker, and an edit whose anchor
has been rewritten by another stage is reported and skipped rather than
aborting the stage.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / "vendor").is_dir():
    # Run directly from tools/stages/: parents[1] is tools/, the repo root is
    # one level up. tools/apply_stages.py rewrites the expression above to the
    # repo root, and its runner executes the script from logs/, where
    # parents[1] already is the repo root.
    port = Path(__file__).resolve().parents[2]
runtime = port / "vendor/NWiiRecomp/nWiiRuntime"
header_src = port / "overlays/native_perf_census.h"
header_dst = runtime / "include/runtime/native_perf_census.h"

assert header_src.is_file(), "overlays/native_perf_census.h is missing"

header_dst.parent.mkdir(parents=True, exist_ok=True)
header_dst.write_bytes(header_src.read_bytes())

skipped = []


def edit(relative, token, *alternatives):
    """Insert the first matching alternative in place of its anchor, once.

    `alternatives` is a list of (old, new) pairs; the first whose anchor is
    found exactly once wins. A stage that rewrote the anchor is reported and
    skipped rather than aborting the whole stage.
    """
    path = runtime / relative
    text = path.read_text()
    if token in text:
        return False
    counts = []
    for old, new in alternatives:
        count = text.count(old)
        if count == 1:
            path.write_text(text.replace(old, new))
            return True
        counts.append(count)
    skipped.append("%s: %s (anchor matched %s times)" % (relative, token, counts))
    return False


# --- src/hle/ios.cpp: account for every poll --------------------------------
edit(
    "src/hle/ios.cpp",
    "NATIVE_PERF_CENSUS_V1_INCLUDE_IOS",
    (
        '#include "input/input_manager.h"\n',
        '#include "input/input_manager.h"\n'
        '#include "runtime/native_perf_census.h" // NATIVE_PERF_CENSUS_V1_INCLUDE_IOS\n',
    ),
)
edit(
    "src/hle/ios.cpp",
    "NATIVE_PERF_CENSUS_V1_POLL",
    (
        "bool process_pending_callbacks(CPUContext &ctx) {\n",
        "bool process_pending_callbacks(CPUContext &ctx) {\n"
        "  // NATIVE_PERF_CENSUS_V1: account for this poll. The scope reads the\n"
        "  // guest PC and the guest's retired-instruction counter and writes\n"
        "  // nothing back, so the guest cannot see it.\n"
        "  native_perf_census::PollScope native_perf_census_poll(ctx.pc, ctx.inst_count); // NATIVE_PERF_CENSUS_V1_POLL\n",
    ),
    (
        "bool process_pending_callbacks(CPUContext &ctx) {\n"
        "  native_fp_context_probe(ctx,\"poll\");\n",
        "bool process_pending_callbacks(CPUContext &ctx) {\n"
        "  native_fp_context_probe(ctx,\"poll\");\n"
        "  // NATIVE_PERF_CENSUS_V1: account for this poll. The scope reads the\n"
        "  // guest PC and the guest's retired-instruction counter and writes\n"
        "  // nothing back, so the guest cannot see it.\n"
        "  native_perf_census::PollScope native_perf_census_poll(ctx.pc, ctx.inst_count); // NATIVE_PERF_CENSUS_V1_POLL\n",
    ),
)
edit(
    "src/hle/ios.cpp",
    "NATIVE_PERF_CENSUS_V1_FAST",
    (
        "      if (ctx.pending_cb_count.load(std::memory_order_relaxed) == 0) {\n"
        "        g_native_fast_polls.fetch_add(1, std::memory_order_relaxed);\n"
        "        native_poll_sample(ctx);\n"
        "        return false;\n"
        "      }\n",
        "      if (ctx.pending_cb_count.load(std::memory_order_relaxed) == 0) {\n"
        "        g_native_fast_polls.fetch_add(1, std::memory_order_relaxed);\n"
        "        native_poll_sample(ctx);\n"
        "        native_perf_census_poll.mark_fast(); // NATIVE_PERF_CENSUS_V1_FAST\n"
        "        return false;\n"
        "      }\n",
    ),
)

# --- src/hle/gx.cpp: the host drains the FIFO once per field -----------------
# ProcessGXFifo runs on the host thread, so it is the right place to count a
# frame and to print the census report: the guest thread never writes to
# stdout from here, so the report cannot interleave with the guest's output.
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_CENSUS_V1_INCLUDE_GX",
    (
        '#include "runtime/gx/fifo_parser.h"\n',
        '#include "runtime/native_perf_census.h" // NATIVE_PERF_CENSUS_V1_INCLUDE_GX\n'
        '#include "runtime/gx/fifo_parser.h"\n',
    ),
)
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_CENSUS_V1_FRAME",
    (
        "void ProcessGXFifo() {\n",
        "void ProcessGXFifo() {\n"
        "  // NATIVE_PERF_CENSUS_V1: the host calls this once per field. Counting\n"
        "  // the frame here keeps the census independent of the guest thread,\n"
        "  // which must not write to stdout.\n"
        "  native_perf_census::Frame(); // NATIVE_PERF_CENSUS_V1_FRAME\n"
        "  native_perf_census::maybe_report(); // NATIVE_PERF_CENSUS_V1_FRAME\n",
    ),
)

for line in skipped:
    print("  WARNING: %s" % line)
print("Guest poll census installed (NWII_PERF_CENSUS=1, NWII_PERF_PC_HISTOGRAM=1).")
