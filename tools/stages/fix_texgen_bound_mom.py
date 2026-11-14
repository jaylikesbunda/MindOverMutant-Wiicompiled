"""Only build the texture-coordinate generators the draw can actually read.

``NativeTexGen`` evaluates all eight GX texture-coordinate generators for every
vertex, and each one costs a 2-3 row matrix multiply plus, when XF normalise is
on, a square root and a 3x4 post-matrix multiply.  The CPU profile attributes
9-12 % of host CPU to ``NativeWriteConvertedVertex``, which is where this lives,
and the movie quad - with ``numTexGens`` of **0** - was paying for all eight.

The bound comes from genMode (``bp[0x00]`` bits 0-3), raised if any active TEV
stage samples a map above it (``bp[0x28+i]`` bits 16-18), because a stage that
samples map N needs coordinate N present.  Lanes above the bound keep whatever
the value-initialised ``BatchVertex`` left, which is zero.

``NWII_NO_TEXGEN_BOUND=1`` restores the unconditional eight.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_TEXGEN_BOUND_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('texgen bound already present.')
    raise SystemExit(0)

anchor = """static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {
  for (unsigned t=0;t<8;++t) {"""
assert text.count(anchor) == 1, 'NativeTexGen anchor'
replacement = """// """ + MARK + """: coordinate generators the shader can read, derived from
// genMode and raised by any active stage that samples a higher map.
static unsigned NativeTexGenCount(const GXState& state) {
  static const bool disabled = std::getenv("NWII_NO_TEXGEN_BOUND") != nullptr;
  if (disabled) return 8;
  unsigned declared = state.bp[0x00] & 0xFu;
  if (declared < 1) declared = 1;
  if (declared > 8) declared = 8;
  for (unsigned i = 0; i < 8; ++i) {
    if (!state.texStages[i].base_addr || !state.texStages[i].width ||
        !state.texStages[i].height)
      continue;
    const unsigned map = (state.bp[0x28 + i] >> 16) & 7u;
    if (map + 1 > declared) declared = map + 1 > 8 ? 8 : map + 1;
  }
  return declared;
}

static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {
  const unsigned generators = NativeTexGenCount(state);
  for (unsigned t=0;t<generators;++t) {"""
text = text.replace(anchor, replacement, 1)
path.write_text(text)
print('Texgen bounded to the generators the draw reads.')
