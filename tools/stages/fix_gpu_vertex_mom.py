"""Move the qualified snapshot position/normal transform (and regular texgen) to the vertex shader.

CotT's ``native-port/tools/fix_gpu_vertex_pipeline.py`` uploads the per-draw XF
matrix banks as a uniform block, carries a per-vertex matrix index in the batch
vertex and transforms position/normal/texgen in the generated vertex shader
(``NativeGpuXfBlock`` / ``NativeUploadGpuXf`` / ``gxPosition`` / ``gxNrm`` /
``gxTexgen``).  This stage is the same mechanism, re-derived against MoM's
structures:

* MoM already freezes the matrix at **parse** time and decodes with it
  (``DrawRaw::snap_pos_bank`` / ``snap_nrm_bank`` / ``snap_pos_mtx`` /
  ``snap_nrm_mtx``, ``fifo_parser.cpp``), so the banks to upload are the draw's
  own snapshot, not the render-time ``g_state`` bank.  That is what makes the
  result identical: the CPU path and the shader consume the same bytes.
* The batch vertex keeps its 160-byte layout (the draw archive asserts it).
  ``bv.u``/``bv.v`` - GLSL location 2, ``vertexTexCoord``, which the generated
  vertex shader declares and never reads - carry the matrix index; the stage
  asserts that it is still unreferenced before repurposing it.
* ``NativeWriteConvertedVertex`` stays the only writer; it gains a GPU branch
  that writes every field, so ``NATIVE_MOM_VERTEX_PASS_V1``'s no-zero-fill
  writer stays total.
* The shader key, the source dedupe, the program store and the lane pruning are
  untouched: the new GLSL is emitted for every program (it is inert while the
  control uniform is zero), so no draw needs a new program variant.

Eligibility is per draw and decided by the decoder, which is the only place that
knows the snapshot and the per-vertex matrix indices:

  GPU:      triangle / quad / strip / fan draws whose DrawRaw carries a frozen
            position **and** normal bank (or a single frozen matrix pair), the
            VAT declares positions, and every vertex's matrix index is inside the
            uploaded banks.  Such a draw keeps object-space attributes and the
            shader applies exactly ``NativeApplySnapshotXf``'s arithmetic.
  CPU:      everything else, unchanged: no snapshot (the renderer's live matrix
            is used, which is not this draw's), a position bank without a normal
            bank, a vertex with no position, an out-of-range matrix index, lines
            and points (expanded on the CPU from view-space positions), and any
            run with one of the diagnostic env vars set.

Regular texgen (phase 2) additionally requires that no generator reads another
generator's coordinate (XF texinfo source 5..12, i.e. TEXCOORDn chaining): the
shader is handed each lane's CPU-side texgen attribute, which is the *raw*
coordinate, while the CPU definition chains the *generated* one.  All other
sources (position, normal, colour, none) are reproduced exactly, including the
no-matrix index 60, the row-count rule and the post matrix.

Switches: ``NWII_NO_GPU_VERTEX=1`` (everything off, per draw) and
``NWII_NO_GPU_TEXGEN=1`` (phase 2 off).  ``NWII_NO_TEXGEN_BOUND``,
``NWII_NO_VERTEX_UNINIT``, ``NWII_NO_STREAM_UPLOAD``, ``NWII_NO_BIND_SHADOW``
and ``NWII_NO_UNIFORM_CACHE`` keep their meaning.

Idempotent on the marker ``NATIVE_MOM_GPU_VERTEX_V1``.
"""
import argparse
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=port)
parser.add_argument('--restore', action='store_true')
args = parser.parse_args()
port = args.root
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
overlay = port / 'overlays/native_gpu_vertex_mom.h'
MARK = 'NATIVE_MOM_GPU_VERTEX_V1'

HEADER = '''#pragma once



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
    if (!requested || requested[0] != '1' || requested[1] != '\\0') return false;
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
'''

SOURCES = ['include/runtime/gx_command.h',
           'src/hle/gx/fifo_parser.cpp',
           'src/hle/gx/renderer_gl.cpp',
           'src/hle/gx/tev_shader_gen.cpp']
texts = {relative: (runtime / relative).read_text() for relative in SOURCES}
staged = [relative for relative, text in texts.items() if MARK in text]
RESTORE = args.restore
if not RESTORE:
    overlay.write_text(HEADER, newline='\n')
    (runtime / 'include/runtime/native_gpu_vertex_mom.h').write_text(HEADER, newline='\n')
if RESTORE:
    if not staged:
        print('gpu vertex pipeline not staged; nothing to restore.')
        raise SystemExit(0)
    assert len(staged) == len(SOURCES),         'partially staged tree: ' + ', '.join(staged)
else:
    if len(staged) == len(SOURCES):
        for relative, text in texts.items():
            updated = '\n'.join(
                '#include "runtime/native_gpu_vertex_mom.h"'
                if 'overlays/native_gpu_vertex_mom.h' in line else line
                for line in text.split('\n'))
            if updated != text:
                (runtime / relative).write_text(updated, newline='\n')
        print('gpu vertex pipeline already staged.')
        raise SystemExit(0)
    assert not staged, 'partially staged tree: ' + ', '.join(staged)
# Attribute 2 (uv, bv.u/bv.v) is used for the per-vertex matrix index only because
# the generated vertex shader declares `vertexTexCoord` and never reads it: that is
# what lets the batch keep its 160-byte layout (the draw archive asserts the size).
# Assert it here, before repurposing it, so a future generator change breaks this
# stage instead of silently moving the vertex.
if not RESTORE:
    assert texts['src/hle/gx/tev_shader_gen.cpp'].count('vertexTexCoord') == 1, \
        'location 2 (vertexTexCoord) is read by the generated shader - bv.u cannot carry the index'


if RESTORE:
    for rel in list(texts):
        texts[rel] = '\n'.join(
            ln for ln in texts[rel].split('\n')
            if 'overlays/native_gpu_vertex_mom.h' not in ln
            and '#include "runtime/native_gpu_vertex_mom.h"' not in ln)


def sub(relative, old, new, what):
    if RESTORE:
        if '#include "runtime/native_gpu_vertex_mom.h"' in new:
            return  # the include line was already stripped above
        old, new = new, old
    text = texts[relative]
    assert text.count(old) == 1, 'anchor %s (%d matches)' % (what, text.count(old))
    texts[relative] = text.replace(old, new, 1)


if not RESTORE and (not overlay.exists() or overlay.read_text() != HEADER):
    # newline='\n': keep the file byte-identical to HEADER on every run, so a
    # second run writes nothing (the runner's own LF normalisation is a no-op).
    overlay.write_text(HEADER, newline='\n')
include = '#include "runtime/native_gpu_vertex_mom.h"\n'

# --- 1. the draw carries the snapshot the shader indexes ---------------------
sub('include/runtime/gx_command.h',
    '    std::shared_ptr<DrawRaw> raw;\n',
    '''    std::shared_ptr<DrawRaw> raw;
    // ''' + MARK + ''': set by the decoder when this draw's position/normal transform
    // is the vertex shader's job. Holds the parse-time XF banks the shader
    // indexes, so they outlive the decoded vertex bytes (c.raw is freed once the
    // draw is decoded). Null means the draw took the CPU path.
    //   snap_pos_bank.size() == 256 -> per-vertex bank (each vertex indexes it)
    //   otherwise                   -> snap_pos_mtx/snap_nrm_mtx for every vertex
    std::shared_ptr<DrawRaw> native_gpu_snapshot;
''',
    'GXCommand::native_gpu_snapshot')

# --- 2. decode: one definition of the snapshot transform ---------------------
sub('src/hle/gx/fifo_parser.cpp',
    '#include "runtime/gx/fifo_parser.h"\n',
    '#include "runtime/gx/fifo_parser.h"\n' + include,
    'fifo_parser include')

sub('src/hle/gx/fifo_parser.cpp',
    'template<bool Direct> static void NativeDecodeDraw(GXCommand& c) {\n',
    '''// ''' + MARK + ''': true when this draw's position/normal transform is the vertex
// shader's job. The conditions are exactly the ones NativeApplySnapshotXf needs,
// so a qualified draw is one the shader can reproduce:
//   * the snapshot exists - a bank frozen at parse time, or the single matrix
//     pair. A draw with no snapshot is transformed by NativeWriteConvertedVertex
//     with the renderer's live bank, which is not this draw's matrix, so it stays
//     on the CPU;
//   * a banked draw has both banks (a position bank without a normal bank leaves
//     normals in object space on the CPU, which the shader cannot express);
//   * the VAT declares positions (a vertex without one is left in object space,
//     and a zero vector through a matrix is the translation column, not zero).
// The per-vertex matrix index is checked while decoding; one out-of-range index
// sends the whole draw to the CPU fix-up.
// NWII_NO_GPU_VERTEX=1 and the diagnostics that read decoded vertices disable it.
static bool NativeGpuVertexEligible(const DrawRaw& r) {
    if (!NativeGpuVertexEnabled()) return false;
    if (r.vat.posMask == VtxAttrMask::None) return false;
    if (r.snap_pos_bank.empty()) return r.have_snap_mtx;
    return r.snap_pos_bank.size() == 256 && r.snap_nrm_bank.size() == 96;
}

// ''' + MARK + ''': the per-draw snapshot transform of a decoded vertex, in one
// place, so the CPU path, the CPU fallback and the vertex shader all describe the
// same arithmetic. The shader reproduces this with the banks uploaded from the
// same DrawRaw (NativeUploadGpuXf / gxPosition / gxNrm in renderer_gl.cpp and
// tev_shader_gen.cpp).
static void NativeApplySnapshotXf(VertexData& vtx, const DrawRaw& r) {
    NativeGpuViewTransform(r, vtx); // shared definition, native_gpu_vertex_mom.h
}

template<bool Direct> static void NativeDecodeDraw(GXCommand& c) {
''',
    'NativeDecodeDraw prologue')

sub('src/hle/gx/fifo_parser.cpp',
    '    c.vertices.reserve(r.count);\n',
    '''    // ''' + MARK + ''': triangles and quads only. Lines and points are expanded on
    // the CPU from view-space positions (NativeEmitLinesPoints), and the shader
    // path never sees them.
    const bool gpu_triangles = c.prim_type == 0x80 || c.prim_type == 0x90 ||
                               c.prim_type == 0x98 || c.prim_type == 0xA0;
    const bool gpu_vertex = gpu_triangles && NativeGpuVertexEligible(r);
    bool gpu_valid = true;
    c.vertices.reserve(r.count);
''',
    'decode eligibility')

sub('src/hle/gx/fifo_parser.cpp',
    '''        // Transform by the per-draw matrix snapshot (GX 3x4). Doing it here,
        // with the matrix captured at this draw's stream position, fixes the
        // matrix-swap skinning scramble where a live render-time lookup would
        // use whatever matrix was written last.
        if (!r.snap_pos_bank.empty() && vtx.has_pos) {
            const uint32_t pb = (uint32_t)vtx.posMtxIdx * 4;
            const uint32_t nb = 3u * (vtx.posMtxIdx & 31);
            if (pb + 12 <= 256 && nb + 9 <= 96) {
                const float* m = &r.snap_pos_bank[pb];
                float x = vtx.pos[0], y = vtx.pos[1], z = vtx.pos[2];
                vtx.pos[0] = m[0]*x + m[1]*y + m[2]*z + m[3];
                vtx.pos[1] = m[4]*x + m[5]*y + m[6]*z + m[7];
                vtx.pos[2] = m[8]*x + m[9]*y + m[10]*z + m[11];
                if (vtx.has_norm && !r.snap_nrm_bank.empty()) {
                    const float* n = &r.snap_nrm_bank[nb];
                    float a = vtx.norm[0], b = vtx.norm[1], cc = vtx.norm[2];
                    vtx.norm[0] = n[0]*a + n[1]*b + n[2]*cc;
                    vtx.norm[1] = n[3]*a + n[4]*b + n[5]*cc;
                    vtx.norm[2] = n[6]*a + n[7]*b + n[8]*cc;
                }
                vtx.pre_xf = true;
            }
        } else if (r.have_snap_mtx && vtx.has_pos) {
            const float* m = r.snap_pos_mtx;
            float x = vtx.pos[0], y = vtx.pos[1], z = vtx.pos[2];
            vtx.pos[0] = m[0]*x + m[1]*y + m[2]*z + m[3];
            vtx.pos[1] = m[4]*x + m[5]*y + m[6]*z + m[7];
            vtx.pos[2] = m[8]*x + m[9]*y + m[10]*z + m[11];
            if (vtx.has_norm) {
                const float* n = r.snap_nrm_mtx;
                float a = vtx.norm[0], b = vtx.norm[1], cc = vtx.norm[2];
                vtx.norm[0] = n[0]*a + n[1]*b + n[2]*cc;
                vtx.norm[1] = n[3]*a + n[4]*b + n[5]*cc;
                vtx.norm[2] = n[6]*a + n[7]*b + n[8]*cc;
            }
            vtx.pre_xf = true;
        }
''',
    '''        // Transform by the per-draw matrix snapshot (GX 3x4). Doing it here,
        // with the matrix captured at this draw's stream position, fixes the
        // matrix-swap skinning scramble where a live render-time lookup would
        // use whatever matrix was written last.
        // ''' + MARK + ''': a qualified draw leaves its attributes in object space
        // for the vertex shader; anything the shader cannot reproduce with the
        // uploaded banks marks the draw for the CPU fix-up below.
        if (!gpu_vertex) {
            NativeApplySnapshotXf(vtx, r);
        } else if (!vtx.has_pos ||
                   (!r.snap_pos_bank.empty() &&
                    ((uint32_t)vtx.posMtxIdx * 4u + 12u > 256u ||
                     3u * (vtx.posMtxIdx & 31u) + 9u > 96u))) {
            gpu_valid = false;
        }
''',
    'decode snapshot transform')

sub('src/hle/gx/fifo_parser.cpp',
    '  // NATIVE_NORMAL_DRAW_PROBE_BEGIN\n',
    '''  // ''' + MARK + ''': hand the draw to the vertex shader with the banks it indexes,
  // or run the CPU definition of the same transform over every vertex. The
  // fix-up applies the identical per-vertex condition, so a mixed draw ends up
  // exactly where the CPU path would have put it.
  if (gpu_vertex) {
    if (gpu_valid) c.native_gpu_snapshot = c.raw;
    else for (auto& vtx : c.vertices) NativeApplySnapshotXf(vtx, r);
  }
  // NATIVE_NORMAL_DRAW_PROBE_BEGIN
''',
    'decode commit')

# --- 3. renderer: batch-wide vertex mode, bank upload, GPU vertex writer -----
sub('src/hle/gx/renderer_gl.cpp',
    '#include "runtime/gx/tev_shader_gen.h"\n',
    '#include "runtime/gx/tev_shader_gen.h"\n' + include,
    'renderer include')

sub('src/hle/gx/renderer_gl.cpp',
    'static std::vector<BatchVertex> s_batch;\n',
    '''// ''' + MARK + ''': the batch has one vertex mode and one uploaded XF block for its
// whole life; DrawPrimitive closes the batch when either would change.
static bool s_gpu_vertex_mode = false;
static unsigned s_gpu_texgen_lanes = 0;
static std::shared_ptr<DrawRaw> s_gpu_vertex_snapshot;
static std::vector<BatchVertex> s_batch;
''',
    'gpu vertex statics')

sub('src/hle/gx/renderer_gl.cpp',
    'static void NativeWriteConvertedVertex(const VertexData &vtx, BatchVertex& bv) {\n',
    '''static void NativeWriteConvertedVertex(const VertexData &vtx, BatchVertex& bv) {
  // ''' + MARK + ''': a qualified draw keeps its object-space attributes; the
  // generated vertex shader applies the snapshot banks. Every field is still
  // written, so EmitVertex's no-zero-fill path (NATIVE_MOM_VERTEX_PASS_V1) stays
  // total.
  //   x,y,z / nx,ny,nz : object-space position / normal (0 when not declared,
  //                      which is what the CPU path leaves there)
  //   u                : position-matrix index; the shader indexes the uploaded
  //                      bank with it (attribute 2, see NativeUploadGpuXf)
  //   v                : unused by the generated vertex shader, kept 0
  //   texgen[t]        : the CPU result {x,y,q}, or {tex u, tex v, matrix index}
  //                      when the shader evaluates the regular generators
  if (s_gpu_vertex_mode) {
    bv.x = vtx.pos[0]; bv.y = vtx.pos[1]; bv.z = vtx.pos[2];
    bv.nx = vtx.has_norm ? vtx.norm[0] : 0.f;
    bv.ny = vtx.has_norm ? vtx.norm[1] : 0.f;
    bv.nz = vtx.has_norm ? vtx.norm[2] : 0.f;
    bv.r = vtx.has_color ? vtx.color[0] : 1.f;
    bv.g = vtx.has_color ? vtx.color[1] : 1.f;
    bv.b = vtx.has_color ? vtx.color[2] : 1.f;
    bv.a = vtx.has_color ? vtx.color[3] : 1.f;
    std::memcpy(bv.color1, vtx.color1, sizeof(bv.color1));
    bv.u = float(vtx.posMtxIdx);
    bv.v = vtx.has_norm ? 1.f : 0.f; // NATIVE_MOM_GPU_VERTEX_V1: has_normal flag for gxNrm
    if (s_gpu_texgen_lanes) {
      for (unsigned t = 0; t < 8; ++t) {
        bv.texgen[t][0] = vtx.tex[t][0];
        bv.texgen[t][1] = vtx.tex[t][1];
        bv.texgen[t][2] = float(vtx.texMtxIdx[t]);
      }
    } else {
      // NATIVE_MOM_GPU_VERTEX_V1: view-space temp from the same frozen snapshot.
      VertexData tvtx = vtx;
      if (!tvtx.pre_xf && s_gpu_vertex_snapshot)
        NativeGpuViewTransform(*s_gpu_vertex_snapshot, tvtx);
      NativeTexGen(g_state, tvtx, bv.texgen);
    }
    return;
  }
''',
    'NativeWriteConvertedVertex')

sub('src/hle/gx/renderer_gl.cpp',
    'static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {\n',
    '''// ''' + MARK + ''': the regular texgen lanes the vertex shader may evaluate for this
// batch, or 0 when they all stay on the CPU. NativeTexGen is the reference: a
// generator reads its input from position, normal or ANOTHER generator's
// coordinate (XF texinfo source 5..12). The shader is handed each lane's CPU-side
// texgen attribute, which is the raw coordinate, while the CPU chains the
// generated one - so a chained source is the single case the shader cannot
// reproduce, and it puts the whole batch back on the CPU texgen. Every other
// source (position, normal, colour, none) is exact, including index 60, the
// row-count rule and the post matrix.
// NWII_NO_GPU_TEXGEN=1 forces 0; NWII_NO_TEXGEN_BOUND=1 raises the lane count to 8
// on both paths, as before.
static unsigned NativeGpuTexgenLanes(const GXState& state) {
  if (!NativeGpuTexgenEnabled()) return 0;
  const unsigned lanes = NativeTexGenLanes(state);
  for (unsigned t = 0; t < lanes && t < 8; ++t) {
    const unsigned source = (state.xf[0x40 + t] >> 7) & 31;
    if (source >= 5 && source <= 12) return 0;
  }
  return lanes;
}

static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {
''',
    'NativeTexGen prologue')

sub('src/hle/gx/renderer_gl.cpp',
    '  native_batch_census::Draw();\n  const uint8_t topology = NativeBatchTopology((uint8_t)cmd.prim_type, n);\n',
    '''  // ''' + MARK + ''': the batch has one vertex mode and one uploaded XF block, so a
  // draw that changes either closes the batch first - exactly like the existing
  // state boundaries. This runs before the batch-open check below, so the draw
  // that changes the mode also re-runs SetupDrawState (and therefore the upload)
  // for itself.
  const bool gpu = bool(cmd.native_gpu_snapshot);
  const unsigned gpu_texgen = gpu ? NativeGpuTexgenLanes(g_state) : 0u;
  bool gpu_boundary = gpu != s_gpu_vertex_mode || (gpu && gpu_texgen != s_gpu_texgen_lanes);
  if (gpu && !gpu_boundary && s_gpu_vertex_snapshot &&
      s_gpu_vertex_snapshot != cmd.native_gpu_snapshot) {
    const DrawRaw &old = *s_gpu_vertex_snapshot;
    const DrawRaw &now = *cmd.native_gpu_snapshot;
    gpu_boundary = old.snap_pos_bank != now.snap_pos_bank ||
                   old.snap_nrm_bank != now.snap_nrm_bank ||
                   old.have_snap_mtx != now.have_snap_mtx ||
                   std::memcmp(old.snap_pos_mtx, now.snap_pos_mtx,
                               sizeof(now.snap_pos_mtx)) != 0 ||
                   std::memcmp(old.snap_nrm_mtx, now.snap_nrm_mtx,
                               sizeof(now.snap_nrm_mtx)) != 0;
  }
  if (s_batch_open && gpu_boundary) {
    FlushBatch(native_batch_census::Other);
    s_batch_open = false;
  }
  s_gpu_vertex_mode = gpu;
  s_gpu_texgen_lanes = gpu_texgen;
  s_gpu_vertex_snapshot = cmd.native_gpu_snapshot;
  static const bool s_gpu_probe = std::getenv("NWII_GPU_VERTEX_PROBE") != nullptr;
  if (s_gpu_probe) {
    static uint64_t gpu_draws = 0, cpu_draws = 0, boundaries = 0, gpu_verts = 0;
    if (gpu) { ++gpu_draws; gpu_verts += n; } else ++cpu_draws;
    if (gpu_boundary) ++boundaries;
    static auto last = std::chrono::steady_clock::now();
    const auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::seconds>(now - last).count() >= 2) {
      last = now;
      std::printf("[GPUVERT] gpu_draws=%llu cpu_draws=%llu gpu_verts=%llu "
                  "boundaries=%llu texgen_lanes=%u\\n",
                  (unsigned long long)gpu_draws, (unsigned long long)cpu_draws,
                  (unsigned long long)gpu_verts, (unsigned long long)boundaries,
                  s_gpu_texgen_lanes);
      fflush(stdout);
      gpu_draws = cpu_draws = boundaries = gpu_verts = 0;
    }
  }
  native_batch_census::Draw();
  const uint8_t topology = NativeBatchTopology((uint8_t)cmd.prim_type, n);
''',
    'DrawPrimitive boundary')

sub('src/hle/gx/renderer_gl.cpp',
    'static void SetupDrawState(const GXCommand &cmd) {\n  // NATIVE_MOM_VERTEX_PASS_V1:',
    '''// ''' + MARK + ''': one std140 block with the banks this batch indexes. The GL 3.3
// guaranteed vertex-side ordinary-uniform budget (256 vec4 for the whole stage)
// cannot hold 64 position rows + 32 normal rows + 64 texgen rows + 64 post rows,
// so the matrices live in a uniform block bound to point 1, and every program
// binds the block to that point once (see the compile site below).
//   pos[64]    : XF 0x0000 position matrices, 64 rows of 4; slot k is rows k..k+2
//   normal[32] : XF 0x0400 normal matrices, 32 rows of 4 floats carrying 3;
//                slot k is rows k..k+2
//   tex[64]    : g_state.posMatrices (the regular texgen matrices), 64 rows of 4
//   post[64]   : g_state.postMatrices, 64 rows of 4
//   info[8]    : [t][0] = XF 0x1040+t (texgen info), [t][1] = XF 0x1050+t (post)
//   control    : [0] 0 CPU / 1 per-vertex bank / 2 single matrix
//                [1] post-matrix enabled (XF 0x1012 bit 0)
//                [2] regular-texgen lanes (0 = the attributes already hold the
//                    CPU result and the shader must not evaluate them)
struct NativeGpuXfBlock {
  float pos[64][4];
  float normal[32][4];
  float tex[64][4];
  float post[64][4];
  int info[8][4];
  int control[4];
};

static void NativeUploadGpuXf() {
  static GLuint buffer = 0;
  static NativeGpuXfBlock previous{};
  static bool previous_gpu = false;
  if (!buffer) {
    // Control must read zero even if no qualified draw ever arrives: an
    // unbacked uniform block has no defined contents.
    glGenBuffers(1, &buffer);
    glBindBufferBase(GL_UNIFORM_BUFFER, 1, buffer);
    glBufferData(GL_UNIFORM_BUFFER, sizeof(NativeGpuXfBlock), &previous, GL_STREAM_DRAW);
  }
  // The buffer already holds the all-zero CPU-mode block.
  if (!s_gpu_vertex_mode && !previous_gpu) return;
  NativeGpuXfBlock block{};
  block.control[0] =
      s_gpu_vertex_mode ? (s_gpu_vertex_snapshot->snap_pos_bank.empty() ? 2 : 1) : 0;
  if (s_gpu_vertex_mode) {
    const DrawRaw &q = *s_gpu_vertex_snapshot;
    if (block.control[0] == 1) {
      std::memcpy(block.pos, q.snap_pos_bank.data(), sizeof(block.pos));
      for (unsigned row = 0; row < 32; ++row)
        std::memcpy(block.normal[row], q.snap_nrm_bank.data() + row * 3, 12);
    } else {
      std::memcpy(block.pos, q.snap_pos_mtx, sizeof(q.snap_pos_mtx));
      for (unsigned row = 0; row < 3; ++row)
        std::memcpy(block.normal[row], q.snap_nrm_mtx + row * 3, 12);
    }
    block.control[2] = int(s_gpu_texgen_lanes);
    if (s_gpu_texgen_lanes) {
      std::memcpy(block.tex, g_state.posMatrices, sizeof(block.tex));
      std::memcpy(block.post, g_state.postMatrices, sizeof(block.post));
      for (unsigned t = 0; t < 8; ++t) {
        block.info[t][0] = int(g_state.xf[0x40 + t]);
        block.info[t][1] = int(g_state.xf[0x50 + t]);
      }
      block.control[1] = int(g_state.xf[0x12] & 1u);
    }
  }
  if (!previous_gpu || std::memcmp(&previous, &block, sizeof(block)) != 0) {
    glBindBuffer(GL_UNIFORM_BUFFER, buffer);
    glBufferData(GL_UNIFORM_BUFFER, sizeof(block), &block, GL_STREAM_DRAW);
    previous = block;
  }
  previous_gpu = s_gpu_vertex_mode;
}

static void SetupDrawState(const GXCommand &cmd) {
  // NATIVE_MOM_VERTEX_PASS_V1:''',
    'NativeUploadGpuXf')

sub('src/hle/gx/renderer_gl.cpp',
    '  s_draw_texgen_lanes = NativeTexGenLanes(g_state);\n',
    '''  s_draw_texgen_lanes = NativeTexGenLanes(g_state);
  // ''' + MARK + ''': same instant, same state - the XF block the shader reads is
  // uploaded here, once per batch, exactly where the shader key is taken.
  NativeUploadGpuXf();
''',
    'SetupDrawState upload')

sub('src/hle/gx/renderer_gl.cpp',
    '    s_shader_cache[hash] = sh;\n',
    '''    // ''' + MARK + ''': a program created here (compiled or loaded from the program
    // binary cache) has the uniform block, so point it at binding 1 - link and
    // glProgramBinary both leave the binding at zero.
    {
      const GLuint xfblock = glGetUniformBlockIndex(sh.id, "NativeGpuXF");
      if (xfblock != GL_INVALID_INDEX)
        glUniformBlockBinding(sh.id, xfblock, 1);
    }
    s_shader_cache[hash] = sh;
''',
    'shader block binding')

# --- 4. the shader: transform, normal, regular texgen -----------------------
sub('src/hle/gx/tev_shader_gen.cpp',
    '    vs << "void main() {\\n";\n',
    r'''    // NATIVE_MOM_GPU_VERTEX_V1: the snapshot XF banks of a qualified draw, and
    // the same arithmetic the CPU path uses. gxControl.x is zero for every draw
    // that kept the CPU transform, so this is inert for them; the block is
    // declared unconditionally so the generated source (and therefore the shader
    // key, the source dedupe and the program cache) never depends on the mode.
    vs << R"(
layout(std140) uniform NativeGpuXF {
    vec4 gxPos[64];
    vec4 gxNormal[32];
    vec4 gxTex[64];
    vec4 gxPost[64];
    ivec4 gxInfo[8];
    ivec4 gxControl;
};
// Left-to-right, the CPU's order: m0*x + m1*y + m2*z + m3.
float gxRow(vec4 row, vec4 v) { return ((row.x*v.x + row.y*v.y) + row.z*v.z) + row.w*v.w; }
vec3 gxPosition(vec3 p, int index) {
    int b = (gxControl.x == 2) ? 0 : min(index, 61);
    vec4 v = vec4(p, 1.0);
    return vec3(gxRow(gxPos[b], v), gxRow(gxPos[b+1], v), gxRow(gxPos[b+2], v));
}
vec3 gxNrm(vec3 n, int index) {
    int b = (gxControl.x == 2) ? 0 : min(index & 31, 29);
    vec4 v = vec4(n, 0.0);
    return vec3(gxRow(gxNormal[b], v), gxRow(gxNormal[b+1], v), gxRow(gxNormal[b+2], v));
}
vec3 gxTexgen(int t, vec3 p, vec3 n, vec3 inputs[8]) {
    int info = gxInfo[t].x;
    int source = (info >> 7) & 31;
    vec4 inputValue = vec4(0.0, 0.0, 1.0, 1.0);
    if (source == 0) inputValue.xyz = p;
    else if (source == 1) inputValue.xyz = n;
    else if (source >= 5 && source <= 12) inputValue.xy = inputs[source-5].xy;
    if ((info & 4) == 0) inputValue.z = 1.0;
    // Index 60 means "no texgen matrix"; above 61 the bank has no room, and the
    // CPU leaves the input untouched in both cases.
    int index = int(inputs[t].z);
    vec3 result = vec3(inputValue.xy, (info & 2) != 0 ? inputValue.z : 1.0);
    if (index != 60 && index + 3 <= 64) {
        result.x = gxRow(gxTex[index], inputValue);
        result.y = gxRow(gxTex[index+1], inputValue);
        if ((info & 2) != 0) result.z = gxRow(gxTex[index+2], inputValue);
    }
    if (gxControl.y != 0) {
        int post = gxInfo[t].y;
        vec3 value = result;
        if ((post & 256) != 0) {
            float len = sqrt((value.x*value.x + value.y*value.y) + value.z*value.z);
            if (len > 0.0) value /= len;
        }
        int start = post & 63;
        vec4 m0 = gxPost[start];
        vec4 m1 = gxPost[(start + 1) & 63];
        vec4 m2 = gxPost[(start + 2) & 63];
        result.x = ((m0.w + m0.x*value.x) + m0.y*value.y) + m0.z*value.z;
        result.y = ((m1.w + m1.x*value.x) + m1.y*value.y) + m1.z*value.z;
        result.z = ((m2.w + m2.x*value.x) + m2.y*value.y) + m2.z*value.z;
    }
    return result;
}
)";
    vs << "void main() {\n";
''',
    'gpu vertex glsl')

sub('src/hle/gx/tev_shader_gen.cpp',
    '''    vs << "    gl_Position = mvp * vec4(vertexPosition, 1.0);\\n";
    vs << "    vColor0 = dolphin_chan(0, vertexPosition, vertexNormal, vertexColor);\\n";
    vs << "    vColor1 = dolphin_chan(1, vertexPosition, vertexNormal, vertexColor1);\\n";
''',
    r'''    // NATIVE_MOM_GPU_VERTEX_V1: a qualified draw arrives in object space; the
    // lighting below consumes view space, exactly like the CPU path did.
    vs << "    vec3 gxViewPosition = vertexPosition;\n";
    vs << "    vec3 gxViewNormal = vertexNormal;\n";
    vs << "    if (gxControl.x != 0) {\n";
    vs << "        int gxIndex = int(vertexTexCoord.x);\n";
    vs << "        gxViewPosition = gxPosition(vertexPosition, gxIndex);\n";
    vs << "        gxViewNormal = vertexTexCoord.y != 0.0 ? gxNrm(vertexNormal, gxIndex) : vec3(0.0);\n";
    vs << "    }\n";
    vs << "    gl_Position = mvp * vec4(gxViewPosition, 1.0);\n";
    vs << "    vColor0 = dolphin_chan(0, gxViewPosition, gxViewNormal, vertexColor);\n";
    vs << "    vColor1 = dolphin_chan(1, gxViewPosition, gxViewNormal, vertexColor1);\n";
''',
    'gpu vertex main')

sub('src/hle/gx/tev_shader_gen.cpp',
    '''    for (unsigned i=0;i<8;++i) {
        const unsigned type=(state.xf[0x40+i]>>4)&7;
        if(type==2 || type==3) vs << "    vTex" << i << " = vec3(vColor" << type-2 << ".rg,1.0);\\n";
        else vs << "    vTex" << i << " = generatedTex" << i << ";\\n";
    }
''',
    r'''    // NATIVE_MOM_GPU_VERTEX_V1: gxInputs is what a chained generator would read -
    // the CPU-side attribute of that lane - and gxControl.z is the lane count the
    // CPU path evaluates (0 keeps every lane on the CPU value). Lanes at or above
    // it are the zeros NativeTexGen writes there.
    vs << "    vec3 gxInputs[8]=vec3[8](generatedTex0,generatedTex1,generatedTex2,generatedTex3,generatedTex4,generatedTex5,generatedTex6,generatedTex7);\n";
    for (unsigned i=0;i<8;++i) {
        const unsigned type=(state.xf[0x40+i]>>4)&7;
        if(type==2 || type==3) vs << "    vTex" << i << " = vec3(vColor" << type-2 << ".rg,1.0);\n";
        else vs << "    vTex" << i << " = (gxControl.x != 0 && " << i << " < gxControl.z) ? gxTexgen(" << i << ", gxViewPosition, gxViewNormal, gxInputs) : generatedTex" << i << ";\n";
    }
''',
    'gpu texgen main')

# The guard above ran on the pristine source; the patched source must now read it.
tev = texts['src/hle/gx/tev_shader_gen.cpp']
if not RESTORE: assert tev.count('vertexTexCoord') == 3, 'vertexTexCoord occurrences after patch'
# The uniform block the renderer binds and the one the shader declares are the
# same name; a rename on either side would leave the control uniform unbacked.
if not RESTORE: assert tev.count('uniform NativeGpuXF {') == 1, 'shader block declaration'
if not RESTORE: assert texts['src/hle/gx/renderer_gl.cpp'].count('"NativeGpuXF"') == 1, 'block index lookup'

for relative in SOURCES:
    (runtime / relative).write_text(texts[relative], newline='\n')
print('GPU vertex pipeline staged: snapshot banks + per-vertex index in the batch, '
      'position/normal (and regular texgen) in the vertex shader.')
