"""Cut the guest thread's per-poll and per-print overhead (MoM runtime lane).

Marker: NATIVE_MOM_GUEST_RUNTIME_TRIM_V1.

The guest thread is the wall in this port: 917 ms/s of one core in gameplay, and
the CPU profile attributes almost none of it to generated guest code.  In order:
ntdll (syscalls) 27 %, ``process_pending_callbacks`` 15.8 % (leaf), ucrtbase
3.6 %, libwinpthread 3.3 %, ``run_game`` 3.9 %, generated ``func_*`` 3-6 %.  The
poll fast path is running - ``[Sample] fast=20233576011`` over a 1480 s session
is 13.7 M fast polls/s - so what is left is the fixed cost of *reaching* it.

Seven independent items, each with its own switch, all defaulting to the optimised
behaviour.  Nothing here changes a firing count, an interrupt, the ``inst_count``
value, the poll throttle (1/16), the DEC test, the IPC/DI countdown or the
idle-park gate.

1. NWII_MOM_TRIM=fcounter   ``ios.cpp``   -- plain counter instead of a locked RMW
2. NWII_MOM_TRIM=gxpub      ``ios.cpp`` + ``gx.cpp`` -- inline the empty test in
                               front of native_gx_publish()
3. NWII_MOM_TRIM=sample     ``ios.cpp``   -- inline the sampler's flag test
4. NWII_MOM_TRIM=verbose    ``ios.cpp`` + ``export/main_output.cpp`` -- one cached
                               log flag (the prints themselves are NWII_LOG_VERBOSE)
5. NWII_MOM_TRIM=dispatch   ``export/main_output.cpp`` -- per-dispatch cleanup in run_game()
6. NWII_MOM_TRIM=wgp        ``gx.cpp``    -- hoist wgp_push's five env probes
7. NWII_MOM_TRIM=diagclock  ``ios.cpp``   -- the 5 s DIAG report read
                               steady_clock on every due poll; read it every
                               256th due poll (CotT's NATIVE_DIAG_CLOCK_THROTTLE_V1)
                               and resolve NWII_NO_DIAG once at namespace scope
                               instead of a guarded function-local static.

``NWII_MOM_TRIM`` is a comma-separated subset of ``fcounter,gxpub,sample,verbose,
dispatch,wgp,diagclock``; unset (or ``all``) means all seven, ``none`` means none
of the behaviour changes (the reference build for an A/B of the whole lane).

Item 1-3 are the ones that pay the 13.7 M/s bill.  ``g_native_fast_polls`` was
written with ``fetch_add(1, relaxed)`` - on x86 a ``lock xadd``, ~15-20 cycles on
a line that only this thread touches - read by ``native_poll_sample`` and by
nothing else.  ``native_gx_publish()`` was called before the fast-path test even
though its first statement is ``if (g_cpu_fifo.empty()) return;``, and
``native_poll_sample`` was an out-of-line function whose body opened with two
guarded (``__cxa_guard``) function-local statics.  None of the three can be
observed by the guest: all three touch host diagnostics only.

Per-item evidence that the shortcuts are exact:

  * the counter is read at exactly two places (the fast-path increment and the
    sampler's print), both in this file, both on the guest thread; it is
    ``static`` here, so no other translation unit can see it.
    ``process_pending_callbacks`` is entered only from the guest code itself -
    ``run_game``, ``interpreter.cpp`` (three sites), ``ios.cpp:717`` and the
    generated chunk polling sites - and the port has exactly one guest thread,
    so a plain ``uint64_t`` is not a race.
  * ``native_gx_publish`` publishes nothing when the staging buffer is empty, so
    testing ``empty()`` at the call site publishes on exactly the same polls that
    used to publish.  The host still drains the same bytes at the same moment;
    there is no "skip a publish and catch up later" arm here at all.
  * the sampler's flag is launch-fixed (``NWII_SAMPLE``), so hoisting it out of
    the function body changes no branch outcome.

What is deliberately *not* done, and why:

  * No publish is deferred below the fast-path gate.  That variant is safe for
    the FIFO parser (it carries partial GX commands and the host drains per
    field), but it still moves *when* the host drains, which is out of scope for
    a stage whose whole claim is "same bytes, same moment".
  * ``[Sched]``'s block (``ios.cpp`` ~1643) is **not** gated as a whole: below its
    thread dump it re-saves the guest context and longjmps into the decrementer
    handler.  Only the printing is gated; the block still runs on every 500th
    timer interrupt.  (Gating the whole ``if`` would silently drop that
    interrupt - it fires once per 500 DEC fires, so it would look like a
    rare, tolerated hiccup rather than a dropped IRQ.)
  * ``native_wpad_poll``'s throttle, the ``poll_clock_phase`` cadence, the DEC
    test, the IPC/DI countdown and ``native_idle`` are untouched.

Idempotent: every edit is an exact inverse pair (``--restore`` puts the tree back
byte for byte), so a second run is a byte-identical no-op.  The stage edits
``export/main_output.cpp``, so it must run after every regeneration and after the
stages that install the text it anchors on (``fix_poll_fast_path.py`` for the
fast path and the sampler, ``fix_gx_fifo_batch.py`` for ``g_cpu_fifo``) - i.e.
last in ``tools/apply_stages.py``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_GUEST_RUNTIME_TRIM_V1'

ROOT = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
IOS = RUNTIME + '/src/hle/ios.cpp'
GX = RUNTIME + '/src/hle/gx.cpp'
VERBOSE = RUNTIME + '/include/runtime/native_verbose_log.h'
OVERLAY = 'overlays/native_verbose_log.h'
MAIN = 'export/main_output.cpp'

# ---------------------------------------------------------------------------
# Item 4's shared flag.  Installed to overlays/ (the record of what the stage
# added) and to the runtime include dir the build actually sees.
# ---------------------------------------------------------------------------

HEADER = '''#pragma once
// NATIVE_MOM_VERBOSE_GUEST_LOG_V1: one cached launch-time switch for the guest
// thread's unconditional progress prints.
//
// Every "[HLE Callback] Dispatching", "[HLE PI] Context restore", "[Sched]" and
// "[NativeWPAD]" line is formatted on the guest CPU thread even though it only
// describes what the port already did, and the callback line and the dispatcher
// report also end in std::endl - a flush plus a write syscall per callback.  With
// ~27 % of guest CPU in ntdll and 3.6 % in ucrtbase, none of that is free.
//
// They are off unless NWII_LOG_VERBOSE=1.  Error and warning lines are untouched:
// std::cerr uses, "[NativeWPAD] Cannot open script", "[NativeWPAD] Invalid
// timeline", the run_game zero-PC dump, the heartbeat and every [Sample]/[DIAG]
// counter line keep their old behaviour (the counters are the measurement).
//
// A plain bool read on one thread: a disabled print costs one load and a
// predicted branch, and the flag is resolved once at startup.  It is declared
// `extern` and defined in ios.cpp rather than being an inline variable here: an
// inline variable with a getenv() initializer is dynamically initialised, so
// every use site carries a guard variable and a "is it initialised yet" branch.
namespace nwii::runtime {
extern const bool g_log_verbose;
}
'''

# ---------------------------------------------------------------------------
# ios.cpp
# ---------------------------------------------------------------------------

IOS_EDITS = [
    # --- item 4: the include and the flag's one definition -------------------
    ('''#include "runtime/native_perf_census.h" // NATIVE_PERF_CENSUS_V1_INCLUDE_IOS
''',
     '''#include "runtime/native_perf_census.h" // NATIVE_PERF_CENSUS_V1_INCLUDE_IOS
#include "runtime/native_verbose_log.h" // NATIVE_MOM_VERBOSE_GUEST_LOG_V1
'''),

    ('''namespace nwii::runtime {
    std::string read_guest_string(CPUContext &ctx, uint32_t addr, int max_len = 256);
    bool valid_callback(uint32_t cb);
}
''',
     '''namespace nwii::runtime {
    std::string read_guest_string(CPUContext &ctx, uint32_t addr, int max_len = 256);
    bool valid_callback(uint32_t cb);
    // NATIVE_MOM_VERBOSE_GUEST_LOG_V1: the one definition of the launch-time
    // progress-log switch (declared in runtime/native_verbose_log.h).  Defined
    // here instead of inline in the header so every use is a plain load.
    const bool g_log_verbose = std::getenv("NWII_LOG_VERBOSE") != nullptr;
}
'''),

    # --- item 1: the counter ------------------------------------------------
    ('''static uint64_t g_diag_dec_fires = 0; // NATIVE_DIAG_TIMELINE_V1
static std::atomic<uint64_t> g_native_fast_polls{0}; // NATIVE_FAST_POLL_PATH_V1
''',
     '''static uint64_t g_diag_dec_fires = 0; // NATIVE_DIAG_TIMELINE_V1
// NATIVE_MOM_FAST_POLL_COUNTER_V1: NATIVE_FAST_POLL_PATH_V1's counter was a
// `fetch_add(1, relaxed)` on every fast poll - a locked read-modify-write on a
// shared cache line 13.7M times a second (measured `fast=20233576011` over
// 1480 s) for one diagnostic.  It is written only by the fast-path return below
// and read only by the sampler, and both run on the guest thread:
// process_pending_callbacks is entered only from run_game, interpreter.cpp
// (three sites) and ios.cpp:717, never from a host thread.  So the default is a
// plain uint64_t, kept only while its one reader is on.
//   NWII_FAST_POLL_COUNTER_ALWAYS=1  count every fast poll, plain increment
//   NWII_FAST_POLL_COUNTER_LOCKED=1  the pre-stage atomic fetch_add
static std::atomic<uint64_t> g_native_fast_polls_atomic{0}; // NWII_FAST_POLL_COUNTER_LOCKED only
static uint64_t g_native_fast_polls = 0;
static const int g_fast_poll_counter_mode = []() {
  if (std::getenv("NWII_FAST_POLL_COUNTER_LOCKED")) return 2;
  if (std::getenv("NWII_FAST_POLL_COUNTER_ALWAYS")) return 1;
  return 0;
}();
static inline uint64_t native_fast_poll_count() {
  return g_fast_poll_counter_mode == 2
             ? g_native_fast_polls_atomic.load(std::memory_order_relaxed)
             : g_native_fast_polls;
}
'''),

    # --- item 3: the sampler ------------------------------------------------
    ('''// NATIVE_FAST_POLL_PATH_V1: the cheap sampled diagnostic, shared by the
// fast and the slow poll so its cadence does not depend on which one runs.
static void native_poll_sample(CPUContext &ctx) {
  static bool sample = NativeLaunchFlag_NWII_SAMPLE() != nullptr;
  static uint64_t n = 0;
  if (sample && (++n % 400000) == 0)
    std::cout << "[Sample] pc=0x" << std::hex << ctx.pc << " lr=0x" << ctx.lr
              << " r3=0x" << ctx.gpr[3] << " inst=" << std::dec << ctx.inst_count
              << " fast=" << g_native_fast_polls << std::endl;
}
''',
     '''// NATIVE_FAST_POLL_PATH_V1: the cheap sampled diagnostic, shared by the
// fast and the slow poll so its cadence does not depend on which one runs.
// NATIVE_MOM_POLL_SAMPLE_INLINE_V1: it was an out-of-line function whose body
// opened with two guarded function-local statics (the launch flag and the
// counter), so a fast poll with sampling off still paid a call, two
// __cxa_guard loads and two branches.  Flag and counter are namespace-scope
// objects now (guest thread only, like the poll itself), the test is inline and
// the formatting/flush body is cold: sampling off costs one byte load and a
// predicted branch.  The print cadence (1 in 400000 polls) is unchanged.
// NWII_POLL_SAMPLE_LEGACY=1 restores the guarded out-of-line shape.
static const bool native_poll_sample_flag = NativeLaunchFlag_NWII_SAMPLE() != nullptr;
static const bool native_poll_sample_legacy = std::getenv("NWII_POLL_SAMPLE_LEGACY") != nullptr;
static uint64_t native_poll_sample_n = 0;
static void native_poll_sample_print(CPUContext &ctx) {
  std::cout << "[Sample] pc=0x" << std::hex << ctx.pc << " lr=0x" << ctx.lr
            << " r3=0x" << ctx.gpr[3] << " inst=" << std::dec << ctx.inst_count
            << " fast=" << native_fast_poll_count() << std::endl;
}
static void native_poll_sample_legacy_body(CPUContext &ctx) {
  static bool sample = NativeLaunchFlag_NWII_SAMPLE() != nullptr;
  static uint64_t n = 0;
  if (sample && (++n % 400000) == 0) native_poll_sample_print(ctx);
}
static inline void native_poll_sample(CPUContext &ctx) {
  if (native_poll_sample_legacy) { native_poll_sample_legacy_body(ctx); return; }
  if (native_poll_sample_flag && (++native_poll_sample_n % 400000) == 0)
    native_poll_sample_print(ctx);
}
'''),

    # --- item 2: the staging buffer becomes visible --------------------------
    ('''extern void native_gx_publish(); // NATIVE_GX_PRIVATE_FIFO_V1
''',
     '''extern void native_gx_publish(); // NATIVE_GX_PRIVATE_FIFO_V1
// NATIVE_MOM_GX_PUBLISH_INLINE_V1: gx.cpp's staging buffer (it lost its `static`
// for this).  Guest-CPU-thread-only, so a poll can test it inline instead of
// calling publish to find out that it is empty.
extern std::vector<uint8_t> g_cpu_fifo;
'''),

    # --- item 2: the switch, at namespace scope so the poll pays no guard ----
    ('''bool process_pending_callbacks(CPUContext &ctx) {
''',
     '''// NATIVE_MOM_GX_PUBLISH_INLINE_V1: launch-fixed switch for the publish test.
static const bool g_gx_publish_always = std::getenv("NWII_NO_GX_PUBLISH_INLINE") != nullptr;

bool process_pending_callbacks(CPUContext &ctx) {
'''),

    # --- item 2: the call site ----------------------------------------------
    ('''  native_gx_publish();
  // NATIVE_CALLBACK_FLAGS_V1
''',
     '''  // NATIVE_MOM_GX_PUBLISH_INLINE_V1: native_gx_publish() opens with
  // `if (g_cpu_fifo.empty()) return;`, so on the (common) poll that staged no GX
  // byte it was an out-of-line call that did nothing.  Testing the staging buffer
  // here publishes on exactly the polls that used to publish, so the host drains
  // the same bytes at the same moment and no byte can be stranded - only the
  // empty call disappears.  NWII_NO_GX_PUBLISH_INLINE=1 restores the bare call.
  if (g_gx_publish_always || !g_cpu_fifo.empty()) native_gx_publish();
  // NATIVE_CALLBACK_FLAGS_V1
'''),

    # --- item 1: the increment ----------------------------------------------
    ('''        g_native_fast_polls.fetch_add(1, std::memory_order_relaxed);
        native_poll_sample(ctx);
''',
     '''        // NATIVE_MOM_FAST_POLL_COUNTER_V1: see the declaration above.
        if (g_fast_poll_counter_mode == 2)
          g_native_fast_polls_atomic.fetch_add(1, std::memory_order_relaxed);
        else if (g_fast_poll_counter_mode == 1 || native_poll_sample_flag)
          ++g_native_fast_polls;
        native_poll_sample(ctx);
'''),

    # --- item 4: [NativeWPAD] connected -------------------------------------
    ('''      std::cout<<"[NativeWPAD] SDL controller connected="<<connected<<"\\n";
''',
     '''      if (g_log_verbose) // NATIVE_MOM_VERBOSE_GUEST_LOG_V1
      std::cout<<"[NativeWPAD] SDL controller connected="<<connected<<"\\n";
'''),

    # --- item 4: [NativeWPAD] per event -------------------------------------
    ('''    std::cout<<"[NativeWPAD] ms="<<ms<<" buttons="<<std::hex<<s.buttons<<std::dec
             <<" connected="<<s.connected<<"\\n";
''',
     '''    if (g_log_verbose) // NATIVE_MOM_VERBOSE_GUEST_LOG_V1
    std::cout<<"[NativeWPAD] ms="<<ms<<" buttons="<<std::hex<<s.buttons<<std::dec
             <<" connected="<<s.connected<<"\\n";
'''),

    # --- item 4: [HLE PI] Context restore -----------------------------------
    ('''      if (ctx.pc == 0 || (restore_count % 64) == 1) {
''',
     '''      if (g_log_verbose && // NATIVE_MOM_VERBOSE_GUEST_LOG_V1
          (ctx.pc == 0 || (restore_count % 64) == 1)) {
'''),

    # --- item 4: [Sched] - print only, never the DEC handler dispatch --------
    ('''          uint32_t cur = ctx.mmu.read32(0x800000E4);
          uint32_t th = ctx.mmu.read32(0x800000DC);
          std::cout << "[Sched] cur=0x" << std::hex << cur << std::dec << "\\n";
          int guard = 0;
          while (th != 0 && guard++ < 24) {
''',
     '''          // NATIVE_MOM_VERBOSE_GUEST_LOG_V1: only the dump is gated.  This block
          // also re-saves the guest context and longjmps into the decrementer
          // handler at its end, so it must still run on every 500th timer
          // interrupt; gating the whole block would drop that interrupt.
          uint32_t cur = ctx.mmu.read32(0x800000E4);
          uint32_t th = ctx.mmu.read32(0x800000DC);
          if (g_log_verbose)
              std::cout << "[Sched] cur=0x" << std::hex << cur << std::dec << "\\n";
          int guard = 0;
          while (g_log_verbose && th != 0 && guard++ < 24) {
'''),

    # --- item 4: [HLE Callback] Dispatching (std::endl = flush + syscall) ----
    ('''  std::cout << "[HLE Callback] Dispatching cb=0x" << std::hex << cb.cb_addr
            << " arg1=0x" << cb.arg1 << " arg2=0x" << cb.arg2 << " from PC=0x"
            << ctx.pc << " LR=0x" << ctx.lr << " r1=0x" << ctx.gpr[1]
            << std::dec << std::endl;
''',
     '''  // NATIVE_MOM_VERBOSE_GUEST_LOG_V1: this one ends in std::endl, i.e. a flush
  // and a write syscall on the guest thread for every dispatched callback.
  if (g_log_verbose)
  std::cout << "[HLE Callback] Dispatching cb=0x" << std::hex << cb.cb_addr
            << " arg1=0x" << cb.arg1 << " arg2=0x" << cb.arg2 << " from PC=0x"
            << ctx.pc << " LR=0x" << ctx.lr << " r1=0x" << ctx.gpr[1]
            << std::dec << std::endl;
'''),

    # --- item 7: the launch flag, resolved once at namespace scope ------------
    # The literal function header stays in place under both this edit and the
    # gxpub one above, so they compose in either order.
    ('''bool process_pending_callbacks(CPUContext &ctx) {
''',
     '''// NATIVE_MOM_DIAG_CLOCK_THROTTLE_V1: NWII_NO_DIAG is launch-fixed, but the
// DIAG block read it through a guarded function-local static on every poll.
static const bool native_diag_report_on = std::getenv("NWII_NO_DIAG") == nullptr;

bool process_pending_callbacks(CPUContext &ctx) {
'''),

    # --- item 7: every-256th-due-poll clock read (CotT's throttle, re-keyed) --
    ('''    static const bool diag_on = std::getenv("NWII_NO_DIAG") == nullptr;
    static const auto diag_t0 = std::chrono::steady_clock::now();
    static auto diag_last = diag_t0;
    if (diag_on && poll_clock_due) {
''',
     '''    // NATIVE_MOM_DIAG_CLOCK_THROTTLE_V1: the report prints every 5 s, but
    // this block read steady_clock on every due poll (~440k/s) just to find
    // out that the window had not elapsed.  The clock read now runs every
    // 256th due poll; the 5 s comparison and the report itself are unchanged.
    static const auto diag_t0 = std::chrono::steady_clock::now();
    static auto diag_last = diag_t0;
    static uint32_t diag_phase = 0;
    if (native_diag_report_on && poll_clock_due && ((++diag_phase) & 255u) == 0) {
'''),
]

# ---------------------------------------------------------------------------
# gx.cpp
# ---------------------------------------------------------------------------

GX_EDITS = [
    # --- item 2: the staging buffer loses internal linkage -------------------
    ('''static std::vector<uint8_t> g_cpu_fifo;
''',
     '''// NATIVE_MOM_GX_PUBLISH_INLINE_V1: no longer `static` - ios.cpp tests empty()
// inline before calling native_gx_publish(), so a poll that staged no GX byte
// does not pay the call.  Still guest-CPU-thread-only.
std::vector<uint8_t> g_cpu_fifo;
'''),

    # --- item 6: one struct, resolved once, instead of five guarded statics --
    ('''static inline void wgp_push(uint8_t b) {

    {
        static const bool ring_probe = std::getenv("NWII_GX_DESYNC_PROBE") != nullptr;
        if (ring_probe) {
''',
     '''// NATIVE_MOM_WGP_PUSH_GUARDS_V1: the five probe/dump switches wgp_push tested
// were guarded function-local statics, so every GX byte read five __cxa_guard
// bytes and branched on them to discover that no probe is on.  They are
// launch-fixed, so they are resolved once here and read as plain members.
// (Nothing in the shipping configuration reaches wgp_push at all: with no probe
// set, native_wgp_diagnostics_flag is false, native_ordinary_fifo_fast() is true
// and GX_WGPIPE_Write* take native_wgp_bulk - so this pays in a probe run, where
// the byte path matters most.)
struct WgpProbeFlags {
    bool ring, thread, trace;
    const char *dump, *seg;
};
static const WgpProbeFlags g_wgp_probe = [] {
    WgpProbeFlags f{};
    f.ring = std::getenv("NWII_GX_DESYNC_PROBE") != nullptr;
    f.thread = std::getenv("NWII_GX_THREAD_PROBE") != nullptr;
    f.dump = std::getenv("NWII_GXDUMP");
    f.seg = std::getenv("NWII_WGSEG");
    f.trace = std::getenv("NWII_WGTRACE") != nullptr;
    return f;
}();
static inline void wgp_push(uint8_t b) {

    {
        if (g_wgp_probe.ring) {
'''),

    ('''        static const bool thread_probe = std::getenv("NWII_GX_THREAD_PROBE") != nullptr;
        if (thread_probe) {
''',
     '''        if (g_wgp_probe.thread) {
'''),

    ('''    static const char* s_dump_path = std::getenv("NWII_GXDUMP");
    static const char* s_seg_path = std::getenv("NWII_WGSEG");
    if (s_dump_path) {
''',
     '''    const char* s_dump_path = g_wgp_probe.dump;
    const char* s_seg_path = g_wgp_probe.seg;
    if (s_dump_path) {
'''),

    ('''    static const bool wgtrace = std::getenv("NWII_WGTRACE") != nullptr;
    if (wgtrace) {
''',
     '''    if (g_wgp_probe.trace) {
'''),
]

# ---------------------------------------------------------------------------
# export/main_output.cpp
# ---------------------------------------------------------------------------

MAIN_EDITS = [
    # --- item 4: the include ------------------------------------------------
    ('''#include "runtime/config.h"
''',
     '''#include "runtime/config.h"
#include "runtime/native_verbose_log.h" // NATIVE_MOM_VERBOSE_GUEST_LOG_V1
'''),

    # --- item 5: the report counter -----------------------------------------
    ('''namespace nwii::runtime { bool native_wpad_dispatch(CPUContext&, uint32_t); }
// --- Entry Point Wrapper ---
''',
     '''namespace nwii::runtime { bool native_wpad_dispatch(CPUContext&, uint32_t); }
// NATIVE_MOM_DISPATCH_TRIM_V1: guest dispatches since the last "Dispatcher PC"
// report.  It replaces `(++ctx.inst_count % 100000) == 0`, which put a
// magic-multiply chain on every guest bl/blr for a line that now only prints when
// NWII_LOG_VERBOSE=1.
static uint32_t g_dispatch_report_n = 0;
// --- Entry Point Wrapper ---
'''),

    # --- item 5: pc_history index -------------------------------------------
    ('''      pc_history[pc_history_idx] = ctx.pc;
      pc_history_idx = (pc_history_idx + 1) % 10;
''',
     '''      pc_history[pc_history_idx] = ctx.pc;
      // NATIVE_MOM_DISPATCH_TRIM_V1: compare-and-reset instead of % 10.  The index
      // is always in [0,10) before the increment, so the sequence is identical.
      if (++pc_history_idx == 10) pc_history_idx = 0;
'''),

    # --- item 5: the report and the WPAD range test --------------------------
    ('''        if ((++ctx.inst_count % 100000) == 0) std::cout << "Dispatcher PC: 0x" << std::hex << target << std::endl;
        // NATIVE_SMALL_DISPATCH_CALL_V1
        if (nwii::runtime::native_wpad_dispatch(ctx,target)) continue;
''',
     '''        // NATIVE_MOM_DISPATCH_TRIM_V1: the instruction counter is still bumped
        // exactly once per dispatch and keeps the same value; only the modulo
        // (and the print, now off unless NWII_LOG_VERBOSE=1) is replaced by a
        // countdown.
        ++ctx.inst_count;
        if (nwii::runtime::g_log_verbose && ++g_dispatch_report_n >= 100000) {
            g_dispatch_report_n = 0;
            std::cout << "Dispatcher PC: 0x" << std::hex << target << std::endl;
        }
        // NATIVE_SMALL_DISPATCH_CALL_V1
        // NATIVE_MOM_DISPATCH_TRIM_V1: CotT's range test around the WPAD HLE
        // dispatch.  native_wpad_dispatch's switch accepts only targets inside
        // 0x804A4CE0..0x804A788C, and its `default: return false;` runs before any
        // side effect (including the physical-pad advance), so an out-of-range
        // target could only ever have said no.
        if (target >= 0x804A4CE0u && target <= 0x804A788Cu &&
            nwii::runtime::native_wpad_dispatch(ctx,target)) continue;
'''),
]

PARTS = ('fcounter', 'gxpub', 'sample', 'verbose', 'dispatch', 'wgp', 'diagclock')

# Which edits belong to which item.  The two includes and the shared switch
# declarations are attributed to the item that needs them.
OWNER = {
    IOS: [('verbose', 0), ('verbose', 1), ('fcounter', 2), ('sample', 3),
          ('gxpub', 4), ('gxpub', 5), ('gxpub', 6), ('fcounter', 7),
          ('verbose', 8), ('verbose', 9), ('verbose', 10), ('verbose', 11),
          ('verbose', 12), ('diagclock', 13), ('diagclock', 14)],
    GX: [('gxpub', 0), ('wgp', 1), ('wgp', 2), ('wgp', 3), ('wgp', 4)],
    MAIN: [('verbose', 0), ('dispatch', 1), ('dispatch', 2), ('dispatch', 3)],
}
EDITS = {IOS: IOS_EDITS, GX: GX_EDITS, MAIN: MAIN_EDITS}
TAG = {IOS: 'ios.cpp', GX: 'gx.cpp', MAIN: 'main_output.cpp'}


def read(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline='') as handle:
        return handle.read().replace('\r\n', '\n')


def write(path, text):
    with open(path, 'rb') as handle:
        crlf = b'\r\n' in handle.read(4096)
    data = text.replace('\n', '\r\n') if crlf else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as handle:
        handle.write(data)


def enabled(selection):
    if not selection or selection == 'all':
        return set(PARTS)
    if selection == 'none':
        return set()
    picked = {part.strip() for part in selection.split(',') if part.strip()}
    unknown = picked - set(PARTS)
    if unknown:
        raise SystemExit('%s: unknown NWII_MOM_TRIM part(s) %s (known: %s)'
                         % (MARKER, ','.join(sorted(unknown)),
                            ','.join(PARTS)))
    return picked


def apply_pairs(text, pairs, tag):
    for old, new in pairs:
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:70]))
        text = text.replace(old, new, 1)
    return text


def restore_pairs(text, pairs, tag):
    for old, new in reversed(pairs):
        found = text.count(new)
        if found == 0:
            continue                       # already pristine
        if found != 1:
            raise SystemExit('%s: %s restore matched %d times (expected 1)'
                             % (MARKER, tag, found))
        text = text.replace(new, old, 1)
    return text


def selected_pairs(path, parts):
    """This file's (old, new) pairs for the enabled parts, in file order."""
    return [EDITS[path][index] for part, index in sorted(OWNER[path], key=lambda o: o[1])
            if part in parts]


def install_header(root):
    data = HEADER.encode('utf-8')
    wrote = []
    for rel in (OVERLAY, VERBOSE):
        path = root / rel
        if path.exists() and path.read_bytes() == data:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(data)
        wrote.append(rel)
    return wrote


def apply(root, parts):
    wrote = install_header(root) if 'verbose' in parts else []
    changed = []
    for path in (IOS, GX, MAIN):
        target = root / path
        if not target.is_file():
            if path == MAIN:
                print('export not generated; the dispatcher and verbose halves '
                      'of the stage are deferred (re-run after regeneration).')
                continue
            raise SystemExit('%s: %s is missing' % (MARKER, path))
        pairs = selected_pairs(path, parts)
        if not pairs:
            continue
        text = read(target)
        updated = apply_pairs(restore_pairs(text, pairs, TAG[path]), pairs, TAG[path])
        if updated != text:
            write(target, updated)
            changed.append(path)
    print('%s staged: %d file(s) rewritten, %d new header(s); parts %s; switch '
          'NWII_MOM_TRIM=%s (unset = all, none = reference), NWII_LOG_VERBOSE=1 '
          'restores the progress prints'
          % (MARKER, len(changed), len(wrote), ','.join(sorted(parts)) or 'none',
             ','.join(PARTS)))


def restore(root):
    changed = []
    for path in (IOS, GX, MAIN):
        target = root / path
        if not target.is_file():
            continue
        text = read(target)
        updated = restore_pairs(text, list(EDITS[path]), TAG[path])
        if updated != text:
            write(target, updated)
            changed.append(path)
    print('%s restored: %d file(s); the installed header stays in place (it is '
          'inert once nothing includes it).' % (MARKER, len(changed)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--parts', default=None,
                        help='comma-separated subset of %s (default: NWII_MOM_TRIM '
                             'or all)' % ','.join(PARTS))
    args = parser.parse_args()
    if args.restore:
        restore(args.root)
        return
    import os
    parts = enabled(args.parts if args.parts is not None
                    else os.environ.get('NWII_MOM_TRIM', ''))
    apply(args.root, parts)


if __name__ == '__main__':
    main()
