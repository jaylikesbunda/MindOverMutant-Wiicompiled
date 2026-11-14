"""Make the three render-path state caches safe instead of merely fast.

Three landed stages shorten the draw path by caching driver state or by
addressing vertices with an offset:

    fix_uniform_cache_mom.py    NATIVE_MOM_UNIFORM_CACHE_V1   NWII_NO_UNIFORM_CACHE
    fix_upload_ring_mom.py      NATIVE_MOM_UPLOAD_RING_V1      NWII_NO_STREAM_UPLOAD
    fix_bind_shadow_mom.py      NATIVE_MOM_BIND_SHADOW_V1      NWII_NO_BIND_SHADOW

Each one is correct only while an assumption about the surrounding renderer
holds.  Reading renderer_gl.cpp (line numbers are the state this stage anchors
on) shows where the assumptions break.

1.  Uniform cache -- writes it did not make, and keys that are not the bound
    program.
    ``native_uniform_cache::Changed`` (renderer_gl.cpp:784) returns true for
    ``program == 0`` *without* recording anything (renderer_gl.cpp:787), and
    ``ApplyProjection`` is also called from the XF-register path with no program
    argument at all (``ApplyProjection(s_active_shader.uProjMtx)``,
    renderer_gl.cpp:2038).  ``SetupDrawState`` can also return after a
    shader-source dedupe hit without ever calling ``NativeUseProgram``
    (renderer_gl.cpp:1479-1486), so the program that is bound is then not the
    shader those locations belong to.  The XF upload therefore lands in
    *whatever program is current* while the cache entry for the shader that owns
    that location keeps the previous matrix; the next ``Changed`` for that shader
    compares equal and skips the upload, so the driver keeps the XF value while
    the renderer believes the old one is in force.  Every draw in that state uses
    a stale mvp (degenerate clip space, draws still counted).  The same key
    problem records values GL rejected: glUniform* into a non-current program is
    a no-op with GL_INVALID_OPERATION, so a later compare can skip an upload that
    never happened.
    Fix: a value is only recorded when the key program is the program that is
    actually current, tracked exactly and independently of the bind-shadow
    switch, and any upload the cache did not record invalidates every entry for
    that location before it is issued.

2.  Bind shadow -- an assumption with no way to be revalidated.
    ``s_native_bound_program`` / ``s_native_bound_vao`` (renderer_gl.cpp:581)
    are only ever written from this file, and the header has no invalidation
    path.  Everything that binds inside renderer_gl.cpp does go through the two
    wrappers, and ``glDeleteProgram`` (renderer_gl.cpp:116) only deletes a
    freshly created binary-cache program that was never made current, so the
    shadow is exact today.  But it is the only state shadow in the tree without
    an invalidation protocol (compare ``native_raster::invalidate()``, called at
    context creation from src/core/main.cpp:225), while the F3 overlay is
    already a second GL client that binds its own program and VAO and restores
    only what it queried (include/runtime/native_dev_hud.h:803-818).  One changed
    restore path, one new client, or one context recreation and the shadow
    silently skips a bind the driver needs.
    Fix: re-read both bindings from the driver once per drain, and count every
    time the shadow disagreed with the driver.  Two glGetIntegerv calls per drain
    make the assumption measured instead of assumed.

3.  Upload ring -- bookkeeping that outlives the store, and a base-vertex
    conversion that trusts its own alignment.
    ``NativeRingUpload`` (renderer_gl.cpp:545) returns SIZE_MAX for a submission
    larger than the ring, and the caller then reallocates the shared buffer to
    that submission with ``glBufferData`` (renderer_gl.cpp:644-648).  The ring
    keeps ``capacity == kNativeRingBytes`` and its cursor, so the next call skips
    the allocation and issues ``glBufferSubData`` at an offset past the end of a
    now smaller store: GL_INVALID_VALUE, no upload, and the draw reads whatever
    was there before.  ``glDrawArrays``/``glDrawElementsBaseVertex`` take the
    base in whole vertices, and the conversion at renderer_gl.cpp:649 only stays
    exact because the caller happens to align to ``sizeof(BatchVertex)``; the
    helper accepts any alignment.
    Fix: forget the ring whenever the legacy path takes the buffer over, and
    check that the returned offset really is a whole number of vertices, falling
    back to the legacy single-use buffer if it is not.
    Ruled out by reading: the arithmetic itself.  Indices in ``s_indices`` are
    absolute batch indices, ``base_vertex`` is ``offset / sizeof(BatchVertex)``,
    and ``NativeEmitLinesPoints`` expands into ``s_batch`` before the upload, so
    offset plus index is the same vertex the legacy path would draw.  Nothing
    reads ``s_batch`` after the upload except the CPU-side probe and archive,
    which copy it before ``FlushBatch`` clears it.

Switches.  Every fix is on by default and each one reverts to the exact
pre-stage code, so one binary can be A/B tested:

    NWII_RENDER_STATE_SAFE=0       all three fixes off
    NWII_NO_UNIFORM_CACHE_GUARD=1  fix 1 off (cache trusts its own keys again)
    NWII_NO_BIND_RESYNC=1          fix 2 off (shadow is never revalidated)
    NWII_NO_RING_GUARD=1           fix 3 off (offset trusted, ring not reset)

The three optimisations themselves keep their own switches.  Independently of
them, ``NWII_RING_ORPHAN_PER_FRAME=1`` discards both ring stores once per drain:
that is the experiment for a driver that aliases an in-flight submission with a
later ``glBufferSubData`` (2 allocations per drain instead of ~170), and
``NWII_RENDER_STATE_TRACE=1`` prints bounded evidence:

    bind_desyncs        > 0 proves the bind shadow was stale in this run
    uniform_refusals    > 0 proves a cached uniform key was not the bound program
    ring_untiled        > 0 proves the base-vertex conversion was not exact

Idempotent: the marker below is written first and checked on entry.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_RENDER_STATE_SAFE_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_bytes().decode('utf-8').replace('\r\n', '\n')
if MARK in text:
    print('render-state safety already present.')
    raise SystemExit(0)

# This stage rewrites the text the three optimisation stages install, so it must
# only ever run on a tree where all three are already present.
for required in ('NATIVE_MOM_UNIFORM_CACHE_V1', 'NATIVE_MOM_UPLOAD_RING_V1',
                 'NATIVE_MOM_BIND_SHADOW_V1'):
    if required not in text:
        raise SystemExit('%s: %s is missing; apply fix_uniform_cache_mom.py, '
                         'fix_upload_ring_mom.py and fix_bind_shadow_mom.py first'
                         % (MARK, required))


def sub(old, new, what):
    global text
    assert text.count(old) == 1, 'anchor %s (%d)' % (what, text.count(old))
    text = text.replace(old, new, 1)


# ---- 1. switches, counters and the ring bookkeeping helper -------------------
ring_head = '''// Returns the byte offset the draw must start at, or SIZE_MAX when the caller
// should fall back to the legacy glBufferData path.
static size_t NativeRingUpload(GLenum target, NativeUploadRing &ring,
                               const void *data, size_t bytes, size_t align) {
  static const bool disabled = std::getenv("NWII_NO_STREAM_UPLOAD") != nullptr;
  if (disabled || bytes == 0 || bytes > kNativeRingBytes) return SIZE_MAX;
'''

ring_new = '''// ''' + MARK + ''': switches that make the render-path caches safe.  Each fix
// reverts to the exact pre-stage behaviour when its own variable is set, so one
// binary can be A/B tested without a rebuild.
static bool NativeRenderStateSafe() {
  static const bool disabled = []() {
    const char *e = std::getenv("NWII_RENDER_STATE_SAFE");
    return e && e[0] == '0';
  }();
  return !disabled;
}
static bool NativeRenderStateTrace() {
  static const bool on = std::getenv("NWII_RENDER_STATE_TRACE") != nullptr;
  return on;
}
static uint64_t s_native_ring_untiled = 0;
static uint64_t s_native_bind_desyncs = 0;
static uint64_t s_native_uniform_refusals = 0;

// The ring's bookkeeping only describes the buffer while the ring owns it.  The
// legacy fallback reallocates that buffer to a different size, so forget the
// store instead of writing past its end on the next submission.
static void NativeRingReset(NativeUploadRing &ring) {
  ring.capacity = 0;
  ring.cursor = 0;
}
static bool NativeRingGuard() {
  static const bool off = std::getenv("NWII_NO_RING_GUARD") != nullptr;
  return NativeRenderStateSafe() && !off;
}

// Returns the byte offset the draw must start at, or SIZE_MAX when the caller
// should fall back to the legacy glBufferData path.
static size_t NativeRingUpload(GLenum target, NativeUploadRing &ring,
                               const void *data, size_t bytes, size_t align) {
  static const bool disabled = std::getenv("NWII_NO_STREAM_UPLOAD") != nullptr;
  if (disabled || bytes == 0) {
    if (NativeRingGuard())
      NativeRingReset(ring);
    return SIZE_MAX;
  }
  if (bytes > kNativeRingBytes) {
    if (NativeRingGuard())
      NativeRingReset(ring);
    return SIZE_MAX;
  }
'''
sub(ring_head, ring_new, 'NativeRingUpload head')

# ---- 2. exact bind tracking, and a per-drain revalidation -------------------
bind_block = '''// NATIVE_MOM_BIND_SHADOW_V1: the program and the VAO this renderer last bound.  There is
// one VAO in the tree and few programs, so most batches re-bind what is already
// bound; these shadows are exact because every bind site is in this file.
static GLuint s_native_bound_program = 0;
static GLuint s_native_bound_vao = 0;

static inline bool NativeBindShadow() {
  static const bool disabled = std::getenv("NWII_NO_BIND_SHADOW") != nullptr;
  return !disabled;
}

static inline void NativeUseProgram(GLuint program) {
  if (!NativeBindShadow()) { glUseProgram(program); return; }
  if (program == s_native_bound_program) return;
  glUseProgram(program);
  s_native_bound_program = program;
}

static inline void NativeBindVertexArray(GLuint vao) {
  if (!NativeBindShadow()) { glBindVertexArray(vao); return; }
  if (vao == s_native_bound_vao) return;
  glBindVertexArray(vao);
  s_native_bound_vao = vao;
}
'''

bind_new = '''// NATIVE_MOM_BIND_SHADOW_V1: the program and the VAO this renderer last bound.  There is
// one VAO in the tree and few programs, so most batches re-bind what is already
// bound; these shadows are exact because every bind site is in this file.
// NATIVE_MOM_RENDER_STATE_SAFE_V1: "exact" is an assumption about every other GL
// client, and the tree has one (the F3 overlay binds its own program and VAO).
// Both shadows are still written on every bind, but they are re-read from the
// driver once per drain, and every disagreement is counted.  The program shadow
// is maintained even with NWII_NO_BIND_SHADOW set, because the uniform cache uses
// it to decide whether a glUniform* call will reach the program it is keyed on.
static GLuint s_native_bound_program = 0;
static GLuint s_native_bound_vao = 0;

static inline bool NativeBindShadow() {
  static const bool disabled = std::getenv("NWII_NO_BIND_SHADOW") != nullptr;
  return !disabled;
}

// Ask the driver what is really bound.  Wrong shadow plus this call is a bind;
// right shadow plus this call is two queries and no state change.
static void NativeBindShadowResync() {
  static const bool off = std::getenv("NWII_NO_BIND_RESYNC") != nullptr;
  if (off || !NativeRenderStateSafe() || !NativeBindShadow())
    return;
  GLint program = 0, vao = 0;
  glGetIntegerv(GL_CURRENT_PROGRAM, &program);
  glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
  if (GLuint(program) != s_native_bound_program ||
      GLuint(vao) != s_native_bound_vao)
    ++s_native_bind_desyncs;
  s_native_bound_program = GLuint(program);
  s_native_bound_vao = GLuint(vao);
}

static inline void NativeUseProgram(GLuint program) {
  if (NativeBindShadow() && program == s_native_bound_program)
    return;
  glUseProgram(program);
  s_native_bound_program = program;
}

static inline void NativeBindVertexArray(GLuint vao) {
  if (NativeBindShadow() && vao == s_native_bound_vao)
    return;
  glBindVertexArray(vao);
  s_native_bound_vao = vao;
}

// Once per drain: revalidate the bindings and optionally discard both ring stores.
static void NativeRenderStateFrameBegin() {
  if (!NativeRenderStateSafe())
    return;
  NativeBindShadowResync();
  static const bool orphan_ring =
      std::getenv("NWII_RING_ORPHAN_PER_FRAME") != nullptr;
  static const bool ring_disabled =
      std::getenv("NWII_NO_STREAM_UPLOAD") != nullptr;
  if (orphan_ring && !ring_disabled && s_vao) {
    NativeBindVertexArray(s_vao);
    glBindBuffer(GL_ARRAY_BUFFER, s_vbo);
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(kNativeRingBytes), nullptr,
                 GL_STREAM_DRAW);
    s_vertex_ring.cursor = 0;
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s_ebo);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, GLsizeiptr(kNativeRingBytes), nullptr,
                 GL_STREAM_DRAW);
    s_index_ring.cursor = 0;
  }
  if (NativeRenderStateTrace()) {
    static uint64_t next_report = 0;
    const uint64_t now = native_batch_census::Now();
    if (!next_report)
      next_report = now + 3000000000ull;
    else if (now >= next_report) {
      next_report = now + 3000000000ull;
      std::printf("[RSTATE] bind_desyncs=%llu uniform_refusals=%llu "
                  "ring_untiled=%llu\\n",
                  (unsigned long long)s_native_bind_desyncs,
                  (unsigned long long)s_native_uniform_refusals,
                  (unsigned long long)s_native_ring_untiled);
      std::fflush(stdout);
    }
  }
}
'''
sub(bind_block, bind_new, 'bind shadow block')

# ---- 3. the uniform cache records only what GL really stored ---------------
cache_block = '''// NATIVE_MOM_UNIFORM_CACHE_V1: exact-value cache for per-draw uniform uploads.
namespace native_uniform_cache {
struct Entry {
  GLuint program = 0;
  GLint location = -1;
  uint32_t bytes = 0;
  uint8_t data[128] = {};
};
static const unsigned kSlots = 256;
static Entry slots[kSlots];

inline bool Changed(GLuint program, GLint location, const void *data,
                    uint32_t bytes) {
  static const bool disabled = std::getenv("NWII_NO_UNIFORM_CACHE") != nullptr;
  if (disabled || program == 0 || location < 0 || bytes > sizeof(Entry::data))
    return true;
  const uint32_t h = (uint32_t(program) * 2654435761u) ^
                     (uint32_t(location) * 40503u) ^ (bytes * 2246822519u);
  Entry &e = slots[h & (kSlots - 1)];
  if (e.program == program && e.location == location && e.bytes == bytes &&
      std::memcmp(e.data, data, bytes) == 0)
    return false;
  e.program = program;
  e.location = location;
  e.bytes = bytes;
  std::memcpy(e.data, data, bytes);
  return true;
}

inline void Invalidate() {
  for (auto &e : slots) {
    e.program = 0;
    e.location = -1;
    e.bytes = 0;
  }
}
}  // namespace native_uniform_cache
'''

cache_new = '''// NATIVE_MOM_UNIFORM_CACHE_V1: exact-value cache for per-draw uniform uploads.
// NATIVE_MOM_RENDER_STATE_SAFE_V1: an entry means "GL holds these bytes for
// (program, location)".  That is only true when glUniform* was issued into that
// program while it was current, so a value is recorded only when the key is the
// program that is bound right now; every upload the cache does not record
// invalidates the entries for that location first, because the caller uploaded
// to a program the cache cannot name (ApplyProjection from the XF-register path
// passes no program) and those entries no longer describe the driver.
namespace native_uniform_cache {
struct Entry {
  GLuint program = 0;
  GLint location = -1;
  uint32_t bytes = 0;
  uint8_t data[128] = {};
};
static const unsigned kSlots = 256;
static Entry slots[kSlots];

static bool Guard() {
  static const bool off = std::getenv("NWII_NO_UNIFORM_CACHE_GUARD") != nullptr;
  return NativeRenderStateSafe() && !off;
}

inline void Invalidate() {
  for (auto &e : slots) {
    e.program = 0;
    e.location = -1;
    e.bytes = 0;
  }
}

// Conservative: every program that had a value at this location is forgotten,
// because the untracked upload did not say which program received it.
inline void InvalidateLocation(GLint location) {
  if (location < 0)
    return;
  for (auto &e : slots)
    if (e.location == location) {
      e.program = 0;
      e.location = -1;
      e.bytes = 0;
    }
}

inline bool Changed(GLuint program, GLint location, const void *data,
                    uint32_t bytes) {
  static const bool disabled = std::getenv("NWII_NO_UNIFORM_CACHE") != nullptr;
  if (disabled)
    return true;
  const bool guarded = Guard();
  const bool scoped = program != 0 && program == s_native_bound_program;
  if (program == 0 || location < 0 || bytes > sizeof(Entry::data) ||
      (guarded && !scoped)) {
    if (guarded) {
      InvalidateLocation(location);
      ++s_native_uniform_refusals;
      if (NativeRenderStateTrace() && s_native_uniform_refusals <= 4)
        std::printf("[RSTATE] uniform refusal key=%u bound=%u location=%d "
                    "bytes=%u\\n",
                    unsigned(program), unsigned(s_native_bound_program),
                    int(location), unsigned(bytes));
    }
    return true;
  }
  const uint32_t h = (uint32_t(program) * 2654435761u) ^
                     (uint32_t(location) * 40503u) ^ (bytes * 2246822519u);
  Entry &e = slots[h & (kSlots - 1)];
  if (e.program == program && e.location == location && e.bytes == bytes &&
      std::memcmp(e.data, data, bytes) == 0)
    return false;
  e.program = program;
  e.location = location;
  e.bytes = bytes;
  std::memcpy(e.data, data, bytes);
  return true;
}
}  // namespace native_uniform_cache
'''
sub(cache_block, cache_new, 'uniform cache namespace')

# ---- 4. base-vertex conversion is checked, not assumed ---------------------
base_old = '''  const GLint native_base_vertex =
      GLint(native_vertex_off / sizeof(BatchVertex));
'''
base_new = '''  // ''' + MARK + ''': the draw entry points take the base in whole vertices, so
  // the ring offset must be an exact multiple of the batch stride.  It is, because
  // the upload aligns to sizeof(BatchVertex); check it rather than assume it, and
  // use the legacy single-use allocation if a caller ever changes the alignment.
  if (NativeRingGuard() &&
      (native_vertex_off % sizeof(BatchVertex)) != 0) {
    ++s_native_ring_untiled;
    glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(native_vertex_bytes),
                 s_batch.data(), GL_STREAM_DRAW);
    NativeRingReset(s_vertex_ring);
    native_vertex_off = 0;
  }
  const GLint native_base_vertex =
      GLint(native_vertex_off / sizeof(BatchVertex));
'''
sub(base_old, base_new, 'base vertex conversion')

# ---- 5. one revalidation point per drain ----------------------------------
render_old = '''  void Render(const std::vector<GXCommand> &commands) override {
    NativeRestoreBpSnapshot();
'''
render_new = '''  void Render(const std::vector<GXCommand> &commands) override {
    NativeRestoreBpSnapshot();
    NativeRenderStateFrameBegin(); // ''' + MARK + '''
'''
sub(render_old, render_new, 'Render entry')

path.write_bytes(text.encode('utf-8'))
print('Render-path state caches validated: uniform keys scoped to the bound '
      'program, bind shadow rechecked per drain, ring bookkeeping reset when the '
      'legacy path takes the buffer.')
