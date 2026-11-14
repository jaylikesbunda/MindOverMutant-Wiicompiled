"""Install the CotT frame-trace recorder and wire it into the host loop.

Ported from the Crash of the Titans port's runtime-overlays/frame_trace.h.

The header is the one CotT's own performance tooling reads: it records a
Chrome-trace event stream (per-thread CPU scopes, a completed-frame boundary,
guest PC samples) into a JSON file that analyze_frame_trace.py turns into a
frame-time report. It is game-independent - it names no guest address and
reads no guest state.

Two things make it usable here:

  * the runtime already calls into the ``native_trace`` namespace (the ported
    CotT performance stages use ``native_trace::mark`` for their diagnostics),
    so the namespace has to exist or the runtime does not compile;
  * the recorder needs a frame boundary and a periodic poll. This stage wires
    both into the host render loop, and registers the guest thread so the
    trace can report the guest's CPU time.

MoM's runtime has no GL timestamp queries, so the GPU region events of the
CotT trace are absent; the CPU scopes, the frame boundary and the guest PC
samples are present.

Env:
    NWII_FRAME_TRACE=<path>   write the trace to <path>.json
    <path>.arm                 holds the number of frames to capture

Idempotent: the header is rewritten from overlays/ every time and every
source edit is guarded by its own marker.
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
header_src = port / "overlays/frame_trace.h"
header_dst = runtime / "include/runtime/frame_trace.h"

assert header_src.is_file(), "overlays/frame_trace.h is missing"

header_dst.parent.mkdir(parents=True, exist_ok=True)
header_dst.write_bytes(header_src.read_bytes())

skipped = []


def edit(relative, token, *alternatives):
    """Insert the first matching alternative in place of its anchor, once."""
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


# --- src/hle/gx.cpp: the namespace the ported stages already call into ------
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_FRAME_TRACE_V1_INCLUDE_GX",
    (
        '#include "runtime/gx/renderer.h"\n',
        '#include "runtime/gx/renderer.h"\n'
        '#include "runtime/frame_trace.h" // NATIVE_PERF_FRAME_TRACE_V1_INCLUDE_GX\n',
    ),
)

# --- src/core/main.cpp: the header, the guest thread and the frame boundary -
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_FRAME_TRACE_V1_INCLUDE_MAIN",
    (
        '#include "runtime/config.h"\n',
        '#include "runtime/config.h"\n'
        '#include "runtime/frame_trace.h" // NATIVE_PERF_FRAME_TRACE_V1_INCLUDE_MAIN\n',
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_FRAME_TRACE_V1_GUEST",
    (
        "void cpu_thread_func(nwii::runtime::CPUContext *ctx) {\n"
        "  nwii::runtime::g_ctx_ptr = ctx;\n",
        "void cpu_thread_func(nwii::runtime::CPUContext *ctx) {\n"
        "  nwii::runtime::g_ctx_ptr = ctx;\n"
        "  native_trace::guest_thread(); // NATIVE_PERF_FRAME_TRACE_V1_GUEST\n",
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_FRAME_TRACE_V1_BOUNDARY",
    (
        "      if (audio_dev > 0) {\n"
        "        if (SDL_GetQueuedAudioSize(audio_dev) < 1024 * 4 * 2) {\n",
        "      native_trace::boundary(0); // NATIVE_PERF_FRAME_TRACE_V1_BOUNDARY\n"
        "      native_trace::poll(); // NATIVE_PERF_FRAME_TRACE_V1_BOUNDARY\n"
        "      if (audio_dev > 0) {\n"
        "        if (SDL_GetQueuedAudioSize(audio_dev) < 1024 * 4 * 2) {\n",
    ),
)

for line in skipped:
    print("  WARNING: %s" % line)
print("Frame trace recorder installed (NWII_FRAME_TRACE=<path>).")
