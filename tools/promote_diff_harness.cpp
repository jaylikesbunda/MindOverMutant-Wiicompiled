// Differential harness for the guest register promotion stage.
//
// tools/promote_diff_harness.py generates three includes next to this file:
//   targets.inc   the pristine emitter output and the promoted version of every
//                 hot guest function, side by side, calls redirected to stubs
//   stubs.inc     one stub per guest callee
//   table.inc     the list of (name, original, promoted, entry address)
//
// Both copies of a function run from the same seeded CPUContext and the whole
// guest-visible result is compared: gpr[], cr[], ctr, xer, pc, lr, inst_count
// and a hash of guest RAM.  The runtime is stubbed and the stubs deliberately
// scribble on the register file, so the spill/reload protocol around a barrier
// is exercised rather than glossed over.  Every guest backward branch calls
// process_pending_callbacks; every fourth call claims it ran a callback, which
// makes the generated code return to the dispatcher, and the mini dispatcher
// below re-enters the function at that pc - the interior-entry path.
//
// Built and run by tools/promote_diff_harness.py; see logs/cott-promote-regs.md.
#include "runtime/cpu_context.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <chrono>
#include <vector>

using nwii::runtime::CPUContext;

namespace nwii::runtime {

bool g_trace_calls = false;

struct Budget {};

static uint64_t g_poll = 0;
static uint64_t g_budget = 3000;

static inline uint32_t mix(uint32_t x) {
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  return x;
}

// Every poll and every callee perturbs the register file deterministically.
static void scribble(CPUContext& c, uint32_t salt) {
  const uint32_t n = mix(uint32_t(g_poll) + salt);
  c.gpr[3] = mix(c.gpr[3] + n);
  c.gpr[4] = mix(c.gpr[4] ^ n);
  c.gpr[0] = mix(c.gpr[0] + 1);
  c.cr[0].eq = (n & 1) != 0;
  c.cr[0].lt = (n & 2) != 0;
  c.ctr = mix(c.ctr);
  c.xer ^= (n & 0x20000000u);
}

bool process_pending_callbacks(CPUContext& c) {
  ++g_poll;
  if (g_poll > g_budget) throw Budget{};
  scribble(c, 0x9E3779B9u);
  // Every fourth poll behaves like one that ran a guest callback: ctx is left
  // authoritative, pc still names the poll site, and the generated code returns
  // to the dispatcher, which re-enters the function at that pc.
  return (g_poll & 3) == 0;
}

void trace_call(uint32_t, CPUContext&) {}
void note_null_call(uint32_t, uint32_t) {}
bool handle_syscall(CPUContext& c) { scribble(c, 0x85EBCA6Bu); return false; }
void interpret_step(CPUContext& c) { c.pc = 0xFFFFFFF0u; }
void micro_interpret(CPUContext& c, uint32_t, uint32_t) { c.pc = 0xFFFFFFF1u; }

// MMU slow-path / device hooks: there are no devices in the harness.
uint32_t g_watch_addr = 0;
void watch_hit(uint32_t, uint32_t, int) {}
uint32_t HW_Reg_Read32(uint32_t) { return 0; }
uint16_t HW_Reg_Read16(uint32_t) { return 0; }
void HW_Reg_Write32(uint32_t, uint32_t) {}
void HW_Reg_Write16(uint32_t, uint16_t) {}
void GX_WGPIPE_Write8(uint8_t) {}
void GX_WGPIPE_Write16(uint16_t) {}
void GX_WGPIPE_Write32(uint32_t) {}
void GX_WGPIPE_WriteF32(float) {}
void GX_WGPIPE_WriteF64(double) {}
void GX_DL_Snapshot(uint32_t, uint32_t) {}
bool GX_DL_Fetch(uint32_t, uint32_t, std::vector<uint8_t>&) { return false; }
uint32_t native_wpar_read() { return 0; }
void native_wpar_write(uint32_t) {}
void init_ipc_client(CPUContext&) {}

}  // namespace nwii::runtime

#include "stubs.inc"
#include "targets.inc"

namespace {

struct Snapshot {
  uint32_t gpr[32];
  uint32_t ctr, xer, pc, lr, msr, fpscr;
  bool cr[8][4];
  uint64_t inst;
  uint64_t ram;
  uint64_t polls;
  int mode;  // 0 returned, 1 poll budget, 2 exception, 3 interpreter
};

uint64_t hash_ram(const CPUContext& c) {
  uint64_t h = 1469598103934665603ull;
  const uint8_t* p = c.mmu.mem1.data();
  const size_t n = c.mmu.mem1.size() - 8;
  for (size_t i = 0; i + 8 <= n; i += 8) {
    uint64_t v;
    std::memcpy(&v, p + i, 8);
    h = (h ^ v) * 1099511628211ull;
    h ^= h >> 29;
  }
  return h;
}

// Everything the promoted code can observe has to start from the same value in
// both runs, or one trial contaminates the next.
void seed(CPUContext& c, uint32_t s, uint32_t entry) {
  uint32_t x = s * 2654435761u + 1u;
  auto next = [&x]() { x = nwii::runtime::mix(x); return x; };
  std::memset(c.mmu.mem2.data(), 0, c.mmu.mem2.size());
  for (size_t i = 0; i + 4 <= c.mmu.mem1.size(); i += 4) {
    const uint32_t v = next();
    std::memcpy(c.mmu.mem1.data() + i, &v, 4);
  }
  for (int i = 0; i < 32; ++i) c.gpr[i] = next();
  c.gpr[1] = 0x80690000u + (next() & 0xFFF0u);
  c.gpr[2] = 0x805DDF20u;
  c.gpr[13] = 0x805D9EC0u;
  c.gpr[3] = 0x80010000u + (next() & 0xFFFFu);
  c.gpr[4] = 0x80010000u + (next() & 0xFFFFu);
  c.gpr[5] = next() & 0x3FFu;
  for (int i = 0; i < 8; ++i) {
    c.cr[i].lt = (next() & 1) != 0;
    c.cr[i].gt = (next() & 1) != 0;
    c.cr[i].eq = (next() & 1) != 0;
    c.cr[i].so = (next() & 1) != 0;
  }
  c.ctr = next();
  c.xer = next();
  c.msr = (next() & 1) ? 0xA032u : 0x2032u;
  for (int i = 0; i < 32; ++i) {
    c.fpr[i] = double(next()) / 4294967296.0;
    c.ps1[i] = double(next()) / 4294967296.0;
  }
  c.fpscr = 0;
  c.srr0 = 0;
  c.srr1 = 0;
  c.exception_pc = 0;
  c.reservation_addr = 0xFFFFFFFFu;
  c.dec_value = 0xFFFFFFFFu;
  c.dec_written_tb = 0;
  c.dec_irq_pending = false;
  c.tb_cache_valid = false;
  c.tb_cache_inst = 0;
  c.tb_cache_val = 0;
  c.pending_cb_count.store(0);
  c.vblank_pending.store(false);
  c.is_running.store(true);
  c.ext_resched_pending = false;
  c.dispatch_saved_thread = 0;
  c.dispatch_saved_ctx = 0;
  c.in_callback = false;
  c.callback_depth = 0;
  for (int i = 0; i < 8; ++i) c.gqr[i] = next();
  c.pc = entry;
  c.lr = 0x80000000u + (next() & 0xFFFu);
  c.inst_count = next() & 0xFFFFu;
  c.mmu.write32(0x800000D4u, 0x80002000u);
  c.mmu.write32(0x800000D8u, 0x80002000u);
  std::memset(c.mmu.mem1.data() + 0x2000, 0, 0x200);
}

Snapshot run(void (*fn)(CPUContext&), CPUContext& c, uint32_t s, uint32_t entry) {
  seed(c, s, entry);
  nwii::runtime::g_poll = 0;
  Snapshot out{};
  int mode = 0;
  const uint32_t ret = c.lr;
  try {
    for (int hop = 0; hop < 100000; ++hop) {
      fn(c);
      if (c.pc == ret) break;  // returned to its caller
      if (c.pc == 0xFFFFFFF0u || c.pc == 0xFFFFFFF1u) { mode = 3; break; }
    }
  } catch (nwii::runtime::Budget&) {
    mode = 1;
  } catch (...) {
    mode = 2;
  }
  std::memcpy(out.gpr, c.gpr.data(), sizeof(out.gpr));
  out.ctr = c.ctr;
  out.xer = c.xer;
  out.pc = c.pc;
  out.lr = c.lr;
  out.msr = c.msr;
  out.fpscr = c.fpscr;
  for (int i = 0; i < 8; ++i) {
    out.cr[i][0] = c.cr[i].lt;
    out.cr[i][1] = c.cr[i].gt;
    out.cr[i][2] = c.cr[i].eq;
    out.cr[i][3] = c.cr[i].so;
  }
  out.inst = c.inst_count;
  out.ram = hash_ram(c);
  out.polls = nwii::runtime::g_poll;
  out.mode = mode;
  return out;
}

struct Target {
  const char* name;
  void (*orig)(CPUContext&);
  void (*promoted)(CPUContext&);
  uint32_t entry;
};

const Target kTargets[] = {
#include "table.inc"
};

double now_ms() {
  using namespace std::chrono;
  return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
}

double seed_only(CPUContext& c, int trials) {
  double best = 1e30;
  for (int rep = 0; rep < 3; ++rep) {
    const double t0 = now_ms();
    for (int i = 0; i < trials; ++i) seed(c, uint32_t(i), kTargets[0].entry);
    const double t1 = now_ms();
    if (t1 - t0 < best) best = t1 - t0;
  }
  return best;
}

// The seeding (24 MiB of guest RAM) costs far more than the guest code, so the
// benchmark measures seed-only and subtracts it.  Reported separately.
void bench(CPUContext& c, int trials) {
  const double base = seed_only(c, trials);
  std::printf("seed only: %.1f ms per %d trials\n\n", base, trials);
  std::printf("%-46s %10s %10s %8s\n", "function", "orig ms", "prom ms", "ratio");
  double sum_o = 0, sum_p = 0;
  for (const Target& t : kTargets) {
    double best_o = 1e30, best_p = 1e30;
    for (int rep = 0; rep < 3; ++rep) {
      const double t0 = now_ms();
      for (int i = 0; i < trials; ++i) run(t.orig, c, uint32_t(i), t.entry);
      const double t1 = now_ms();
      for (int i = 0; i < trials; ++i) run(t.promoted, c, uint32_t(i), t.entry);
      const double t2 = now_ms();
      if (t1 - t0 < best_o) best_o = t1 - t0;
      if (t2 - t1 < best_p) best_p = t2 - t1;
    }
    const double o = best_o - base, q = best_p - base;
    std::printf("%-46s %10.1f %10.1f %8.3f\n", t.name, o, q, q / o);
    sum_o += o;
    sum_p += q;
  }
  std::printf("%-46s %10.1f %10.1f %8.3f\n", "TOTAL (seed removed)", sum_o, sum_p,
              sum_p / sum_o);
}

}  // namespace

int main(int argc, char** argv) {
  const int trials = argc > 1 ? std::atoi(argv[1]) : 200;
  if (argc > 2) nwii::runtime::g_budget = std::strtoul(argv[2], nullptr, 10);
  const char* only = argc > 3 ? argv[3] : nullptr;
  CPUContext ctx;
  if (argc > 4 && std::strcmp(argv[4], "time") == 0) {
    bench(ctx, argc > 5 ? std::atoi(argv[5]) : 20);
    return 0;
  }
  int bad_functions = 0;
  for (const Target& t : kTargets) {
    if (only && std::strcmp(only, t.name) != 0) continue;
    int bad = 0, long_runs = 0;
    uint64_t polls = 0;
    for (int i = 0; i < trials; ++i) {
      const Snapshot a = run(t.orig, ctx, uint32_t(i), t.entry);
      const Snapshot b = run(t.promoted, ctx, uint32_t(i), t.entry);
      polls += a.polls;
      if (a.mode == 1) ++long_runs;
      const bool same = a.mode == b.mode && a.polls == b.polls && a.ram == b.ram &&
                        a.ctr == b.ctr && a.xer == b.xer && a.pc == b.pc &&
                        a.lr == b.lr && a.inst == b.inst &&
                        std::memcmp(a.gpr, b.gpr, sizeof(a.gpr)) == 0 &&
                        std::memcmp(a.cr, b.cr, sizeof(a.cr)) == 0;
      if (!same) {
        if (bad < 3) {
          std::printf("  %s seed %d: mode %d/%d polls %llu/%llu ram %016llx/%016llx\n",
                      t.name, i, a.mode, b.mode, (unsigned long long)a.polls,
                      (unsigned long long)b.polls, (unsigned long long)a.ram,
                      (unsigned long long)b.ram);
          for (int r = 0; r < 32; ++r)
            if (a.gpr[r] != b.gpr[r])
              std::printf("      gpr[%2d] %08x / %08x\n", r, a.gpr[r], b.gpr[r]);
          if (a.ctr != b.ctr) std::printf("      ctr %08x / %08x\n", a.ctr, b.ctr);
          if (a.xer != b.xer) std::printf("      xer %08x / %08x\n", a.xer, b.xer);
          if (a.pc != b.pc) std::printf("      pc  %08x / %08x\n", a.pc, b.pc);
          if (a.inst != b.inst)
            std::printf("      inst %llu / %llu\n", (unsigned long long)a.inst,
                        (unsigned long long)b.inst);
          for (int f = 0; f < 8; ++f)
            if (std::memcmp(a.cr[f], b.cr[f], 4) != 0)
              std::printf("      cr%d %d%d%d%d / %d%d%d%d\n", f, a.cr[f][0],
                          a.cr[f][1], a.cr[f][2], a.cr[f][3], b.cr[f][0],
                          b.cr[f][1], b.cr[f][2], b.cr[f][3]);
        }
        ++bad;
      }
    }
    std::printf("%-46s %s (%d/%d trials differ, avg polls %.0f, %d hit the budget)\n",
                t.name, bad ? "FAIL" : "ok  ", bad, trials, double(polls) / trials,
                long_runs);
    if (bad) ++bad_functions;
  }
  std::printf("\n%d of %d functions differ\n", bad_functions,
              int(sizeof(kTargets) / sizeof(kTargets[0])));
  return bad_functions ? 1 : 0;
}
