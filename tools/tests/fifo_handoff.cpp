// NATIVE_MOM_FIFO_HANDOFF_V1 fixture: exercises the real NativeAppendFifo and
// NativeFifoLock from include/runtime/native_fifo_lock.h, driven through the
// exact publish/drain sequence gx.cpp performs (mirrored here line for line;
// check_perf_stages.py asserts the gx.cpp shape so the two cannot drift apart).
//
// Covers: publish to empty/nonempty destination, byte-exact concatenation,
// partial-command carry in the parse buffer, the pending-byte counter, and a
// producer publishing while the host processes its drain buffer.
#include "runtime/native_fifo_lock.h"
#include <atomic>
#include <cstdio>
#include <cstring>
#include <vector>

using nwii::runtime::NativeAppendFifo;
using nwii::runtime::NativeFifoLock;
using nwii::runtime::NativeFifoLockGuard;

static int fails = 0;
#define CHECK(cond, msg) do { if (!(cond)) { ++fails; \
    std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, msg); } } while (0)

// Mirror of vendor/.../src/hle/gx.cpp state.
static NativeFifoLock g_fifo_mutex;
static std::vector<uint8_t> g_hw_fifo;
static std::vector<uint8_t> g_cpu_fifo;
static std::atomic<size_t> g_gx_pending_bytes{0};
static std::vector<uint8_t> s_parse_buf;          // render-thread-owned

// Mirror of native_gx_publish().
static void publish() {
  if (g_cpu_fifo.empty()) return;
  {
    NativeFifoLockGuard lk(g_fifo_mutex);
    const size_t published = g_cpu_fifo.size();
    NativeAppendFifo(g_hw_fifo, g_cpu_fifo);
    g_gx_pending_bytes.fetch_add(published, std::memory_order_relaxed);
  }
  g_cpu_fifo.clear();
}

// Mirror of ProcessGXFifo()'s head: swap under the lock, append outside.
static void drain() {
  std::vector<uint8_t> native_drain_buf;
  {
    NativeFifoLockGuard lock(g_fifo_mutex);
    if (!g_hw_fifo.empty()) native_drain_buf.swap(g_hw_fifo);
    g_gx_pending_bytes.store(0, std::memory_order_relaxed);
  }
  if (!native_drain_buf.empty())
    NativeAppendFifo(s_parse_buf, native_drain_buf);
}

// The parser consumes whole commands and carries the partial tail.
static void parse(size_t whole_bytes) {
  CHECK(whole_bytes <= s_parse_buf.size(), "parse bound");
  s_parse_buf.erase(s_parse_buf.begin(), s_parse_buf.begin() + whole_bytes);
}

static std::vector<uint8_t> seq(size_t n, uint8_t base) {
  std::vector<uint8_t> v(n);
  for (size_t i = 0; i < n; ++i) v[i] = uint8_t(base + i);
  return v;
}

int main() {
  // 1. Publish into an empty shared FIFO swaps (transfers) the buffer.
  g_cpu_fifo = seq(7, 0x10);
  publish();
  CHECK(g_hw_fifo == seq(7, 0x10), "publish empty dest: bytes");
  CHECK(g_cpu_fifo.empty(), "publish empty dest: source cleared");
  CHECK(g_gx_pending_bytes.load() == 7, "pending = 7");

  // 2. Publish into a nonempty shared FIFO concatenates in order.
  g_cpu_fifo = seq(5, 0x40);
  publish();
  std::vector<uint8_t> expect = seq(7, 0x10);
  {
    const auto tail = seq(5, 0x40);
    expect.insert(expect.end(), tail.begin(), tail.end());
  }
  CHECK(g_hw_fifo == expect, "publish nonempty dest: bytes");
  CHECK(g_gx_pending_bytes.load() == 12, "pending = 12");

  // 3. Drain swaps out under the lock; parse buffer gets the exact bytes.
  drain();
  CHECK(g_hw_fifo.empty(), "drain emptied shared fifo");
  CHECK(g_gx_pending_bytes.load() == 0, "pending reset under lock");
  CHECK(s_parse_buf == expect, "drain byte-exact concatenation");

  // 4. Partial command carry: parser consumes 9 of 12, keeps 3.
  parse(9);
  CHECK(s_parse_buf.size() == 3 && s_parse_buf[0] == 0x42,
        "partial carry keeps tail bytes");

  // 5. Producer publishes while the drain buffer is being processed:
  //    the new bytes land in g_hw_fifo for the *next* drain, never mingled
  //    with the in-flight parse data.
  g_cpu_fifo = seq(4, 0x60);
  publish();                                   // 4 bytes -> hw fifo
  std::vector<uint8_t> mid;
  {
    NativeFifoLockGuard lock(g_fifo_mutex);
    if (!g_hw_fifo.empty()) mid.swap(g_hw_fifo);     // drain takes them
    g_gx_pending_bytes.store(0, std::memory_order_relaxed);
  }
  g_cpu_fifo = seq(3, 0x80);
  publish();                                   // published *during* processing
  CHECK(g_gx_pending_bytes.load() == 3, "pending counts post-swap publish");
  NativeAppendFifo(s_parse_buf, mid);          // drain appends after unlock
  drain();                                     // next drain picks up the 3
  std::vector<uint8_t> want{0x42, 0x43, 0x44}; // carried tail
  want.insert(want.end(), {0x60, 0x61, 0x62, 0x63, 0x80, 0x81, 0x82});
  CHECK(s_parse_buf == want, "mid-drain publish ordered after in-flight bytes");

  // 6. Alternating empty/nonempty appends stay byte-exact.
  s_parse_buf.clear(); g_hw_fifo.clear(); g_cpu_fifo.clear();
  g_gx_pending_bytes.store(0);
  std::vector<uint8_t> all;
  for (int round = 0; round < 50; ++round) {
    const size_t n = size_t((round * 37) % 19 + 1);
    auto chunk = seq(n, uint8_t(round));
    g_cpu_fifo = chunk;
    publish();
    all.insert(all.end(), chunk.begin(), chunk.end());
    if (round % 7 == 0) {
      drain();
      CHECK(s_parse_buf == all, "round drain byte-exact");
      parse(s_parse_buf.size());
      all.clear();                       // everything so far was consumed
    }
  }
  drain();
  CHECK(s_parse_buf == all, "final drain byte-exact");
  CHECK(g_gx_pending_bytes.load() == 0, "pending reset at end");
  CHECK(g_cpu_fifo.empty() && g_hw_fifo.empty(), "sources drained");

  std::printf("%s (%d failure%s)\n", fails ? "FAIL" : "PASS", fails,
              fails == 1 ? "" : "s");
  return fails ? 1 : 0;
}
