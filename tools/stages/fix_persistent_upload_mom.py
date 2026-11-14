"""Persistent, coherently mapped segmented upload rings (MoM).

Marker: NATIVE_MOM_PERSISTENT_UPLOAD_V1.

Installs overlays/native_persistent_upload_mom.h as
include/runtime/native_persistent_upload_mom.h, defines the two rings
(vertex + index, separate objects and fences) and the render-thread resolver
and release hook in renderer_gl.cpp, rewires ONLY the FlushBatch upload sites,
and calls nwii::runtime::NativePersistentReleaseAll() in main.cpp immediately
before SDL_GL_DeleteContext.

NWII_PERSISTENT_UPLOAD=1 opts in (exact '1'); NWII_NO_PERSISTENT_UPLOAD wins.
Without ARB_buffer_storage / GL 4.4 the resolver leaves the function table
incomplete and every upload falls back to the existing orphan/glBufferData
ring - same offsets, same buffers, same fences semantics as before.

Idempotent; --restore removes its own insertions only (installed header left
inert).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_PERSISTENT_UPLOAD_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'
MAIN = RUNTIME + '/src/core/main.cpp'
HDR = RUNTIME + '/include/runtime/native_persistent_upload_mom.h'

INCLUDE_LINE = '#include "runtime/native_persistent_upload_mom.h" // ' + MARKER + '\n'

DECLS = '''// ''' + MARKER + ''': opt-in persistent upload.  Strict '1' only.
static const bool s_persist_wanted = [] {
  const char* e = std::getenv("NWII_PERSISTENT_UPLOAD");
  return e && e[0] == '1' && !e[1] &&
         std::getenv("NWII_NO_PERSISTENT_UPLOAD") == nullptr;
}();
} // namespace nwii::runtime::gx -- ring state lives at nwii::runtime scope
namespace nwii::runtime {
NativePersistentGL g_persistent_gl;
NativePersistentRing g_persist_vertex; // vertex uploads - own buffer/fences
NativePersistentRing g_persist_index;  // index uploads - own buffer/fences
void NativePersistentReleaseAll() {
  // ONE glFinish across both rings, then each unmaps/deletes what it owns -
  // the second call skips its own Finish so shutdown costs one sync, not two.
  const bool live = g_persist_vertex.buffer() || g_persist_index.buffer();
  g_persist_vertex.Release(live);
  g_persist_index.Release(false);
}
} // namespace nwii::runtime
namespace nwii::runtime::gx {
static bool s_persist_gl_done = false;
static bool s_persist_gl_avail = false;
// ''' + MARKER + ''': buffer_storage exists on GL 4.4+ or when the driver
// reports GL_ARB_buffer_storage; probing the address alone would also accept a
// 3.3 context that exports the symbol but cannot honour the flags.
static bool NativePersistentAvailable() {
  if (s_persist_gl_done) return s_persist_gl_avail;
  s_persist_gl_done = true;
  GLint major = 0, minor = 0, next = 0;
  glGetIntegerv(GL_MAJOR_VERSION, &major);
  glGetIntegerv(GL_MINOR_VERSION, &minor);
  bool have = major > 4 || (major == 4 && minor >= 4);
  if (!have) {
    glGetIntegerv(GL_NUM_EXTENSIONS, &next);
    for (GLint i = 0; i < next && !have; ++i) {
      const char* ext = (const char*)glGetStringi(GL_EXTENSIONS, GLuint(i));
      if (ext && std::strcmp(ext, "GL_ARB_buffer_storage") == 0) have = true;
    }
  }
  return s_persist_gl_avail = have;
}
static void NativePersistentResolve() {
  if (s_persist_gl_done && s_persist_gl_avail == false) { /* resolved already */ }
  static bool resolved = false;
  if (resolved) return;
  resolved = true;
  if (!NativePersistentAvailable()) return; // table stays incomplete -> fallback
  using nwii::runtime::g_persistent_gl;
  // Typed non-capturing adapters: each wrapper calls the real glad symbol at
  // its declared signature and converts GLsync<->uintptr_t, GLintptr/
  // GLsizeiptr offsets and GLboolean explicitly - no ABI-unsafe casts.
  g_persistent_gl.GenBuffers = +[](int n, unsigned int* o) {
    glGenBuffers(n, (GLuint*)o); };
  g_persistent_gl.DeleteBuffers = +[](int n, const unsigned int* o) {
    glDeleteBuffers(n, (const GLuint*)o); };
  g_persistent_gl.BindBuffer = +[](unsigned int t, unsigned int b) {
    glBindBuffer((GLenum)t, (GLuint)b); };
  g_persistent_gl.BufferStorage = +[](unsigned int t, long long bytes,
                                    const void* d, unsigned int f) {
    // GL3.3 glad has no PFNGLBUFFERSTORAGEPROC; declare the exact 4.4/ARB
    // signature (x64 calling convention is uniform for this project).
    typedef void (*PFNBufferStorageMom)(GLenum, GLsizeiptr, const void*,
                                      GLbitfield);
    static const PFNBufferStorageMom fn =
        (PFNBufferStorageMom)SDL_GL_GetProcAddress("glBufferStorage");
    if (fn) fn((GLenum)t, (GLsizeiptr)bytes, d, (GLbitfield)f); };
  g_persistent_gl.MapBufferRange = +[](unsigned int t, long long off,
                                     long long len, unsigned int a) -> void* {
    return glMapBufferRange((GLenum)t, (GLintptr)off, (GLsizeiptr)len,
                          (GLbitfield)a); };
  g_persistent_gl.FenceSync = +[](unsigned int c, unsigned int f)
      -> unsigned long long {
    return (unsigned long long)(uintptr_t)glFenceSync((GLenum)c, (GLbitfield)f); };
  g_persistent_gl.ClientWaitSync = +[](unsigned long long s, unsigned int fl,
                                     unsigned long long to) -> unsigned int {
    return (unsigned int)glClientWaitSync((GLsync)(uintptr_t)s, (GLbitfield)fl,
                                        (GLuint64)to); };
  g_persistent_gl.DeleteSync = +[](unsigned long long s) {
    glDeleteSync((GLsync)(uintptr_t)s); };
  g_persistent_gl.UnmapBuffer = +[](unsigned int t) -> int {
    return (int)glUnmapBuffer((GLenum)t); };
  g_persistent_gl.Finish = +[]() { glFinish(); };
  g_persistent_gl.GetError = +[]() { return (unsigned int)glGetError(); };
}

'''

# The DECLS block emitted by the previous revision of this stage (raw function
# -pointer casts, no availability probe).  apply()/restore() normalise it to
# DECLS before the paired subs run so an already-staged tree round-trips.
DECLS_PREV = '''// ''' + MARKER + ''': opt-in persistent upload.  Strict '1' only.
static const bool s_persist_wanted = [] {
  const char* e = std::getenv("NWII_PERSISTENT_UPLOAD");
  return e && e[0] == '1' && !e[1] &&
         std::getenv("NWII_NO_PERSISTENT_UPLOAD") == nullptr;
}();
} // namespace nwii::runtime::gx -- ring state lives at nwii::runtime scope
namespace nwii::runtime {
NativePersistentGL g_persistent_gl;
NativePersistentRing g_persist_vertex; // vertex uploads - own buffer/fences
NativePersistentRing g_persist_index;  // index uploads - own buffer/fences
void NativePersistentReleaseAll() {
  g_persist_vertex.Release();
  g_persist_index.Release();
}
} // namespace nwii::runtime
namespace nwii::runtime::gx {
static bool s_persist_gl_done = false;
static void NativePersistentResolve() {
  if (s_persist_gl_done) return;
  s_persist_gl_done = true;
  using nwii::runtime::g_persistent_gl;
  g_persistent_gl.GenBuffers   = (void(*)(int,unsigned int*))glGenBuffers;
  g_persistent_gl.DeleteBuffers= (void(*)(int,const unsigned int*))glDeleteBuffers;
  g_persistent_gl.BindBuffer   = (void(*)(unsigned int,unsigned int))glBindBuffer;
  // GL 4.4 / ARB_buffer_storage only - resolve dynamically so a 3.3 context
  // simply leaves it null and the ring reports unsupported.
  g_persistent_gl.BufferStorage= (void(*)(unsigned int,long long,const void*,unsigned int))
                                 SDL_GL_GetProcAddress("glBufferStorage");
  g_persistent_gl.MapBufferRange=(void*(*)(unsigned int,long long,long long,unsigned int))glMapBufferRange;
  g_persistent_gl.FenceSync    = (unsigned long long(*)(unsigned int,unsigned int))glFenceSync;
  g_persistent_gl.ClientWaitSync=(unsigned int(*)(unsigned long long,unsigned int,unsigned long long))glClientWaitSync;
  g_persistent_gl.DeleteSync   = (void(*)(unsigned long long))glDeleteSync;
  g_persistent_gl.UnmapBuffer  = (int(*)(unsigned int))glUnmapBuffer;
  g_persistent_gl.Finish       = (void(*)())glFinish;
  g_persistent_gl.GetError     = (unsigned int(*)())glGetError;
}

'''

GL_PAIRS = [
    ('''#include "runtime/native_packed_vertices_mom.h" // NATIVE_MOM_PACKED_VERTICES_V1
''',
     '''#include "runtime/native_packed_vertices_mom.h" // NATIVE_MOM_PACKED_VERTICES_V1
''' + INCLUDE_LINE),

    ('''static void NativeConfigureBatchLayout(GLuint buffer, bool packed) {
''',
     DECLS + '''static void NativeConfigureBatchLayout(GLuint buffer, bool packed) {
'''),

    ('''  const size_t native_vertex_bytes = s_batch.size() * native_vertex_stride;
  size_t native_vertex_off = NativeRingUpload(
      GL_ARRAY_BUFFER, s_vertex_ring, native_vertex_data, native_vertex_bytes,
      native_vertex_stride);
  if (native_vertex_off == SIZE_MAX) {
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 native_vertex_data, GL_STREAM_DRAW);
    native_vertex_off = 0;
  }
  // NATIVE_MOM_RENDER_STATE_SAFE_V1: the draw entry points take the base in whole vertices, so
  // the ring offset must be an exact multiple of the batch stride.  It is, because
  // the upload aligns to sizeof(BatchVertex); check it rather than assume it, and
  // use the legacy single-use allocation if a caller ever changes the alignment.
  if (NativeRingGuard() &&
      (native_vertex_off % native_vertex_stride) != 0) {
    ++s_native_ring_untiled;
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 native_vertex_data, GL_STREAM_DRAW);
    NativeRingReset(s_vertex_ring);
    native_vertex_off = 0;
  }
  const GLint native_base_vertex =
      GLint(native_vertex_off / native_vertex_stride);''',
     '''  const size_t native_vertex_bytes = s_batch.size() * native_vertex_stride;
  // ''' + MARKER + ''': try the immutable segmented ring first (context owner
  // only).  On any failure the mutable buffer is explicitly re-bound before the
  // old ring/glBufferData path runs, so the VAO never keeps a stale pointer.
  size_t native_vertex_off = SIZE_MAX;
  bool native_persist_v = false;
  if (s_persist_wanted) {
    NativePersistentResolve();
    if (nwii::runtime::g_persist_vertex.Init()) {
      const size_t poff = nwii::runtime::g_persist_vertex.upload(
          GL_ARRAY_BUFFER, native_vertex_data, native_vertex_bytes,
          native_vertex_stride);
      if (poff != SIZE_MAX && (poff % native_vertex_stride) == 0) {
        native_vertex_off = poff;
        native_persist_v = true;
      }
    }
  }
  if (native_persist_v) {
    glBindBuffer(GL_ARRAY_BUFFER, nwii::runtime::g_persist_vertex.buffer());
    NativeConfigureBatchLayout(nwii::runtime::g_persist_vertex.buffer(),
                             native_packed_layout);
  } else {
    glBindBuffer(GL_ARRAY_BUFFER, s_vbo); // explicit mutable rebind
    NativeConfigureBatchLayout(s_vbo, native_packed_layout);
    native_vertex_off = NativeRingUpload(
        GL_ARRAY_BUFFER, s_vertex_ring, native_vertex_data, native_vertex_bytes,
        native_vertex_stride);
    if (native_vertex_off == SIZE_MAX) {
      glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                   native_vertex_data, GL_STREAM_DRAW);
      native_vertex_off = 0;
    }
    // The immutable buffer never reaches this guard: persist offsets are
    // already stride-aligned, and a surprise value fell back above.
    if (NativeRingGuard() &&
        (native_vertex_off % native_vertex_stride) != 0) {
      ++s_native_ring_untiled;
      glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                   native_vertex_data, GL_STREAM_DRAW);
      NativeRingReset(s_vertex_ring);
      native_vertex_off = 0;
    }
  }
  const GLint native_base_vertex =
      GLint(native_vertex_off / native_vertex_stride);'''),

    ('''  glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s_ebo);
  const size_t native_index_bytes = s_indices.size() * sizeof(uint32_t);
  size_t native_index_off = NativeRingUpload(
      GL_ELEMENT_ARRAY_BUFFER, s_index_ring, s_indices.data(),
      native_index_bytes, alignof(uint32_t));
  if (native_index_off == SIZE_MAX) {
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, GLsizeiptr(native_index_bytes),
                 s_indices.data(), GL_STREAM_DRAW);
    native_index_off = 0;
  }
  glDrawElementsBaseVertex(GL_TRIANGLES, GLsizei(s_indices.size()),''',
     '''  // ''' + MARKER + ''': the index ring is a second independent object -
  // vertex and index uploads never share a fence or buffer.
  size_t native_index_off = SIZE_MAX;
  bool native_persist_i = false;
  if (s_persist_wanted) {
    NativePersistentResolve();
    if (nwii::runtime::g_persist_index.Init()) {
      const size_t poff = nwii::runtime::g_persist_index.upload(
          GL_ELEMENT_ARRAY_BUFFER, s_indices.data(),
          s_indices.size() * sizeof(uint32_t), alignof(uint32_t));
      if (poff != SIZE_MAX && (poff % alignof(uint32_t)) == 0) {
        native_index_off = poff;
        native_persist_i = true;
      }
    }
  }
  if (native_persist_i) {
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER,
                 nwii::runtime::g_persist_index.buffer());
  } else {
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s_ebo); // explicit mutable rebind
    const size_t native_index_bytes = s_indices.size() * sizeof(uint32_t);
    native_index_off = NativeRingUpload(
        GL_ELEMENT_ARRAY_BUFFER, s_index_ring, s_indices.data(),
        native_index_bytes, alignof(uint32_t));
    if (native_index_off == SIZE_MAX) {
      glBufferData(GL_ELEMENT_ARRAY_BUFFER, GLsizeiptr(native_index_bytes),
                   s_indices.data(), GL_STREAM_DRAW);
      native_index_off = 0;
    }
  }
  glDrawElementsBaseVertex(GL_TRIANGLES, GLsizei(s_indices.size()),'''),
]

MAIN_PAIRS = [
    ('''      SDL_GL_DeleteContext(gl_ctx);
''',
     '''      // ''' + MARKER + ''': context-owner shutdown of the persistent rings
      // BEFORE the context dies - one glFinish then unmap/delete.
      nwii::runtime::NativePersistentReleaseAll();
      SDL_GL_DeleteContext(gl_ctx);
'''),
]

MAIN_DECL = ('''#include <glad/glad.h>
''',
             '''#include <glad/glad.h>

// ''' + MARKER + '''
namespace nwii::runtime { void NativePersistentReleaseAll(); }
''')


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
    return '\n'.join(ln for ln in text.split('\n')
                     if 'native_persistent_upload_mom.h' not in ln)


def _migrate(text):
    # normalise a previously emitted DECLS block to the current form so the
    # pairs round-trip instead of stranding or duplicating it.
    if DECLS_PREV in text:
        text = text.replace(DECLS_PREV, DECLS, 1)
    return text


def apply(root):
    gl = root / GL
    orig = gl.read_bytes().decode('utf-8', errors='surrogateescape')
    text = orig.replace('\r\n', '\n')
    if INCLUDE_LINE.strip() in text:
        text = _strip_include(text)
    text = _migrate(text)
    updated = apply_pairs(restore_pairs(text, GL_PAIRS, GL), GL_PAIRS, GL)
    if updated != orig:
        write(gl, updated, orig)
    main = root / MAIN
    orig = main.read_bytes().decode('utf-8', errors='surrogateescape')
    text = orig.replace('\r\n', '\n')
    text = text.replace(MAIN_DECL[1], MAIN_DECL[0]) if MAIN_DECL[1] in text else text
    updated = apply_pairs(restore_pairs(text, MAIN_PAIRS, MAIN), MAIN_PAIRS, MAIN)
    updated = updated.replace(MAIN_DECL[0], MAIN_DECL[1], 1)
    if updated != orig:
        write(main, updated, orig)
    hdr = root / HDR
    overlay = root / 'overlays/native_persistent_upload_mom.h'
    data = overlay.read_bytes()
    if not hdr.is_file() or hdr.read_bytes() != data:
        hdr.parent.mkdir(parents=True, exist_ok=True)
        hdr.write_bytes(data)
    print('%s staged: %s; NWII_PERSISTENT_UPLOAD=1 opts in '
          '(NWII_NO_PERSISTENT_UPLOAD wins)' % (MARKER, HDR))


def restore(root):
    for rel, pairs in ((GL, GL_PAIRS), (MAIN, MAIN_PAIRS)):
        target = root / rel
        if not target.is_file():
            continue
        orig = target.read_bytes().decode('utf-8', errors='surrogateescape')
        text = orig.replace('\r\n', '\n')
        if rel == GL:
            text = _strip_include(text)
            text = _migrate(text)
        else:
            text = text.replace(MAIN_DECL[1], MAIN_DECL[0]) if MAIN_DECL[1] in text else text
        updated = restore_pairs(text, pairs, rel)
        if updated != orig:
            write(target, updated, orig)
    print('%s restored: %s, %s (installed header left inert)'
          % (MARKER, GL, MAIN))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
