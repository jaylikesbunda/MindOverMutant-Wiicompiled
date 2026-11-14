// NativePersistentRing lifecycle tests using the REAL header and a stub GL
// table - missing extension, create/map failure, oversized, alignment,
// in-segment append, seal-once rotation, busy-segment timeout (no overwrite),
// retry after signal, WAIT_FAILED disable, release ordering.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include <map>
#include "runtime/native_persistent_upload_mom.h"

using nwii::runtime::NativePersistentRing;
using nwii::runtime::NativePersistentGL;
using nwii::runtime::g_persistent_gl;

// ---------------- stub GL ----------------
static int s_finish_calls = 0, s_bufferstorage_calls = 0, s_bufferdata_ok = 1;
static int s_map_fail = 0, s_create_fail = 0;
static unsigned int s_next_buf = 100;
static std::map<unsigned long long, int> s_fence_signaled; // fence id -> signaled
static std::vector<unsigned long long> s_fences_made;
static std::vector<unsigned char> s_backing; // pretend device memory

namespace nwii::runtime { NativePersistentGL g_persistent_gl; }

static void stub_GenBuffers(int n, unsigned int* out) {
  if (s_create_fail) { *out = 0; return; }
  for (int i = 0; i < n; ++i) out[i] = s_next_buf++;
}
static void stub_DeleteBuffers(int, const unsigned int*) {}
static void stub_BindBuffer(unsigned int, unsigned int) {}
static void stub_BufferStorage(unsigned int, long long bytes, const void*,
                               unsigned int) {
  ++s_bufferstorage_calls;
  s_backing.assign((size_t)bytes, 0);
}
static void* stub_MapBufferRange(unsigned int, long long, long long bytes,
                                 unsigned int) {
  if (s_map_fail) return nullptr;
  s_backing.assign((size_t)bytes, 0);
  return s_backing.data();
}
static unsigned long long s_fence_id = 0;
static unsigned long long stub_FenceSync(unsigned int, unsigned int) {
  const unsigned long long f = ++s_fence_id;
  s_fences_made.push_back(f);
  return f;
}
static unsigned int stub_ClientWaitSync(unsigned long long f, unsigned int,
                                        unsigned long long) {
  return s_fence_signaled[f] ? GL_CONDITION_SATISFIED : 0x9118u /*TIMEOUT_EXPIRED*/;
}
static void stub_DeleteSync(unsigned long long) {}
static int stub_UnmapBuffer(unsigned int) { return 1; }
static void stub_Finish() { ++s_finish_calls; }
static unsigned int stub_GetError() { return GL_NO_ERROR; }

static void install_stub() {
  g_persistent_gl = {};
  g_persistent_gl.GenBuffers = stub_GenBuffers;
  g_persistent_gl.DeleteBuffers = stub_DeleteBuffers;
  g_persistent_gl.BindBuffer = stub_BindBuffer;
  g_persistent_gl.BufferStorage = stub_BufferStorage;
  g_persistent_gl.MapBufferRange = stub_MapBufferRange;
  g_persistent_gl.FenceSync = stub_FenceSync;
  g_persistent_gl.ClientWaitSync = stub_ClientWaitSync;
  g_persistent_gl.DeleteSync = stub_DeleteSync;
  g_persistent_gl.UnmapBuffer = stub_UnmapBuffer;
  g_persistent_gl.Finish = stub_Finish;
  g_persistent_gl.GetError = stub_GetError;
}

static int fails = 0;
#define CK(cond, name) do { if (!(cond)) { ++fails; printf("FAIL %s\n", name); } } while (0)

static const size_t SEG = 1024; // fixture-sized segments

int main() {
  install_stub();

  // 1) unsupported: no BufferStorage -> Init reports off, upload falls back.
  {
    g_persistent_gl.BufferStorage = nullptr;
    NativePersistentRing r(SEG);
    CK(!r.Init() && !r.ok(), "no buffer_storage -> unsupported");
    uint8_t b[4] = {1,2,3,4};
    CK(r.upload(GL_ARRAY_BUFFER, b, 4, 4) == SIZE_MAX, "unsupported fallback");
    install_stub();
  }

  // 2) create failure / map failure -> off, no leak crash.
  {
    s_create_fail = 1;
    NativePersistentRing r(SEG);
    CK(!r.Init(), "create fail");
    s_create_fail = 0;
    s_map_fail = 1;
    NativePersistentRing r2(SEG);
    CK(!r2.Init(), "map fail");
    s_map_fail = 0;
  }

  // 3) aligned append within a segment, offsets divisible by align.
  {
    NativePersistentRing r(SEG);
    CK(r.Init() && r.ok(), "init ok");
    CK(r.buffer() != 0, "buffer created");
    uint8_t data[64]; memset(data, 0xAB, 64);
    size_t o1 = r.upload(GL_ARRAY_BUFFER, data, 100, 160);
    CK(o1 != SIZE_MAX && o1 % 160 == 0, "align160");
    size_t o2 = r.upload(GL_ARRAY_BUFFER, data, 10, 4);
    CK(o2 != SIZE_MAX && o2 % 4 == 0 && o2 >= o1 + 100, "align4 append");
    CK(std::memcmp(s_backing.data() + o1, data, 100) == 0, "bytes written");
    size_t o3 = r.upload(GL_ARRAY_BUFFER, data, 8, 136);
    CK(o3 != SIZE_MAX && o3 % 136 == 0, "align136");
    // zero bytes / zero align / oversized -> fallback
    CK(r.upload(GL_ARRAY_BUFFER, data, 0, 4) == SIZE_MAX, "bytes0");
    CK(r.upload(GL_ARRAY_BUFFER, data, 4, 0) == SIZE_MAX, "align0");
    CK(r.upload(GL_ARRAY_BUFFER, data, SEG + 1, 4) == SIZE_MAX, "oversized");
  }

  // 4) wrap: seal once; busy fenced next -> SIZE_MAX without overwrite; signal -> reuse.
  {
    s_fences_made.clear(); s_fence_signaled.clear();
    NativePersistentRing r(64);          // 64-byte segments
    CK(r.Init(), "wrap init");
    uint8_t d[48]; memset(d, 1, sizeof d);
    size_t o0 = r.upload(GL_ARRAY_BUFFER, d, 48, 4);
    CK(o0 != SIZE_MAX && o0 < 64, "seg0 upload");
    size_t o1 = r.upload(GL_ARRAY_BUFFER, d, 48, 4); // wraps to seg1 (free)
    CK(o1 >= 64 && o1 < 128, "wrap to free seg1");
    CK(s_fences_made.size() == 1, "seal inserted exactly one fence");
    size_t o2 = r.upload(GL_ARRAY_BUFFER, d, 48, 4); // wraps to seg2 (free)
    CK(o2 >= 128 && o2 < 192, "wrap to free seg2");
    // now seg0 is fenced+busy: next wrap must fail WITHOUT overwriting
    CK(r.upload(GL_ARRAY_BUFFER, d, 48, 4) == SIZE_MAX, "fenced seg busy -> fallback");
    CK(s_fences_made.size() == 3, "all three segs sealed once each");
    CK(r.upload(GL_ARRAY_BUFFER, d, 48, 4) == SIZE_MAX, "still busy");
    CK(s_fences_made.size() == 3, "no duplicate seal");
    CK(std::memcmp(s_backing.data() + o0, d, 48) == 0, "no overwrite of in-flight bytes");
    // signal seg0's fence -> the next upload rotates into seg0 and reuses it
    s_fence_signaled[s_fences_made[0]] = 1;
    size_t o3 = r.upload(GL_ARRAY_BUFFER, d, 48, 4);
    CK(o3 != SIZE_MAX && o3 < 64, "seg0 reused after signal");
  }

  // 5) WAIT_FAILED disables the ring -> permanent fallback.
  {
    install_stub();
    s_fences_made.clear(); s_fence_signaled.clear();
    NativePersistentRing r(64);
    CK(r.Init(), "wf init");
    g_persistent_gl.ClientWaitSync =
        [](unsigned long long, unsigned int, unsigned long long) -> unsigned int {
            return GL_WAIT_FAILED;
        };
    uint8_t d[48]; memset(d, 2, sizeof d);
    r.upload(GL_ARRAY_BUFFER, d, 48, 4);  // seg0
    r.upload(GL_ARRAY_BUFFER, d, 48, 4);  // seg1
    r.upload(GL_ARRAY_BUFFER, d, 48, 4);  // seg2
    // wrap to fenced seg0 -> ClientWaitSync -> WAIT_FAILED -> disabled
    CK(r.upload(GL_ARRAY_BUFFER, d, 48, 4) == SIZE_MAX, "wait_failed fallback");
    CK(!r.ok(), "ring disabled after WAIT_FAILED");
  }

  // 6) null fence -> permanent disable, bytes untouched (defect 1).
  {
    install_stub();
    s_fences_made.clear();
    g_persistent_gl.FenceSync = [](unsigned int, unsigned int) -> unsigned long long {
      return 0; // driver refused the fence
    };
    NativePersistentRing r(64);
    CK(r.Init(), "nf init");
    uint8_t d[48]; memset(d, 7, sizeof d);
    CK(r.upload(GL_ARRAY_BUFFER, d, 48, 4) != SIZE_MAX, "nf seg0");
    CK(r.upload(GL_ARRAY_BUFFER, d, 48, 4) == SIZE_MAX, "null fence -> fallback");
    CK(!r.ok(), "null fence disables ring");
    CK(std::memcmp(s_backing.data(), d, 48) == 0, "in-flight bytes preserved");
    CK(r.upload(GL_ARRAY_BUFFER, d, 4, 4) == SIZE_MAX, "disabled stays disabled");
  }

  // 7) disabled ring still releases map+buffer; two rings -> ONE glFinish.
  {
    install_stub();
    s_finish_calls = 0;
    NativePersistentRing a(SEG), b(SEG);
    a.Init(); b.Init();
    g_persistent_gl.ClientWaitSync =
        [](unsigned long long, unsigned int, unsigned long long) -> unsigned int {
            return GL_WAIT_FAILED;
        };
    // force a disabled-but-live ring
    uint8_t d[2000]; memset(d, 3, sizeof d);
    NativePersistentRing c(64);
    c.Init();
    c.upload(GL_ARRAY_BUFFER, d, 48, 4);
    c.upload(GL_ARRAY_BUFFER, d, 48, 4);
    c.upload(GL_ARRAY_BUFFER, d, 48, 4);
    c.upload(GL_ARRAY_BUFFER, d, 48, 4); // wraps to fenced seg -> WAIT_FAILED
    // ReleaseAll pattern: one finish then each releases its own objects.
    const bool live = a.buffer() || b.buffer();
    a.Release(live);
    b.Release(false);
    c.Release(false);
    CK(s_finish_calls == 1, "two rings + disabled: one glFinish total");
    CK(a.buffer() == 0 && b.buffer() == 0 && c.buffer() == 0,
       "all buffers released incl disabled");
  }

  // Direct reservations share the upload cursor and never hand out a pointer
  // when the requested span cannot fit.
  {
    install_stub();
    NativePersistentRing r(64);
    CK(r.Init(), "reserve init");
    uint8_t input[7] = {1, 2, 3, 4, 5, 6, 7};
    CK(r.upload(GL_ARRAY_BUFFER, input, sizeof input, 4) == 0, "reserve prior upload");
    void* out = nullptr;
    const size_t offset = r.reserve(12, 16, &out);
    CK(offset == 16 && out == s_backing.data() + offset, "reserve aligned mapped range");
    if (out) std::memset(out, 0x5a, 12);
    CK(s_backing[16] == 0x5a && s_backing[27] == 0x5a, "reserve writes mapped bytes");
    out = reinterpret_cast<void*>(1);
    CK(r.reserve(65, 4, &out) == SIZE_MAX && !out, "reserve oversized fallback");
    CK(r.upload(GL_ARRAY_BUFFER, input, sizeof input, 4) == 28, "failed reserve preserves cursor");
    r.Release();
  }
  printf("%s (%d failures)\n", fails ? "FAIL" : "PASS", fails);
  return fails;
}
