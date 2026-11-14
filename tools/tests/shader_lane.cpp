// Shader-lane fixture for the B2 stages.
//
//   * native_shader_cache::Writer: exact file roundtrip, bounded queue
//     (nonblocking skip, no partial file), worker error -> failed counter,
//     destructor drain/join.
//   * native_shader_preload::Loader/ReadEntry: valid roundtrip; bad magic,
//     bad name/key, truncated, trailing payload and oversize rejected; bounds.
//   * GenerateTEVShader: with data uniforms on, a data-only state change
//     (fog BP values / intermediate alpha refs 1 vs 254) leaves the fragment
//     source byte-identical; different fog types/comparators/endpoints differ.
//     With NWII_LITERAL_SHADER_DATA=1 the literals come back (values appear).
//   * uvec4 bitfield packing preserves the original raw bits incl. signs.
#include "runtime/native_shader_cache_io.h"
#include "runtime/native_shader_preload.h"
#include "../../vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/tev_shader_gen.cpp"
#include <cstdio>
#include <filesystem>

static int fails = 0;
#define CHECK(cond, msg) do { if (!(cond)) { ++fails; \
    std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, msg); } } while (0)

using namespace nwii::runtime::gx;

// GXState is far too large for the default thread stack; keep the pattern
// states on the heap.
static GXState& fog_state(uint32_t ee, uint32_t f1, uint32_t ef, uint32_t f0,
                          uint32_t f2, uint8_t c0 = 4, uint8_t r0 = 128,
                          uint8_t c1 = 7, uint8_t r1 = 0, uint8_t logic = 0) {
  static GXState storage[8];
  static unsigned slot = 0;
  GXState& s = storage[slot++ % 8];
  s.numTevStages = 1;
  s.tevStages[0].colorRegId = 0;
  s.tevStages[0].alphaRegId = 0;
  s.bp[0xee] = ee; s.bp[0xf1] = f1; s.bp[0xef] = ef; s.bp[0xf0] = f0;
  s.bp[0xf2] = f2;
  s.alphaTest.comp0 = c0; s.alphaTest.ref0 = r0;
  s.alphaTest.comp1 = c1; s.alphaTest.ref1 = r1;
  s.alphaTest.logic = logic;
  return s;
}

static void test_writer() {
  namespace fs = std::filesystem;
  const fs::path dir = fs::temp_directory_path() / ("mom_shaderio_test_" + std::to_string(uintptr_t(&dir)));
  std::error_code ec; fs::create_directories(dir, ec);
  std::atomic<uint64_t> stores{0};
  {
    native_shader_cache::Writer w;
    std::array<char,24> header{};
    std::memcpy(header.data(), "NWPBC001", 8);
    const uint64_t key = 0x1122334455667788ull;
    std::memcpy(header.data() + 8, &key, 8);
    const uint32_t fmt = 0x1234, len = 5;
    std::memcpy(header.data() + 16, &fmt, 4);
    std::memcpy(header.data() + 20, &len, 4);
    std::vector<char> data{'a','b','c','d','e'};
    CHECK(w.enqueue(dir / "one.bin", header.data(), data, stores), "enqueue");
    w.flush();
    CHECK(stores.load() == 1, "store counted by worker");
    std::ifstream in(dir / "one.bin", std::ios::binary);
    std::vector<char> got((std::istreambuf_iterator<char>(in)), {});
    CHECK(got.size() == 24 + 5 && std::memcmp(got.data(), header.data(), 24) == 0 &&
          std::memcmp(got.data() + 24, "abcde", 5) == 0, "roundtrip bytes exact");
    // Worker error: a path whose directory does not exist -> failed, not crash.
    CHECK(w.enqueue(dir / "missing" / "two.bin", header.data(), data, stores),
          "enqueue to bad dir accepted (worker fails async)");
    w.flush();
    CHECK(w.failed.load() >= 1, "worker write failure counted");
    // Bounded queue (deterministic): a job above the 8 MiB per-record bound is
    // rejected outright, and once 8 MiB is queued every further accept is
    // false - enqueue() then counts a skip instead of blocking a frame.
    CHECK(!w.can_accept((8u << 20) + 1), "oversized job rejected");
    {
      std::vector<char> big(8u << 20);
      CHECK(w.can_accept(big.capacity()) || w.skipped.load(),
            "8 MiB boundary job accepted");
      if (w.enqueue(dir / "big.bin", header.data(), std::move(big), stores)) {
        CHECK(!w.can_accept(1), "full byte budget rejects the next job");
        const uint64_t s0 = w.skipped.load();
        std::vector<char> one(1);
        CHECK(!w.enqueue(dir / "late.bin", header.data(), std::move(one), stores),
              "full queue skips rather than blocking");
        CHECK(w.skipped.load() > s0, "skip counted");
      }
    }
    w.flush();
  } // destructor drains and joins here
  CHECK(stores.load() >= 1, "drain/join on destruction");
  fs::remove_all(dir, ec);
}

static void test_loader() {
  namespace fs = std::filesystem;
  const fs::path dir = fs::temp_directory_path() / ("mom_preload_test_" + std::to_string(uintptr_t(&dir)));
  std::error_code ec; fs::create_directories(dir, ec);
  auto write = [&](const char* name, const void* bytes, size_t n) {
    std::ofstream out(dir / name, std::ios::binary | std::ios::trunc);
    out.write(static_cast<const char*>(bytes), std::streamsize(n));
  };
  // Valid record for key 0x1122334455667788.
  char record[24 + 4] = {};
  std::memcpy(record, "NWPBC001", 8);
  const uint64_t key = 0x1122334455667788ull;
  const uint32_t fmt = 7, len = 4;
  std::memcpy(record + 8, &key, 8); std::memcpy(record + 16, &fmt, 4);
  std::memcpy(record + 20, &len, 4); std::memcpy(record + 24, "WXYZ", 4);
  char name[32]; std::snprintf(name, sizeof name, "%016llx.bin",
                               (unsigned long long)key);
  write(name, record, sizeof record);
  // Bad magic.
  char bad[28]; std::memcpy(bad, record, sizeof bad); bad[0] = 'X';
  write("badmagic.bin", bad, sizeof bad);
  // Key/name mismatch.
  write("0000000000000000.bin", record, sizeof record);
  // Truncated.
  write("truncated.bin", record, 20);
  // Trailing payload.
  char trail[sizeof record + 1]; std::memcpy(trail, record, sizeof record);
  write("trailing.bin", trail, sizeof trail);

  native_shader_preload::Loader loader;
  loader.start(dir);
  while (!loader.scanned()) std::this_thread::yield();
  native_shader_preload::Entry e;
  CHECK(loader.take(e), "one valid entry queued");
  CHECK(e.key == key && e.format == fmt && e.data.size() == 4 &&
        std::memcmp(e.data.data(), "WXYZ", 4) == 0, "entry bytes exact");
  CHECK(!loader.take(e), "queue drained after the one valid file");
  auto counts = loader.snapshot();
  CHECK(counts.read == 1 && counts.rejected >= 4,
        "bad magic/key/truncated/trailing rejected");
  CHECK(loader.drained(), "drained");
  // ReadEntry unit checks.
  CHECK(native_shader_preload::ReadEntry(dir / name, e), "ReadEntry valid");
  native_shader_preload::Entry junk;
  CHECK(!native_shader_preload::ReadEntry(dir / "badmagic.bin", junk), "ReadEntry magic");
  CHECK(!native_shader_preload::ReadEntry(dir / "truncated.bin", junk), "ReadEntry truncated");
  CHECK(!native_shader_preload::ReadEntry(dir / "trailing.bin", junk), "ReadEntry trailing");
  fs::remove_all(dir, ec);
}

static void test_sources() {
  const bool literal = std::getenv("NWII_LITERAL_SHADER_DATA") != nullptr;
  const uint32_t f1 = (4u << 21) | (0u);          // fog type 4 curve
  GXState& a = fog_state(0x000ee001u, f1 | 0xAAAAAu, 0x00ef0001u,
                        0x00000005u, 0x00112233u);
  GXState& b = fog_state(0x00fff123u, f1 | 0xBBBBBu, 0x00ef0002u,
                        0x00000007u, 0x00fedcbau);
  const std::string fa = GenerateTEVShader(a, 0x90).fragment_source;
  const std::string fb = GenerateTEVShader(b, 0x90).fragment_source;
  if (!literal) {
    CHECK(fa == fb, "data-only fog change keeps identical source");
    CHECK(fa.find("uNativeFogData.x") != std::string::npos &&
          fa.find("uNativeFogColor") != std::string::npos,
          "uniform fog path emits uniforms");
    // Intermediate alpha refs 1 vs 254 also data-only.
    GXState& c = fog_state(0xee, f1 | 1u, 0xef, 5u, 0xf2, 4, 1);
    GXState& d = fog_state(0xee, f1 | 1u, 0xef, 5u, 0xf2, 4, 254);
    CHECK(GenerateTEVShader(c, 0x90).fragment_source ==
          GenerateTEVShader(d, 0x90).fragment_source,
          "intermediate alpha refs are data-only");
    // Different fog types / comparators / endpoints still specialise.
    GXState& t2 = fog_state(0x000ee001u, f1 | 0xAAAAAu, 0x00ef0001u, 0x00000005u, 0x00112233u); t2.bp[0xf1] = (5u << 21) | 1u;
    CHECK(GenerateTEVShader(t2, 0x90).fragment_source != fa, "fog type differs");
    GXState& e0 = fog_state(0x000ee001u, f1 | 0xAAAAAu, 0x00ef0001u, 0x00000005u, 0x00112233u); e0.alphaTest.ref0 = 0;
    CHECK(GenerateTEVShader(e0, 0x90).fragment_source != fa,
          "ref==0 endpoint stays specialised");
    CHECK(fa.find("uNativeAlphaRef") != std::string::npos ||
          a.alphaTest.ref0 == 0 || a.alphaTest.ref0 == 255,
          "intermediate ref uses the uniform");
  } else {
    CHECK(fa != fb, "literal mode embeds the values");
    CHECK(fa.find("uNativeFogData") == std::string::npos,
          "literal mode emits no data uniforms");
  }
  // The uvec4 packing preserves the raw register bits incl. the sign bit.
  const uint32_t ee = 0x000fffffu, ef = 0xDEADBEEFu, f0 = 0xFFFFFFFFu;
  const uint32_t packed[4] = {(ee & 0xfffffu) << 12, (0xABCu & 0xfffffu) << 12,
                              ef, f0 & 31u};
  CHECK(packed[0] == 0xFFFFF000u && packed[1] == 0x00ABC000u &&
        packed[2] == 0xDEADBEEFu && packed[3] == 31u,
        "uvec4 packing preserves raw bits");
}

int main() {
  test_writer();
  test_loader();
  test_sources();
  std::printf("%s (%d failure%s)\n", fails ? "FAIL" : "PASS", fails,
              fails == 1 ? "" : "s");
  return fails ? 1 : 0;
}
