#pragma once
// NATIVE_PERF_PHASE_V1
//
// Ported from the Crash of the Titans port's frame_trace.h / native_stutter.h
// family and adapted to this runtime. Game-independent: it names no guest
// address, reads no guest RAM and prints nothing unless NWII_PERF_PHASE is
// set.
//
// CotT's frame_trace.h is a Chrome-trace event recorder with per-thread
// buffers, GPU query fences and a Windows thread-time bridge. None of that
// exists here, and none of it is needed to answer the questions this port
// actually asks: how long a frame takes, which part of the frame costs what,
// and how much of the frame is spent waiting instead of working.
//
// This header keeps the part that transfers: a set of named phase scopes
// around the host's render work, a per-frame record of how long each phase
// took, and a periodic report.
//
// The frame is one iteration of the host render loop. That loop is field-paced
// (see NATIVE_FIELD_PACING_V1), so the frame period is the display field
// period and the interesting number is how much of it the host actually
// spends working.
//
// Env:
//   NWII_PERF_PHASE=1        enable the phase timers
//   NWII_PERF_PHASE_FILE     append the per-frame timeline as CSV
//   NWII_PERF_PHASE_PERIOD   seconds between [PERFPHASE] reports (default 5)
//
// Output:
//   [PERFPHASE] t=12.0s window=5.0s frames=300 frame_ms=16.63 p95=17.20
//               max=25.40 gx_parse=0.42 gx_decode=1.20 gl_render=8.90
//               xfb_copy=0.55 present=0.20 swap=0.10 host_field=0.90
//               host_drain_wait=0.85 guest_idle=0.00 host_sum=11.37
//               budget=16.67 busy=68.2% guest_cpu_ms_s=900.0
//
// All the phase numbers are milliseconds per frame over the report window.
//
// Caveats, which the report repeats:
//   * the frame phases are host-thread CPU time; the guest thread runs
//     concurrently and its execution time is not part of any frame phase, so
//     "host_sum" is the host's work and "frame_ms - host_sum" is the host's
//     own overhead plus whatever the guest thread did during the frame;
//   * guest_idle is the time the guest spent parked on its waitable timer;
//     it is a stall measure, not a frame phase, and is reported separately;
//   * the phase timers are wall clock, so a preempted host thread inflates
//     the phase it was in.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace native_perf_phase {

enum Phase : unsigned {
  Phase_GxParse = 0,
  Phase_GxDecode,
  Phase_GlRender,
  Phase_XfbCopy,
  Phase_Present,
  Phase_Swap,
  Phase_HostField,
  Phase_HostDrainWait,
  Phase_GuestIdle,
  Phase_Count
};

inline const char *phase_name(unsigned phase) {
  static const char *const names[Phase_Count] = {
      "gx_parse",  "gx_decode", "gl_render", "xfb_copy", "present",
      "swap",      "host_field", "host_drain_wait", "guest_idle"};
  return phase < Phase_Count ? names[phase] : "unknown";
}

inline const bool enabled = std::getenv("NWII_PERF_PHASE") != nullptr;

inline uint64_t now_ns() {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
                       std::chrono::steady_clock::now().time_since_epoch())
                       .count());
}

// One counter per cache line. The host thread writes most of them and the
// guest thread writes guest_idle; on one shared line the two threads would
// bounce it on every update.
struct alignas(64) PhaseCounter {
  std::atomic<uint64_t> ns{0};
  std::atomic<uint64_t> hits{0};
  char pad[64 - 2 * sizeof(std::atomic<uint64_t>)];
};

inline PhaseCounter counters[Phase_Count];

__attribute__((always_inline)) inline void Add(Phase phase, uint64_t ns) {
  counters[phase].ns.store(
      counters[phase].ns.load(std::memory_order_relaxed) + ns,
      std::memory_order_relaxed);
  counters[phase].hits.store(
      counters[phase].hits.load(std::memory_order_relaxed) + 1,
      std::memory_order_relaxed);
}
inline uint64_t Total(Phase phase) {
  return counters[phase].ns.load(std::memory_order_relaxed);
}
inline uint64_t Hits(Phase phase) {
  return counters[phase].hits.load(std::memory_order_relaxed);
}

// A phase scope. Costs one clock read when the phase is enabled and none
// when it is not. stop() ends the phase early; the destructor stops it too.
struct Scope {
  Phase phase;
  uint64_t t0;
  bool active;
  explicit Scope(Phase p) : phase(p), t0(0), active(false) {
    if (enabled) {
      t0 = now_ns();
      active = true;
    }
  }
  Scope(const Scope &) = delete;
  Scope &operator=(const Scope &) = delete;
  void stop() {
    if (!active) return;
    Add(phase, now_ns() - t0);
    active = false;
  }
  ~Scope() { stop(); }
};

// ---------------------------------------------------------------------------
// Per-frame timeline.
// ---------------------------------------------------------------------------
constexpr unsigned kFrameRing = 8192;

struct FrameRecord {
  uint64_t wall_ns;
  uint64_t phase_ns[Phase_Count];
};

inline FrameRecord ring[kFrameRing];
inline uint64_t frame_index = 0;      // frames recorded so far
inline uint64_t frame_start_ns = 0;
inline uint64_t frame_snapshot[Phase_Count];
inline uint64_t run_wall_ns = 0;
inline uint64_t run_phase_ns[Phase_Count];
inline uint64_t run_max_wall_ns = 0;

inline void frame_begin() {
  if (!enabled) return;
  frame_start_ns = now_ns();
  for (unsigned i = 0; i < Phase_Count; ++i)
    frame_snapshot[i] = counters[i].ns.load(std::memory_order_relaxed);
}

inline void frame_end() {
  if (!enabled) return;
  const uint64_t t = now_ns();
  FrameRecord &r = ring[frame_index % kFrameRing];
  r.wall_ns = t - frame_start_ns;
  for (unsigned i = 0; i < Phase_Count; ++i) {
    const uint64_t now = counters[i].ns.load(std::memory_order_relaxed);
    r.phase_ns[i] = now >= frame_snapshot[i] ? now - frame_snapshot[i] : 0;
    run_phase_ns[i] += r.phase_ns[i];
  }
  run_wall_ns += r.wall_ns;
  if (r.wall_ns > run_max_wall_ns) run_max_wall_ns = r.wall_ns;
  ++frame_index;
}

// ---------------------------------------------------------------------------
// Guest thread CPU time (Windows thread accounting). The guest thread is the
// one that runs the recompiled code; its CPU time is the port's headline
// cost, and it is not visible in any host-side phase.
// ---------------------------------------------------------------------------
inline std::atomic<uint32_t> g_guest_tid{0};

// Call this from the thread that runs the recompiled guest code.
inline void set_guest_thread() {
#ifdef _WIN32
  g_guest_tid.store(GetCurrentThreadId(), std::memory_order_relaxed);
#endif
}

inline uint64_t guest_cpu_us() {
#ifdef _WIN32
  const uint32_t tid = g_guest_tid.load(std::memory_order_relaxed);
  if (!tid) return 0;
  HANDLE h = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, tid);
  if (!h) return 0;
  FILETIME c, e, k, u;
  uint64_t us = 0;
  if (GetThreadTimes(h, &c, &e, &k, &u)) {
    const auto ticks = [](FILETIME f) {
      return (uint64_t(f.dwHighDateTime) << 32) | uint64_t(f.dwLowDateTime);
    };
    us = (ticks(k) + ticks(u)) / 10;
  }
  CloseHandle(h);
  return us;
#else
  return 0;
#endif
}

// ---------------------------------------------------------------------------
// Reporting.
// ---------------------------------------------------------------------------
inline double report_period_s() {
  static const double seconds = [] {
    const char *e = std::getenv("NWII_PERF_PHASE_PERIOD");
    const double v = e ? std::strtod(e, nullptr) : 5.0;
    return v > 0.05 ? v : 5.0;
  }();
  return seconds;
}

inline const char *phase_file() {
  static const char *path = std::getenv("NWII_PERF_PHASE_FILE");
  return path;
}

inline uint64_t g_report_last_ns = 0;
inline uint64_t g_report_start_ns = 0;
inline uint64_t g_report_last_frames = 0;
inline uint64_t g_report_last_guest_cpu_us = 0;
inline std::FILE *g_csv = nullptr;

inline void csv_header() {
  if (!phase_file() || !*phase_file()) return;
  if (g_csv) return;
  g_csv = std::fopen(phase_file(), "wb");
  if (!g_csv) return;
  std::fprintf(g_csv, "frame,wall_ms");
  for (unsigned i = 0; i < Phase_Count; ++i)
    std::fprintf(g_csv, ",%s_ms", phase_name(i));
  std::fprintf(g_csv, "\n");
  std::fflush(g_csv);
}

inline void flush_frames(uint64_t first, uint64_t last) {
  csv_header();
  if (!g_csv) return;
  // The ring holds kFrameRing frames; anything older has been overwritten.
  if (last - first > kFrameRing) {
    first = last - kFrameRing;
    std::fprintf(g_csv, "# %llu frame(s) dropped, the ring holds %u\n",
                 (unsigned long long)(last - first), kFrameRing);
  }
  for (uint64_t i = first; i < last; ++i) {
    const FrameRecord &r = ring[i % kFrameRing];
    std::fprintf(g_csv, "%llu,%.3f", (unsigned long long)i,
                 double(r.wall_ns) / 1e6);
    for (unsigned p = 0; p < Phase_Count; ++p)
      std::fprintf(g_csv, ",%.3f", double(r.phase_ns[p]) / 1e6);
    std::fprintf(g_csv, "\n");
  }
  std::fflush(g_csv);
}

inline void maybe_report() {
  if (!enabled) return;
  const uint64_t now = now_ns();
  if (!g_report_last_ns) {
    g_report_last_ns = now;
    g_report_start_ns = now;
    g_report_last_guest_cpu_us = guest_cpu_us();
    return;
  }
  const double elapsed_s = double(now - g_report_last_ns) / 1e9;
  if (elapsed_s < report_period_s()) return;
  const double total_s = double(now - g_report_start_ns) / 1e9;

  const uint64_t first = g_report_last_frames;
  const uint64_t last = frame_index;
  const uint64_t frames = last > first ? last - first : 0;

  std::vector<uint64_t> walls;
  walls.reserve(frames < 4096 ? size_t(frames) : 4096);
  for (uint64_t i = (last > kFrameRing ? last - kFrameRing : 0); i < last; ++i)
    walls.push_back(ring[i % kFrameRing].wall_ns);
  std::sort(walls.begin(), walls.end());
  const auto pct = [&walls](double p) -> double {
    if (walls.empty()) return 0.0;
    size_t i = size_t((double(walls.size()) * p + 0.999999) - 1.0);
    if (i >= walls.size()) i = walls.size() - 1;
    return double(walls[i]) / 1e6;
  };

  double mean_wall_ms = 0.0, max_wall_ms = 0.0;
  for (uint64_t i = first; i < last; ++i) {
    const uint64_t w = ring[i % kFrameRing].wall_ns;
    mean_wall_ms += double(w) / 1e6;
    if (double(w) / 1e6 > max_wall_ms) max_wall_ms = double(w) / 1e6;
  }
  if (frames) mean_wall_ms /= double(frames);

  double phase_ms[Phase_Count];
  double host_sum_ms = 0.0;
  for (unsigned p = 0; p < Phase_Count; ++p) {
    uint64_t sum = 0;
    for (uint64_t i = first; i < last; ++i)
      sum += ring[i % kFrameRing].phase_ns[p];
    phase_ms[p] = frames ? double(sum) / 1e6 / double(frames) : 0.0;
  }
  // The frame phases are the host's render work; the field wait and the
  // guest's idle park are stalls and are reported, not summed into the frame.
  host_sum_ms = phase_ms[Phase_GxParse] + phase_ms[Phase_GxDecode] +
                phase_ms[Phase_GlRender] + phase_ms[Phase_XfbCopy] +
                phase_ms[Phase_Present] + phase_ms[Phase_Swap];

  const uint64_t guest_cpu_us_now = guest_cpu_us();
  const double guest_cpu_ms_s =
      elapsed_s > 0 ? double(guest_cpu_us_now - g_report_last_guest_cpu_us) / 1000.0 / elapsed_s : 0.0;

  std::printf("[PERFPHASE] t=%.1fs window=%.1fs frames=%llu frame_ms=%.3f p95=%.3f max=%.3f",
              total_s, elapsed_s, (unsigned long long)frames, mean_wall_ms,
              pct(0.95), max_wall_ms);
  for (unsigned p = 0; p < Phase_Count; ++p)
    std::printf(" %s=%.3f", phase_name(p), phase_ms[p]);
  std::printf(" host_sum=%.3f guest_cpu_ms_s=%.1f\n", host_sum_ms, guest_cpu_ms_s);
  std::fflush(stdout);

  flush_frames(first, last);

  g_report_last_ns = now;
  g_report_last_frames = last;
  g_report_last_guest_cpu_us = guest_cpu_us_now;
}

}  // namespace native_perf_phase
