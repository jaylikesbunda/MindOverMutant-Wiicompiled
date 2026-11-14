#pragma once



// NATIVE_MOM_GPU_VERTEX_V1: the switches for the vertex-shader transform.
//
// A qualified draw keeps its *object-space* position and normal in the batch and
// the generated vertex shader applies the XF banks the draw was parsed with
// (`NativeGpuXF`, binding point 1).  Everything the shader cannot reproduce with
// those banks stays on the CPU path, per draw - see the eligibility test in
// ``fifo_parser.cpp`` and the stage docstring.
//
// Two independent runtime switches, each read once per process:
//
//   NWII_NO_GPU_VERTEX=1  every draw takes today's CPU path, byte for byte
//   NWII_NO_GPU_TEXGEN=1  position/normal on the GPU, regular texgen on the CPU
//
// The diagnostics below read a decoded vertex as if it were view space (they
// print, archive or re-project position/normal/uv before the shader sees it), so
// they keep the whole pipeline on the CPU - the same idea as CotT's
// ``NativeGpuVertexEnabled``:
//
//   NWII_VTXTRACE            prints raw and view position per vertex
//   NWII_SKINDBG             reports per-vertex matrix rows during decode
//   NWII_DRAWLOG             prints cmd.vertices[].pos/tex from the draw ring
//   NWII_NORMAL_DRAW_PROBE   prints decoded normals per draw
//   NWII_DRAW_ARCHIVE        writes the batch and per-draw vertices to disk
//   NWII_EFB_PROBE(_AFTER)   archives the batch around a draw
//   NWII_EFB_PROBE_ARM_FILE  arms the two probes above
//   NWII_NOSHADER            there is no vertex shader to do the transform
//
// NWII_FLATCOLOR is a fragment-shader override and stays compatible.
#include <cstdlib>
#include "runtime/gx_command.h"
#include "runtime/gx/fifo_parser.h"

namespace nwii::runtime::gx {

inline bool NativeGpuVertexEnabled() {
  static const bool enabled = []() {
    // Experimental until a scene-matched GPU/CPU image comparison passes.
    const char* requested = std::getenv("NWII_GPU_VERTEX");
    if (!requested || requested[0] != '1' || requested[1] != '\0') return false;
    const char* blocked[] = {"NWII_NO_GPU_VERTEX", "NWII_NOSHADER",
                             "NWII_VTXTRACE", "NWII_SKINDBG", "NWII_DRAWLOG",
                             "NWII_NORMAL_DRAW_PROBE", "NWII_DRAW_ARCHIVE",
                             "NWII_EFB_PROBE", "NWII_EFB_PROBE_AFTER",
                             "NWII_EFB_PROBE_ARM_FILE"};
    for (const char* key : blocked)
      if (std::getenv(key)) return false;
    return true;
  }();
  return enabled;
}

// Regular texgen (ST/STQ from position, normal or a previous coordinate, plus the
// post matrix) moves to the vertex shader only for draws the transform already
// qualified for, and only when no generator chains another generator's output.
inline bool NativeGpuTexgenEnabled() {
  static const bool enabled = NativeGpuVertexEnabled() &&
                              std::getenv("NWII_NO_GPU_TEXGEN") == nullptr;
  return enabled;
}

}  // namespace nwii::runtime::gx

namespace nwii::runtime::gx {
inline void NativeGpuViewTransform(const DrawRaw& r, VertexData& vtx) {
  // One definition of the per-draw snapshot transform; the parser's
  // NativeApplySnapshotXf delegates here, so the CPU path, the CPU texgen
  // fallback and the vertex shader all describe the same arithmetic.
  if (!r.snap_pos_bank.empty() && vtx.has_pos) {
    const uint32_t pb = (uint32_t)vtx.posMtxIdx * 4;
    const uint32_t nb = 3u * (vtx.posMtxIdx & 31u);
    if (pb + 12 <= 256 && nb + 9 <= 96) {
      const float* m = &r.snap_pos_bank[pb];
      const float x = vtx.pos[0], y = vtx.pos[1], z = vtx.pos[2];
      vtx.pos[0] = m[0]*x + m[1]*y + m[2]*z + m[3];
      vtx.pos[1] = m[4]*x + m[5]*y + m[6]*z + m[7];
      vtx.pos[2] = m[8]*x + m[9]*y + m[10]*z + m[11];
      if (vtx.has_norm && !r.snap_nrm_bank.empty()) {
        const float* n = &r.snap_nrm_bank[nb];
        const float a = vtx.norm[0], b = vtx.norm[1], cc = vtx.norm[2];
        vtx.norm[0] = n[0]*a + n[1]*b + n[2]*cc;
        vtx.norm[1] = n[3]*a + n[4]*b + n[5]*cc;
        vtx.norm[2] = n[6]*a + n[7]*b + n[8]*cc;
      }
      vtx.pre_xf = true;
    }
  } else if (r.have_snap_mtx && vtx.has_pos) {
    const float* m = r.snap_pos_mtx;
    const float x = vtx.pos[0], y = vtx.pos[1], z = vtx.pos[2];
    vtx.pos[0] = m[0]*x + m[1]*y + m[2]*z + m[3];
    vtx.pos[1] = m[4]*x + m[5]*y + m[6]*z + m[7];
    vtx.pos[2] = m[8]*x + m[9]*y + m[10]*z + m[11];
    if (vtx.has_norm) {
      const float* n = r.snap_nrm_mtx;
      const float a = vtx.norm[0], b = vtx.norm[1], cc = vtx.norm[2];
      vtx.norm[0] = n[0]*a + n[1]*b + n[2]*cc;
      vtx.norm[1] = n[3]*a + n[4]*b + n[5]*cc;
      vtx.norm[2] = n[6]*a + n[7]*b + n[8]*cc;
    }
    vtx.pre_xf = true;
  }
}
}  // namespace nwii::runtime::gx
