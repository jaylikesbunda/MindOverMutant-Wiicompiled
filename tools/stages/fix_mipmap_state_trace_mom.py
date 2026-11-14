"""Mip generation current-unit fix + CPU wall-clock trace scopes (MoM).

Marker: NATIVE_MOM_MIP_STATE_TRACE_V1.

1) ensure_mips currently saves the *current-unit* binding but then switches to
   texture unit 0, generates, and restores the saved binding ON UNIT 0 - any
   caller on another unit loses its own binding AND unit 0 gets clobbered.
   Replace with current-unit preservation only: save GL_TEXTURE_BINDING_2D,
   bind the target, raise MAX_LEVEL BEFORE glGenerateMipmap (a level-0-only
   texture generates no levels per the GL spec), generate, restore.  No
   glActiveTexture at all.  Budget accounting / mip_ready / NWII_NO_MIPMAPS
   unchanged.

2) native_trace::Scope keys so a NWII_FRAME_TRACE run attributes CPU time:
   texture.lookup_hash / texture.decode / texture.upload / texture.mipmap in
   texture_cache_gl.cpp; gx.shader_binary_load / gx.shader_binary_extract /
   gx.shader_compile_link / gx.shader_generate / gx.batch in renderer_gl.cpp.
   Scopes are inclusive wall-clock only when frame_trace is active.

Idempotent; --restore removes its own insertions.
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_MIP_STATE_TRACE_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
TEX = RUNTIME + '/src/hle/texture_cache_gl.cpp'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'

TEX_INCLUDE = '#include "runtime/frame_trace.h" // ' + MARKER

TEX_PAIRS = [
    # mip scope: after the early return, before mip_ready.insert + accounting
    ('''    mip_ready.insert(tex);''',
     '''    native_trace::Scope scope_mipmap("texture.mipmap"); // NATIVE_MOM_MIP_STATE_TRACE_V1
    mip_ready.insert(tex);'''),
    ('#include "runtime/texture_cache_gl.h"',
     '#include "runtime/texture_cache_gl.h"\n' + TEX_INCLUDE),

    ('''GLuint TextureCache::get_texture(CPUContext& ctx, const gx::TexStage& stage) {
''',
     '''GLuint TextureCache::get_texture(CPUContext& ctx, const gx::TexStage& stage) {
    native_trace::Scope scope_lookup("texture.lookup_hash"); // ''' + MARKER + ''' whole lookup
'''),

    ('''    Image img = decode_texture(ctx, stage);
    GLuint tex;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, stage.width, stage.height, 0, GL_RGBA, GL_UNSIGNED_BYTE, img.data);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    // NATIVE_MOM_MIPMAPS_V1: level 0 only until ensure_mips() builds the chain.
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0);
    UnloadImage(img);''',
     '''    Image img = [&] { // ''' + MARKER + '''
        native_trace::Scope scope_decode("texture.decode");
        return decode_texture(ctx, stage);
    }();
    GLuint tex;
    { // ''' + MARKER + ''' upload-only GL work
      native_trace::Scope scope_upload("texture.upload");
      glGenTextures(1, &tex);
      glBindTexture(GL_TEXTURE_2D, tex);
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, stage.width, stage.height, 0, GL_RGBA, GL_UNSIGNED_BYTE, img.data);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
      // NATIVE_MOM_MIPMAPS_V1: level 0 only until ensure_mips() builds the chain.
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0);
      UnloadImage(img);
    }'''),

    # ensure_mips: current-unit preservation + MAX_LEVEL before generate + scope
    ('''    GLint active = 0, bound = 0;
    glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex);
    glGenerateMipmap(GL_TEXTURE_2D);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 1000);
    glBindTexture(GL_TEXTURE_2D, GLuint(bound));
    glActiveTexture(GLuint(active));
}''',
     '''    // ''' + MARKER + ''': stay on the caller's unit - save the CURRENT unit's
    // binding, bind the target, raise MAX_LEVEL BEFORE generating (a texture
    // with MAX_LEVEL=0 generates zero extra levels), generate, restore.  No
    // glActiveTexture anywhere: the previous unit-0 switch clobbered unit 0's
    // binding whenever a mip-ready texture arrived on another unit.
    GLint bound = 0;
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 1000);
    glGenerateMipmap(GL_TEXTURE_2D);
    glBindTexture(GL_TEXTURE_2D, GLuint(bound));
}'''),

    # evict_lru: scope after the budget-okay early return; mark() the pass end.
    ('''    if (cache_bytes <= budget && cache.size() <= kMaxEntries)
        return;
    std::vector<std::pair<uint64_t, TextureKey>> order;''',
     '''    if (cache_bytes <= budget && cache.size() <= kMaxEntries)
        return;
    native_trace::Scope scope_evict("texture.evict"); // ''' + MARKER + '''
    std::vector<std::pair<uint64_t, TextureKey>> order;'''),
    ('''    static const bool trace = std::getenv("NWII_TEXTURE_EVICT_TRACE") != nullptr;''',
     '''    native_trace::mark("texture.evict_entries", dropped, cache_bytes); // ''' + MARKER + '''
    static const bool trace = std::getenv("NWII_TEXTURE_EVICT_TRACE") != nullptr;'''),
]

GL_PAIRS = [
    # shader binary cache entries (call-site wrapped so the scope ends with
    # the call, not the function); compile scope closes BEFORE cache.store.
    ('''    if(const GLuint cached=cache.load(cache_key)){''',
     '''    if(const GLuint cached=[&]{native_trace::Scope s("gx.shader_binary_load");return cache.load(cache_key);}()){'''), # ''' + MARKER
    ('''  GLuint v = glCreateShader(GL_VERTEX_SHADER);''',
     '''  native_trace::Scope scope_link("gx.shader_compile_link"); // ''' + MARKER + '''
  GLuint v = glCreateShader(GL_VERTEX_SHADER);'''),
    ('''  if(cache.enabled)cache.store(cache_key,p);''',
     '''  scope_link.close(); // ''' + MARKER + ''': compile+link ends before extraction
  if(cache.enabled)[&]{native_trace::Scope s("gx.shader_binary_extract");cache.store(cache_key,p);return 0;}();'''),

    ('''    auto src = GenerateTEVShader(g_state, cmd.prim_type);''',
     '''    auto src = [&] { // ''' + MARKER + '''
      native_trace::Scope scope_gen("gx.shader_generate");
      return GenerateTEVShader(g_state, cmd.prim_type);
    }();'''),

    # gx.batch after the empty-batch early return - whole FlushBatch inclusive
    ('''  if (s_batch.empty())
    return;''',
     '''  if (s_batch.empty())
    return;
  native_trace::Scope scope_batch("gx.batch"); // NATIVE_MOM_MIP_STATE_TRACE_V1'''),
    # function-entry scopes so an outdoor trace attributes every top-level lane
    ('static void SetupDrawState(const GXCommand &cmd) {\n',
     'static void SetupDrawState(const GXCommand &cmd) {\n'
     '  native_trace::Scope scope_state("gx.state"); // ' + MARKER + '\n'),
    ('static void DrawPrimitive(const GXCommand &cmd) {\n',
     'static void DrawPrimitive(const GXCommand &cmd) {\n'
     '  native_trace::Scope scope_vertices("gx.vertices"); // ' + MARKER + '\n'),
    ('static void NativeCopyEfbTexture(GLuint texture, int x, int y, int width, int height,int scale=1) {\n',
     'static void NativeCopyEfbTexture(GLuint texture, int x, int y, int width, int height,int scale=1) {\n'
     '  native_trace::Scope scope_copy("gx.efb_texture_copy"); // ' + MARKER + '\n'),
    ('static void NativeLatchXfb(uint32_t address,int x,int y,int width,int height,int scale=1) {\n',
     'static void NativeLatchXfb(uint32_t address,int x,int y,int width,int height,int scale=1) {\n'
     '  native_trace::Scope scope_xfb("gx.xfb_copy"); // ' + MARKER + '\n'),
]


def write(path, text, orig):
    data = text.replace('\n', '\r\n') if '\r\n' in orig else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as h:
        h.write(data)


def apply_pairs(text, pairs, tag):
    for old, new in pairs:
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:70]))
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


OWNED_INCLUDE = '#include "runtime/frame_trace.h" // ' + MARKER


def _prepare(path, pairs, do_include, apply_mode):
    # read + normalise + apply the pairs WITHOUT writing, so a mid-set anchor
    # failure never leaves a half-patched file.
    orig = path.read_bytes().decode('utf-8', errors='surrogateescape')
    text = orig.replace(chr(13) + chr(10), chr(10))
    # Migrate the earlier revision, which scoped only the GL mip work.
    if do_include:
        for old, new in pairs:
            if old.startswith('    GLint active = 0, bound = 0;'):
                previous = new.replace(
                    '    GLint bound = 0;',
                    '    native_trace::Scope scope_mipmap("texture.mipmap");\n'
                    '    GLint bound = 0;', 1)
                text = text.replace(previous, new, 1)
    else:
        text = text.replace(
            '  // ' + MARKER + ': whole upload+draw, inclusive wall clock only.\n'
            '  native_trace::Scope scope_batch("gx.batch");\n', '', 1)
    if do_include:
        # strip ONLY the exact owned include line, nothing else
        text = chr(10).join(ln for ln in text.split(chr(10)) if ln != OWNED_INCLUDE)
    if apply_mode:
        text = apply_pairs(restore_pairs(text, pairs, str(path)), pairs, str(path))
    else:
        text = restore_pairs(text, pairs, str(path))
    return path, orig, text


def apply(root):
    planned = [_prepare(root / rel, pairs, inc, True)
               for rel, pairs, inc in ((TEX, TEX_PAIRS, True), (GL, GL_PAIRS, False))]
    for path, orig, text in planned:
        if text != orig:
            write(path, text, orig)
    print('%s staged: mip unit fix + trace scopes (NWII_NO_MIPMAPS still wins)'
          % MARKER)


def restore(root):
    planned = [_prepare(root / rel, pairs, inc, False)
               for rel, pairs, inc in ((TEX, TEX_PAIRS, True), (GL, GL_PAIRS, False))
               if (root / rel).is_file()]
    leftover = [str(path) for path, _o, text in planned if MARKER in text]
    if leftover:
        raise SystemExit('%s: restore left owned markers in %s (not writing)'
                         % (MARKER, leftover))
    for path, orig, text in planned:
        if text != orig:
            write(path, text, orig)
    print('%s restored: %s, %s' % (MARKER, TEX, GL))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
