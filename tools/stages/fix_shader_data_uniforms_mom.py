"""Fog BP data and alpha-test references ride uniforms, not shader text (MoM).

Marker: NATIVE_MOM_SHADER_DATA_UNIFORMS_V1 / NATIVE_MOM_SHADER_SRC_EXACT_V1.

MoM emits literal BP ee/f1 float bits, ef integer, f0 shift and f2 colour, and
literal alpha refs, into the fragment source: a value-only state change
compiled a new program.  This is CotT's fix_shader_data_uniforms mechanism
re-derived on MoM's tev_shader_gen.cpp:

* ``uNativeFogData uvec4 = {(bp[ee]&0xfffff)<<12, (bp[f1]&0xfffff)<<12,
  bp[ef], bp[f0]&31}`` carries the exact integer fields; the shader keeps the
  original uintBitsToFloat/integer-depth/rounding math, and the fog type,
  projection flag and comparison ops stay source-specialised.
* ``uNativeFogColor vec3`` carries the f2 byte RGB; ``uNativeAlphaRef vec2``
  the raw refs, but only for non-endpoint refs (0 and 255 keep their literal
  specialisation).
* ``NWII_LITERAL_SHADER_DATA=1`` restores the literal GLSL byte for byte.
* Uploads use the existing native_uniform_cache::Changed cache at the batch
  boundary inside SetupDrawState - they can never retro-edit a pending draw.
* The stale s_shader_by_src early return that skipped NativeUseProgram and all
  per-draw state setup is removed (CompileShader already dedups; the caller's
  setup must always run).  Both dedup maps now require exact VS+FS string
  identity, so an FNV collision cannot alias different sources - this also
  covers programs that arrive through the binary cache.
* g_stat_programs counts programs actually created (binary loads + links),
  distinct from s_shader_cache's state-key aliases (nwii_stat_programs()).

Idempotent and reversible via exact inverse pairs (--restore).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_SHADER_DATA_UNIFORMS_V1'
EXACT = 'NATIVE_MOM_SHADER_SRC_EXACT_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
TEV = RUNTIME + '/src/hle/gx/tev_shader_gen.cpp'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'
OVERLAY = 'overlays/native_shader_data.h'
HEADER = RUNTIME + '/include/runtime/native_shader_data.h'

TEV_PAIRS = [
    ('''#include "runtime/gx/tev_shader_gen.h"
''',
     '''#include "runtime/gx/tev_shader_gen.h"
#include "runtime/native_shader_data.h" // ''' + MARKER + '''
'''),

    # Fog data -> uniforms.  The literal branch emits the original text.
    ('''  const unsigned control=state.bp[0xf1];
  const unsigned type=(control>>21)&7;
  if(type==0) return;
  output << "    float fogA=uintBitsToFloat(" << ((state.bp[0xee]&0xfffff)<<12) << "u);\\n";
  output << "    float fogC=uintBitsToFloat(" << ((control&0xfffff)<<12) << "u);\\n";
''',
     '''  const unsigned control=state.bp[0xf1];
  const unsigned type=(control>>21)&7;
  if(type==0) return;
  const bool data_uniforms=NativeShaderDataUniformsEnabled(); // ''' + MARKER + '''
  output << "    float fogA=uintBitsToFloat(" << (data_uniforms?std::string("uNativeFogData.x"):std::to_string((state.bp[0xee]&0xfffff)<<12)+"u") << ");\\n";
  output << "    float fogC=uintBitsToFloat(" << (data_uniforms?std::string("uNativeFogData.y"):std::to_string((control&0xfffff)<<12)+"u") << ");\\n";
'''),
    ('''    output << "    float fogDenom=float(" << state.bp[0xef] << "u)-float(fogZ>>" << (state.bp[0xf0]&31) << "u);\\n";
''',
     '''    output << "    float fogDenom=float(" << (data_uniforms?std::string("uNativeFogData.z"):std::to_string(state.bp[0xef])+"u") << ")-float(fogZ>>" << (data_uniforms?std::string("uNativeFogData.w"):std::to_string(state.bp[0xf0]&31)+"u") << ");\\n";
'''),
    ('''  const unsigned color=state.bp[0xf2];
  output << "    vec3 fogColor=vec3(" << ((color>>16)&255) << "," << ((color>>8)&255) << "," << (color&255) << ")/255.0;\\n";
''',
     '''  const unsigned color=state.bp[0xf2];
  output << "    vec3 fogColor=" << (data_uniforms?std::string("uNativeFogColor/255.0"):std::string("vec3(")+std::to_string((color>>16)&255)+","+std::to_string((color>>8)&255)+","+std::to_string(color&255)+")/255.0") << ";\\n";
'''),

    # Uniform declarations, emitted only in uniform mode.
    ('''    fs << "uniform vec4 uTevKColor[4];\\n";
''',
     '''    fs << "uniform vec4 uTevKColor[4];\\n";
    if (NativeShaderDataUniformsEnabled())
        fs << "uniform uvec4 uNativeFogData;\\nuniform vec3 uNativeFogColor;\\nuniform vec2 uNativeAlphaRef;\\n"; // ''' + MARKER + '''
'''),

    # Alpha refs -> uNativeAlphaRef for non-endpoint values only.
    ('''        auto cmp = [&](uint8_t comp, uint8_t ref) -> std::string {
''',
     '''        auto cmp = [&](uint8_t comp, uint8_t ref, unsigned channel) -> std::string {
'''),
    ('''            std::string r(rb);
''',
     '''            // ''' + MARKER + ''': endpoints stay specialised literals; every
            // other reference value reads its uniform channel.
            std::string r = (NativeShaderDataUniformsEnabled() && ref != 0 && ref != 255)
                ? std::string(channel ? "uNativeAlphaRef.y" : "uNativeAlphaRef.x")
                : std::string(rb);
'''),
    ('''            std::string a = cmp(at.comp0, at.ref0);
            std::string b = cmp(at.comp1, at.ref1);
''',
     '''            std::string a = cmp(at.comp0, at.ref0, 0);
            std::string b = cmp(at.comp1, at.ref1, 1);
'''),
]

GL_PAIRS = [
    ('''#include "runtime/gx/tev_shader_gen.h"
''',
     '''#include "runtime/gx/tev_shader_gen.h"
#include "runtime/native_shader_data.h" // ''' + MARKER + '''
'''),
    ('''  GLint uLightCol, uLightPos, uLightDir, uLightCosAtt, uLightDistAtt;
};
''',
     '''  GLint uLightCol, uLightPos, uLightDir, uLightCosAtt, uLightDistAtt;
  GLint uNativeFogData=-1,uNativeFogColor=-1,uNativeAlphaRef=-1; // ''' + MARKER + '''
};
'''),
    ('''uint64_t g_stat_shaders = 0;
''',
     '''uint64_t g_stat_shaders = 0;
uint64_t g_stat_programs = 0; // ''' + EXACT + ''': distinct programs, not state-key aliases
'''),
    ('''  if (dedupe_exact) {
    auto same = s_program_by_source.find(source_key);
    if (same != s_program_by_source.end()) return same->second;
  }
''',
     '''  if (dedupe_exact) {
    auto same = s_program_by_source.find(source_key);
    // ''' + EXACT + ''': FNV identity confirmed by the full strings - a hash
    // collision can never alias a different source to this program.
    if (same != s_program_by_source.end() &&
        s_program_src_text[source_key] == std::string(vs) + '\\x1f' + fs)
      return same->second;
  }
'''),
    ('''  static std::unordered_map<uint64_t, GLuint> s_program_by_source;
''',
     '''  static std::unordered_map<uint64_t, GLuint> s_program_by_source;
  static std::unordered_map<uint64_t, std::string> s_program_src_text; // ''' + EXACT + '''
'''),
    ('''  if (dedupe_exact) s_program_by_source[source_key] = p; // NATIVE_MOM_SHADER_DEDUPE_FLOW_V1
''',
     '''  if (dedupe_exact) { s_program_by_source[source_key] = p; s_program_src_text[source_key] = std::string(vs) + '\\x1f' + fs; } // NATIVE_MOM_SHADER_DEDUPE_FLOW_V1
  ++g_stat_programs; // ''' + EXACT + ''': a program was linked here
'''),
    ('''  static std::unordered_map<uint64_t, GLShader> s_shader_by_src;
''',
     '''  static std::unordered_map<uint64_t, GLShader> s_shader_by_src;
  static std::unordered_map<uint64_t, std::string> s_src_text_by_key; // ''' + EXACT + '''
'''),
    ('''    static const bool dedupe_sources =
        std::getenv("NWII_NO_SHADER_SRC_DEDUP") == nullptr;
    if (dedupe_sources) {
      auto same = s_shader_by_src.find(source_key);
      if (same != s_shader_by_src.end()) {
        s_shader_cache[hash] = same->second;
        s_active_shader = same->second;
        if (gx_trace())
          printf("[GXTRACE] Reused program %u for key %llx\\n", same->second.id,
                 (unsigned long long)hash);
        return;
      }
''',
     '''    static const bool dedupe_sources =
        std::getenv("NWII_NO_SHADER_SRC_DEDUP") == nullptr;
    GLShader sh = {};
    bool shader_reused = false; // ''' + EXACT + '''
    if (dedupe_sources) {
      auto same = s_shader_by_src.find(source_key);
      if (same != s_shader_by_src.end() &&
          s_src_text_by_key[source_key] ==
              src.vertex_source + '\\x1f' + src.fragment_source) { // ''' + EXACT + '''
        s_shader_cache[hash] = same->second;
        sh = same->second;        // reuse the real GLShader incl. locations
        shader_reused = true;   // skip CompileShader + the lookup sweep
        if (gx_trace())
          printf("[GXTRACE] Reused program %u for key %llx\\n", same->second.id,
                 (unsigned long long)hash);
        // ''' + EXACT + ''': fall through to NativeUseProgram and the per-draw
        // state setup - the old early return left the previous program bound.
      }
'''),
    ('''    NativeShaderKeyFingerprint(g_state); // NATIVE_COARSE_SHADER_KEY_V1
    GLShader sh = {};
    sh.uProjMtx = -1;
''',
     '''    NativeShaderKeyFingerprint(g_state); // NATIVE_COARSE_SHADER_KEY_V1
    if (!shader_reused) {
    sh.uProjMtx = -1;
'''),
    ('''    s_shader_cache[hash] = sh;
    if (dedupe_sources) s_shader_by_src[source_key] = sh; // NATIVE_MOM_SHADER_SRC_DEDUP_V1
    s_active_shader = sh;
    if (gx_trace())
      printf("[GXTRACE] Compiled new shader! Hash: %llx\\n",
             (unsigned long long)hash);
  }
''',
     '''    s_shader_cache[hash] = sh;
    if (dedupe_sources) { s_shader_by_src[source_key] = sh; s_src_text_by_key[source_key] = src.vertex_source + '\\x1f' + src.fragment_source; } // NATIVE_MOM_SHADER_SRC_DEDUP_V1
    if (gx_trace())
      printf("[GXTRACE] Compiled new shader! Hash: %llx\\n",
             (unsigned long long)hash);
    }
    // ''' + EXACT + ''': reused alias or fresh program -
    // the uniform/state setup below always runs.
    s_active_shader = sh;
  }
'''),
    ('''    sh.uLightDistAtt = glGetUniformLocation(sh.id, "uLightDistAtt[0]");
''',
     '''    sh.uLightDistAtt = glGetUniformLocation(sh.id, "uLightDistAtt[0]");
    sh.uNativeFogData=glGetUniformLocation(sh.id,"uNativeFogData"); // ''' + MARKER + '''
    sh.uNativeFogColor=glGetUniformLocation(sh.id,"uNativeFogColor");
    sh.uNativeAlphaRef=glGetUniformLocation(sh.id,"uNativeAlphaRef");
'''),
    ('''  if (s_active_shader.uTevColor >= 0 &&
      native_uniform_cache::Changed(s_active_shader.id,
                                    s_active_shader.uTevColor, color, 64))
    glUniform4fv(s_active_shader.uTevColor, 4, color);
}
''',
     '''  if (s_active_shader.uTevColor >= 0 &&
      native_uniform_cache::Changed(s_active_shader.id,
                                    s_active_shader.uTevColor, color, 64))
    glUniform4fv(s_active_shader.uTevColor, 4, color);

  // ''' + MARKER + ''': fog/alpha data ride uniforms, so a value-only state
  // change no longer costs a program.  SetupDrawState runs only at a batch
  // boundary, so these uploads can never retro-edit a pending draw's values.
  if (NativeShaderDataUniformsEnabled()) {
    const GLuint fogdata[4] = {(g_state.bp[0xee]&0xfffffu)<<12,
                               (g_state.bp[0xf1]&0xfffffu)<<12,
                               g_state.bp[0xef], g_state.bp[0xf0]&31u};
    const uint32_t fogrgb = g_state.bp[0xf2];
    const float fogcol[3] = {float((fogrgb>>16)&255), float((fogrgb>>8)&255),
                             float(fogrgb&255)};
    const float alpharef[2] = {float(g_state.alphaTest.ref0),
                               float(g_state.alphaTest.ref1)};
    if (s_active_shader.uNativeFogData >= 0 &&
        native_uniform_cache::Changed(s_active_shader.id,
                                      s_active_shader.uNativeFogData, fogdata,
                                      sizeof fogdata))
      glUniform4uiv(s_active_shader.uNativeFogData, 1, fogdata);
    if (s_active_shader.uNativeFogColor >= 0 &&
        native_uniform_cache::Changed(s_active_shader.id,
                                      s_active_shader.uNativeFogColor, fogcol,
                                      sizeof fogcol))
      glUniform3fv(s_active_shader.uNativeFogColor, 1, fogcol);
    if (s_active_shader.uNativeAlphaRef >= 0 &&
        native_uniform_cache::Changed(s_active_shader.id,
                                      s_active_shader.uNativeAlphaRef,
                                      alpharef, sizeof alpharef))
      glUniform2fv(s_active_shader.uNativeAlphaRef, 1, alpharef);
  }
}
'''),
    ('''uint64_t nwii_stat_shaders() { return nwii::runtime::gx::g_stat_shaders; }
''',
     '''uint64_t nwii_stat_shaders() { return nwii::runtime::gx::g_stat_shaders; }
uint64_t nwii_stat_programs() { return nwii::runtime::gx::g_stat_programs; } // ''' + EXACT + '''
'''),
]

GL_PAIRS += [
    ('    if(const GLuint cached=cache.load(cache_key)){return cached;} // NATIVE_MOM_ADAPT_A4\n',
     '    if(const GLuint cached=cache.load(cache_key)){if(dedupe_exact){s_program_by_source[source_key]=cached;s_program_src_text[source_key]=std::string(vs)+char(31)+fs;}++g_stat_programs;return cached;} // NATIVE_MOM_ADAPT_A4 / NATIVE_MOM_SHADER_SRC_EXACT_V1: counted+registered once\n'),
    ('  glDeleteShader(f);\n',
     '''  glDeleteShader(f);
  GLint linked = 0;
  glGetProgramiv(p, GL_LINK_STATUS, &linked);
  if (!linked) {
    char log[2048] = {};
    glGetProgramInfoLog(p, sizeof(log) - 1, nullptr, log);
    std::fprintf(stderr, "[GX] shader link failed: %s\\n", log);
    glDeleteProgram(p);
    return 0;
  }
'''),
    ('        CompileShader(src.vertex_source.c_str(), src.fragment_source.c_str());\n',
     '''        CompileShader(src.vertex_source.c_str(), src.fragment_source.c_str());
    if (!sh.id) { s_skip_draw = true; return; }
'''),
]


def read(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline='') as h:
        return h.read().replace('\r\n', '\n')


def write(path, text):
    with open(path, 'rb') as h:
        crlf = b'\r\n' in h.read(4096)
    data = text.replace('\n', '\r\n') if crlf else text
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


EDITS = {TEV: TEV_PAIRS, GL: GL_PAIRS}


def install_header(root):
    data = (root / OVERLAY).read_bytes()
    target = root / HEADER
    if not target.exists() or target.read_bytes() != data:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return True
    return False


OWN_INCLUDES = [
    '#include "runtime/native_shader_data.h" // ' + MARKER + chr(10),
]


def apply(root):
    wrote = install_header(root)
    changed = []
    for rel, pairs in EDITS.items():
        target = root / rel
        if not target.is_file():
            raise SystemExit('%s: %s is missing' % (MARKER, rel))
        text = read(target)
        if rel == GL:
            text = '\n'.join(
                '    if(const GLuint cached=cache.load(cache_key)){return cached;} // NATIVE_MOM_ADAPT_A4'
                if line.lstrip().startswith('if(const GLuint cached=cache.load(cache_key))')
                else line for line in text.split('\n'))
        for line in OWN_INCLUDES:
            text = text.replace(line, '')  # dedupe: another stage may have inserted between anchor and line
        updated = apply_pairs(restore_pairs(text, pairs, rel), pairs, rel)
        if updated != text:
            write(target, updated)
            changed.append(rel)
    print('%s staged: %d file(s), header %s; NWII_LITERAL_SHADER_DATA=1 '
          'restores literal GLSL' % (MARKER, len(changed),
                                     'installed' if wrote else 'present'))


def restore(root):
    changed = []
    for rel, pairs in EDITS.items():
        target = root / rel
        if not target.is_file():
            continue
        orig = read(target)
        text = orig
        for line in OWN_INCLUDES:
            text = text.replace(line, '')
        updated = restore_pairs(text, pairs, rel)
        if updated != orig:
            write(target, updated)
            changed.append(rel)
    print('%s restored: %s (the header stays; it is inert once nothing uses it)'
          % (MARKER, ', '.join(changed) or 'already pristine'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
