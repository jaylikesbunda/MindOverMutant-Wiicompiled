"""Remove the redundant per-vertex work in the renderer's vertex pass.

The CPU profile puts 9-12 % of the host thread in ``NativeWriteConvertedVertex``
(``src/hle/gx/renderer_gl.cpp``).  Every vertex is decoded into a ``VertexData``
by ``FifoParser::DecodeDraw`` and then *processed again* by
``NativeWriteConvertedVertex``, which transforms position and normal, evaluates
the texture-coordinate generators and copies 160 bytes into a ``BatchVertex``.
CotT removed the second pass by moving the transform and the texgen to the
vertex shader (``native-port/tools/fix_gpu_vertex_pipeline.py``), with compact
64+12N strides (``fix_packed_vertices.py``) and a fused batch
(``fix_fused_gpu_batch.py``).  That needs a shader rewrite, which this stage does
not do.  What it does instead is remove the two parts of the second pass that
change no vertex the shader can observe:

1. ``NativeTexGen`` evaluated all eight generators regardless of what the
   generated shader reads.  ``tools/stages/fix_texgen_bound_mom.py`` tried to
   bound that (``NATIVE_MOM_TEXGEN_BOUND_V1``) and is off in every bisect run
   because a real draw sampled a lane it had not computed.  Its bound is wrong:
   the map field it reads is not the map field, and it looks the texture up by
   the wrong index (analysis in ``logs/vertex-pass-mom.md``).  ``NATIVE_TEXGEN_V1``
   plus this stage derive the generators from the lanes the shader can actually
   read: ``vTex<stage.texCoord>`` for every generated TEV stage and
   ``vTex<(order>>3)>`` for a live indirect stage, with genMode's ``numTexGens``
   as the floor.  Lanes above the bound are written as zero - exactly what the
   value-initialised batch element left there - so every lane the fragment
   shader divides by keeps its old value.

2. ``EmitVertex`` did ``s_batch.emplace_back()``, which value-initialises the
   160-byte ``BatchVertex``, and then ``NativeWriteConvertedVertex`` overwrote
   every one of those bytes.  ``NativeWriteConvertedVertex`` is made total (the
   two conditional fields - the normal when the vertex declares none, and the
   texgen lanes above the bound - are now written explicitly), so the element
   can be constructed without the zero-fill.

Both parts are independently switchable at run time:

  ``NWII_NO_TEXGEN_BOUND=1``   unconditional eight generators (part 1 off)
  ``NWII_NO_VERTEX_UNINIT=1``  zero-fill the batch element first (part 2 off)

The stage is idempotent (marker ``NATIVE_MOM_VERTEX_PASS_V1``) and accepts both
a tree that still carries ``fix_texgen_bound_mom.py``'s helper and a pristine
one, so it can replace that stage in ``tools/apply_stages.py`` or run after it.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_VERTEX_PASS_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('vertex pass already trimmed.')
    raise SystemExit(0)


def sub(old, new, what):
    global text
    assert text.count(old) == 1, 'anchor %s' % what
    text = text.replace(old, new, 1)


# --- 1. the coordinate generators this draw can read -------------------------
prologue = '''// NATIVE_TEXGEN_V1: evaluate each regular generator before perspective interpolation.
// ''' + MARK + ''': only the coordinate generators the draw can read.
// tev_shader_gen.cpp reads vTex<stage.texCoord> for every generated TEV stage
// (NativeWriteIndirect, and the texMap lookup) and vTex<(order>>3)> for an
// indirect stage whose matrix or bump is live; the vertex shader feeds vTex<t>
// from generatedTex<t> for every lane that is not a colour-channel generator.
// genMode's numTexGens is the floor - GX produces that many generators - raised
// by any stage that reads a higher lane.  Lanes in [lanes,8) are zeroed here,
// which is what the value-initialised batch element used to leave there.
// NWII_NO_TEXGEN_BOUND=1 restores the unconditional eight.
static unsigned s_draw_texgen_lanes = 8;

static unsigned NativeTexGenLanes(const GXState& state) {
  static const bool disabled = std::getenv("NWII_NO_TEXGEN_BOUND") != nullptr;
  if (disabled) return 8;
  unsigned lanes = state.numTexGens;                     // genMode bits 0-3
  if (lanes < 1) lanes = 1;                              // bv.u/bv.v read lane 0
  const unsigned stages = state.numTevStages ? state.numTevStages : 1;
  const unsigned indirect = (state.bp[0x00] >> 16) & 7;   // as tev_shader_gen reads it
  for (unsigned i = 0; i < stages && i < 16; ++i) {
    const unsigned coord = state.tevStages[i].texCoord & 7;
    if (coord + 1 > lanes) lanes = coord + 1;
    const unsigned config = state.bp[0x10 + i];           // this stage's indirect setup
    const unsigned selected = config & 3;
    if (selected < indirect && (((config >> 9) & 3) || ((config >> 7) & 3))) {
      const unsigned order = (state.bp[0x27] >> (selected * 6)) & 63;
      const unsigned lane = order >> 3;
      if (lane + 1 > lanes) lanes = lane + 1;
    }
  }
  return lanes > 8 ? 8 : lanes;
}

static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {
  const unsigned generators = s_draw_texgen_lanes;
  for (unsigned t=generators;t<8;++t) { out[t][0]=out[t][1]=out[t][2]=0.0f; }
  for (unsigned t=0;t<generators;++t) {
'''
# The region replaced runs from the NATIVE_TEXGEN_V1 comment to the loop body, and
# covers both possible prologues: fix_texgen_bound_mom.py's helper or the plain
# eight-generator loop.
a = text.index('// NATIVE_TEXGEN_V1:')
b = text.index('    const uint32_t info=state.xf[0x40+t];')
old_prologue = text[a:b]
assert old_prologue.count('static void NativeTexGen(') == 1, 'NativeTexGen prologue'
assert ('NativeTexGenCount' in old_prologue) != ('for (unsigned t=0;t<8;++t) {' in old_prologue), \
    'unexpected NativeTexGen prologue shape'
text = text[:a] + prologue + text[b:]

# The shader is keyed on this state, so take the read set at the same instant.
sub("""static void SetupDrawState(const GXCommand &cmd) {
  static const bool use_shader = (std::getenv("NWII_NOSHADER") == nullptr);""",
    """static void SetupDrawState(const GXCommand &cmd) {
  // """ + MARK + """: snapshot the read set with the state the shader is keyed on, so a
  // batched draw that continues without a new SetupDrawState (unchanged state, or a
  // source-identical program reused) keeps the generators its program was built for.
  s_draw_texgen_lanes = NativeTexGenLanes(g_state);
  static const bool use_shader = (std::getenv("NWII_NOSHADER") == nullptr);""",
    'SetupDrawState')

# --- 2. no zero-fill before a total writer ----------------------------------
sub("""struct BatchVertex {
  float x, y, z;
  float r, g, b, a;
  float u, v;
  float nx, ny, nz;
  float color1[4];
  float texgen[8][3];
};""",
    """struct BatchVertex {
  float x, y, z;
  float r, g, b, a;
  float u, v;
  float nx, ny, nz;
  float color1[4];
  float texgen[8][3];
  // """ + MARK + """: NativeWriteConvertedVertex writes every field, so a batch element can
  // be constructed without the 160-byte zero-fill.  The defaulted default constructor
  // stays trivial, so `BatchVertex v{}` (ConvertVertex, the line/point copies) still
  // zero-fills.
  struct NoInitTag {};
  BatchVertex() = default;
  explicit BatchVertex(NoInitTag) {}
};""",
    'BatchVertex')

# Every field the writer used to leave to the zero-fill, written explicitly.
sub("""    } else {
      bv.nx = vtx.norm[0]; bv.ny = vtx.norm[1]; bv.nz = vtx.norm[2];
    }
  }
""",
    """    } else {
      bv.nx = vtx.norm[0]; bv.ny = vtx.norm[1]; bv.nz = vtx.norm[2];
    }
  } else {
    // """ + MARK + """: a vertex that declares no normal used to keep the zero-fill's
    // zeros; write them, so the writer is total.
    bv.nx = bv.ny = bv.nz = 0.f;
  }
""",
    'normal else branch')

sub("""static void EmitVertex(const VertexData &vtx) {
  s_batch.emplace_back();NativeWriteConvertedVertex(vtx,s_batch.back());
}""",
    """static void EmitVertex(const VertexData &vtx) {
  // """ + MARK + """: the writer fills every field, so the element needs no zero-fill
  // first.  NWII_NO_VERTEX_UNINIT=1 restores the value-initialised element.
  static const bool zero_fill = std::getenv("NWII_NO_VERTEX_UNINIT") != nullptr;
  if (zero_fill) s_batch.emplace_back();
  else s_batch.emplace_back(BatchVertex::NoInitTag());
  NativeWriteConvertedVertex(vtx, s_batch.back());
}""",
    'EmitVertex')

path.write_text(text)
print('Vertex pass: texgen bounded to the shader read set, batch element no longer zero-filled.')
