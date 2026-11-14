"""Park the guest's verified OS idle loop instead of spinning on it.

MoM's guest SDK idle loop is the backward branch at 0x80492408 inside
SelectThread (FUN_804922B4 + 0x154):

    80492400  lwz  r0, -0x4210(r13)   ; ready-thread bitmap
    80492404  cmpwi r0, 0
    80492408  beq  0x80492400         ; spin until a thread becomes ready

80.6 % of the guest's retired-instruction samples land on that branch, so the
emulated CPU burns a whole core spinning between frames while the runtime's
poll path is re-entered millions of times per second.

CotT parks exactly this loop (tools/fix_guest_idle_wait.py) on a wall-clock
deadline. This stage ports that behaviour to MoM with the addresses derived
from this DOL:

  * idle branch  0x80492408   (FUN_804922B4 + 0x154; the cross-match pairs
                               SelectThread with CotT's 0x803BBE4C, and the
                               same +0x154 offset gives CotT's 0x803BBFA0)
  * ready bitmap r13 - 0x4210 (CotT reads r13 - 0x2948 next to its counter
                               at r13 - 0x2950; MoM's counter is at
                               r13 - 0x4218, so the adjacent word is 0x4210)

The same stage also repairs the GX idle-drain probe, which still tested
CotT's 0x803BBFA0 and therefore never fired in this game.

Nothing about the guest changes: no register, no RAM, no instruction count and
no virtual clock is advanced while parked, the original polls and timer
handlers still run, and a single park is bounded to 250 us.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
overlays = port / 'overlays'

# --- the wait primitive ----------------------------------------------------
dst = runtime / 'include/runtime/native_idle_wait.h'
src = overlays / 'native_idle_wait.h'
if not dst.exists() or dst.read_text() != src.read_text():
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text())

IDLE_PC = '0x80492408u'
READY_OFFSET = '16896u'   # r13 - 0x4210


def edit(path, old, new, marker):
    text = path.read_text()
    if marker in text:
        return False
    assert text.count(old) == 1, (str(path), old[:60])
    path.write_text(text.replace(old, new, 1))
    return True


# --- wake the parked guest -------------------------------------------------
edit(runtime / 'include/runtime/cpu_context.h',
     '#include <array>',
     '#include "runtime/native_idle_wait.h" // NATIVE_IDLE_QUEUE_INCLUDE_V1\n#include <array>',
     'NATIVE_IDLE_QUEUE_INCLUDE_V1')
edit(runtime / 'include/runtime/cpu_context.h',
     '    pending_cb_count.fetch_add(1, std::memory_order_relaxed);',
     '    pending_cb_count.fetch_add(1, std::memory_order_release);\n'
     '    native_idle::notify(); // NATIVE_IDLE_QUEUE_WAKE_V1',
     'NATIVE_IDLE_QUEUE_WAKE_V1')
edit(runtime / 'include/runtime/hw/hw.h',
     '#pragma once',
     '#pragma once\n#include "runtime/native_idle_wait.h" // NATIVE_IDLE_IRQ_INCLUDE_V1',
     'NATIVE_IDLE_IRQ_INCLUDE_V1')
edit(runtime / 'include/runtime/hw/hw.h',
     '  pi_intsr.fetch_or(mask, std::memory_order_relaxed);',
     '  pi_intsr.fetch_or(mask, std::memory_order_release);\n'
     '  native_idle::notify(); // NATIVE_IDLE_IRQ_WAKE_V1',
     'NATIVE_IDLE_IRQ_WAKE_V1')

# --- the host loop must wake a parked guest -------------------------------
main = runtime / 'src/core/main.cpp'
text = main.read_text()
if 'NATIVE_IDLE_HOST_WAKE_V1' not in text:
    # The wake-up goes inside the existing single_vi block, before its closing
    # brace. The marker has to sit in the trailing comment, after the brace:
    # a marker placed before it would comment the brace out.
    old = 'ctx->vblank_pending = true;} // NATIVE_SINGLE_VI_SOURCE_V1'
    if text.count(old) == 2:
        text = text.replace(
            old,
            'ctx->vblank_pending = true; nwii::runtime::native_idle::notify();} '
            '// NATIVE_IDLE_HOST_WAKE_V1 NATIVE_SINGLE_VI_SOURCE_V1')
    else:
        old = 'ctx->vblank_pending = true;'
        assert text.count(old) >= 1
        text = text.replace(old, 'ctx->vblank_pending = true; nwii::runtime::native_idle::notify();')
    old = 'ctx->is_running = false;'
    assert text.count(old) >= 1
    text = text.replace(old, 'ctx->is_running = false; nwii::runtime::native_idle::notify();')
    main.write_text(text)

# --- the park site, and the repaired GX idle-drain probe -------------------
ios = runtime / 'src/hle/ios.cpp'
text = ios.read_text()
if 'NATIVE_OS_IDLE_WAIT_MOM_V1' not in text:
    # The GX idle-drain probe still named CotT's idle branch, so it never
    # fired in this game. Point it at this game's idle loop.
    old = 'ctx.pc == 0x803BBFA0u'
    assert text.count(old) == 1
    text = text.replace(old, 'ctx.pc == ' + IDLE_PC)

    old = """  const bool poll_clock_due = poll_no_throttle || ((++poll_clock_phase) & 15u) == 0;"""
    assert text.count(old) == 1
    new = old + """
  // NATIVE_OS_IDLE_WAIT_MOM_V1: the guest's own idle loop, verified by
  // address and by the state it is spinning on. It is parked, never dropped:
  // the original polls and timer handlers still run, and one park is bounded
  // to 250 us so a missed wake can never stall the game.
  {
    static const bool idle_wait = std::getenv("NWII_WALLTB") && !std::getenv("NWII_NO_IDLE_WAIT");
    if (idle_wait && poll_clock_due && !ctx.in_callback &&
        ctx.pc == """ + IDLE_PC + """ && ctx.cr[0].eq &&
        (ctx.msr & 0x8000u) && ctx.dispatch_saved_ctx == 0 &&
        g_ipc_interrupt_delay == 0 && hw::g_di_interrupt_delay == 0 &&
        ctx.mmu.read32(0x800000E4) == 0 &&
        ((ctx.gpr[13] - """ + READY_OFFSET + """) & 0x3fffffffu) < 0x01800000u &&
        ctx.mmu.read32(ctx.gpr[13] - """ + READY_OFFSET + """) == 0) {
      auto pending = [&] {
        return !ctx.is_running.load(std::memory_order_acquire) ||
               ctx.vblank_pending.load(std::memory_order_acquire) ||
               ctx.pending_cb_count.load(std::memory_order_acquire) != 0 ||
               (hw::pi_intsr.load(std::memory_order_acquire) &
                hw::pi_intmr.load(std::memory_order_relaxed)) != 0;
      };
      if (!pending()) {
        const uint64_t now = ctx.read_timebase();
        const uint64_t ticks = native_idle::budget_ticks(
            now, EventScheduler::get().next_due(), ctx.dec_irq_pending,
            ctx.dec_written_tb, ctx.dec_value, ctx.tb_freq);
        if (ticks) native_idle::wait(ticks, ctx.tb_freq, pending);
      }
    }
  }"""
    text = text.replace(old, new, 1)
    ios.write_text(text)
print('Guest OS idle wait parked; GX idle-drain probe re-keyed to this game.')
