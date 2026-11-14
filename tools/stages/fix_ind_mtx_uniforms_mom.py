r"""Indirect-texture matrices as shader uniforms, so an animated matrix stops costing a program per value.

Marker: NATIVE_MOM_IND_MTX_UNIFORMS_V1.

Measured problem (save state outside Coco's house, left stick held, 200 host frames): the renderer compiled 81-100
GL programs in 11-12 s, each 90-120 ms, and the guest waited through every one (3-5 FPS while Crash runs, back to ~20
when he stops or walks into a wall). The 168 programs compiled in one such run were all different, but only 20
differed once numbers were ignored: the indirect matrix (BP 0x06-0x0E, three 2x3 matrices with a scale exponent) was
written into the GLSL as literal coefficients, and the game moves that matrix with the camera, so each frame produced
a new source, a cache miss and a synchronous compile. The program binary cache cannot help: the values never repeat.

This stage makes the coefficients and the exponent uniforms, the way NATIVE_MOM_SHADER_DATA_UNIFORMS_V1 already does
for fog and the alpha reference:

  * tev_shader_gen.cpp declares `uniform vec4 uNativeIndMtx[6]` (matrix m, row r at index m*2+r: xyz = the signed
    11-bit coefficients, w = exponent-17, the same values the literals held) and reads it in the static-matrix and
    the dynamic S/T forms. The arithmetic is unchanged: coefficient/1024, then exp2(exponent-17).
  * renderer_gl.cpp looks the location up wherever a program is created or loaded, and uploads the six vectors in
    SetupDrawState next to the fog data, through the same per-program value cache. Any BP write that changes a value
    already closes the open batch (NativeBatchContinues), so a matrix change cannot retro-edit a pending draw.

The uniforms follow the shader-data switch: NWII_LITERAL_SHADER_DATA=1 restores the literal form for an A/B check, and
NWII_LITERAL_IND_MTX=1 restores only this part.

Edits the vendored runtime and mirrors the two files into export/nWiiRuntime. All anchors are checked before
anything is written. Idempotent; --restore removes every edit byte for byte.

    python tools/stages/fix_ind_mtx_uniforms_mom.py [--restore] [--root <dir holding nWiiRuntime/>]
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_IND_MTX_UNIFORMS_V1'
ROOT = Path(__file__).resolve().parents[2]
ROOTS = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', ROOT / 'export/nWiiRuntime']
CRLF, LF = '\r\n', '\n'


class Skip(Exception):
    pass


GEN = 'src/hle/gx/tev_shader_gen.cpp'
REN = 'src/hle/gx/renderer_gl.cpp'

EDITS = {
    GEN: [
        # the switch, next to the shader-data one
        ('#include "runtime/native_shader_data.h" // NATIVE_MOM_SHADER_DATA_UNIFORMS_V1\n',
         '#include "runtime/native_shader_data.h" // NATIVE_MOM_SHADER_DATA_UNIFORMS_V1\n'
         '#include <cstdlib> // ' + MARK + '\n'
         'namespace nwii::runtime::gx { // ' + MARK + ': indirect matrices ride uNativeIndMtx unless the literal form is asked for\n'
         'bool NativeIndMtxUniforms() { static const bool on = NativeShaderDataUniformsEnabled() && std::getenv("NWII_LITERAL_IND_MTX") == nullptr; return on; }\n'
         '}\n'),
        # static matrix rows
        ('''        for(unsigned row=0;row<2;++row) {
          output << "    indOffset[" << row << "]=dot(vec3(";
          for(unsigned col=0;col<3;++col) {
            if(col) output << ",";
            output << coefficient(state.bp[base+col]>>(row*11)) << ".0";
          }
          output << "),indValue)/1024.0;\\n";
        }''',
         '''        for(unsigned row=0;row<2;++row) {
          if(NativeIndMtxUniforms()) { // ''' + MARK + '''
            output << "    indOffset[" << row << "]=dot(uNativeIndMtx[" << (matrix-1)*2+row << "].xyz,indValue)/1024.0;\\n";
            continue;
          }
          output << "    indOffset[" << row << "]=dot(vec3(";
          for(unsigned col=0;col<3;++col) {
            if(col) output << ",";
            output << coefficient(state.bp[base+col]>>(row*11)) << ".0";
          }
          output << "),indValue)/1024.0;\\n";
        }'''),
        # scale exponent
        ('''      output << "    indOffset*=exp2(" << int(exponent)-17 << ".0);\\n";''',
         '''      if(NativeIndMtxUniforms()) output << "    indOffset*=exp2(uNativeIndMtx[" << (matrix-1)*2 << "].w);\\n"; // ''' + MARK + '''
      else output << "    indOffset*=exp2(" << int(exponent)-17 << ".0);\\n";'''),
        # declaration
        ('''    fs << "uniform vec4 uTevKColor[4];\\n";\n''',
         '''    fs << "uniform vec4 uTevKColor[4];\\n";\n'''
         '''    if (NativeIndMtxUniforms()) fs << "uniform vec4 uNativeIndMtx[6];\\n"; // ''' + MARK + '\n'),
    ],
    REN: [
        ('  GLint uNativeFogData=-1,uNativeFogColor=-1,uNativeAlphaRef=-1; // NATIVE_MOM_SHADER_DATA_UNIFORMS_V1\n',
         '  GLint uNativeFogData=-1,uNativeFogColor=-1,uNativeAlphaRef=-1; // NATIVE_MOM_SHADER_DATA_UNIFORMS_V1\n'
         '  GLint uNativeIndMtx=-1; // ' + MARK + '\n'),
        ('    sh.uNativeAlphaRef=glGetUniformLocation(sh.id,"uNativeAlphaRef");\n',
         '    sh.uNativeAlphaRef=glGetUniformLocation(sh.id,"uNativeAlphaRef");\n'
         '    sh.uNativeIndMtx=glGetUniformLocation(sh.id,"uNativeIndMtx"); // ' + MARK + '\n'),
        ('''      glUniform2fv(s_active_shader.uNativeAlphaRef, 1, alpharef);
  }
}
''',
         '''      glUniform2fv(s_active_shader.uNativeAlphaRef, 1, alpharef);
  }
  // ''' + MARK + ''': the three indirect matrices (BP 0x06-0x0E) as the generator's literals were: signed 11-bit
  // coefficients and the scale exponent minus 17. A changed register already closed the batch.
  if (s_active_shader.uNativeIndMtx >= 0) {
    float ind[24];
    for (unsigned m = 0; m < 3; ++m) {
      const unsigned base = 6 + m * 3;
      const unsigned exponent = ((g_state.bp[base] >> 22) & 3) | (((g_state.bp[base + 1] >> 22) & 3) << 2) |
                                (((g_state.bp[base + 2] >> 22) & 3) << 4);
      for (unsigned row = 0; row < 2; ++row) {
        float* v = ind + (m * 2 + row) * 4;
        for (unsigned col = 0; col < 3; ++col) {
          const int bits = int((g_state.bp[base + col] >> (row * 11)) & 2047);
          v[col] = float(bits >= 1024 ? bits - 2048 : bits);
        }
        v[3] = float(int(exponent) - 17);
      }
    }
    if (native_uniform_cache::Changed(s_active_shader.id, s_active_shader.uNativeIndMtx, ind, sizeof ind))
      glUniform4fv(s_active_shader.uNativeIndMtx, 6, ind);
  }
}
'''),
    ],
}
# Declaration of the switch for the renderer TU is not needed: the renderer only tests the uniform location.
GEN_DECL_OLD = 'namespace nwii::runtime::gx {\n\n// NATIVE_INDIRECT_V1'
GEN_DECL_NEW = 'namespace nwii::runtime::gx {\nbool NativeIndMtxUniforms(); // ' + MARK + '\n\n// NATIVE_INDIRECT_V1'
EDITS[GEN].append((GEN_DECL_OLD, GEN_DECL_NEW))


def read(path):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    return raw.replace(CRLF, LF), CRLF in raw


def write(path, text, crlf):
    path.write_bytes((text.replace(LF, CRLF) if crlf else text).encode('utf-8', errors='surrogateescape'))


def transform(rel, text, restore):
    if restore:
        if MARK not in text:
            return text
        for old, new in reversed(EDITS[rel]):
            if text.count(new) != 1:
                raise Skip('%s: applied text not found once: %r' % (rel, new.strip()[:70]))
            text = text.replace(new, old, 1)
        if MARK in text:
            raise Skip(rel + ': marker left after restore')
        return text
    if MARK in text:
        return text
    for old, new in EDITS[rel]:
        if text.count(old) != 1:
            raise Skip('%s: expected one occurrence of %r, found %d' % (rel, old.strip()[:70], text.count(old)))
        text = text.replace(old, new, 1)
    return text


def main(argv):
    restore = '--restore' in argv
    roots = ROOTS
    if '--root' in argv:
        roots = [Path(argv[argv.index('--root') + 1]) / 'nWiiRuntime']
    plan = []
    for base in roots:
        if not base.is_dir():
            continue
        for rel in EDITS:
            path = base / rel
            if not path.is_file():
                raise Skip('missing ' + str(path))
            text, crlf = read(path)
            new = transform(rel, text, restore)
            if new != text:
                plan.append((path, new, crlf))
    for path, text, crlf in plan:
        write(path, text, crlf)
    print('Indirect matrix uniforms %s: %d file(s) changed.' % ('removed' if restore else 'installed', len(plan)))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Skip as reason:
        print('fix_ind_mtx_uniforms_mom: nothing written: ' + str(reason))
        sys.exit(1)
