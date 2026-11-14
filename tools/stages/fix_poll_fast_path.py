"""Give the guest's poll a cheap fast path.

The guest calls process_pending_callbacks() at every backward branch. In this
game that is four million calls a second: the SDK's cache-maintenance routines
alone (a dcbf loop over 32-byte lines) were 27 % of the guest's samples and
the OS lock/unlock pair another 6 %. Each of those calls ran the *whole* poll:
the diagnostic blocks, the loop trace, the sample print, the heartbeat, the DI
tick, the event scheduler, the decrementer test, the interrupt dispatch and the
callback dispatch - far more work than the poll had to do.

The fast path below returns immediately when there is provably nothing to do:

  * no masked processor interrupt is pending,
  * no vertical blank is pending,
  * no callback is queued,
  * neither the IPC nor the disc interrupt is counting down,
  * the guest is not at the callback-return sentinel, and
  * this is not one of the sixteen polls that advance the clock.

Only the WPAD poll and the cheap sampled diagnostic still run, so a sampled
run keeps reporting at the same cadence. Everything the fast path skips is
re-checked on the next slow poll, and the slow poll still happens on every
sixteenth call, so no interrupt, callback or timer is lost - it is delivered
at most sixteen polls later, which is what the clock throttle already did.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
p = port / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/ios.cpp'
t = p.read_text()
if 'NATIVE_FAST_POLL_PATH_V1' in t:
    print('Poll fast path already present.')
else:
    # 1. The sampled diagnostic becomes a shared helper so both paths use it.
    old = """  {
    static bool sample = callback_sample;
    static uint64_t n = 0;
    if (sample && (++n % 400000) == 0)
      std::cout << "[Sample] pc=0x" << std::hex << ctx.pc << " lr=0x" << ctx.lr
                << " r3=0x" << ctx.gpr[3] << std::dec << "\\n";
  }"""
    assert t.count(old) == 1
    new = """  native_poll_sample(ctx);"""
    t = t.replace(old, new)

    # 2. The helper itself, placed in front of the poll.
    old = """bool process_pending_callbacks(CPUContext &ctx) {
  native_fp_context_probe(ctx,"poll");"""
    assert t.count(old) == 1
    new = """// NATIVE_FAST_POLL_PATH_V1: the cheap sampled diagnostic, shared by the
// fast and the slow poll so its cadence does not depend on which one runs.
static void native_poll_sample(CPUContext &ctx) {
  static bool sample = NativeLaunchFlag_NWII_SAMPLE() != nullptr;
  static uint64_t n = 0;
  if (sample && (++n % 400000) == 0)
    std::cout << "[Sample] pc=0x" << std::hex << ctx.pc << " lr=0x" << ctx.lr
              << " r3=0x" << ctx.gpr[3] << " inst=" << std::dec << ctx.inst_count
              << " fast=" << g_native_fast_polls << std::endl;
}

bool process_pending_callbacks(CPUContext &ctx) {
  native_fp_context_probe(ctx,"poll");"""
    t = t.replace(old, new)

    # 2b. The counter the diagnostic reports.
    old = """static uint64_t g_diag_dec_fires = 0; // NATIVE_DIAG_TIMELINE_V1"""
    assert t.count(old) == 1
    new = """static uint64_t g_diag_dec_fires = 0; // NATIVE_DIAG_TIMELINE_V1
static std::atomic<uint64_t> g_native_fast_polls{0}; // NATIVE_FAST_POLL_PATH_V1"""
    t = t.replace(old, new)

    # 3. The fast path.
    old = """  const bool poll_clock_due = poll_no_throttle || ((++poll_clock_phase) & 15u) == 0;
"""
    assert t.count(old) == 1
    new = """  const bool poll_clock_due = poll_no_throttle || ((++poll_clock_phase) & 15u) == 0;
  // NATIVE_FAST_POLL_PATH_V1: nothing is pending, so the poll has nothing to
  // do. The guest polls at every backward branch and this game makes four
  // million such calls a second; the full poll cost far more than the work
  // it performed. Everything skipped here is re-checked by the next slow
  // poll, and the slow poll still runs on every sixteenth call.
  {
    const bool idle_sources =
        (hw::pi_intsr.load(std::memory_order_relaxed) &
         hw::pi_intmr.load(std::memory_order_relaxed)) == 0 &&
        !ctx.vblank_pending.load(std::memory_order_relaxed) &&
        ctx.pending_cb_count.load(std::memory_order_relaxed) == 0;
    if (!poll_clock_due && idle_sources && ctx.pc != 0 && ctx.pc != 0xFFFFFFFCu &&
        g_ipc_interrupt_delay == 0 && hw::g_di_interrupt_delay == 0) {
      if (ctx.in_callback || !(ctx.msr & 0x8000u) || ctx.dispatch_saved_ctx != 0)
        return false;
      native_wpad_poll(ctx);
      if (ctx.pending_cb_count.load(std::memory_order_relaxed) == 0) {
        g_native_fast_polls.fetch_add(1, std::memory_order_relaxed);
        native_poll_sample(ctx);
        return false;
      }
    }
  }
"""
    t = t.replace(old, new)
    p.write_text(t)
print('Poll fast path installed.')
