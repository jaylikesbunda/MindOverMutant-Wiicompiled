#pragma once
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <algorithm>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

// NATIVE_IDLE_WAIT_MOM_V1
// MoM's variant of the CotT idle-wait overlay. The CotT original pulls in
// runtime/native_hud.h, which needs runtime/native_perf_census.h; that census
// belongs to CotT's developer overlay and has no MoM counterpart, so the wait
// primitive is kept self-contained here. The behaviour is the same: the guest
// thread owns its timer, producers only signal the wake event, and no guest
// register, RAM, instruction count or virtual clock is advanced while parked.
namespace nwii::runtime::native_idle {
inline thread_local uint64_t waits = 0;
// Producer attention latch: a raised interrupt or a queued callback sets it.
inline std::atomic<uint32_t> attention{0};

// Upper bound on one park: 250 us. That also bounds a source that forgot to
// signal, so a lost notification can never stall the guest.
inline uint64_t budget_ticks(uint64_t now, uint64_t next, bool dec_pending,
                             uint64_t dec_origin, uint64_t dec_value, uint64_t freq) {
  // NATIVE_MOM_IDLE_PARK_CHURN_V1: 1 ms of speculative park instead of 250 us.  The clamp
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
  if (!ticks) return 0;
  if (next <= now) return 0;
  ticks = std::min(ticks, next - now);
  if (dec_pending) {
    const uint64_t elapsed = now - dec_origin;
    if (elapsed >= dec_value) return 0;
    ticks = std::min(ticks, dec_value - elapsed);
  }
  return ticks;
}

#ifdef _WIN32
struct Wake {
  HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
  ~Wake() { if (event) CloseHandle(event); }
};
inline Wake& wake() { static Wake s; return s; }
inline void notify() {
  attention.store(1, std::memory_order_relaxed);
  if (wake().event) SetEvent(wake().event);
}
struct Timer {
  HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, 0x2, TIMER_ALL_ACCESS);
  ~Timer() { if (timer) CloseHandle(timer); }
};
template <class Pending>
inline bool wait(uint64_t ticks, uint64_t freq, Pending pending) {
  if (!ticks || !freq || pending()) return false;
  static thread_local Timer t;
  auto& w = wake();
  if (!t.timer || !w.event) return false;
  LARGE_INTEGER due{};
  const uint64_t ns100 = ticks * 10000000ull / freq;
  if (!ns100) return false;
  due.QuadPart = -static_cast<LONGLONG>(ns100);
  if (!SetWaitableTimer(t.timer, &due, 0, nullptr, nullptr, FALSE)) return false;
  if (pending()) { CancelWaitableTimer(t.timer); return false; }
  HANDLE handles[] = {w.event, t.timer};
  // A signal that arrives between the predicate check and the wait stays
  // pending in the event, so no notification is lost.
  ++waits;
  static const bool keep_cancel =
      std::getenv("NWII_NO_IDLE_PARK_CHURN") != nullptr;
  const DWORD result = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
  // NATIVE_MOM_IDLE_PARK_CHURN_V1: the timer is one-shot and the next arm replaces its due
  // time, so cancelling here is a third syscall per park for nothing.
  if (keep_cancel) CancelWaitableTimer(t.timer);
  return true;
}
#else
inline void notify() { attention.store(1, std::memory_order_relaxed); }
template <class Pending>
inline bool wait(uint64_t, uint64_t, Pending) { return false; }
#endif
}  // namespace nwii::runtime::native_idle
