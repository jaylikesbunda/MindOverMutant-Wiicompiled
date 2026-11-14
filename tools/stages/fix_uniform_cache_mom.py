"""Stop re-uploading identical uniforms on every draw.

``SetupDrawState`` runs once per draw run (~74 times per frame) and uploads every
TEV/material/light/sampler uniform unconditionally: 0-8 sampler ints, material
and ambient colours, four channel-control words, five eight-light arrays of
128 B, and the two TEV arrays.  The CPU profile puts the driver at 25-45 % of the
host thread and the per-draw cost at ~0.19 ms, most of it driver entry points.

GLEW/GL has no value cache, so two draws that share lighting and TEV state pay
for those uploads twice even though the bytes are identical.  This stage adds a
direct-mapped value cache keyed by (program, location) holding the exact bytes,
so an unchanged array costs one 128-byte memcmp instead of a driver call.

Safety:

* the key includes the program id, so a recompiled or different program can
  never inherit a stale value;
* the cache is only consulted for uploads issued after ``glUseProgram`` in
  ``SetupDrawState``; ``ApplyProjection`` is also called from the XF-register
  path where the bound program is not this one, so that call passes no program
  and stays uncached;
* ``NWII_NO_UNIFORM_CACHE=1`` disables it and restores the unconditional
  uploads, so one binary can be A/B'd.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_UNIFORM_CACHE_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('uniform cache already present.')
    raise SystemExit(0)


def sub(old, new, what):
    global text
    assert text.count(old) == 1, 'anchor %s' % what
    text = text.replace(old, new, 1)


# ---- 1. the cache ----------------------------------------------------------
anchor = 'static void ApplyProjection(GLint uProj) {'
cache = '''// ''' + MARK + ''': exact-value cache for per-draw uniform uploads.
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

''' + anchor
sub(anchor, cache, 'ApplyProjection definition')

# ---- 2. projection: cache only when the caller knows the bound program -----
sub('static void ApplyProjection(GLint uProj) {',
    'static void ApplyProjection(GLint uProj, GLuint program = 0) {',
    'ApplyProjection signature')
sub("""  std::memcpy(s_proj, p, sizeof(s_proj));
  glUniformMatrix4fv(uProj, 1, GL_FALSE, p);""",
    """  std::memcpy(s_proj, p, sizeof(s_proj));
  if (native_uniform_cache::Changed(program, uProj, p, sizeof(p)))
    glUniformMatrix4fv(uProj, 1, GL_FALSE, p);""",
    'projection upload')

# ---- 3. SetupDrawState: the bound program is known from here on ------------
sub("""  glUseProgram(s_active_shader.id);
  ApplyProjection(s_active_shader.uProjMtx);""",
    """  glUseProgram(s_active_shader.id);
  ApplyProjection(s_active_shader.uProjMtx, s_active_shader.id);""",
    'SetupDrawState projection call')

sub("""    if (g_state.texStages[i].base_addr != 0 && s_active_shader.uTex[i] >= 0) {
      glUniform1i(s_active_shader.uTex[i], i);
    }""",
    """    if (g_state.texStages[i].base_addr != 0 && s_active_shader.uTex[i] >= 0) {
      const GLint unit = i;
      if (native_uniform_cache::Changed(s_active_shader.id,
                                        s_active_shader.uTex[i], &unit,
                                        sizeof(unit)))
        glUniform1i(s_active_shader.uTex[i], unit);
    }""",
    'sampler ints')

sub("""    if (s_active_shader.uMatColor >= 0)
      glUniform4fv(s_active_shader.uMatColor, 2, mat);
    if (s_active_shader.uAmbColor >= 0)
      glUniform4fv(s_active_shader.uAmbColor, 2, amb);""",
    """    if (s_active_shader.uMatColor >= 0 &&
        native_uniform_cache::Changed(s_active_shader.id,
                                      s_active_shader.uMatColor, mat, 32))
      glUniform4fv(s_active_shader.uMatColor, 2, mat);
    if (s_active_shader.uAmbColor >= 0 &&
        native_uniform_cache::Changed(s_active_shader.id,
                                      s_active_shader.uAmbColor, amb, 32))
      glUniform4fv(s_active_shader.uAmbColor, 2, amb);""",
    'material/ambient')

sub("""      glUniform1iv(s_active_shader.uChanCtrl, 4, cc);""",
    """      if (native_uniform_cache::Changed(s_active_shader.id,
                                        s_active_shader.uChanCtrl, cc,
                                        sizeof(cc)))
        glUniform1iv(s_active_shader.uChanCtrl, 4, cc);""",
    'channel control')

for name, arr in (('uLightCol', 'lcol'), ('uLightPos', 'lpos'),
                  ('uLightDir', 'ldir'), ('uLightCosAtt', 'lcos'),
                  ('uLightDistAtt', 'ldist')):
    sub("""    if (s_active_shader.%s >= 0)
      glUniform4fv(s_active_shader.%s, 8, %s);""" % (name, name, arr),
        """    if (s_active_shader.%s >= 0 &&
        native_uniform_cache::Changed(s_active_shader.id,
                                      s_active_shader.%s, %s, 128))
      glUniform4fv(s_active_shader.%s, 8, %s);""" % (name, name, arr, name, arr),
        'light array %s' % name)

sub("""  if (s_active_shader.uTevKColor >= 0)
    glUniform4fv(s_active_shader.uTevKColor, 4, kcolor);
  if (s_active_shader.uTevColor >= 0)
    glUniform4fv(s_active_shader.uTevColor, 4, color);""",
    """  if (s_active_shader.uTevKColor >= 0 &&
      native_uniform_cache::Changed(s_active_shader.id,
                                    s_active_shader.uTevKColor, kcolor, 64))
    glUniform4fv(s_active_shader.uTevKColor, 4, kcolor);
  if (s_active_shader.uTevColor >= 0 &&
      native_uniform_cache::Changed(s_active_shader.id,
                                    s_active_shader.uTevColor, color, 64))
    glUniform4fv(s_active_shader.uTevColor, 4, color);""",
    'TEV arrays')

path.write_text(text)
print('Per-draw uniform uploads are now compared against the last value.')
