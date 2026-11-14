"""Hoist invariant CPU texgen state from each vertex to each primitive.

The arithmetic, matrix accesses, lane count, and inputs remain unchanged.
The renderer applies register commands only between primitives. Reversible.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
changes = [
('''static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {''', '''// NATIVE_MOM_TEXGEN_PLAN_V1: GX state is fixed while one primitive is emitted.
struct NativeTexgenPlan {
  uint32_t info[8], post[8];
  unsigned source[8];
  bool post_enabled;
};
static NativeTexgenPlan s_native_texgen_plan;
static void NativePrepareTexgen(const GXState& state) {
  s_native_texgen_plan.post_enabled = (state.xf[0x12] & 1) != 0;
  for (unsigned t = 0; t < s_draw_texgen_lanes; ++t) {
    const uint32_t info = state.xf[0x40 + t];
    s_native_texgen_plan.info[t] = info;
    s_native_texgen_plan.source[t] = (info >> 7) & 31;
    s_native_texgen_plan.post[t] = state.xf[0x50 + t];
  }
}
static void NativeTexGen(const GXState& state, const VertexData& vertex, float out[8][3]) {'''),
('''    const uint32_t info=state.xf[0x40+t];
    const unsigned source=(info>>7)&31;''', '''    const uint32_t info=s_native_texgen_plan.info[t];
    const unsigned source=s_native_texgen_plan.source[t];'''),
('''    if(state.xf[0x12]&1) {
      const uint32_t post=state.xf[0x50+t];''', '''    if(s_native_texgen_plan.post_enabled) {
      const uint32_t post=s_native_texgen_plan.post[t];'''),
('''static void EmitPrimitive(const GXCommand &cmd) {
  // NATIVE_VERTEX_REUSE_V1''', '''static void EmitPrimitive(const GXCommand &cmd) {
  NativePrepareTexgen(RenderState()); // NATIVE_MOM_TEXGEN_PLAN_V1
  // NATIVE_VERTEX_REUSE_V1'''),
]
for tree in ('vendor/NWiiRecomp/nWiiRuntime', 'export/nWiiRuntime'):
    path = ROOT / tree / 'src/hle/gx/renderer_gl.cpp'
    raw = path.read_bytes().decode('utf-8', 'surrogateescape')
    crlf = '\r\n' in raw
    s = raw.replace('\r\n', '\n')
    for old, new in changes:
        if '--restore' in sys.argv:
            old, new = new, old
            if s.count(old) == 1:
                s = s.replace(old, new, 1)
                continue
        if new in s: continue
        if s.count(old) != 1: raise ValueError((str(path), old[:60], s.count(old)))
        s = s.replace(old, new, 1)
    path.write_bytes((s.replace('\n', '\r\n') if crlf else s).encode('utf-8', 'surrogateescape'))
print('CPU texgen plan ' + ('removed' if '--restore' in sys.argv else 'installed'))
