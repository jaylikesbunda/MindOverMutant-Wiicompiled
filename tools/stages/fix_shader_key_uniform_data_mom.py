r"""Drop uniform-carried values from the shader cache key (MoM; CotT fix_shader_source_reuse / fix_uniform_lookup_cache line).

Marker: NATIVE_MOM_SHADER_KEY_UNIFORM_DATA_V1.

Why: in the busy scene the GL thread spent ~19% in ucrtbase strchr and showed NativeWriteFog(std::stringstream&): shader
source was being regenerated (GenerateTEVShader, stringstream text) on cache misses that produce a byte-identical program.
The state hash that keys s_shader_cache folded in every byte of
  * BP 0x06..0x0E, the indirect matrices: since NATIVE_MOM_IND_MTX_UNIFORMS_V1 the generator emits uNativeIndMtx[] for
    them (coefficients and scale exponent) and reads none of their bits;
  * BP 0xEE..0xF2, the fog block: with NATIVE_MOM_SHADER_DATA_UNIFORMS_V1 the generator reads only the fog type
    (BP 0xF1 bits 21..23) and the projection bit (BP 0xF1 bit 20); A, B, C and the colour ride uNativeFogData/Color;
  * BP 0xF3, the alpha test: the comparators and logic op are code, but a reference value other than 0 or 255 rides
    uNativeAlphaRef (0 and 255 stay literals).
So every animated indirect matrix (water, heat haze), fog change or alpha-reference change missed the cache and regenerated
the whole shader text only to find the same program. With the uniform modes on (the defaults), the key now folds exactly
what the generated text depends on: no indirect-matrix values, the fog type/projection bits, the alpha comparators/logic
and each reference's class (0, 255, other). The uniforms themselves are uploaded by SetupDrawState on every batch, hit or
miss, as before. NWII_LITERAL_IND_MTX / NWII_LITERAL_SHADER_DATA (literal GLSL) restore the full key for their registers.

Edits include/runtime/gx_state.h and src/hle/gx/renderer_gl.cpp in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_SHADER_KEY_UNIFORM_DATA_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', ROOT / 'export/nWiiRuntime']

STATE_EDITS = [
    ('#include <cstdlib>\n',
     '#include <cstdlib>\n#include "runtime/native_shader_data.h" // ' + MARK + '\n'),
    ('      add_u32(bp[0xF3]); \n',
     '      if (nwii::runtime::gx::NativeShaderDataUniformsEnabled()) { // ' + MARK + ': refs other than 0/255 are uniforms\n'
     '        auto cls = [](uint32_t r) { r &= 0xFFu; return r == 0u ? 0u : (r == 255u ? 255u : 1u); };\n'
     '        add_u32((bp[0xF3] & 0xFFFF0000u) | (cls(bp[0xF3] >> 8) << 8) | cls(bp[0xF3]));\n'
     '      } else add_u32(bp[0xF3]); \n'),
    ('      if (PruneShaderKey()) add_u32(bp[0xEE]);\n',
     '      if (nwii::runtime::gx::NativeShaderDataUniformsEnabled()) {} // ' + MARK + ': fog A is uNativeFogData.x\n'
     '      else if (PruneShaderKey()) add_u32(bp[0xEE]);\n'),
]
RENDER_EDITS = [
    ('static uint64_t NativeTevShaderHashUncached(const GXState &state, uint8_t primitive) {\n'
     '  uint64_t hash = state.GetShaderHash(primitive);\n'
     '  for(unsigned t=0;t<8;++t)\n'
     '    hash=(hash ^ ((state.xf[0x40+t]>>4)&7))*1099511628211ull;\n'
     '  for(unsigned reg=0xee;reg<=0xf2;++reg)\n'
     '    for(unsigned shift=0;shift<32;shift+=8)\n'
     '      hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;\n',
     'bool NativeIndMtxUniforms(); // ' + MARK + ' (tev_shader_gen.cpp)\n'
     'static uint64_t NativeTevShaderHashUncached(const GXState &state, uint8_t primitive) {\n'
     '  uint64_t hash = state.GetShaderHash(primitive);\n'
     '  for(unsigned t=0;t<8;++t)\n'
     '    hash=(hash ^ ((state.xf[0x40+t]>>4)&7))*1099511628211ull;\n'
     '  if (NativeShaderDataUniformsEnabled()) { // ' + MARK + ': the generated text reads only the fog type and projection bit\n'
     '    hash=(hash ^ ((state.bp[0xf1]>>20)&15))*1099511628211ull;\n'
     '  } else\n'
     '  for(unsigned reg=0xee;reg<=0xf2;++reg)\n'
     '    for(unsigned shift=0;shift<32;shift+=8)\n'
     '      hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;\n'),
    ('  if (GXState::PruneShaderKey()) {\n'
     '    for(unsigned reg=0x06;reg<=0x0E;++reg)\n',
     '  if (GXState::PruneShaderKey()) {\n'
     '    if (!NativeIndMtxUniforms()) // ' + MARK + ': the indirect matrices are uNativeIndMtx, not code\n'
     '    for(unsigned reg=0x06;reg<=0x0E;++reg)\n'),
]
EDITS = {'include/runtime/gx_state.h': STATE_EDITS, 'src/hle/gx/renderer_gl.cpp': RENDER_EDITS}


def main():
    restore = '--restore' in sys.argv
    for base in TREES:
        for rel, pairs in EDITS.items():
            p = base / rel
            raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
            crlf = '\r\n' in raw
            t = raw.replace('\r\n', '\n')
            if restore:
                for old, new in pairs:
                    t = t.replace(new, old, 1)
            elif MARK in t:
                continue
            else:
                for old, new in pairs:
                    if t.count(old) != 1:
                        print('fix_shader_key_uniform_data_mom: %s: anchor found %d times: %r' % (p, t.count(old), old[:50])); sys.exit(1)
                    t = t.replace(old, new, 1)
            p.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
    print('shader key uniform data %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
