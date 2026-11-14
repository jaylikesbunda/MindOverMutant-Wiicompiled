"""Overlap the host's GL work with guest execution.

This installs scheduling experiments, not a measured MoM performance result.
Threshold drains remain opt-in pending a scene-matched gameplay comparison.

Why they alternate
------------------
There is no render thread.  ``RendererGL::Render`` runs only inside
``ProcessGXFifo`` (gx.cpp), which runs from the host loop (main.cpp) and from
the on-demand drain serviced inside ``pace_video_field``.  The guest asks for a
drain in exactly one place: when it is parked in its idle loop at PC
0x80492408 with bytes pending (ios.cpp, NATIVE_GX_IDLE_DRAIN).  Everywhere else
the FIFO simply accumulates until the next field, so the host's ~11 ms of work
happens *after* the guest has stopped producing.  Overlapping them means
letting the host drain while the guest is still building the frame.

The PE signal (BP 0x45/0x47/0x48 -> NativeGpuCompletion) stays exactly where it
is: inside the Render loop, after FlushBatch + glFlush.  Nothing here moves it.

This stage has three parts, each independently switchable at run time.

Part A (prerequisite) - snapshot the EFB copy state per command
---------------------------------------------------------------
The historical seam description below predates MoM's execution-order changes.
The mandatory ``fix_copy_snapshot_mom.py`` successor captures RAW parse-time BP
registers, not the derived fields, and also freezes clear colour/depth/masks.
Replay through ``tools/apply_perf_stages.py``, not this stage in isolation.

``fifo_parser.cpp`` parses BP 0x49/0x4A/0x4B/0x4D/0x52 into the globals
``g_state.efbSrcX/efbSrcY/efbW/efbH/efbCopyDest/efbCopyStride`` (and, for bit
14, ``xfbAddr/xfbW/xfbH/xfbStride/frame_ready``).  The renderer reads those
globals *at execute time*: the render-to-texture blit, ``NativeLatchXfb``, the
copy-clear probe line and the copy-clear scissor.

Because ``Parse()`` completes a whole drain before ``Render()`` runs, a drain
that carries two display copies executes both with the *last* copy's
destination and rectangle.  Today that is masked only because the guest is
parked inside Render at each PE signal, so a drain rarely holds two copies.
Allow the guest to run ahead and it becomes live - it is the historical
"alternating finished / half-rendered frame" bug (see the note about
``fix_early_gpu_signal`` in tools/apply_stages.py).

The parser now freezes the copy descriptor into the GXCommand that carries the
0x52 write (``native_copy_*``) at that write's position in the stream, and the
renderer's copy path reads the command instead of the globals.  FIFO order
follows: the globals are a single "latest wins" cell, the snapshot is per
command, so copy #1 is rendered with copy #1's rectangle and destination and
copy #2 with its own, whatever else the drain contains.  A display-list cache
hit re-freezes the snapshot while replaying the cached commands, so a cached
0x52 also takes the geometry in force at its own replay position.  The globals
keep every write they had (see the reader census in the stage body).

Re-pointed readers (all in ``renderer_gl.cpp``'s BP 0x52 branch): the
render-to-texture destination/size and its ``NativeCopyEfbTexture`` source
rectangle, the ``NativeLatchXfb`` geometry (whose guard rejects a copy it
thinks is out of range), the ``[CopyClear]`` probe line and the copy-clear
``glScissor`` rectangle.

Left on the globals: ``NativeClearEfb``'s BP 0x40/0x41/0x51 (colour mask, depth
mask, clear depth - replayed in FIFO order by ``ApplyBPRegister`` and not part
of the copy descriptor; a drain that writes one of them *after* a copy-clear
still has the old ordering hazard, which this stage neither fixes nor widens),
``GX_GetXfb`` / the VI present path (``g_state.xfbAddr/xfbW/xfbH/xfbStride``),
and the EFB-texture guest-RAM range in ``texture_cache_gl.cpp``
(``efbW/efbH/efbCopyStride``).  ``pe_clear_pending`` and ``frame_ready`` have
no reader at execute time.

Default ON: it is a correctness fix and changes nothing when a drain holds one
copy.  ``NWII_NO_GX_COPY_SNAPSHOT=1`` restores the global reads.

Part B - request a drain on a byte threshold
--------------------------------------------
``native_gx_publish`` (guest thread, called from every poll that staged a GX
byte) now asks the host to drain once the bytes waiting in ``g_hw_fifo`` cross
a threshold, instead of waiting for the guest to reach its idle PC.

* threshold: ``NWII_GX_DRAIN_BYTES`` (default 16384), clamped to 1..1048576.
* hysteresis: a request is raised when ``pending >= requested_at + trigger``
  with ``trigger = max(NWII_GX_DRAIN_BYTES, last_drain_bytes / 2)``, where
  ``last_drain_bytes`` is the size of the batch the previous drain took.  One
  request per ``trigger`` bytes, never one per publish (the pending counter is
  not reset until the host drains, so without the latch every publish above
  the threshold - thousands per frame - would take the mutex and signal).  A
  slow host raises the bar instead of splitting the frame finer, which is what
  keeps the per-drain fixed cost (renderer prologue, batch flush, shader
  setup) from growing when the host falls behind.

No byte can be stranded or reordered: the request is a *hint*, never a
transfer.  Bytes stay in ``g_cpu_fifo``/``g_hw_fifo``/``s_parse_buf`` and the
per-field ``ProcessGXFifo`` still drains whatever is left, under the same
``g_fifo_mutex``; the parser still carries a partial command across drains in
``s_parse_buf``/carry.  The extra drains only make the existing per-field drain
happen sooner, in the order the bytes were published (a single FIFO vector,
copied and parsed in order).  The request is raised after the publish has
released ``g_fifo_mutex``, so there is no new lock nesting: the guest takes
``g_gx_drain_mutex`` alone, and the host never holds it while taking
``g_fifo_mutex``.

``NWII_GX_EARLY_DRAIN=1`` opts in; ``NWII_NO_GX_EARLY_DRAIN`` overrides it.
Without the opt-in, reference idle-PC requests remain.

Part C - exact condition-variable wake
--------------------------------------
``pace_video_field`` waited on ``g_gx_drain_cv`` with a 2 ms cap and polled the
request flag, because the guest's ``store(true)`` could land between the host's
``exchange(false)`` and its ``wait_until`` - a lost wake that cost up to 2 ms,
and cost the *whole* field if the cap were removed naively.

The request is now a generation counter (``g_gx_drain_gen``) incremented under
``g_gx_drain_mutex``, and the host evaluates the wait predicate and consumes
the generation under the same mutex.  Both sides modify/observe the state while
holding the mutex that the condition variable is bound to, which is exactly the
contract that makes a lost wake impossible; the 2 ms cap is gone and the field
deadline is the only timeout.  A request that arrives during a drain is seen on
the next iteration (the drain consumes only the generation it observed).

``NWII_NO_GX_WAKE_EXACT=1`` restores the flag + 2 ms cap.

Registration
------------
Add ``"fix_gx_overlap_mom.py",`` as the **last** entry of ``STAGES`` in
``tools/apply_stages.py`` (after ``fix_guest_runtime_trim_mom.py``).  It
anchors on the final text of four files - the host loop the idle/pacing/
perf-phase stages rewrite, ``native_gx_publish`` as
``fix_guest_runtime_trim_mom.py`` leaves it, the renderer's BP branch after
``fix_renderer_batching_mom.py``, and the parser after the display-list cache
stages - so it has to run after all of them.
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARK = 'NATIVE_MOM_GX_OVERLAP_V1'

ROOT = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent
RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
RESTORE = False

COPY_CLEAR_COMMENT = (
    '        // scissor. NativeClearEfb reads the frozen per-command copy state;\n'
    '        // BP 0x40/0x41/0x51 arrive via the same FIFO-order descriptor.\n'
)
COPY_CLEAR_LEGACY = (
    '        // scissor. NativeClearEfb still reads BP 0x40/0x41/0x51 through\n'
    '        // g_state.bp - those are replayed in FIFO order by ApplyBPRegister and\n'
    '        // are not the copy descriptor.\n'
)
COPY_CLEAR_PARTIAL = (
    '        // scissor. NativeClearEfb reads the frozen per-command copy state; BP 0x40/0x41/0x51\n'
    '        // g_state.bp - those are replayed in FIFO order by ApplyBPRegister and\n'
    '        // are not the copy descriptor.\n'
)


class Source:
    """One file of the runtime tree, edited by anchored substitution."""

    def __init__(self, path, check):
        self.path = path
        self.check = check
        self.text = path.read_text(encoding='utf-8') if path.is_file() else ''
        original = self.text
        if path.name == 'renderer_gl.cpp':
            # Older comment-only edits split the exact inverse span, leaving
            # a marker that made re-apply skip the missing flag declaration.
            self.text = self.text.replace(COPY_CLEAR_LEGACY, COPY_CLEAR_COMMENT)
            self.text = self.text.replace(COPY_CLEAR_PARTIAL, COPY_CLEAR_COMMENT)
        elif path.name == 'ios.cpp':
            declaration = 'extern void native_gx_request_drain(); // ' + MARK + '\n'
            anchor = 'extern std::condition_variable g_gx_drain_cv;\n'
            # Poll-credit installation may have inserted its definition inside
            # this inverse span. Move only our declaration back to its anchor.
            if self.text.count(declaration) == 1 and self.text.count(anchor) == 1:
                self.text = self.text.replace(declaration, '', 1)
                self.text = self.text.replace(anchor, anchor + declaration, 1)
        self.edits = int(self.text != original)

    def installed(self):
        # A pristine file can also lack prerequisite stages' anchors.
        return MARK not in self.text if RESTORE else MARK in self.text

    def sub(self, old, new, what):
        if RESTORE:
            old, new = new, old
            if old not in self.text and new in self.text:
                return
        count = self.text.count(old)
        assert count == 1, '%s: anchor %r matched %d times' % (
            self.path.name, what, count)
        self.text = self.text.replace(old, new, 1)
        self.edits += 1

    def save(self):
        if self.check or not self.edits:
            return False
        self.path.write_text(self.text, encoding='utf-8', newline='\n')
        return True


# ---------------------------------------------------------------------------
# Part A: per-command EFB copy snapshot.
# ---------------------------------------------------------------------------

CMD_H = 'include/runtime/gx_command.h'
PARSER = 'src/hle/gx/fifo_parser.cpp'
RENDERER = 'src/hle/gx/renderer_gl.cpp'


def part_a(runtime, check):
    cmd_h = Source(runtime / CMD_H, check)
    if not cmd_h.installed():
        cmd_h.sub(
            '    std::shared_ptr<DrawRaw> native_gpu_snapshot;\n'
            '\n'
            '    std::vector<float> payload;\n'
            '};',
            '    std::shared_ptr<DrawRaw> native_gpu_snapshot;\n'
            '\n'
            '    std::vector<float> payload;\n'
            '    // ' + MARK + ': the EFB copy descriptor in force at this BP 0x52\n'
            '    // write, frozen by the parser at the write\'s own position in the\n'
            '    // stream. Parse() finishes the whole drain before Render() runs, so the\n'
            '    // live g_state.efb*/xfb* copy globals hold the LAST copy of the drain;\n'
            '    // reading them at execute time renders every copy in the drain with\n'
            '    // that one\'s destination and rectangle. The renderer reads these\n'
            '    // instead. valid==false means the command did not come from the parser\n'
            '    // (a 0x52 built by hand): the renderer then falls back to the globals.\n'
            '    bool native_copy_valid = false;\n'
            '    uint16_t native_copy_src_x = 0, native_copy_src_y = 0;\n'
            '    uint16_t native_copy_w = 0, native_copy_h = 0;\n'
            '    uint32_t native_copy_dest = 0, native_copy_stride = 0;\n'
            '};',
            'GXCommand copy fields')

    parser = Source(runtime / PARSER, check)
    if not parser.installed():
        parser.sub(
            '    void ApplyBPRegisterImpl(uint8_t reg, uint32_t val);\n'
            '\n'
            '    void ParseBP(uint8_t reg, uint32_t val) {',
            '    void ApplyBPRegisterImpl(uint8_t reg, uint32_t val);\n'
            '\n'
            '    // ' + MARK + ': freeze the EFB copy state that is live at this\n'
            '    // point of the stream onto the command that carries the 0x52 write.\n'
            '    // The g_state.efb*/xfb* writes below stay exactly as they were: they\n'
            '    // are still what GX_GetXfb / the VI present path reads, and what sizes\n'
            '    // the EFB texture\'s guest-RAM range in texture_cache_gl.cpp. Only the\n'
            '    // renderer\'s per-copy path stops reading them.\n'
            '    void NativeFreezeCopyState(GXCommand& c, uint8_t reg) {\n'
            '        if (reg != 0x52) return;\n'
            '        c.native_copy_valid = true;\n'
            '        c.native_copy_src_x = g_state.efbSrcX;\n'
            '        c.native_copy_src_y = g_state.efbSrcY;\n'
            '        c.native_copy_w = g_state.efbW;\n'
            '        c.native_copy_h = g_state.efbH;\n'
            '        c.native_copy_dest = g_state.efbCopyDest;\n'
            '        c.native_copy_stride = g_state.efbCopyStride;\n'
            '    }\n'
            '\n'
            '    void ParseBP(uint8_t reg, uint32_t val) {',
            'copy snapshot helper')
        # The seam RMW marker can carry a BP write too (BP 0xfe masks GEN_MODE /
        # CMODE0 today, but nothing restricts it to those registers).
        parser.sub(
            '                uint32_t merged = (g_state.bp[reg] & ~mask) | (val & mask);\n'
            '                ParseBP(reg, merged);\n'
            '                c.type = GXCommandType::BPRegister;\n'
            '                c.val = merged;\n',
            '                uint32_t merged = (g_state.bp[reg] & ~mask) | (val & mask);\n'
            '                ParseBP(reg, merged);\n'
            '                c.type = GXCommandType::BPRegister;\n'
            '                c.val = merged;\n'
            '                NativeFreezeCopyState(c, reg); // ' + MARK + '\n',
            'seam BP write')
        parser.sub(
            '            uint8_t reg = fifo[offset + 1];\n'
            '            uint32_t val = Read24(fifo, offset + 2);\n'
            '            ParseBP(reg, val);\n'
            '\n'
            '            NativeParseCommandSlot<NativeInPlace> native_slot(commands);\n'
            '            GXCommand& c=native_slot.value;\n'
            '            c.type = GXCommandType::BPRegister;\n'
            '            c.reg = reg;\n'
            '            c.val = val;\n'
            '            native_slot.Commit(commands);\n',
            '            uint8_t reg = fifo[offset + 1];\n'
            '            uint32_t val = Read24(fifo, offset + 2);\n'
            '            ParseBP(reg, val);\n'
            '\n'
            '            NativeParseCommandSlot<NativeInPlace> native_slot(commands);\n'
            '            GXCommand& c=native_slot.value;\n'
            '            c.type = GXCommandType::BPRegister;\n'
            '            c.reg = reg;\n'
            '            c.val = val;\n'
            '            NativeFreezeCopyState(c, reg); // ' + MARK + '\n'
            '            native_slot.Commit(commands);\n',
            'BP 0x61 write')
        # A display-list cache hit replays cached commands instead of re-parsing
        # them. Re-freeze from the state this replay has rebuilt: a list whose
        # 0x52 relies on geometry set outside the list must take *today's*
        # geometry, not the geometry of the parse that first filled the cache.
        parser.sub(
            '                if (c.type == GXCommandType::CPRegister)\n'
            '                    ParseCP((uint8_t)c.reg, c.val);\n'
            '                else if (c.type == GXCommandType::BPRegister)\n'
            '                    ParseBP((uint8_t)c.reg, c.val);\n',
            '                if (c.type == GXCommandType::CPRegister)\n'
            '                    ParseCP((uint8_t)c.reg, c.val);\n'
            '                else if (c.type == GXCommandType::BPRegister) {\n'
            '                    ParseBP((uint8_t)c.reg, c.val);\n'
            '                    NativeFreezeCopyState(c, (uint8_t)c.reg); // ' + MARK + ' (DL replay)\n'
            '                }\n',
            'display-list cache replay')

    renderer = Source(runtime / RENDERER, check)
    if not renderer.installed():
        renderer.sub(
            '// NATIVE_EFB_COPY_GEOMETRY_BEGIN\n',
            '// ' + MARK + ': the copy descriptor now rides on the 0x52 command\n'
            '// (GXCommand::native_copy_*), so a drain that carries two display copies\n'
            '// renders each one with its own destination and rectangle.\n'
            '// NWII_NO_GX_COPY_SNAPSHOT=1 reads the live globals again.\n'
            'static const bool s_gx_no_copy_snapshot =\n'
            '    std::getenv("NWII_NO_GX_COPY_SNAPSHOT") != nullptr;\n'
            '\n'
            '// NATIVE_EFB_COPY_GEOMETRY_BEGIN\n',
            'copy snapshot switch')
        renderer.sub(
            '        // EFB copy WITHOUT bit14 = copy to a texture in guest RAM. Grab the\n',
            '        // ' + MARK + ': every reader of the copy geometry in this BP 0x52\n'
            '        // branch now reads the command\'s own snapshot: the render-to-texture\n'
            '        // blit below, NativeLatchXfb (whose geometry guard rejects a copy it\n'
            '        // thinks is out of range), the copy-clear probe line and the copy-clear\n'
            + COPY_CLEAR_COMMENT +
            '        const bool copy_snap =\n'
            '            cmd.native_copy_valid && !s_gx_no_copy_snapshot;\n'
            '        const uint32_t copy_dest = copy_snap ? cmd.native_copy_dest : g_state.efbCopyDest;\n'
            '        const uint32_t copy_x = copy_snap ? cmd.native_copy_src_x : g_state.efbSrcX;\n'
            '        const uint32_t copy_y = copy_snap ? cmd.native_copy_src_y : g_state.efbSrcY;\n'
            '        const uint32_t copy_w = copy_snap ? cmd.native_copy_w : g_state.efbW;\n'
            '        const uint32_t copy_h = copy_snap ? cmd.native_copy_h : g_state.efbH;\n'
            '        // EFB copy WITHOUT bit14 = copy to a texture in guest RAM. Grab the\n',
            'copy geometry locals')
        renderer.sub(
            '          uint32_t dest = g_state.efbCopyDest;\n'
            '          uint32_t w = g_state.efbW, h = g_state.efbH;\n',
            '          uint32_t dest = copy_dest;\n'
            '          uint32_t w = copy_w, h = copy_h;\n',
            'render-to-texture destination')
        renderer.sub(
            '            NativeCopyEfbTexture(tex,g_state.efbSrcX,g_state.efbSrcY,w,h,NativeResolutionScale());\n',
            '            NativeCopyEfbTexture(tex,copy_x,copy_y,w,h,NativeResolutionScale());\n',
            'render-to-texture source')
        renderer.sub(
            '            NativeLatchXfb(g_state.efbCopyDest,g_state.efbSrcX,g_state.efbSrcY,g_state.efbW,g_state.efbH,NativeResolutionScale());\n',
            '            NativeLatchXfb(copy_dest,copy_x,copy_y,copy_w,copy_h,NativeResolutionScale());\n',
            'xfb latch geometry')
        renderer.sub(
            '                          << g_state.efbSrcX << "," << g_state.efbSrcY << " "\n'
            '                          << g_state.efbW << "x" << g_state.efbH << std::endl;\n',
            '                          << copy_x << "," << copy_y << " "\n'
            '                          << copy_w << "x" << copy_h << std::endl;\n',
            'copy-clear probe line')
        renderer.sub(
            '            glScissor(g_state.efbSrcX*NativeResolutionScale(),(kNativeEfbHeight-g_state.efbSrcY-g_state.efbH)*NativeResolutionScale(),g_state.efbW*NativeResolutionScale(),g_state.efbH*NativeResolutionScale());\n',
            '            glScissor(copy_x*NativeResolutionScale(),(kNativeEfbHeight-copy_y-copy_h)*NativeResolutionScale(),copy_w*NativeResolutionScale(),copy_h*NativeResolutionScale());\n',
            'copy-clear scissor')

    return [cmd_h, parser, renderer]


# ---------------------------------------------------------------------------
# Parts B + C: the raise path, the threshold, and the exact wake.
# ---------------------------------------------------------------------------

GX = 'src/hle/gx.cpp'
IOS = 'src/hle/ios.cpp'
MAIN = 'src/core/main.cpp'


def part_bc(runtime, check):
    gx = Source(runtime / GX, check)
    if not gx.installed():
        gx.sub(
            'namespace nwii::runtime {\n'
            'std::atomic<size_t> g_gx_pending_bytes{0};\n'
            'std::atomic<bool> g_gx_drain_request{false};\n'
            'std::mutex g_gx_drain_mutex;\n'
            'std::condition_variable g_gx_drain_cv;\n'
            '}\n',
            'namespace nwii::runtime {\n'
            'std::atomic<size_t> g_gx_pending_bytes{0};\n'
            'std::atomic<bool> g_gx_drain_request{false};\n'
            'std::mutex g_gx_drain_mutex;\n'
            'std::condition_variable g_gx_drain_cv;\n'
            '// ' + MARK + ': the exact-wake request. The guest increments the\n'
            '// generation under g_gx_drain_mutex and the host evaluates its wait\n'
            '// predicate under the same mutex, so a request cannot slip between the\n'
            '// predicate and the block (see native_gx_request_drain below).\n'
            'std::atomic<uint64_t> g_gx_drain_gen{0};\n'
            '// Size of the batch the previous ProcessGXFifo took off g_hw_fifo.\n'
            '// Part B\'s hysteresis: the next request needs half of it.\n'
            'std::atomic<size_t> g_gx_last_drain_bytes{0};\n'
            'static const bool g_gx_wake_exact = std::getenv("NWII_NO_GX_WAKE_EXACT") == nullptr;\n'
            'static const bool g_gx_early_drain = std::getenv("NWII_GX_EARLY_DRAIN") != nullptr && std::getenv("NWII_NO_GX_EARLY_DRAIN") == nullptr;\n'
            'static const size_t g_gx_drain_bytes = [] {\n'
            '    const char *e = std::getenv("NWII_GX_DRAIN_BYTES");\n'
            '    const long long v = e ? std::strtoll(e, nullptr, 10) : 0;\n'
            '    if (v <= 0) return size_t(16384);\n'
            '    return v > (1 << 20) ? size_t(1 << 20) : size_t(v);\n'
            '}();\n'
            '// Guest-CPU-thread only: the pending value at which the last threshold\n'
            '// request was raised. Reset when a drain consumes the buffer.\n'
            'static size_t g_gx_requested_at = 0;\n'
            'bool native_gx_wake_exact() { return g_gx_wake_exact; }\n'
            '// The one place a drain is requested from the guest thread. Called from\n'
            '// the byte threshold (native_gx_publish) and from the idle park (ios.cpp),\n'
            '// after the caller has released g_fifo_mutex, so the guest never holds two\n'
            '// locks here and the host never takes g_fifo_mutex while holding this one.\n'
            'void native_gx_request_drain() {\n'
            '    if (g_gx_wake_exact) {\n'
            '        {\n'
            '            std::lock_guard<std::mutex> lk(g_gx_drain_mutex);\n'
            '            g_gx_drain_gen.fetch_add(1, std::memory_order_release);\n'
            '        }\n'
            '        g_gx_drain_cv.notify_one();\n'
            '        return;\n'
            '    }\n'
            '    g_gx_drain_request.store(true, std::memory_order_release);\n'
            '    g_gx_drain_cv.notify_one();\n'
            '}\n'
            '}\n',
            'drain request objects')
        gx.sub(
            '    if (!native_drain_buf.empty())\n'
            '        nwii::runtime::NativeAppendFifo(s_parse_buf, native_drain_buf);\n',
            '    const size_t drained_size = native_drain_buf.size();\n'
            '    if (!native_drain_buf.empty())\n'
            '        nwii::runtime::NativeAppendFifo(s_parse_buf, native_drain_buf);\n'
            '    // ' + MARK + ': what this drain had to render, for part B\'s hysteresis.\n'
            '    // Capture before NativeAppendFifo transfers or clears the source.\n'
            '    g_gx_last_drain_bytes.store(drained_size, std::memory_order_relaxed);\n',
            'drain batch size')
        gx.sub(
            '        nwii::runtime::NativeAppendFifo(g_hw_fifo, g_cpu_fifo);\n'
            '        g_gx_pending_bytes.fetch_add(published, std::memory_order_relaxed);\n'
            '    }\n'
            '    g_cpu_fifo.clear();\n'
            '}\n',
            '        nwii::runtime::NativeAppendFifo(g_hw_fifo, g_cpu_fifo);\n'
            '        g_gx_pending_bytes.fetch_add(published, std::memory_order_relaxed);\n'
            '    }\n'
            '    g_cpu_fifo.clear();\n'
            '    // ' + MARK + ': ask for a drain once enough new bytes are waiting, so the\n'
            '    // host renders them while the guest is still building the frame instead of\n'
            '    // after it has parked. g_fifo_mutex is already released above.\n'
            '    //\n'
            '    // The request is a hint, not a transfer: the bytes stay in g_hw_fifo and\n'
            '    // the per-field ProcessGXFifo drains whatever is left, so nothing can be\n'
            '    // stranded, and both paths parse the same FIFO in the same order.\n'
            '    //\n'
            '    // One request per `trigger` bytes: `pending` is not reset until the host\n'
            '    // drains, so testing pending alone would signal on every publish above the\n'
            '    // threshold. `trigger` is max(NWII_GX_DRAIN_BYTES, last_drain_bytes/2), so\n'
            '    // a host that falls behind sees larger batches rather than more of them.\n'
            '    if (!g_gx_early_drain) return;\n'
            '    const size_t pending = g_gx_pending_bytes.load(std::memory_order_relaxed);\n'
            '    if (pending < g_gx_requested_at) g_gx_requested_at = 0; // a drain emptied it\n'
            '    const size_t last = g_gx_last_drain_bytes.load(std::memory_order_relaxed);\n'
            '    const size_t trigger =\n'
            '        (last / 2) > g_gx_drain_bytes ? (last / 2) : g_gx_drain_bytes;\n'
            '    if (pending >= g_gx_requested_at + trigger) {\n'
            '        g_gx_requested_at = pending;\n'
            '        nwii::runtime::native_gx_request_drain();\n'
            '    }\n'
            '}\n',
            'publish threshold')

    ios = Source(runtime / IOS, check)
    if not ios.installed():
        ios.sub(
            'extern std::atomic<bool> g_gx_drain_request;\n'
            'extern std::condition_variable g_gx_drain_cv;\n',
            'extern std::atomic<bool> g_gx_drain_request;\n'
            'extern std::condition_variable g_gx_drain_cv;\n'
            'extern void native_gx_request_drain(); // ' + MARK + '\n',
            'drain request declaration')
        ios.sub(
            '  // NATIVE_GX_IDLE_DRAIN: guest idle with GX bytes pending -> ask the host loop to drain now.\n'
            '  { static const bool idle_drain = std::getenv("NWII_NO_GX_IDLE_DRAIN") == nullptr;\n'
            '    if (idle_drain && ctx.pc == 0x80492408u && g_gx_pending_bytes.load(std::memory_order_relaxed) > 0)\n'
            '      { g_gx_drain_request.store(true, std::memory_order_release); g_gx_drain_cv.notify_one(); } }\n',
            '  // NATIVE_GX_IDLE_DRAIN: guest idle with GX bytes pending -> ask the host loop to drain now.\n'
            '  // ' + MARK + ': the idle park and part B\'s byte threshold share one raise path,\n'
            '  // so the wake is exact whenever NWII_NO_GX_WAKE_EXACT is unset (and the\n'
            '  // legacy flag + 2 ms re-poll when it is set). Still fires on exactly the same\n'
            '  // condition and with exactly the same pending-bytes test.\n'
            '  { static const bool idle_drain = std::getenv("NWII_NO_GX_IDLE_DRAIN") == nullptr;\n'
            '    if (idle_drain && ctx.pc == 0x80492408u && g_gx_pending_bytes.load(std::memory_order_relaxed) > 0)\n'
            '      nwii::runtime::native_gx_request_drain(); }\n',
            'idle-PC drain request')

    main = Source(runtime / MAIN, check)
    if not main.installed():
        main.sub(
            'namespace nwii::runtime { extern std::atomic<bool> g_gx_drain_request; extern std::mutex g_gx_drain_mutex; extern std::condition_variable g_gx_drain_cv; }\n',
            'namespace nwii::runtime { extern std::atomic<bool> g_gx_drain_request; extern std::mutex g_gx_drain_mutex; extern std::condition_variable g_gx_drain_cv; extern std::atomic<uint64_t> g_gx_drain_gen; extern bool native_gx_wake_exact(); }\n'
            '// ' + MARK + ': the last drain generation this host loop served. File\n'
            '// scope rather than a static local so the wait predicate below needs no\n'
            '// capture (a lambda may not capture an object with static storage\n'
            '// duration). Only the host thread touches it.\n'
            'static uint64_t g_native_gx_drain_seen = 0;\n',
            'drain declarations')
        main.sub(
            '    // NATIVE_GX_IDLE_DRAIN: service on-demand GX drains until the field deadline.\n'
            '    extern void ProcessGXFifo();\n'
            '    for (;;) {\n'
            '      const auto wait_now = std::chrono::steady_clock::now();\n'
            '      if (wait_now >= field_deadline) break;\n'
            '      if (nwii::runtime::g_gx_drain_request.exchange(false, std::memory_order_acq_rel)) {\n'
            '        if (!headless) { glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo); glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight()); }\n'
            '        ProcessGXFifo();\n'
            '        continue;\n'
            '      }\n'
            '      std::unique_lock<std::mutex> drain_lock(nwii::runtime::g_gx_drain_mutex);\n'
            '      { native_perf_phase::Scope native_perf_phase_drain(native_perf_phase::Phase_HostDrainWait); // NATIVE_PERF_PHASE_V1_DRAIN\n'
            '        nwii::runtime::g_gx_drain_cv.wait_until(drain_lock, std::min(field_deadline, wait_now + std::chrono::milliseconds(2)),\n'
            '                                                [] { return nwii::runtime::g_gx_drain_request.load(std::memory_order_acquire); }); }\n'
            '    }\n',
            '    // NATIVE_GX_IDLE_DRAIN: service on-demand GX drains until the field deadline.\n'
            '    extern void ProcessGXFifo();\n'
            '    for (;;) {\n'
            '      const auto wait_now = std::chrono::steady_clock::now();\n'
            '      if (wait_now >= field_deadline) break;\n'
            '      if (nwii::runtime::native_gx_wake_exact()) {\n'
            '        // ' + MARK + ': exact wake. The guest raises a request by incrementing\n'
            '        // g_gx_drain_gen under g_gx_drain_mutex; the predicate is evaluated\n'
            '        // and the generation consumed under the same mutex, so a request can\n'
            '        // never land between the check and the block - no 2 ms re-poll is\n'
            '        // needed and the field deadline is the only timeout. A request that\n'
            '        // arrives during the drain below is seen on the next iteration\n'
            '        // (g_native_gx_drain_seen only advances to the generation this drain\n'
            '        // consumed).\n'
            '        std::unique_lock<std::mutex> drain_lock(nwii::runtime::g_gx_drain_mutex);\n'
            '        { native_perf_phase::Scope native_perf_phase_drain(native_perf_phase::Phase_HostDrainWait); // NATIVE_PERF_PHASE_V1_DRAIN\n'
            '          nwii::runtime::g_gx_drain_cv.wait_until(drain_lock, field_deadline, [] {\n'
            '            return nwii::runtime::g_gx_drain_gen.load(std::memory_order_acquire) != g_native_gx_drain_seen; }); }\n'
            '        if (nwii::runtime::g_gx_drain_gen.load(std::memory_order_acquire) == g_native_gx_drain_seen)\n'
            '          break; // the field deadline passed with nothing requested\n'
            '        if (std::chrono::steady_clock::now() >= field_deadline)\n'
            '          break; // requested, but this field is over: the next one serves it\n'
            '        g_native_gx_drain_seen = nwii::runtime::g_gx_drain_gen.load(std::memory_order_acquire);\n'
            '        drain_lock.unlock();\n'
            '        if (!headless) { glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo); glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight()); }\n'
            '        ProcessGXFifo();\n'
            '        continue;\n'
            '      }\n'
            '      if (nwii::runtime::g_gx_drain_request.exchange(false, std::memory_order_acq_rel)) {\n'
            '        if (!headless) { glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo); glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight()); }\n'
            '        ProcessGXFifo();\n'
            '        continue;\n'
            '      }\n'
            '      std::unique_lock<std::mutex> drain_lock(nwii::runtime::g_gx_drain_mutex);\n'
            '      { native_perf_phase::Scope native_perf_phase_drain(native_perf_phase::Phase_HostDrainWait); // NATIVE_PERF_PHASE_V1_DRAIN\n'
            '        nwii::runtime::g_gx_drain_cv.wait_until(drain_lock, std::min(field_deadline, wait_now + std::chrono::milliseconds(2)),\n'
            '                                                [] { return nwii::runtime::g_gx_drain_request.load(std::memory_order_acquire); }); }\n'
            '    }\n',
            'paced drain wait')

    return [gx, ios, main]


def main():
    global RESTORE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=ROOT,
                        help='tree to patch (default: this repo)')
    parser.add_argument('--check', action='store_true',
                        help='assert every anchor and write nothing')
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    RESTORE = args.restore
    runtime = args.root / RUNTIME

    files = part_a(runtime, args.check) + part_bc(runtime, args.check)
    if RESTORE:
        for source in files:
            if MARK in source.text:
                raise SystemExit('%s: incomplete restore in %s; refusing a partial tree' %
                                 (MARK, source.path))
    written = [f for f in files if f.save()]
    edits = sum(f.edits for f in files)
    if args.check:
        print('%s: anchors ok (%d edit sites, nothing written)' % (MARK, edits))
        return
    if not edits:
        print('%s: already installed (all %d files carry the marker)'
              % (MARK, len(files)))
        return
    print('%s: %d edit sites in %d file(s); NWII_NO_GX_COPY_SNAPSHOT / '
          'NWII_NO_GX_EARLY_DRAIN / NWII_NO_GX_WAKE_EXACT switch the three parts '
          'off, NWII_GX_DRAIN_BYTES (default 16384) sets the threshold'
          % (MARK, edits, len(written)))


if __name__ == '__main__':
    main()
