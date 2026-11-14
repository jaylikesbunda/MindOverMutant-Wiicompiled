"""Add an opt-in, in-process instruction-pointer profiler for both threads.

This is the MoM form of the Crash of the Titans profiling work. CotT has three
separate lanes and none of them transfers unchanged:

  * fix_startup_profile.py samples ``ctx.pc``/``ctx.lr`` at callback points on
    the guest thread only (a guest-PC census, not a host profile);
  * fix_perf_census.py counts recompiler operations and entry/switch PCs, and
    is exported through the CotT F3 HUD / F4 JSON, which MoM does not have;
  * analyze_cpu_profile.py / summarize_cpu_etl.py consume a Windows ETW/ETL
    capture made by WPR outside the tree. The tree has no WPR capture harness,
    and the decoder assumes CotT's process name, base address and symbol-file
    format.

MoM already has a guest-PC poll census (fix_perf_census.py) and host phase
timers (fix_perf_phase_trace.py), but neither can say which *function* inside
``gl_render`` costs the 11.6-12.2 ms/frame, and the census only sees the guest
thread. This stage adds the missing measurement: a helper thread that wakes at
a fixed rate, suspends each registered thread for a few microseconds, reads
its instruction pointer with ``GetThreadContext``, resumes it, and records the
sample with that thread's CPU-time delta. Nothing is captured from an external
tool; the runtime only writes module-relative addresses, and the analyser
(tools/analyze_cpu_profile_mom.py) resolves them against the exact executable's
``.pdata`` ranges and ``nm`` symbols.

The profile is opt-in at run time and free when disabled: ``enabled`` is a
constant false unless ``NWII_CPU_PROFILE`` is set, the call sites are one
getenv at static-init and a branch per frame, and no thread is created.

Env:
    NWII_CPU_PROFILE=1          enable (NWII_CPU_PROFILE=0 also disables)
    NWII_CPU_PROFILE_HZ         samples per second per thread (default 250)
    NWII_CPU_PROFILE_SECONDS    stop and write after N s (default 60; 0 = until
                                the host loop exits)
    NWII_CPU_PROFILE_DELAY      skip N s before sampling (default 0); use it to
                                drop boot movies and menus from the window
    NWII_CPU_PROFILE_MAX        stored-sample cap per run (default 200000)
    NWII_CPU_PROFILE_PERIOD     seconds between progress lines (default 5)
    NWII_CPU_PROFILE_FILE       output prefix (default cpuprofile.csv)

Output (bounded):
    <prefix>                 role,module,rva,cpu_us
    <prefix>.modules.csv     index,base,size,name
    <prefix>.meta.json       tids, rate, counts, elapsed
    [CPUPROFILE] ...         one line per period and one done line

Idempotent: every edit is guarded by its own marker and asserted to match its
anchor exactly once. The header is written from this file, so the stage is a
single file and does not depend on an overlay being installed first.
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
header_dst = runtime / "include/runtime/native_cpu_profile.h"

CPU_PROFILE_HEADER = r'''#pragma once
// NATIVE_CPU_PROFILE_V1
//
// Opt-in, in-process instruction-pointer sampler for BOTH native threads:
//   * the guest thread (the one that runs the recompiled func_XXXXXXXX code,
//     the dispatcher and the interpreter fallback);
//   * the host/render thread (the one that runs the main loop, the GX parse,
//     RendererGL::Render and the field wait).
//
// This is not a guest-PC census and not ETW. A helper thread wakes at
// NWII_CPU_PROFILE_HZ, suspends each registered thread for a few microseconds,
// reads RIP with GetThreadContext, resumes it, and records the sample with the
// thread's CPU-time delta since the previous sample. The CPU-time weight means
// a sample only counts when the thread actually ran, so time blocked in a wait
// is not attributed to whatever function happened to be on top.
//
// Symbolisation happens offline (tools/analyze_cpu_profile_mom.py): the
// runtime writes module-relative addresses, and the analyser reads the exact
// executable's PE .pdata ranges and nm symbols. No PDB, no ETW, no WPR and no
// external profiler is required.
//
// Env:
//   NWII_CPU_PROFILE=1        enable (NWII_CPU_PROFILE=0 disables)
//   NWII_CPU_PROFILE_HZ       samples per second per thread (default 250,
//                             clamped 50..1000)
//   NWII_CPU_PROFILE_SECONDS  stop and write after N seconds (default 60;
//                             0 = run until the host loop exits)
//   NWII_CPU_PROFILE_DELAY    seconds to wait before sampling starts (default
//                             0). Use it to skip boot movies and menus so the
//                             window is gameplay, e.g. 150 with the synthetic
//                             press timeline in tools/run_perf.ps1.
//   NWII_CPU_PROFILE_MAX      stored-sample cap (default 200000)
//   NWII_CPU_PROFILE_PERIOD   seconds between [CPUPROFILE] lines (default 5)
//   NWII_CPU_PROFILE_FILE     output prefix (default cpuprofile.csv)
//
// Cost, measured on the CotT version of this sampler: one suspend/context/
// resume is a few microseconds. At the default 250 Hz for two threads the
// perturbation is well under 1% of a core, and the guest thread is not
// descheduled long enough to change its scheduling. Raise the rate only for
// short captures.
//
// The file writes are bounded: at most NWII_CPU_PROFILE_MAX rows, one module
// table, one small JSON. If the process is killed before the deadline or
// before the host loop exits, no file is written by design; use
// NWII_CPU_PROFILE_SECONDS so the sampler closes its own capture.
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <tlhelp32.h>
#endif

namespace native_cpu_profile {

inline bool env_on(const char *name) {
  const char *value = std::getenv(name);
  return value && *value && !(value[0] == '0' && value[1] == '\0');
}
inline double env_number(const char *name, double fallback) {
  const char *value = std::getenv(name);
  if (!value || !*value) return fallback;
  char *end = nullptr;
  const double parsed = std::strtod(value, &end);
  return end != value && parsed == parsed ? parsed : fallback;
}
inline uint32_t env_u32(const char *name, uint32_t fallback, uint32_t low,
                        uint32_t high) {
  const double parsed = env_number(name, double(fallback));
  if (!(parsed >= double(low))) return low;
  if (parsed > double(high)) return high;
  return uint32_t(parsed);
}

// A constant false here is what makes the disabled path free: the compiler
// folds every maybe_start() call site to a load, and no sampler exists.
inline const bool enabled = env_on("NWII_CPU_PROFILE");

enum : unsigned { RoleGuest = 0, RoleHost = 1, RoleCount = 2 };

inline const char *role_name(unsigned role) {
  return role == RoleGuest ? "guest" : role == RoleHost ? "host" : "unknown";
}

inline uint64_t now_ns() {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
                      std::chrono::steady_clock::now().time_since_epoch())
                      .count());
}

struct Sample {
  uint64_t rva;
  uint32_t cpu_us;
  uint16_t module;
  uint8_t role;
  uint8_t used;
};
struct Module {
  uint64_t base;
  uint64_t size;
  char name[64];
};

inline Sample *g_samples = nullptr;
inline size_t g_capacity = 0;
inline Module g_modules[128];
inline unsigned g_module_count = 0;

inline std::atomic<uint64_t> g_count{0};
inline std::atomic<uint64_t> g_idle{0};
inline std::atomic<uint64_t> g_failed{0};
inline std::atomic<uint64_t> g_dropped{0};
inline std::atomic<uint64_t> g_role_samples[RoleCount];
inline std::atomic<uint32_t> g_guest_tid{0};
inline std::atomic<uint32_t> g_host_tid{0};
inline std::atomic<bool> g_started{false};
inline std::atomic<bool> g_stop{false};
inline std::atomic<bool> g_done{false};
inline std::atomic<uint64_t> g_deadline_ns{0};
inline uint64_t g_arm_ns = 0;
inline double g_delay_s = 0.0;
inline uint64_t g_start_ns = 0;
inline uint64_t g_last_report_ns = 0;
inline uint64_t g_report_period_ns = 5000000000ull;
inline uint32_t g_hz = 250;
inline uint32_t g_seconds = 60;
inline std::thread g_thread;

inline uint64_t thread_cpu_us(void *handle) {
#ifdef _WIN32
  FILETIME creation, exit_time, kernel, user;
  if (!GetThreadTimes(HANDLE(handle), &creation, &exit_time, &kernel, &user))
    return 0;
  const auto ticks = [](const FILETIME &value) -> uint64_t {
    return (uint64_t(value.dwHighDateTime) << 32) | uint64_t(value.dwLowDateTime);
  };
  return (ticks(kernel) + ticks(user)) / 10u;
#else
  (void)handle;
  return 0;
#endif
}

inline void snapshot_modules() {
#ifdef _WIN32
  g_module_count = 0;
  HANDLE snapshot = CreateToolhelp32Snapshot(
      TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
  if (snapshot == INVALID_HANDLE_VALUE) return;
  MODULEENTRY32W entry{};
  entry.dwSize = sizeof(entry);
  if (Module32FirstW(snapshot, &entry)) {
    do {
      if (g_module_count == sizeof(g_modules) / sizeof(g_modules[0])) break;
      Module &module = g_modules[g_module_count];
      module.base = uint64_t(uintptr_t(entry.modBaseAddr));
      module.size = uint64_t(entry.modBaseSize);
      const int written = WideCharToMultiByte(
          CP_UTF8, 0, entry.szModule, -1, module.name,
          int(sizeof(module.name)) - 1, nullptr, nullptr);
      if (written <= 0)
        std::snprintf(module.name, sizeof(module.name), "module%u",
                      g_module_count);
      else
        module.name[written - 1] = '\0';
      ++g_module_count;
    } while (Module32NextW(snapshot, &entry));
  }
  CloseHandle(snapshot);
#endif
}

inline uint16_t module_of(uint64_t ip) {
  for (unsigned i = 0; i < g_module_count; ++i) {
    const Module &module = g_modules[i];
    if (ip >= module.base && ip - module.base < module.size)
      return uint16_t(i);
  }
  return 0xFFFFu;
}

inline void sampler(uint32_t hz, uint32_t seconds) {
#ifdef _WIN32
  HANDLE handles[RoleCount] = {nullptr, nullptr};
  const uint32_t tids[RoleCount] = {g_guest_tid.load(std::memory_order_relaxed),
                                    g_host_tid.load(std::memory_order_relaxed)};
  for (unsigned role = 0; role < RoleCount; ++role)
    handles[role] = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                                   THREAD_QUERY_INFORMATION,
                               FALSE, tids[role]);
  uint64_t previous[RoleCount] = {thread_cpu_us(handles[0]),
                                  thread_cpu_us(handles[1])};
  const uint32_t rate = hz ? hz : 250;
  const auto period = std::chrono::nanoseconds(1000000000ull / rate);
  auto next = std::chrono::steady_clock::now();
  while (!g_stop.load(std::memory_order_relaxed)) {
    next += period;
    std::this_thread::sleep_until(next);
    if (g_stop.load(std::memory_order_relaxed)) break;
    if (seconds) {
      const uint64_t deadline = g_deadline_ns.load(std::memory_order_relaxed);
      if (deadline && now_ns() >= deadline) {
        g_stop.store(true, std::memory_order_relaxed);
        break;
      }
    }
    for (unsigned role = 0; role < RoleCount; ++role) {
      HANDLE handle = handles[role];
      if (!handle) {
        g_failed.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      if (SuspendThread(handle) == DWORD(-1)) {
        g_failed.fetch_add(1, std::memory_order_relaxed);
        previous[role] = thread_cpu_us(handle);
        continue;
      }
      CONTEXT context{};
      context.ContextFlags = CONTEXT_CONTROL;
      const bool ok = GetThreadContext(handle, &context) != FALSE;
      ResumeThread(handle);
      const uint64_t cpu = thread_cpu_us(handle);
      const uint64_t delta = cpu >= previous[role] ? cpu - previous[role] : 0;
      previous[role] = cpu;      if (!ok) {
        g_failed.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      if (!delta) {
        // The thread was not running during this interval. Counting it would
        // attribute blocked time to a function; skip it instead.
        g_idle.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      const uint64_t slot = g_count.load(std::memory_order_relaxed);
      if (slot >= g_capacity) {
        g_dropped.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      const uint64_t ip = uint64_t(context.Rip);
      const uint16_t module = module_of(ip);
      Sample &sample = g_samples[slot];
      sample.rva = module != 0xFFFFu ? ip - g_modules[module].base : ip;
      sample.cpu_us = uint32_t(delta > UINT32_MAX ? UINT32_MAX : delta);
      sample.module = module;
      sample.role = uint8_t(role);
      sample.used = 1;
      g_count.store(slot + 1, std::memory_order_relaxed);
      g_role_samples[role].fetch_add(1, std::memory_order_relaxed);
    }
  }
  for (unsigned role = 0; role < RoleCount; ++role)
    if (handles[role]) CloseHandle(handles[role]);
#else
  (void)hz;
  (void)seconds;
#endif
}

inline void write_outputs() {
  const char *configured = std::getenv("NWII_CPU_PROFILE_FILE");
  const char *prefix =
      configured && *configured ? configured : "cpuprofile.csv";
  const uint64_t count = g_count.load(std::memory_order_relaxed);
  char path[1024];
  std::FILE *file = std::fopen(prefix, "wb");
  if (file) {
    std::fprintf(file, "role,module,rva,cpu_us\n");
    for (uint64_t i = 0; i < count; ++i) {
      const Sample &sample = g_samples[i];
      if (!sample.used) continue;
      std::fprintf(file, "%s,%u,%llu,%u\n", role_name(sample.role),
                   unsigned(sample.module), (unsigned long long)sample.rva,
                   unsigned(sample.cpu_us));
    }
    std::fclose(file);
  }
  std::snprintf(path, sizeof(path), "%s.modules.csv", prefix);
  file = std::fopen(path, "wb");
  if (file) {
    std::fprintf(file, "index,base,size,name\n");
    for (unsigned i = 0; i < g_module_count; ++i)
      std::fprintf(file, "%u,%llu,%llu,%s\n", i,
                   (unsigned long long)g_modules[i].base,
                   (unsigned long long)g_modules[i].size,
                   g_modules[i].name);
    std::fclose(file);
  }
  const double elapsed_s =
      g_start_ns ? double(now_ns() - g_start_ns) / 1e9 : 0.0;
  std::snprintf(path, sizeof(path), "%s.meta.json", prefix);
  file = std::fopen(path, "wb");
  if (file) {
    std::fprintf(
        file,
        "{\"enabled\":true,\"hz\":%u,\"seconds\":%u,\"delay_s\":%.3f,"
        "\"elapsed_s\":%.3f,"
        "\"guest_tid\":%u,\"host_tid\":%u,\"samples\":%llu,"
        "\"guest_samples\":%llu,\"host_samples\":%llu,\"idle_intervals\":%llu,"
        "\"failed\":%llu,\"dropped\":%llu,\"capacity\":%llu,"
        "\"module_count\":%u,\"cpu_time_weighted\":true}\n",
        g_hz, g_seconds, g_delay_s, elapsed_s, g_guest_tid.load(),
        g_host_tid.load(),
        (unsigned long long)count,
        (unsigned long long)g_role_samples[RoleGuest].load(),
        (unsigned long long)g_role_samples[RoleHost].load(),
        (unsigned long long)g_idle.load(), (unsigned long long)g_failed.load(),
        (unsigned long long)g_dropped.load(), (unsigned long long)g_capacity,
        g_module_count);
    std::fclose(file);
  }
  std::printf(
      "[CPUPROFILE] done samples=%llu guest=%llu host=%llu idle=%llu "
      "failed=%llu dropped=%llu file=%s\n",
      (unsigned long long)count,
      (unsigned long long)g_role_samples[RoleGuest].load(),
      (unsigned long long)g_role_samples[RoleHost].load(),
      (unsigned long long)g_idle.load(), (unsigned long long)g_failed.load(),
      (unsigned long long)g_dropped.load(), prefix);
  std::fflush(stdout);
}

inline void stop() {
  if (!enabled) return;
  if (!g_started.load(std::memory_order_relaxed)) {
    if (g_arm_ns) {
      std::printf("[CPUPROFILE] no capture written: the delay did not elapse\n");
      std::fflush(stdout);
    }
    return;
  }
  if (g_done.exchange(true)) return;
  g_stop.store(true, std::memory_order_relaxed);
  if (g_thread.joinable()) g_thread.join();
  write_outputs();
  delete[] g_samples;
  g_samples = nullptr;
  g_capacity = 0;
}

inline void start(uint32_t hz, uint32_t seconds) {
  const uint32_t max_samples =
      env_u32("NWII_CPU_PROFILE_MAX", 200000u, 1000u, 1000000u);
  snapshot_modules();
  g_capacity = max_samples;
  g_samples = new Sample[max_samples]();
  g_hz = hz;
  g_seconds = seconds;
  g_start_ns = now_ns();
  g_last_report_ns = g_start_ns;
  g_report_period_ns = uint64_t(env_number("NWII_CPU_PROFILE_PERIOD", 5.0) *
                                1e9);
  if (g_report_period_ns < 1000000000ull) g_report_period_ns = 1000000000ull;
  g_deadline_ns.store(seconds ? g_start_ns + uint64_t(seconds) * 1000000000ull
                              : 0,
                      std::memory_order_relaxed);
  g_started.store(true, std::memory_order_relaxed);
  try {
    g_thread = std::thread(sampler, hz, seconds);
  } catch (...) {
    g_started.store(false, std::memory_order_relaxed);
    delete[] g_samples;
    g_samples = nullptr;
    g_capacity = 0;
    std::printf("[CPUPROFILE] sampler thread could not start\n");
    std::fflush(stdout);
  }
}

// Called from the guest thread once, before run_game().
inline void set_guest_thread() {
#ifdef _WIN32
  if (!enabled) return;
  g_guest_tid.store(GetCurrentThreadId(), std::memory_order_relaxed);
#endif
}

// Called once per host render-loop iteration. Registers the host thread, starts
// the sampler when the guest thread is known, and reports/finishes the capture.
inline void maybe_start() {
  if (!enabled) return;
#ifdef _WIN32
  if (!g_host_tid.load(std::memory_order_relaxed))
    g_host_tid.store(GetCurrentThreadId(), std::memory_order_relaxed);
  if (!g_started.load(std::memory_order_relaxed)) {
    if (!g_arm_ns) {
      g_delay_s = env_number("NWII_CPU_PROFILE_DELAY", 0.0);
      if (!(g_delay_s > 0.0)) g_delay_s = 0.0;
      if (g_delay_s > 3600.0) g_delay_s = 3600.0;
      g_arm_ns = now_ns() + uint64_t(g_delay_s * 1e9);
    }
    if (now_ns() < g_arm_ns) return;
    if (!g_guest_tid.load(std::memory_order_relaxed)) return;
    start(env_u32("NWII_CPU_PROFILE_HZ", 250u, 50u, 1000u),
          env_u32("NWII_CPU_PROFILE_SECONDS", 60u, 0u, 3600u));
    return;
  }
  if (g_done.load(std::memory_order_relaxed)) return;
  const uint64_t stamp = now_ns();
  const uint64_t deadline = g_deadline_ns.load(std::memory_order_relaxed);
  if (deadline && stamp >= deadline) {
    stop();
    return;
  }
  if (stamp - g_last_report_ns >= g_report_period_ns) {
    g_last_report_ns = stamp;
    std::printf(
        "[CPUPROFILE] t=%.1fs guest=%llu host=%llu idle=%llu dropped=%llu\n",
        double(stamp - g_start_ns) / 1e9,
        (unsigned long long)g_role_samples[RoleGuest].load(),
        (unsigned long long)g_role_samples[RoleHost].load(),
        (unsigned long long)g_idle.load(),
        (unsigned long long)g_dropped.load());
    std::fflush(stdout);
  }
#endif
}

}  // namespace native_cpu_profile
'''


def edit(relative, token, old, new):
    """Replace the anchor once, or no-op when this stage already ran."""
    path = runtime / relative
    text = path.read_text()
    if token in text:
        return False
    count = text.count(old)
    assert count == 1, "%s: anchor for %s matched %d times" % (
        relative, token, count)
    path.write_text(text.replace(old, new, 1))
    return True


header_dst.parent.mkdir(parents=True, exist_ok=True)
header_dst.write_text(CPU_PROFILE_HEADER)

# --- src/core/main.cpp ------------------------------------------------------
# The header is only needed by the main translation unit; the guest hooks it
# inserts are calls, not header users.
edit(
    "src/core/main.cpp",
    "NATIVE_CPU_PROFILE_V1_INCLUDE",
    '#include "runtime/hw/hw.h"\n',
    '#include "runtime/hw/hw.h"\n'
    '#include "runtime/native_cpu_profile.h" // NATIVE_CPU_PROFILE_V1_INCLUDE\n',
)

# Register the guest thread where CotT's phase and frame-trace stages also
# register it. The anchor is the runtime's own line, not their output, so this
# stage does not depend on either of them having run first.
edit(
    "src/core/main.cpp",
    "NATIVE_CPU_PROFILE_V1_THREAD",
    "  nwii::runtime::g_ctx_ptr = ctx;\n",
    "  nwii::runtime::g_ctx_ptr = ctx;\n"
    "  native_cpu_profile::set_guest_thread(); // NATIVE_CPU_PROFILE_V1_THREAD\n",
)

# The host thread is the one that iterates the render loop; it is also the
# thread that runs ProcessGXFifo and RendererGL::Render in this port.
edit(
    "src/core/main.cpp",
    "NATIVE_CPU_PROFILE_V1_LOOP",
    "    while (!quit && ctx->is_running) {\n",
    "    while (!quit && ctx->is_running) {\n"
    "      native_cpu_profile::maybe_start(); // NATIVE_CPU_PROFILE_V1_LOOP\n",
)

# A capture with NWII_CPU_PROFILE_SECONDS=0 runs until the window is closed;
# this is the only teardown hook that then writes the files.
edit(
    "src/core/main.cpp",
    "NATIVE_CPU_PROFILE_V1_STOP",
    "      if (audio_dev > 0) SDL_CloseAudioDevice(audio_dev);\n",
    "      native_cpu_profile::stop(); // NATIVE_CPU_PROFILE_V1_STOP\n"
    "      if (audio_dev > 0) SDL_CloseAudioDevice(audio_dev);\n",
)

print("In-process CPU profiler installed "
      "(NWII_CPU_PROFILE=1, NWII_CPU_PROFILE_SECONDS=60).")
