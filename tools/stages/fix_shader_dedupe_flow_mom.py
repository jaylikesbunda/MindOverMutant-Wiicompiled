"""Deduplicate shader programs where it belongs: at the compile site.

`fix_shader_src_dedup_mom.py` hashes the generated GLSL inside
`SetupDrawState` and, on a hit, does:

    s_shader_cache[hash] = same->second;
    s_active_shader = same->second;
    return;                      // <-- never reaches glUseProgram

That `return` runs *before* `glUseProgram(s_active_shader.id)`, before
`ApplyProjection`, before `ApplyZMode/ApplyBlend/ApplyCullMode` and before every
per-draw uniform.  So every draw that hits the dedupe path is rendered with
whichever program was previously bound, using the previous draw's state.  That is
exactly the observed corruption: a character that vanishes (its program expects a
different vertex layout) and effect quads that draw opaque black (alpha never
blended).

It also explains why switching every optimised path off did not help: the dedupe
is on by default and has its own switch that was not in the set.

This stage moves the same idea to `CompileShader`, where a hit is just "reuse the
program for identical source" and the caller runs its normal state setup, so the
control flow cannot change.  The disk binary cache already keys on the source
hash, so this is the in-memory half of the same mechanism.

`NWII_NO_SHADER_SRC_DEDUP=1` keeps the original behaviour (no in-memory reuse);
the removed early return is gone either way, because it was a bug and not a
trade-off.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_SHADER_DEDUPE_FLOW_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('shader dedupe flow fix already present.')
    raise SystemExit(0)


def sub(old, new, what, count=1):
    global text
    assert text.count(old) == count, 'anchor %s (%d)' % (what, text.count(old))
    text = text.replace(old, new)


# ---- 1. remove the early return from SetupDrawState ------------------------
old_block = """    uint64_t source_key = 1469598103934665603ull;
    for (const char *p = src.vertex_source.data(); p != src.vertex_source.data() + src.vertex_source.size(); ++p)
      source_key = (source_key ^ uint8_t(*p)) * 1099511628211ull;
    for (const char *p = src.fragment_source.data(); p != src.fragment_source.data() + src.fragment_source.size(); ++p)
      source_key = (source_key ^ uint8_t(*p)) * 1099511628211ull;
    static const bool dedupe_sources =
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
    }
"""
sub(old_block, '', 'dedupe early return')

sub("""    if (dedupe_sources) s_shader_by_src[source_key] = sh; // NATIVE_MOM_SHADER_SRC_DEDUP_V1
""", '', 'dedupe bookkeeping')

sub("""  // NATIVE_MOM_SHADER_SRC_DEDUP_V1: one program per distinct generated source.
  static std::unordered_map<uint64_t, GLShader> s_shader_by_src;
""", '', 'dedupe map')

# ---- 2. dedupe inside CompileShader, where it cannot change flow -----------
sub("""static GLuint CompileShader(const char *vs, const char *fs) {
  auto& cache=NativeProgramBinaryCache();""",
    """static GLuint CompileShader(const char *vs, const char *fs) {
  // """ + MARK + """: identical source means an identical program, so reuse the one
  // already linked.  Done here rather than in SetupDrawState so the caller's
  // glUseProgram and per-draw state setup always run.
  static const bool dedupe_exact = std::getenv("NWII_NO_SHADER_SRC_DEDUP") == nullptr;
  static std::unordered_map<uint64_t, GLuint> s_program_by_source;
  uint64_t source_key = 1469598103934665603ull;
  for (const char *p = vs; *p; ++p) source_key = (source_key ^ uint8_t(*p)) * 1099511628211ull;
  for (const char *p = fs; *p; ++p) source_key = (source_key ^ uint8_t(*p)) * 1099511628211ull;
  if (dedupe_exact) {
    auto same = s_program_by_source.find(source_key);
    if (same != s_program_by_source.end()) return same->second;
  }
  auto& cache=NativeProgramBinaryCache();""",
    'CompileShader head')

sub("""  glDeleteShader(v);
  glDeleteShader(f);""",
    """  glDeleteShader(v);
  glDeleteShader(f);
  if (dedupe_exact) s_program_by_source[source_key] = p; // """ + MARK,
    'CompileShader tail')

path.write_text(text)
print('Shader dedupe moved to CompileShader; the early return is gone.')
