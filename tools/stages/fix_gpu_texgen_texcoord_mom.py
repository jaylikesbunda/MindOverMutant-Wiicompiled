r"""Keep texcoord-sourced texgen on the GPU (MoM; completes CotT fix_gpu_vertex_pipeline's "regular texgen in the vertex shader").

Marker: NATIVE_MOM_GPU_TEXGEN_TEXCOORD_V1.

NativeGpuTexgenLanes put the whole batch back on the CPU texgen (NativeTexGen per vertex, 11.6% of the GL thread in profile
glprof, plus the heavier CPU vertex record) whenever any generator takes a texture coordinate as its input (XF texinfo
source 5..12), which is the most common generator in the game (TEXn through a texture matrix). Its comment assumed the CPU
chains generators (reads another lane's *generated* coordinate). It does not: NativeTexGen reads vertex.tex[source-5], the
raw decoded coordinate, and on the GPU path NativeWriteConvertedVertex hands the shader every lane's raw coordinate
(texgen[k] = {tex[k].u, tex[k].v, texMtxIdx[k]} for all eight k), and gxTexgen reads inputs[source-5].xy - the same value. So
the texcoord source is as exact on the GPU as position, normal and colour already were, and the rejection is dropped.
NWII_NO_GPU_TEXGEN=1 still forces every lane to the CPU.

Edits src/hle/gx/renderer_gl.cpp in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_GPU_TEXGEN_TEXCOORD_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/renderer_gl.cpp',
         ROOT / 'export/nWiiRuntime/src/hle/gx/renderer_gl.cpp']
OLD = ('  const unsigned lanes = NativeTexGenLanes(state);\n'
       '  for (unsigned t = 0; t < lanes && t < 8; ++t) {\n'
       '    const unsigned source = (state.xf[0x40 + t] >> 7) & 31;\n'
       '    if (source >= 5 && source <= 12) return 0;\n'
       '  }\n'
       '  return lanes;\n')
NEW = ('  // ' + MARK + ': a texcoord source (5..12) reads the raw coordinate on both paths, so it stays on the GPU.\n'
       '  return NativeTexGenLanes(state);\n')


def main():
    restore = '--restore' in sys.argv
    for p in FILES:
        raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
        crlf = '\r\n' in raw
        t = raw.replace('\r\n', '\n')
        a, b = (NEW, OLD) if restore else (OLD, NEW)
        if b in t:
            continue
        if t.count(a) != 1:
            print('fix_gpu_texgen_texcoord_mom: anchor not found once in %s' % p); sys.exit(1)
        t = t.replace(a, b, 1)
        p.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
    print('gpu texgen texcoord %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
