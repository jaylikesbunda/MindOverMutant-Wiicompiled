// NativePackBatch qualification + field-exactness tests for the compact
// post-decode upload layout (NATIVE_MOM_PACKED_VERTICES_V1).
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cmath>
#include <bit>
#include <vector>
#include "runtime/native_packed_vertices_mom.h"

// Minimal dummy matching the fields NativePackBatch reads on a BatchVertex.
struct DummyVertex {
  float x, y, z;
  float r, g, b, a;
  float u, v;
  float nx, ny, nz;
  float color1[4];
  float texgen[8][3];
};

static int fails = 0;
#define CK(cond, name) do { if (!(cond)) { ++fails; \
    printf("FAIL %s\n", name); } } while (0)

static DummyVertex mk(float c0) {
  DummyVertex d{};
  d.x = 1.5f; d.y = -2.25f; d.z = 3.75f;
  d.r = c0; d.g = 0.0f; d.b = 1.0f; d.a = 1.0f;
  d.u = 0.5f; d.v = -0.25f;
  d.nx = 0.0f; d.ny = 1.0f; d.nz = -1.0f;
  d.color1[0] = 1.0f; d.color1[1] = 0.0f;
  d.color1[2] = 0.0f; d.color1[3] = 1.0f;
  for (int t = 0; t < 8; ++t)
    for (int k = 0; k < 3; ++k) d.texgen[t][k] = float(t * 3 + k) * 0.25f;
  return d;
}

int main() {
  // Layout asserts (compile-time too, but print them for the log).
  CK(sizeof(NativePackedBatchVertex) == 136, "size136");
  CK(offsetof(NativePackedBatchVertex, color0) == 12, "off_color0");
  CK(offsetof(NativePackedBatchVertex, uv) == 16, "off_uv");
  CK(offsetof(NativePackedBatchVertex, normal) == 24, "off_normal");
  CK(offsetof(NativePackedBatchVertex, color1) == 36, "off_color1");
  CK(offsetof(NativePackedBatchVertex, texgen) == 40, "off_texgen");

  // All 256 byte values round-trip exactly through float(b)/255.
  for (int b = 0; b < 256; ++b) {
    const float c = float(b) / 255.f;
    uint8_t out = 0xFF;
    if (!NativePackChannel(c, out) || out != b) {
      ++fails; printf("FAIL channel %d -> %d\n", b, out);
      break;
    }
  }

    {
    std::vector<DummyVertex> in{mk(0.5f), mk(0.25f)};
    std::vector<NativePackedBatchVertex> out;
        CK(!NativePackBatch(in, out), "half-byte colour falls back");
    CK(out.empty(), "fallback leaves output empty");
  }

  // -0.0, NaN, Inf, out-of-range channels all force fallback.
  for (float bad : {-0.0f, 0.0f/0.0f, 1.0f/0.0f, -1.0f/0.0f, 1.5f, -0.2f,
                    3.4e38f}) { // huge finite: must reject before c*255
    uint8_t out = 0;
    CK(!NativePackChannel(bad, out), "channel reject");
  }
  { // one bad vertex in a mixed batch fails the WHOLE batch
    std::vector<DummyVertex> in{mk(1.0f), mk(0.25f), mk(0.0f)};
    std::vector<NativePackedBatchVertex> out;
    CK(!NativePackBatch(in, out) && out.empty(), "partial bad batch falls back");
  }
  {
    std::vector<DummyVertex> in{mk(-0.0f)};
    std::vector<NativePackedBatchVertex> out;
    CK(!NativePackBatch(in, out), "negative zero colour falls back");
  }

  // Full qualified batch: fields copied bit-exactly, colours quantized.
  {
    std::vector<DummyVertex> in;
    DummyVertex a = mk(0.0f), b = mk(1.0f);
    b.u = std::bit_cast<float>(uint32_t(0x7fc00000));  // NaN uv - must memcpy
    b.v = std::bit_cast<float>(uint32_t(0x80000000));  // -0.0
    b.nx = std::bit_cast<float>(uint32_t(0xdeadbeef)); // arbitrary pattern
    b.texgen[3][1] = 61.0f;                            // matrix index in texgen
    in = {a, b};
    std::vector<NativePackedBatchVertex> out;
    CK(NativePackBatch(in, out), "qualified batch packs");
    CK(out.size() == 2, "out size");
    if (out.size() == 2) {
      CK(std::memcmp(out[1].position, &b.x, 12) == 0, "position bitexact");
      CK(std::memcmp(out[1].uv, &b.u, 8) == 0, "uv bitexact (NaN,-0)");
      CK(std::memcmp(out[1].normal, &b.nx, 12) == 0, "normal bitexact");
      CK(std::memcmp(out[1].texgen, b.texgen, sizeof(b.texgen)) == 0,
         "texgen bitexact incl matrix idx");
      CK(out[0].color0[0] == 0 && out[1].color0[0] == 255, "color0 bytes");
      CK(out[0].color1[3] == 255 && out[1].color1[0] == 255, "color1 bytes");
    }
  }

  // Byte-representable fractions: 51/255 == 0.2f exactly in float arithmetic
  // (both round to the same nearest float), so 0.2f legitimately packs to 51.
  // A genuinely non-representable value like 0.25f (63.75/255) must reject.
  {
    const float c = 51.f / 255.f;
    uint8_t o = 0;
    CK(NativePackChannel(c, o) && o == 51, "51/255 packs");
    CK(NativePackChannel(0.2f, o) && o == 51, "0.2f packs to 51 (byte-exact)");
    CK(!NativePackChannel(0.25f, o), "0.25f rejected (63.75/255)");
    CK(!NativePackChannel(0.3f, o), "0.3f rejected when not byte-exact");
  }

  // Zero vertices qualifies trivially.
  {
    std::vector<DummyVertex> in;
    std::vector<NativePackedBatchVertex> out;
    CK(NativePackBatch(in, out) && out.empty(), "zero vertices");
  }

  // Alternating qualification: batch 1 packs, batch 2 falls back.
  {
    std::vector<DummyVertex> good{mk(1.0f)}, badv{mk(0.5f)};
    std::vector<NativePackedBatchVertex> out;
    CK(NativePackBatch(good, out), "pack batch");
    CK(!NativePackBatch(badv, out) && out.empty(), "fallback batch");
  }

  printf("%s (%d failures)\n", fails ? "FAIL" : "PASS", fails);
  return fails;
}
