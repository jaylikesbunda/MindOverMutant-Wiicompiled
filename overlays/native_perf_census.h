#pragma once
// NATIVE_PERF_CENSUS_V1
//
// Ported from the Crash of the Titans port's runtime/native_perf_census.h and
// adapted to this runtime. Game-independent: it names no guest address, reads
// no guest RAM and prints nothing unless NWII_PERF_CENSUS is set.
//
// The CotT original counts the operations of CotT's own recompiler (its
// dispatch switch, its quantised gather, its packed-vertex allocator, its
// texture cache). None of those exist here: this runtime has a different
// dispatcher, no quant gather and no packed-vertex path. What does exist, and
// what this header measures, is the guest poll.
//
// The guest polls the host at every backward branch, so:
//   * the poll rate is this runtime's closest thing to a "guest loop
//     iterations per second" meter;
//   * the guest's retired-instruction counter (ctx.inst_count, incremented by
//     the generated code once per guest instruction) advances between two
//     polls, so the instructions executed by one loop can be attributed
//     exactly to the poll site that loop ends at.
//
// The per-site histogram therefore gives a hot-spot profile with exact
// counts - not a sample - of where the guest spends its work.
//
// Cost, and why it is shaped the way it is
// -----------------------------------------
// The guest thread is close to saturating a core and the poll runs two to
// three million times a second, so the census adds as little as possible to
// that path:
//   * the guest's counters share one cache line, and the counter the host
//     thread writes has one of its own, so the two threads never bounce a
//     line;
//   * counters are read-modify-written with a load and a store, never with a
//     locked read-modify-write;
//   * the poll site is found through a four-entry cache, so the usual case is
//     a comparison;
//   * the clock is read only on every 64th poll. The counts stay exact; the
//     wall-clock attribution becomes a sample, and an unbiased one, because a
//     sampled poll draws the same site distribution as the polls themselves.
//
// An earlier revision read the clock on every poll and used locked
// read-modify-writes on counters the host thread also wrote. It cost about a
// third of a core, and on a guest already at 95 % of a core that was the
// difference between the game running and the game stalling.
//
// Env:
//   NWII_PERF_CENSUS=1        enable the census
//   NWII_PERF_PC_HISTOGRAM=1  also record the per-poll-site histogram
//   NWII_PERF_CENSUS_FAST=1   drop the clock read as well (counts only)
//   NWII_PERF_CENSUS_FILE     write the whole per-site table as CSV
//   NWII_PERF_CENSUS_PERIOD   seconds between [PERFCENSUS] reports (default 5)
//
// Output:
//   [PERFCENSUS] t=12.0s polls=... polls_s=... fast=... slow=... instr=...
//                instr_s=... polls_per_instr=... poll_us=... poll_pct=...
//   [PERFPCS] n=1 pc=0x... polls=... instr=... exec_ms=... poll_us=... share_pct=...
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace native_perf_census {

inline bool env_flag(const char *name) { return std::getenv(name) != nullptr; }

inline const bool enabled = env_flag("NWII_PERF_CENSUS");
inline const bool pc_histograms = enabled && env_flag("NWII_PERF_PC_HISTOGRAM");
// The clock read is the expensive half of the histogram. NWII_PERF_CENSUS_FAST
// drops it and keeps the exact instruction attribution.
inline const bool timing = enabled && !env_flag("NWII_PERF_CENSUS_FAST");
// How often the clock is read. The counts stay exact; only the wall-clock
// attribution is sampled.
inline const unsigned kClockEvery = 64;

// The guest's counters. One cache line, one writer (the guest thread).
struct alignas(64) GuestCounters {
  std::atomic<uint64_t> polls{0};
  std::atomic<uint64_t> polls_fast{0};
  std::atomic<uint64_t> polls_slow{0};
  std::atomic<uint64_t> instr{0};
  std::atomic<uint64_t> poll_cost_ns{0};
  std::atomic<uint64_t> poll_samples{0};
  char pad[64 - 6 * sizeof(std::atomic<uint64_t>)];
};
inline GuestCounters g_counters;

// The host's counter, on a line of its own so the two threads never share
// one.
struct alignas(64) HostCounter {
  std::atomic<uint64_t> frames{0};
  char pad[56];
};
inline HostCounter g_frames;

__attribute__((always_inline)) inline void Bump(std::atomic<uint64_t> &field,
                                                uint64_t amount) {
  field.store(field.load(std::memory_order_relaxed) + amount, std::memory_order_relaxed);
}

inline uint64_t Polls() { return g_counters.polls.load(std::memory_order_relaxed); }
inline uint64_t PollsFast() { return g_counters.polls_fast.load(std::memory_order_relaxed); }
inline uint64_t PollsSlow() { return g_counters.polls_slow.load(std::memory_order_relaxed); }
inline uint64_t GuestInstr() { return g_counters.instr.load(std::memory_order_relaxed); }
inline uint64_t PollCostNs() { return g_counters.poll_cost_ns.load(std::memory_order_relaxed); }
inline uint64_t PollSamples() { return g_counters.poll_samples.load(std::memory_order_relaxed); }

// Kept for source compatibility with the CotT overlay headers, which call it
// once per completed frame.
inline void Frame() {
  if (!enabled) return;
  Bump(g_frames.frames, 1);
}

inline uint64_t now_ns() {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
                       std::chrono::steady_clock::now().time_since_epoch())
                       .count());
}

// What one clock read costs. The poll cost is a difference of two reads, so
// the reads' own latency has to come out of it, otherwise the measurement
// reports the clock instead of the poll.
inline uint64_t clock_overhead_ns() {
  static const uint64_t overhead = [] {
    uint64_t best = UINT64_MAX;
    for (int i = 0; i < 64; ++i) {
      const uint64_t a = now_ns();
      const uint64_t b = now_ns();
      const uint64_t d = b >= a ? b - a : 0;
      if (d < best) best = d;
    }
    return best == UINT64_MAX ? 0 : best;
  }();
  return overhead;
}

// ---------------------------------------------------------------------------
// Per-poll-site histogram.
//
// One writer (the guest thread) and one reader (the reporter). Every field is
// a relaxed atomic, and the writer never uses a locked read-modify-write.
// ---------------------------------------------------------------------------
// The guest has tens of thousands of backward branches and every one of them
// is a poll site, so the table has to be large. 64 K sites of 32 bytes is
// 2 MiB; the hot few stay in cache, the cold ones cost an L3 hit.
constexpr unsigned kSlots = 65536;

struct alignas(32) Site {
  std::atomic<uint32_t> pc{0};
  std::atomic<uint64_t> polls{0};
  std::atomic<uint64_t> instr{0};
  std::atomic<uint64_t> exec_ns{0};
  char pad[4];
};

inline Site sites[kSlots];
inline std::atomic<uint64_t> overflow{0};

inline unsigned FindOrCreate(uint32_t pc) {
  const unsigned base = unsigned((pc * 2654435761u) & (kSlots - 1u));
  for (unsigned probe = 0; probe < 16; ++probe) {
    const unsigned index = (base + probe) & (kSlots - 1u);
    Site &s = sites[index];
    const uint32_t cur = s.pc.load(std::memory_order_relaxed);
    if (cur == pc) return index;
    if (cur == 0) {
      s.pc.store(pc, std::memory_order_relaxed);
      return index;
    }
  }
  overflow.fetch_add(1, std::memory_order_relaxed);
  return UINT32_MAX;
}

// A four-entry cache in front of the table: the guest polls the same few
// sites over and over, and this turns the usual lookup into a comparison.
inline uint32_t g_cache_pc[4] = {0, 0, 0, 0};
inline unsigned g_cache_slot[4] = {0, 0, 0, 0};
inline unsigned g_cache_next = 0;

inline unsigned SlotFor(uint32_t pc) {
  if (!pc) return UINT32_MAX;
  for (unsigned i = 0; i < 4; ++i)
    if (g_cache_pc[i] == pc) return g_cache_slot[i];
  const unsigned slot = FindOrCreate(pc);
  if (slot == UINT32_MAX) return slot;
  g_cache_pc[g_cache_next] = pc;
  g_cache_slot[g_cache_next] = slot;
  g_cache_next = (g_cache_next + 1) & 3u;
  return slot;
}

// ---------------------------------------------------------------------------
// Poll scope.
//
// Construct it at the top of the poll with the guest PC and the guest's
// instruction counter; call mark_fast() on the cheap path; the destructor
// accounts for the poll.
// ---------------------------------------------------------------------------

// State carried between two polls. Namespace scope, not function scope, so the
// hot path pays no thread-safe-static guard check.
inline uint64_t g_last_inst = 0;
inline uint64_t g_last_exit_ns = 0;
inline unsigned g_last_slot = UINT32_MAX;
inline unsigned g_poll_index = 0;
inline bool g_have_exit = false;
inline uint64_t g_poll_cost_min = UINT64_MAX;

// A histogram of the sampled poll costs, 10 ns per bucket. The mean is not a
// usable figure here: the guest thread is descheduled for part of the run and
// a poll that the scheduler interrupts measures the interruption too. The
// distribution is not distorted that way.
constexpr unsigned kPollBuckets = 128;
inline std::atomic<uint32_t> g_poll_hist[kPollBuckets];

struct PollScope {
  uint32_t pc;
  uint64_t inst;
  uint64_t t_enter;
  bool fast;
  bool sampled;
  PollScope(uint32_t pc_, uint64_t inst_)
      : pc(pc_), inst(inst_), t_enter(0), fast(false), sampled(false) {
    if (enabled && timing) {
      sampled = ((++g_poll_index) & (kClockEvery - 1u)) == 0;
      if (sampled) t_enter = now_ns();
    }
  }
  PollScope(const PollScope &) = delete;
  PollScope &operator=(const PollScope &) = delete;
  void mark_fast() { fast = true; }
  ~PollScope();
};

inline PollScope::~PollScope() {
  if (!enabled) return;
  const unsigned slot = SlotFor(pc);
  if (slot != UINT32_MAX) Bump(sites[slot].polls, 1);
  Bump(g_counters.polls, 1);
  Bump(fast ? g_counters.polls_fast : g_counters.polls_slow, 1);

  // The instructions executed since the previous poll belong to the loop
  // whose backward branch is the *previous* poll site.
  const uint64_t di = inst >= g_last_inst ? inst - g_last_inst : 0;
  g_last_inst = inst;
  if (di) {
    Bump(g_counters.instr, di);
    const unsigned target = (slot == g_last_slot) ? slot : g_last_slot;
    if (target != UINT32_MAX) Bump(sites[target].instr, di);
  }
  g_last_slot = slot;

  if (!sampled) return;
  const uint64_t t_exit = now_ns();
  // The two clock reads that bracket the poll are not part of the poll.
  const uint64_t overhead = clock_overhead_ns() * 2;
  const uint64_t raw = t_exit >= t_enter ? t_exit - t_enter : 0;
  const uint64_t poll_ns = raw > overhead ? raw - overhead : 0;
  if (!g_have_exit) {
    // The first sample has no predecessor; it only starts the interval.
    g_have_exit = true;
    g_last_exit_ns = t_exit;
    return;
  }
  const uint64_t interval = t_exit >= g_last_exit_ns ? t_exit - g_last_exit_ns : 0;
  if (slot != UINT32_MAX) {
    Bump(sites[slot].exec_ns, interval);
  }
  Bump(g_counters.poll_cost_ns, poll_ns);
  Bump(g_counters.poll_samples, 1);
  if (poll_ns < g_poll_cost_min) g_poll_cost_min = poll_ns;
  {
    const unsigned bucket = unsigned(poll_ns / 10u);
    g_poll_hist[bucket < kPollBuckets ? bucket : kPollBuckets - 1u].fetch_add(
        1, std::memory_order_relaxed);
  }
  g_last_exit_ns = t_exit;
}

// ---------------------------------------------------------------------------
// Reporting. Called from the host loop, never from the guest thread, so the
// line does not interleave with the guest's own output.
// ---------------------------------------------------------------------------
inline double report_period_s() {
  static const double seconds = [] {
    const char *e = std::getenv("NWII_PERF_CENSUS_PERIOD");
    const double v = e ? std::strtod(e, nullptr) : 5.0;
    return v > 0.05 ? v : 5.0;
  }();
  return seconds;
}

inline const char *census_file() {
  static const char *path = std::getenv("NWII_PERF_CENSUS_FILE");
  return path;
}

inline uint64_t g_report_last_ns = 0;
inline uint64_t g_report_start_ns = 0;
inline uint64_t g_report_last_polls = 0;
inline uint64_t g_report_last_instr = 0;

// The percentiles of the sampled poll costs, in nanoseconds.
inline void poll_percentiles(double *p10, double *p50, double *p90) {
  uint64_t total = 0;
  for (unsigned i = 0; i < kPollBuckets; ++i)
    total += g_poll_hist[i].load(std::memory_order_relaxed);
  *p10 = *p50 = *p90 = 0.0;
  if (!total) return;
  const uint64_t want[3] = {total / 10, total / 2, (total * 9) / 10};
  uint64_t seen = 0;
  unsigned slot = 0;
  for (unsigned i = 0; i < kPollBuckets; ++i) {
    seen += g_poll_hist[i].load(std::memory_order_relaxed);
    while (slot < 3 && seen >= want[slot]) {
      double *target = slot == 0 ? p10 : (slot == 1 ? p50 : p90);
      *target = double(i) * 10.0 + 5.0;
      ++slot;
    }
    if (slot == 3) break;
  }
  while (slot < 3) {
    double *target = slot == 0 ? p10 : (slot == 1 ? p50 : p90);
    *target = double(kPollBuckets) * 10.0;
    ++slot;
  }
}

inline void WriteTable() {
  const char *path = census_file();
  if (!path || !*path) return;
  std::FILE *out = std::fopen(path, "wb");
  if (!out) return;
  std::fprintf(out, "pc,polls,instr,exec_ns\n");
  for (unsigned i = 0; i < kSlots; ++i) {
    const uint32_t pc = sites[i].pc.load(std::memory_order_relaxed);
    if (!pc) continue;
    std::fprintf(out, "0x%08X,%llu,%llu,%llu\n", pc,
                 (unsigned long long)sites[i].polls.load(std::memory_order_relaxed),
                 (unsigned long long)sites[i].instr.load(std::memory_order_relaxed),
                 (unsigned long long)sites[i].exec_ns.load(std::memory_order_relaxed));
  }
  std::fclose(out);
}

inline void maybe_report() {
  if (!enabled) return;
  const uint64_t now = now_ns();
  if (!g_report_last_ns) {
    g_report_last_ns = now;
    g_report_start_ns = now;
    return;
  }
  const double elapsed_s = double(now - g_report_last_ns) / 1e9;
  if (elapsed_s < report_period_s()) return;
  const double total_s = double(now - g_report_start_ns) / 1e9;

  const uint64_t polls = Polls();
  const uint64_t instr = GuestInstr();
  const uint64_t dpolls = polls - g_report_last_polls;
  const uint64_t dinstr = instr - g_report_last_instr;
  // The cost of one poll, measured on the sampled polls. The guest thread is
  // descheduled for part of the run, and a poll that is interrupted by the
  // scheduler measures the interruption too, so the mean is an upper bound
  // and the minimum is the better estimate of the poll's own cost.
  const uint64_t samples = PollSamples();
  const double poll_us = samples ? double(PollCostNs()) / double(samples) / 1000.0 : 0.0;
  const double poll_us_min = g_poll_cost_min == UINT64_MAX ? 0.0 : double(g_poll_cost_min) / 1000.0;
  double poll_p10 = 0.0, poll_p50 = 0.0, poll_p90 = 0.0;
  poll_percentiles(&poll_p10, &poll_p50, &poll_p90);
  const double poll_ms = poll_us * double(dpolls) / 1000.0;

  std::printf(
      "[PERFCENSUS] t=%.1fs polls=%llu polls_s=%.0f fast=%llu slow=%llu "
      "instr=%llu instr_s=%.0f polls_per_instr=%.3f poll_us=%.2f poll_us_min=%.2f "
      "poll_us_p10=%.2f poll_us_p50=%.2f poll_us_p90=%.2f poll_pct=%.1f\n",
      total_s, (unsigned long long)polls, double(dpolls) / elapsed_s,
      (unsigned long long)PollsFast(), (unsigned long long)PollsSlow(),
      (unsigned long long)instr, double(dinstr) / elapsed_s,
      dinstr ? double(dpolls) / double(dinstr) : 0.0, poll_us, poll_us_min,
      poll_p10 / 1000.0, poll_p50 / 1000.0, poll_p90 / 1000.0,
      elapsed_s > 0 ? 100.0 * poll_ms / (elapsed_s * 1000.0) : 0.0);
  std::fflush(stdout);

  if (pc_histograms) {
    struct Row {
      uint32_t pc;
      uint64_t polls, instr, exec_ns;
    };
    std::vector<Row> rows;
    rows.reserve(64);
    uint64_t total_exec = 0;
    for (unsigned i = 0; i < kSlots; ++i) {
      const uint32_t pc = sites[i].pc.load(std::memory_order_relaxed);
      if (!pc) continue;
      Row r{pc,
            sites[i].polls.load(std::memory_order_relaxed),
            sites[i].instr.load(std::memory_order_relaxed),
            sites[i].exec_ns.load(std::memory_order_relaxed)};
      total_exec += r.exec_ns;
      rows.push_back(r);
    }
    for (size_t i = 1; i < rows.size(); ++i) {
      Row key = rows[i];
      size_t j = i;
      while (j > 0 && rows[j - 1].exec_ns < key.exec_ns) {
        rows[j] = rows[j - 1];
        --j;
      }
      rows[j] = key;
    }
    const size_t shown = rows.size() < 16 ? rows.size() : 16;
    for (size_t i = 0; i < shown; ++i) {
      const Row &r = rows[i];
      std::printf(
          "[PERFPCS] n=%zu pc=0x%08X polls=%llu instr=%llu exec_ms=%.1f "
          "share_pct=%.1f\n",
          i + 1, r.pc, (unsigned long long)r.polls, (unsigned long long)r.instr,
          double(r.exec_ns) / 1e6,
          total_exec ? 100.0 * double(r.exec_ns) / double(total_exec) : 0.0);
    }
    const uint64_t dropped = overflow.load(std::memory_order_relaxed);
    if (dropped) std::printf("[PERFPCS] overflow=%llu slots=%u\n",
                             (unsigned long long)dropped, kSlots);
    WriteTable();
  }

  g_report_last_ns = now;
  g_report_last_polls = polls;
  g_report_last_instr = instr;
}

}  // namespace native_perf_census
