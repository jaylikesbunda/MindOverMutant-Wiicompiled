// NATIVE_MOM_POLL_DUE_V1 fixture: exercises the real header-side gate from
// include/runtime/cpu_context.h (native_poll_due / native_poll_reload) and the
// attention latch from native_idle_wait.h.  The ios.cpp reload call sequence is
// mirrored here exactly (checked textually by check_perf_stages.py).
//
//   NWII_POLL_CREDIT / NWII_NO_POLL_THROTTLE are read once per process by the
//   static inside native_poll_reload, so each env combination runs its own
//   process (check_perf_stages.py drives the matrix).
#include "runtime/cpu_context.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>

namespace nwii::runtime {
// The fixture owns the credit the header externs; ios.cpp owns it in the game.
int32_t g_native_poll_credit = 1;
// Unused-in-test externs the header references.
uint32_t g_watch_addr = 0;
void watch_hit(uint32_t, uint32_t, int) { std::abort(); }
uint16_t HW_Reg_Read16(uint32_t) { std::abort(); }
uint32_t HW_Reg_Read32(uint32_t) { std::abort(); }
void HW_Reg_Write16(uint32_t, uint16_t) { std::abort(); }
void HW_Reg_Write32(uint32_t, uint32_t) { std::abort(); }
void GX_WGPIPE_Write8(uint8_t) { std::abort(); }
void GX_WGPIPE_Write16(uint16_t) { std::abort(); }
void GX_WGPIPE_Write32(uint32_t) { std::abort(); }
void GX_WGPIPE_WriteF32(float) { std::abort(); }
void GX_WGPIPE_WriteF64(double) { std::abort(); }
}

using namespace nwii::runtime;

static int fails = 0;
#define CHECK(cond, msg) do { if (!(cond)) { ++fails; \
    std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, msg); } } while (0)

static const uint32_t kOrdinary = 0x8000402C;
static const uint32_t kIdle = 0x80492408;

int main() {
  const char* credit_env = std::getenv("NWII_POLL_CREDIT");
  const long expected = credit_env ? std::strtol(credit_env, nullptr, 10)
                                   : 64;
  const long clamped = std::getenv("NWII_NO_POLL_THROTTLE") ? 1
                       : (expected < 1 ? 1 : (expected > 1024 ? 1024 : expected));

  // 1. Reload: IPC/DI countdown forces every-site polling, including the
  //    boundary to and from zero; otherwise the (clamped) env value.
  CHECK(native_poll_reload(false, false) == clamped, "reload default/env");
  CHECK(native_poll_reload(true, false) == 1, "ipc delay forces 1");
  CHECK(native_poll_reload(false, true) == 1, "di delay forces 1");
  CHECK(native_poll_reload(true, true) == 1, "both delays force 1");
  CHECK(native_poll_reload(false, false) == clamped, "delay back to 0 restores reload");
  std::printf("reload=%ld (env NWII_POLL_CREDIT=%s throttle=%s)\n",
              clamped, credit_env ? credit_env : "unset",
              std::getenv("NWII_NO_POLL_THROTTLE") ? "set" : "unset");

  // 2. The gate decrements only the credit and polls when it expires.
  g_native_poll_credit = 2;
  native_idle::attention.store(0, std::memory_order_relaxed);
  CHECK(!native_poll_due(kOrdinary), "credit 2 -> not due");
  CHECK(g_native_poll_credit == 1, "credit decremented to 1");
  CHECK(native_poll_due(kOrdinary), "credit 0 crossing -> due");
  CHECK(g_native_poll_credit <= 0, "credit kept falling (reloaded by the real poll)");

  // 3. Attention forces an immediate poll regardless of credit.
  g_native_poll_credit = 500;
  native_idle::attention.store(1, std::memory_order_relaxed);
  CHECK(native_poll_due(kOrdinary), "attention forces due");
  // Consume-before-processing: the poll clears the flag; a producer notifying
  // during processing leaves it raised for the next site.
  native_idle::attention.store(0, std::memory_order_relaxed);   // consume
  native_idle::attention.store(1, std::memory_order_relaxed);   // producer mid-poll
  CHECK(native_poll_due(kOrdinary), "notification during consume not lost");
  native_idle::attention.store(0, std::memory_order_relaxed);

  // 4. Idle-loop PC and sentinels bypass the credit entirely (every-site).
  g_native_poll_credit = 1;
  for (uint32_t pc : {kIdle, 0u, 0xFFFFFFFCu}) {
    const int32_t before = g_native_poll_credit;
    CHECK(native_poll_due(pc), "exempt pc always due");
    CHECK(g_native_poll_credit == before, "exempt pc does not spend credit");
  }

  // 5. Producer on another thread raising attention wakes a waiting gate.
  {
    g_native_poll_credit = 1 << 20;
    std::atomic<bool> go{false};
    std::thread producer([&] {
      while (!go.load()) std::this_thread::yield();
      native_idle::attention.store(1, std::memory_order_relaxed);
    });
    go.store(true);
    int spins = 0;
    while (!native_poll_due(kOrdinary) && ++spins < 100000000) {}
    producer.join();
    CHECK(spins < 100000000, "producer attention observed by gate");
    native_idle::attention.store(0, std::memory_order_relaxed);
  }

  std::printf("%s (%d failure%s)\n", fails ? "FAIL" : "PASS", fails,
              fails == 1 ? "" : "s");
  return fails ? 1 : 0;
}
