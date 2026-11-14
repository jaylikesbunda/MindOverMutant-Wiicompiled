// NATIVE_MOM_PACKED_VERTICES_V1: compact POST-decode upload layout for FlushBatch.
//
// This is a repack, not a direct packed decoder: the parser/ConvertVertex still
// produces the 160-byte float BatchVertex rows; NativePackBatch rewrites them for
// the upload when every colour channel round-trips through a byte exactly, and
// FlushBatch selects between the packed buffer (stride 136) and the original
// s_batch (stride 160) per draw batch.  Opt-in: NWII_PACKED_VERTICES=1, and
// NWII_NO_PACKED_VERTICES always wins.
//
// The two colour quads become normalised RGBA8.  A channel only packs when
// float(byte)/255 compares bit-equal to the source float: exact 0.0/1.0 and the
// byte-representable fractions pass; NaN, Inf, out-of-range, -0.0 and any value
// sitting between byte steps fails the WHOLE batch back to the float layout, so
// no draw ever gets a silently shifted colour.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cmath>
#include <vector>

struct NativePackedBatchVertex {
  float position[3];   // 0
  uint8_t color0[4];   // 12
  float uv[2];         // 16
  float normal[3];     // 24
  uint8_t color1[4];   // 36
  float texgen[8][3];  // 40
  NativePackedBatchVertex() {}
};
static_assert(sizeof(NativePackedBatchVertex) == 136,
              "packed vertex stride changed");
static_assert(offsetof(NativePackedBatchVertex, color0) == 12 &&
              offsetof(NativePackedBatchVertex, uv) == 16 &&
              offsetof(NativePackedBatchVertex, normal) == 24 &&
              offsetof(NativePackedBatchVertex, color1) == 36 &&
              offsetof(NativePackedBatchVertex, texgen) == 40,
              "packed vertex offsets changed");

// One channel: byte-representable iff lround(c*255)/255 is bit-identical to c.
// Rejects NaN/Inf (isfinite), out-of-range, -0.0 (roundtrip gives +0.0, bits
// differ) and any non-byte-aligned float.
inline bool NativePackChannel(float c, uint8_t& out) {
  if (!std::isfinite(c)) return false;
  if (c < 0.f || c > 1.f) return false; // reject before c*255 can overflow
  const long v = std::lround(c * 255.f);
  if (v < 0 || v > 255) return false;
  const uint8_t b = uint8_t(v);
  const float rt = float(b) / 255.f;
  if (std::memcmp(&rt, &c, 4) != 0) return false;
  out = b;
  return true;
}

// Repack `input` into `out`.  Returns false and clears `out` if any vertex has a
// non-representable colour - the caller then uploads the float batch instead.
// The generic-vertex fields (position, uv, normal, texgen incl. matrix indices,
// STQ and NaN/-0 bit patterns) are copied verbatim.
template<class Vertex>
inline bool NativePackBatch(const std::vector<Vertex>& input,
                            std::vector<NativePackedBatchVertex>& out) {
  out.clear();
  out.resize(input.size()); // every field is assigned below
  for (size_t i = 0; i < input.size(); ++i) {
    const Vertex& s = input[i];
    NativePackedBatchVertex& d = out[i];
    const float c0[4] = {s.r, s.g, s.b, s.a};
    for (int k = 0; k < 4; ++k)
      if (!NativePackChannel(c0[k], d.color0[k])) { out.clear(); return false; }
    for (int k = 0; k < 4; ++k)
      if (!NativePackChannel(s.color1[k], d.color1[k])) { out.clear(); return false; }
    std::memcpy(d.position, &s.x, 12);
    std::memcpy(d.uv, &s.u, 8);
    std::memcpy(d.normal, &s.nx, 12);
    std::memcpy(d.texgen, s.texgen, sizeof(d.texgen));
  }
  return true;
}
