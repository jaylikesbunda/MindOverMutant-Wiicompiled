"""Add a frame-time timeline with per-phase costs to the runtime.

Ported from the Crash of the Titans port's frame_trace.h / native_stutter.h
measurement family, adapted to this runtime.

CotT's frame_trace.h is a Chrome-trace event recorder: per-thread event
buffers, GPU timestamp queries, a Windows thread-time bridge and a JSON dump
that analyze_frame_trace.py turns into a report. This runtime has no GPU
query fences and no event pipeline, and a full event recorder would be a large
amount of new code in the host's hot loop.

What transfers, and what this port needs, is smaller: named phase scopes
around the host's render work, a per-frame record of how long each phase took,
a periodic report, and the frame timeline as CSV. The phases are the ones the
CotT overlay also prices - GX parse, GL render, XFB copy, present - plus the
stalls: the field wait, the GX drain wait, and the guest's idle park.

The frame is one iteration of the host render loop. That loop is field-paced
(NATIVE_FIELD_PACING_V1), so the frame period is the display field period and
the interesting number is how much of it the host spends working.

Env knobs (all new, all opt-in, none of them change the existing diagnostics):
    NWII_PERF_PHASE=1        enable the phase timers
    NWII_PERF_PHASE_FILE     append the per-frame timeline as CSV
    NWII_PERF_PHASE_PERIOD   seconds between [PERFPHASE] reports (default 5)

Output:
    [PERFPHASE] t=12.0s window=5.0s frames=300 frame_ms=16.63 p95=17.20
                max=25.40 gx_parse=0.42 ... host_sum=11.37 guest_cpu_ms_s=900.0

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
header_src = port / "overlays/native_perf_phase.h"
header_dst = runtime / "include/runtime/native_perf_phase.h"

assert header_src.is_file(), "overlays/native_perf_phase.h is missing"

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


def retag(relative, pairs):
    """Rename a marker that an earlier version of this stage wrote.

    A marker that was renamed must not survive, or the stage would insert a
    second copy of the edit it guards.
    """
    path = runtime / relative
    text = path.read_text()
    changed = False
    for old, new in pairs:
        if old in text and new not in text:
            text = text.replace(old, new)
            changed = True
    if changed:
        path.write_text(text)


# --- src/core/main.cpp: the frame boundary and the host-side phases --------
retag("src/hle/gx.cpp", [("NATIVE_PERF_PHASE_V1_DECODE_END", "NATIVE_PERF_PHASE_V1_DECODE_STOP")])

edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_INCLUDE_MAIN",
    (
        '#include "runtime/hw/hw.h"\n',
        '#include "runtime/hw/hw.h"\n'
        '#include "runtime/native_perf_phase.h" // NATIVE_PERF_PHASE_V1_INCLUDE_MAIN\n',
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_THREAD",
    (
        "  nwii::runtime::g_ctx_ptr = ctx;\n",
        "  nwii::runtime::g_ctx_ptr = ctx;\n"
        "  native_perf_phase::set_guest_thread(); // NATIVE_PERF_PHASE_V1_THREAD\n",
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_FRAME_BEGIN",
    (
        "    while (!quit && ctx->is_running) {\n",
        "    while (!quit && ctx->is_running) {\n"
        "      native_perf_phase::frame_begin(); // NATIVE_PERF_PHASE_V1_FRAME_BEGIN\n",
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_PRESENT",
    (
        "      if (!headless) {\n"
        "        glBindFramebuffer(GL_FRAMEBUFFER, 0);\n",
        "      if (!headless) {\n"
        "        native_perf_phase::Scope native_perf_phase_present(native_perf_phase::Phase_Present); // NATIVE_PERF_PHASE_V1_PRESENT\n"
        "        glBindFramebuffer(GL_FRAMEBUFFER, 0);\n",
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_SWAP",
    (
        '        if(std::getenv("NWII_OFFSCREEN")==nullptr)SDL_GL_SwapWindow(window);\n',
        "        native_perf_phase_present.stop(); // NATIVE_PERF_PHASE_V1_SWAP\n"
        '        if(std::getenv("NWII_OFFSCREEN")==nullptr){\n'
        "          native_perf_phase::Scope native_perf_phase_swap(native_perf_phase::Phase_Swap); // NATIVE_PERF_PHASE_V1_SWAP\n"
        "          SDL_GL_SwapWindow(window);\n"
        "        }\n",
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_FIELD",
    (
        "      pace_video_field();\n    }\n",
        "      { native_perf_phase::Scope native_perf_phase_field(native_perf_phase::Phase_HostField); // NATIVE_PERF_PHASE_V1_FIELD\n"
        "        pace_video_field(); }\n"
        "      native_perf_phase::frame_end(); // NATIVE_PERF_PHASE_V1_FIELD\n"
        "      native_perf_phase::maybe_report(); // NATIVE_PERF_PHASE_V1_FIELD\n"
        "    }\n",
    ),
)
edit(
    "src/core/main.cpp",
    "NATIVE_PERF_PHASE_V1_DRAIN",
    (
        "      std::unique_lock<std::mutex> drain_lock(nwii::runtime::g_gx_drain_mutex);\n"
        "      nwii::runtime::g_gx_drain_cv.wait_until(drain_lock, std::min(field_deadline, wait_now + std::chrono::milliseconds(2)),\n"
        "                                              [] { return nwii::runtime::g_gx_drain_request.load(std::memory_order_acquire); });\n",
        "      std::unique_lock<std::mutex> drain_lock(nwii::runtime::g_gx_drain_mutex);\n"
        "      { native_perf_phase::Scope native_perf_phase_drain(native_perf_phase::Phase_HostDrainWait); // NATIVE_PERF_PHASE_V1_DRAIN\n"
        "        nwii::runtime::g_gx_drain_cv.wait_until(drain_lock, std::min(field_deadline, wait_now + std::chrono::milliseconds(2)),\n"
        "                                                [] { return nwii::runtime::g_gx_drain_request.load(std::memory_order_acquire); }); }\n",
    ),
)

# --- src/hle/gx.cpp: the GX work -------------------------------------------
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_PHASE_V1_INCLUDE_GX",
    (
        '#include "runtime/native_fifo_lock.h" // NATIVE_FIFO_SRWLOCK_V1\n',
        '#include "runtime/native_fifo_lock.h" // NATIVE_FIFO_SRWLOCK_V1\n'
        '#include "runtime/native_perf_phase.h" // NATIVE_PERF_PHASE_V1_INCLUDE_GX\n',
    ),
)
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_PHASE_V1_PARSE",
    (
        "    nwii::runtime::gx::FifoParser::Parse(s_parse_buf, commands);\n",
        "    native_perf_phase::Scope native_perf_phase_parse(native_perf_phase::Phase_GxParse); // NATIVE_PERF_PHASE_V1_PARSE\n"
        "    nwii::runtime::gx::FifoParser::Parse(s_parse_buf, commands);\n"
        "    native_perf_phase_parse.stop(); // NATIVE_PERF_PHASE_V1_PARSE\n",
    ),
)
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_PHASE_V1_DECODE_BEGIN",
    (
        "    auto d0 = std::chrono::steady_clock::now();\n",
        "    native_perf_phase::Scope native_perf_phase_decode(native_perf_phase::Phase_GxDecode); // NATIVE_PERF_PHASE_V1_DECODE_BEGIN\n"
        "    auto d0 = std::chrono::steady_clock::now();\n",
    ),
    (
        "    auto d0 = std::chrono::steady_clock::now();\n"
        "    for (auto &c : frame) {\n",
        "    native_perf_phase::Scope native_perf_phase_decode(native_perf_phase::Phase_GxDecode); // NATIVE_PERF_PHASE_V1_DECODE_BEGIN\n"
        "    auto d0 = std::chrono::steady_clock::now();\n"
        "    for (auto &c : frame) {\n",
    ),
)
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_PHASE_V1_DECODE_STOP",
    (
        "    auto d1 = std::chrono::steady_clock::now();\n",
        "    native_perf_phase_decode.stop(); // NATIVE_PERF_PHASE_V1_DECODE_STOP\n"
        "    auto d1 = std::chrono::steady_clock::now();\n",
    ),
)
edit(
    "src/hle/gx.cpp",
    "NATIVE_PERF_PHASE_V1_RENDER",
    (
        "            auto r0 = std::chrono::steady_clock::now();\n"
        "            g_renderer->Render(frame);\n"
        "            auto r1 = std::chrono::steady_clock::now();\n",
        "            native_perf_phase::Scope native_perf_phase_render(native_perf_phase::Phase_GlRender); // NATIVE_PERF_PHASE_V1_RENDER\n"
        "            auto r0 = std::chrono::steady_clock::now();\n"
        "            g_renderer->Render(frame);\n"
        "            native_perf_phase_render.stop(); // NATIVE_PERF_PHASE_V1_RENDER\n"
        "            auto r1 = std::chrono::steady_clock::now();\n",
    ),
)

# --- src/hle/gx/renderer_gl.cpp: the EFB -> XFB copy ------------------------
edit(
    "src/hle/gx/renderer_gl.cpp",
    "NATIVE_PERF_PHASE_V1_INCLUDE_GL",
    (
        '#include "runtime/native_resolution.h"\n',
        '#include "runtime/native_resolution.h"\n'
        '#include "runtime/native_perf_phase.h" // NATIVE_PERF_PHASE_V1_INCLUDE_GL\n',
    ),
)
edit(
    "src/hle/gx/renderer_gl.cpp",
    "NATIVE_PERF_PHASE_V1_XFB",
    (
        "  glBlitFramebuffer(x*scale,(kNativeEfbHeight-y-height)*scale,(x+width)*scale,(kNativeEfbHeight-y)*scale,\n"
        "                    0,0,output_width,output_height,GL_COLOR_BUFFER_BIT,GL_NEAREST);\n",
        "  native_perf_phase::Scope native_perf_phase_copy(native_perf_phase::Phase_XfbCopy); // NATIVE_PERF_PHASE_V1_XFB\n"
        "  glBlitFramebuffer(x*scale,(kNativeEfbHeight-y-height)*scale,(x+width)*scale,(kNativeEfbHeight-y)*scale,\n"
        "                    0,0,output_width,output_height,GL_COLOR_BUFFER_BIT,GL_NEAREST);\n"
        "  native_perf_phase_copy.stop(); // NATIVE_PERF_PHASE_V1_XFB\n",
    ),
)

# --- src/hle/ios.cpp: the guest's idle park --------------------------------
edit(
    "src/hle/ios.cpp",
    "NATIVE_PERF_PHASE_V1_INCLUDE_IOS",
    (
        '#include "runtime/native_guest_bytes.h" // NATIVE_IOS_BULK_TRANSFER_V1\n',
        '#include "runtime/native_guest_bytes.h" // NATIVE_IOS_BULK_TRANSFER_V1\n'
        '#include "runtime/native_perf_phase.h" // NATIVE_PERF_PHASE_V1_INCLUDE_IOS\n',
    ),
)
edit(
    "src/hle/ios.cpp",
    "NATIVE_PERF_PHASE_V1_IDLE",
    (
        "        native_idle::wait(ticks, ctx.tb_freq, pending);\n"
        "        nwii::runtime::native_clock_invalidate(); // NATIVE_CLOCK_CACHE_V1: guest time jumped\n",
        "        native_perf_phase::Scope native_perf_phase_idle(native_perf_phase::Phase_GuestIdle); // NATIVE_PERF_PHASE_V1_IDLE\n"
        "        native_idle::wait(ticks, ctx.tb_freq, pending);\n"
        "        native_perf_phase_idle.stop(); // NATIVE_PERF_PHASE_V1_IDLE\n"
        "        nwii::runtime::native_clock_invalidate(); // NATIVE_CLOCK_CACHE_V1: guest time jumped\n",
    ),
    (
        "        native_idle::wait(ticks, ctx.tb_freq, pending);\n",
        "        native_perf_phase::Scope native_perf_phase_idle(native_perf_phase::Phase_GuestIdle); // NATIVE_PERF_PHASE_V1_IDLE\n"
        "        native_idle::wait(ticks, ctx.tb_freq, pending);\n"
        "        native_perf_phase_idle.stop(); // NATIVE_PERF_PHASE_V1_IDLE\n",
    ),
)

for line in skipped:
    print("  WARNING: %s" % line)
print("Frame phase timeline installed (NWII_PERF_PHASE=1).")
