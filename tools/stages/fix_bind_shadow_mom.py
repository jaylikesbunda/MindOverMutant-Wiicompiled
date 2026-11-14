"""Skip the two GL binds that are already in force on every draw batch.

Outdoors the guest submits ~270 draws per content frame (measured 4,350-4,870
draws/s at 16-18 content frames/s).  Each batch pays:

    glUseProgram(s_active_shader.id)     once per SetupDrawState
    glBindVertexArray(s_vao)             twice per FlushBatch
    glBindBuffer(GL_ARRAY_BUFFER, ...)   once per FlushBatch

The program and the VAO are the *same values* for nearly every batch - this tree
has exactly one VAO and a handful of programs - so those binds are redundant
driver calls.  At 270 draws/frame that is roughly 800 calls per frame for
nothing, and the CPU profile already puts 25-45 % of the host thread inside the
driver.

This stage adds a shadow for both.  It is exact rather than heuristic because
the tree has exactly three bind sites, all here: two ``glBindVertexArray`` calls
in the creation block and per flush, and one ``glUseProgram`` in
``SetupDrawState``.  Nothing else binds a VAO or a program, and the only
``glDeleteProgram`` deletes a freshly created binary-cache program that was never
made current, so the shadow cannot go stale.

``NWII_NO_BIND_SHADOW=1`` restores the unconditional binds.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_BIND_SHADOW_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('bind shadow already present.')
    raise SystemExit(0)


def sub(old, new, what, count=1):
    global text
    assert text.count(old) == count, 'anchor %s (%d)' % (what, text.count(old))
    text = text.replace(old, new)


anchor = """static void FlushBatch(native_batch_census::Reason reason ="""
helpers = '''// ''' + MARK + ''': the program and the VAO this renderer last bound.  There is
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

''' + anchor
sub(anchor, helpers, 'FlushBatch definition')

sub('  glBindVertexArray(s_vao);', '  NativeBindVertexArray(s_vao);',
    'VAO binds', count=2)
sub('  glUseProgram(s_active_shader.id);', '  NativeUseProgram(s_active_shader.id);',
    'program bind')

path.write_text(text)
print('Program and VAO binds are shadowed; redundant binds are skipped.')
