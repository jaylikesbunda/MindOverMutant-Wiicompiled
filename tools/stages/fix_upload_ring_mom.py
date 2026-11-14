"""Upload draw data into a ring instead of reallocating a buffer every draw.

``FlushBatch`` calls ``glBufferData`` twice for every submission - once for the
vertices (about 17 KB at MoM's measured 74 draws and 1.4 MB per frame) and once
for the indices.  ``glBufferData`` on a live buffer makes the driver allocate new
storage, invalidate the old one and copy; doing that ~170 times a frame is one of
the reasons the CPU profile puts 25-45 % of the host thread inside
``nvoglv64.dll`` and 8-11 % in ``ucrtbase`` (allocation/copy).

A ring keeps one buffer per target, appends with ``glBufferSubData`` at an
aligned offset and orphans the whole buffer only when it wraps.  The draw then
starts at the ring offset: a base vertex for ``glDrawArrays`` and
``glDrawElementsBaseVertex``.  Both entry points are GL 3.2 core and present in
this tree's glad, so nothing new is required.

The ring is 64 MiB per target.  That matters: the submission is asynchronous, so
a ring small enough to wrap while the GPU is still reading an earlier frame would
overwrite in-flight vertices and produce flickering or missing geometry.  At the
measured ~1.4 MB of vertices per frame, 64 MiB is roughly 45 frames of slack -
far beyond any plausible driver lag - so a wrap only ever reuses storage the GPU
finished with long ago, and no fence is needed.

``NWII_NO_STREAM_UPLOAD=1`` restores the two ``glBufferData`` calls exactly.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_UPLOAD_RING_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('upload ring already present.')
    raise SystemExit(0)


def sub(old, new, what):
    global text
    assert text.count(old) == 1, 'anchor %s' % what
    text = text.replace(old, new, 1)


helper_anchor = """static void FlushBatch(native_batch_census::Reason reason ="""
helper = '''// ''' + MARK + ''': append-only vertex/index rings.  One glBufferSubData per
// submission; the buffer is orphaned when the ring wraps, not per draw.
struct NativeUploadRing {
  size_t capacity = 0;
  size_t cursor = 0;
  unsigned orphans = 0;
};
static NativeUploadRing s_vertex_ring, s_index_ring;

static const size_t kNativeRingBytes = 64u << 20;

// Returns the byte offset the draw must start at, or SIZE_MAX when the caller
// should fall back to the legacy glBufferData path.
static size_t NativeRingUpload(GLenum target, NativeUploadRing &ring,
                               const void *data, size_t bytes, size_t align) {
  static const bool disabled = std::getenv("NWII_NO_STREAM_UPLOAD") != nullptr;
  if (disabled || bytes == 0 || bytes > kNativeRingBytes) return SIZE_MAX;
  if (ring.capacity != kNativeRingBytes) {
    glBufferData(target, GLsizeiptr(kNativeRingBytes), nullptr, GL_STREAM_DRAW);
    ring.capacity = kNativeRingBytes;
    ring.cursor = 0;
  }
  const size_t a = align ? align : 4;
  size_t off = (ring.cursor + a - 1) / a * a;
  if (off + bytes > ring.capacity) {
    glBufferData(target, GLsizeiptr(ring.capacity), nullptr, GL_STREAM_DRAW);
    ++ring.orphans;
    off = 0;
  }
  glBufferSubData(target, GLintptr(off), GLsizeiptr(bytes), data);
  ring.cursor = off + bytes;
  return off;
}

''' + helper_anchor
sub(helper_anchor, helper, 'FlushBatch definition')

sub("""  glBindVertexArray(s_vao);
  glBindBuffer(GL_ARRAY_BUFFER, s_vbo);
  glBufferData(GL_ARRAY_BUFFER, s_batch.size() * sizeof(BatchVertex),
               s_batch.data(), GL_STREAM_DRAW);""",
    """  glBindVertexArray(s_vao);
  glBindBuffer(GL_ARRAY_BUFFER, s_vbo);
  const size_t native_vertex_bytes = s_batch.size() * sizeof(BatchVertex);
  size_t native_vertex_off = NativeRingUpload(
      GL_ARRAY_BUFFER, s_vertex_ring, s_batch.data(), native_vertex_bytes,
      sizeof(BatchVertex));
  if (native_vertex_off == SIZE_MAX) {
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 s_batch.data(), GL_STREAM_DRAW);
    native_vertex_off = 0;
  }
  const GLint native_base_vertex =
      GLint(native_vertex_off / sizeof(BatchVertex));""",
    'vertex upload')

sub("""  if (s_indices.empty()) {
    glDrawArrays(GL_TRIANGLES, 0, s_batch.size());
  } else {
  glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s_ebo);
  glBufferData(GL_ELEMENT_ARRAY_BUFFER, s_indices.size()*sizeof(uint32_t),
               s_indices.data(), GL_STREAM_DRAW);
  glDrawElements(GL_TRIANGLES, GLsizei(s_indices.size()), GL_UNSIGNED_INT, nullptr);
  }""",
    """  if (s_indices.empty()) {
    glDrawArrays(GL_TRIANGLES, native_base_vertex, GLsizei(s_batch.size()));
  } else {
  glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s_ebo);
  const size_t native_index_bytes = s_indices.size() * sizeof(uint32_t);
  size_t native_index_off = NativeRingUpload(
      GL_ELEMENT_ARRAY_BUFFER, s_index_ring, s_indices.data(),
      native_index_bytes, alignof(uint32_t));
  if (native_index_off == SIZE_MAX) {
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, GLsizeiptr(native_index_bytes),
                 s_indices.data(), GL_STREAM_DRAW);
    native_index_off = 0;
  }
  glDrawElementsBaseVertex(GL_TRIANGLES, GLsizei(s_indices.size()),
                           GL_UNSIGNED_INT,
                           (void *)(uintptr_t)native_index_off,
                           native_base_vertex);
  }""",
    'index upload and draw')

path.write_text(text)
print('Draw uploads use a ring; buffers are orphaned on wrap, not per draw.')
