"""Add the quad's texture coordinates to the draw ring.

A flat wash over exactly the movie area, from a texture that is itself detailed,
means every fragment sampled the same texel: the draw's texture coordinates
collapsed. This records the first two vertices' position and first texture
coordinate, so a capture shows whether the UVs were degenerate (all equal) or
whether the quad was simply small/offset.

Recording only: four floats per draw.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
renderer = port / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/renderer_gl.cpp'
MARK = 'NATIVE_DRAW_RING_UV_V1'
text = renderer.read_text()
if MARK in text:
    print('UV recording already present.')
    raise SystemExit(0)

old_fields = """  uint32_t t0, gen, blend, zmode, texmask, rgba;
};"""
assert text.count(old_fields) == 1, 'record fields anchor'
text = text.replace(old_fields, """  uint32_t t0, gen, blend, zmode, texmask, rgba;
  float uv0[2], uv1[2], pos0[3], pos1[3]; // """ + MARK + """
  int has_uv;
};""", 1)

old_record = """          r.rgba = g_state.bp[0xc0]; // TEV colour 0 low: a flat-colour quad's output
        }"""
assert text.count(old_record) == 1, 'record site anchor'
text = text.replace(old_record, """          r.rgba = g_state.bp[0xc0]; // TEV colour 0 low: a flat-colour quad's output
          r.has_uv = cmd.vertices.size() > 1 && cmd.vertices[0].has_tex[0] ? 1 : 0;
          for (int k = 0; k < 2; ++k) {
            const bool have = cmd.vertices.size() > size_t(k);
            r.uv0[k] = have ? cmd.vertices[0].tex[0][k] : 0.f;
            r.uv1[k] = have ? cmd.vertices[cmd.vertices.size() > 1 ? 1 : 0].tex[0][k] : 0.f;
            r.pos0[k] = have ? cmd.vertices[0].pos[k] : 0.f;
            r.pos1[k] = have ? cmd.vertices[cmd.vertices.size() > 1 ? 1 : 0].pos[k] : 0.f;
          }
          r.pos0[2] = cmd.vertices.size() > 0 ? cmd.vertices[0].pos[2] : 0.f;
          r.pos1[2] = cmd.vertices.size() > 1 ? cmd.vertices[1].pos[2] : 0.f;
        }""", 1)

old_dump = """              << " clr0=0x" << r.rgba << std::dec << std::endl;"""
assert text.count(old_dump) == 1, 'dump anchor'
text = text.replace(old_dump, """              << " clr0=0x" << r.rgba << std::dec << " uv=" << r.uv0[0] << ","
              << r.uv0[1] << " -> " << r.uv1[0] << "," << r.uv1[1]
              << " hasuv=" << r.has_uv << " pos=" << r.pos0[0] << "," << r.pos0[1]
              << "," << r.pos0[2] << " -> " << r.pos1[0] << "," << r.pos1[1]
              << "," << r.pos1[2] << std::endl;""", 1)
renderer.write_text(text)
print('Draw ring records texture coordinates.')
