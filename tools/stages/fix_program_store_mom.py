"""Make the program-binary cache actually store what it compiles.

The render call is normally 0.0x ms, but it spikes to 20-30 ms about thirteen
times a second: each spike is a new TEV program being compiled inside the draw
path (2,924 distinct keys in a 225 s session).  ``NATIVE_PROGRAM_BINARY_CACHE_V1``
exists to turn those compiles into binary loads on later runs, but on disk it
only holds 864 binaries for those 2,924 keys, so most compiles are repeated every
session.

The reason is in ``store()``: it calls ``get_binary`` and then rejects the result
if ``glGetError() != GL_NO_ERROR``, without clearing the error queue first.  Any
error left over from earlier code in the frame - and this renderer deliberately
probes state that can raise one - makes the store return early, so the program is
compiled again next run.  ``load()`` already clears the queue before use; this
stage makes ``store()`` do the same.

``NWII_NO_PROGRAM_STORE_FIX=1`` restores the previous behaviour.

This does not change what is drawn: it only decides whether a compiled program is
written to the cache.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_PROGRAM_STORE_FIX_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('program store fix already present.')
    raise SystemExit(0)

anchor = """  void store(uint64_t value,GLuint program) {
    GLint linked=0,length=0;glGetProgramiv(program,GL_LINK_STATUS,&linked);if(!linked)return;"""
assert text.count(anchor) == 1, 'store() anchor'
replacement = """  void store(uint64_t value,GLuint program) {
    // """ + MARK + """: drain the error queue first.  A stale error made every
    // store below bail out, so the cache held 864 binaries for 2,924 keys and
    // the rest were recompiled (in the draw path) on every run.
    static const bool drain_errors = std::getenv("NWII_NO_PROGRAM_STORE_FIX") == nullptr;
    if (drain_errors) while(glGetError()!=GL_NO_ERROR){}
    GLint linked=0,length=0;glGetProgramiv(program,GL_LINK_STATUS,&linked);if(!linked)return;"""
text = text.replace(anchor, replacement, 1)
path.write_text(text)
print('Program binaries are stored even when an earlier GL call left an error.')
