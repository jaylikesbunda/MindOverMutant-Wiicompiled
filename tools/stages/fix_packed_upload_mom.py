"""Compact post-decode batch upload for FlushBatch (MoM).

Marker: NATIVE_MOM_PACKED_VERTICES_V1.

Installs overlays/native_packed_vertices_mom.h as
include/runtime/native_packed_vertices_mom.h and rewires ONLY FlushBatch:

* NWII_PACKED_VERTICES=1 opts in; NWII_NO_PACKED_VERTICES always wins.  Per
  batch, NativePackBatch qualifies the float s_batch; on success the upload
  uses the 136-byte packed rows, otherwise the original 160-byte BatchVertex
  rows - this is a repack of decoded vertices, not a direct packed decoder.
* NativeConfigureBatchLayout rebinds the attribute pointers whenever the bound
  buffer or the layout changes (byte colours + stride 136 for packed, float +
  160 otherwise), so a packed draw never leaves compact attributes behind for
  a float draw or vice versa.
* The ring upload, byte count, alignment guard and base-vertex division use
  the selected stride; the draw archive and every other sizeof(BatchVertex)
  use are untouched.

Idempotent; --restore removes its own insertions only (the installed header is
left inert).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_PACKED_VERTICES_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

OVERLAY = ROOT / 'overlays/native_packed_vertices_mom.h'
RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'
HDR = RUNTIME + '/include/runtime/native_packed_vertices_mom.h'

INCLUDE_LINE = '#include "runtime/native_packed_vertices_mom.h" // ' + MARKER + '\n'

PAIRS = [
    ('''#include "runtime/native_shader_data.h" // NATIVE_MOM_SHADER_DATA_UNIFORMS_V1
''',
     '''#include "runtime/native_shader_data.h" // NATIVE_MOM_SHADER_DATA_UNIFORMS_V1
''' + INCLUDE_LINE),

    ('''static void FlushBatch(native_batch_census::Reason reason =
                           native_batch_census::Other) {
''',
     '''// ''' + MARKER + ''': opt-in compact layout.  Default off; the kill
// switch wins over the opt-in.
static const bool s_packed_wanted = [] {
  const char* e = std::getenv("NWII_PACKED_VERTICES");
  return e && e[0] == '1' && !e[1] &&
         std::getenv("NWII_NO_PACKED_VERTICES") == nullptr;
}();
static std::vector<NativePackedBatchVertex> s_packed_batch; // reused, no per-draw alloc
static GLuint s_attrib_buffer = 0;
static int s_attrib_layout = -1; // -1 unset, 0 float(160), 1 packed(136)

// ''' + MARKER + ''': (re)point the attribute pointers for the bound buffer's
// layout.  Locations: 0 pos3F, 1 colour (byte+normalised packed / float),
// 2 uv2F, 3 normal3F, 4 colour1 same, 5..12 texgen3F.  The shadow guards the
// reprogram; a layout or buffer swap always reprograms.
static void NativeConfigureBatchLayout(GLuint buffer, bool packed) {
  const int layout = packed ? 1 : 0;
  if (s_attrib_buffer == buffer && s_attrib_layout == layout) return;
  s_attrib_buffer = buffer;
  s_attrib_layout = layout;
  glBindBuffer(GL_ARRAY_BUFFER, buffer);
  const GLsizei stride = GLsizei(packed ? sizeof(NativePackedBatchVertex)
                                        : sizeof(BatchVertex));
  const GLenum ctype = packed ? GL_UNSIGNED_BYTE : GL_FLOAT;
  const GLboolean norm = packed ? GL_TRUE : GL_FALSE;
  const size_t posoff  = packed ? offsetof(NativePackedBatchVertex, position)
                                : offsetof(BatchVertex, x);
  const size_t col0off = packed ? offsetof(NativePackedBatchVertex, color0)
                                : offsetof(BatchVertex, r);
  const size_t uvoff   = packed ? offsetof(NativePackedBatchVertex, uv)
                                : offsetof(BatchVertex, u);
  const size_t nrmoff  = packed ? offsetof(NativePackedBatchVertex, normal)
                                : offsetof(BatchVertex, nx);
  const size_t col1off = packed ? offsetof(NativePackedBatchVertex, color1)
                                : offsetof(BatchVertex, color1);
  const size_t tgoff   = packed ? offsetof(NativePackedBatchVertex, texgen)
                                : offsetof(BatchVertex, texgen);
  glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride, (void *)posoff);
  glVertexAttribPointer(1, 4, ctype, norm, stride, (void *)col0off);
  glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride, (void *)uvoff);
  glVertexAttribPointer(3, 3, GL_FLOAT, GL_FALSE, stride, (void *)nrmoff);
  glVertexAttribPointer(4, 4, ctype, norm, stride, (void *)col1off);
  for (unsigned t = 0; t < 8; ++t)
    glVertexAttribPointer(5 + t, 3, GL_FLOAT, GL_FALSE, stride,
                          (void *)(tgoff + t * 12));
}

static void FlushBatch(native_batch_census::Reason reason =
                           native_batch_census::Other) {
'''),

    ('''  NativeBindVertexArray(s_vao);
  glBindBuffer(GL_ARRAY_BUFFER, s_vbo);
  const size_t native_vertex_bytes = s_batch.size() * sizeof(BatchVertex);
  size_t native_vertex_off = NativeRingUpload(
      GL_ARRAY_BUFFER, s_vertex_ring, s_batch.data(), native_vertex_bytes,
      sizeof(BatchVertex));
  if (native_vertex_off == SIZE_MAX) {
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 s_batch.data(), GL_STREAM_DRAW);
    native_vertex_off = 0;
  }''',
     '''  NativeBindVertexArray(s_vao);
  glBindBuffer(GL_ARRAY_BUFFER, s_vbo);
  // ''' + MARKER + ''': pick the compact repack when it qualifies; the float
  // batch is the fallback.  The buffer, stride and attribute layout move
  // together.
  const bool native_packed_layout =
      s_packed_wanted && NativePackBatch(s_batch, s_packed_batch);
  const void* native_vertex_data = native_packed_layout
      ? (const void*)s_packed_batch.data() : (const void*)s_batch.data();
  const size_t native_vertex_stride = native_packed_layout
      ? sizeof(NativePackedBatchVertex) : sizeof(BatchVertex);
  NativeConfigureBatchLayout(s_vbo, native_packed_layout);
  const size_t native_vertex_bytes = s_batch.size() * native_vertex_stride;
  size_t native_vertex_off = NativeRingUpload(
      GL_ARRAY_BUFFER, s_vertex_ring, native_vertex_data, native_vertex_bytes,
      native_vertex_stride);
  if (native_vertex_off == SIZE_MAX) {
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 native_vertex_data, GL_STREAM_DRAW);
    native_vertex_off = 0;
  }'''),

    ('''  if (NativeRingGuard() &&
      (native_vertex_off % sizeof(BatchVertex)) != 0) {
    ++s_native_ring_untiled;
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 s_batch.data(), GL_STREAM_DRAW);
    NativeRingReset(s_vertex_ring);
    native_vertex_off = 0;
  }
  const GLint native_base_vertex =
      GLint(native_vertex_off / sizeof(BatchVertex));''',
     '''  if (NativeRingGuard() &&
      (native_vertex_off % native_vertex_stride) != 0) {
    ++s_native_ring_untiled;
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 native_vertex_data, GL_STREAM_DRAW);
    NativeRingReset(s_vertex_ring);
    native_vertex_off = 0;
  }
  const GLint native_base_vertex =
      GLint(native_vertex_off / native_vertex_stride);'''),
]


def read(path):
    return Path(path).read_text(encoding='utf-8', errors='surrogateescape')


def write(path, text, orig):
    data = text.replace('\n', '\r\n') if '\r\n' in orig else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as h:
        h.write(data)


def apply_pairs(text, pairs, tag):
    for old, new in pairs:
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:60]))
        text = text.replace(old, new, 1)
    return text


def restore_pairs(text, pairs, tag):
    for old, new in reversed(pairs):
        found = text.count(new)
        if found == 0:
            continue
        if found != 1:
            raise SystemExit('%s: %s restore matched %d times (expected 1)'
                             % (MARKER, tag, found))
        text = text.replace(new, old, 1)
    return text


def _strip_include(text):
    # our include can sit between another stage's include anchors; remove it
    # anywhere before the paired sub runs so restore is not wedged by ordering.
    return '\n'.join(ln for ln in text.split('\n')
                     if 'native_packed_vertices_mom.h' not in ln)


def apply(root):
    gl = root / GL
    orig = gl.read_bytes().decode('utf-8', errors='surrogateescape')
    text = orig.replace('\r\n', '\n')
    if INCLUDE_LINE.strip() in text:
        text = _strip_include(text)  # dedupe before re-anchor
    updated = apply_pairs(restore_pairs(text, PAIRS, GL), PAIRS, GL)
    if updated != orig:
        write(gl, updated, orig)
    hdr = root / HDR
    overlay_path = root / 'overlays/native_packed_vertices_mom.h'
    overlay_text = overlay_path.read_text(encoding='utf-8', errors='surrogateescape')
    if not hdr.is_file() or hdr.read_bytes() != overlay_path.read_bytes():
        hdr.parent.mkdir(parents=True, exist_ok=True)
        hdr.write_bytes(overlay_text.encode('utf-8'))
    print('%s staged: %s installed; NWII_PACKED_VERTICES=1 opts in '
          '(NWII_NO_PACKED_VERTICES wins)' % (MARKER, HDR))


def restore(root):
    gl = root / GL
    if gl.is_file():
        orig = gl.read_bytes().decode('utf-8', errors='surrogateescape')
        text = _strip_include(orig.replace('\r\n', '\n'))
        updated = restore_pairs(text, PAIRS, GL)
        if updated != orig:
            write(gl, updated, orig)
    print('%s restored: %s (the installed header stays; it is inert once '
          'nothing includes it)' % (MARKER, GL))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
