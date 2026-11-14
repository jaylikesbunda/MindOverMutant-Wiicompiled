"""Stop paying the idle-park syscall bill four thousand times a second.

The guest parks in ``native_idle::wait`` whenever it has nothing to do.  Each
park arms a waitable timer, waits on {wake event, timer} and then cancels the
timer - three kernel calls - and ``budget_ticks`` caps a speculative park at
``freq / 4000``, i.e. **250 us**.  A parked guest therefore re-enters the park
thousands of times a second:

    [PERFPHASE] guest_idle 13.53 ms/frame over 35 calls/frame

and the CPU profile puts 25 % of guest CPU and 38 % of host CPU in ``ntdll``
(the syscall path).  Because this runtime's audio production is driven by guest
progress, that stall is also why the DSP starves in-level while it is perfect in
the menus.

Two changes, both safe for wake latency:

* the speculative budget becomes 1 ms (``NWII_IDLE_PARK_US`` overrides it).  The
  budget only bounds *speculative* parks - ``wait`` still clamps it to the next
  real deadline with ``std::min(ticks, next - now)`` - so a timer still fires on
  time, and ``notify()`` sets the wake event, so a real signal is never lost.
* the ``CancelWaitableTimer`` after the wait is dropped.  The timer is one-shot
  and re-armed by the next ``SetWaitableTimer``, so cancelling only adds a third
  syscall per park.  The pre-wait cancel (the pending() re-check) stays.

``NWII_NO_IDLE_PARK_CHURN=1`` restores the 250 us budget and the cancel.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
MARK = 'NATIVE_MOM_IDLE_PARK_CHURN_V1'
paths = [port / 'overlays/native_idle_wait.h',
         port / 'vendor/NWiiRecomp/nWiiRuntime/include/runtime/native_idle_wait.h']

for path in paths:
    if not path.exists():
        continue
    text = path.read_text()
    if MARK in text:
        continue

    old_budget = """  uint64_t ticks = freq / 4000;
  if (!ticks) return 0;"""
    assert text.count(old_budget) == 1, 'budget anchor in %s' % path.name
    new_budget = """  // """ + MARK + """: 1 ms of speculative park instead of 250 us.  The clamp
  // against the next deadline below still bounds a real timer wake, and
  // notify() sets the wake event, so this only reduces how often the guest
  // re-enters the park - it never delays a timer or a signal.
  static const uint64_t park_ticks = []() {
    const char* e = std::getenv("NWII_IDLE_PARK_US");
    const long us = e ? std::strtol(e, nullptr, 10) : 1000;
    return uint64_t(us > 0 ? us : 250);
  }();
  uint64_t ticks = park_ticks * freq / 1000000ull;
  if (!ticks) ticks = 1;
  if (!ticks) return 0;"""
    text = text.replace(old_budget, new_budget, 1)

    old_cancel = """  ++waits;
  const DWORD result = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
  CancelWaitableTimer(t.timer);
  return true;"""
    assert text.count(old_cancel) == 1, 'wait anchor in %s' % path.name
    new_cancel = """  ++waits;
  static const bool keep_cancel =
      std::getenv("NWII_NO_IDLE_PARK_CHURN") != nullptr;
  const DWORD result = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
  // """ + MARK + """: the timer is one-shot and the next arm replaces its due
  // time, so cancelling here is a third syscall per park for nothing.
  if (keep_cancel) CancelWaitableTimer(t.timer);
  return true;"""
    text = text.replace(old_cancel, new_cancel, 1)
    path.write_text(text)
    print('park churn: patched %s' % path.relative_to(port))
