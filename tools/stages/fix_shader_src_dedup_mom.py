"""Link one program per distinct generated shader, not per state key.

The TEV cache is keyed on a 64-bit projection of the GX state, and `Render`
creates a program per key: 2,270 keys in a 240 s session.  But the generated
GLSL is a function of that state, not of the key, and many keys produce byte
identical sources - the on-disk program cache only holds 885 distinct binaries
for those 2,270 keys, so at least 1,385 of them are the same shader under a
different key.  Every one of those still pays a `glCreateProgram` +
link + driver JIT inside the draw call, which is the 20-120 ms spike measured in
`gl_render` (worst frame: `gl_render` 38.5 ms of a 43.2 ms frame).

This stage hashes the *generated source* and keeps a second cache keyed on it:

    s_shader_cache  : state key  -> GLShader   (kept, so lookups stay O(1))
    s_shader_by_src : source hash -> GLShader  (new, one program per source)

On a state-key miss the stage generates the source, hashes it, and if an existing
program was built from identical source it reuses it (uniform locations are a
property of the source, so the whole GLShader struct is valid) instead of
compiling.  Nothing about what is drawn changes: identical source means an
identical program.

`NWII_NO_SHADER_SRC_DEDUP=1` disables the reuse.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_SHADER_SRC_DEDUP_V1'
path = runtime / 'src/hle/gx/renderer_gl.cpp'
text = path.read_text()
if MARK in text:
    print('shader source dedupe already present.')
    raise SystemExit(0)


def sub(old, new, what):
    global text
    assert text.count(old) == 1, 'anchor %s' % what
    text = text.replace(old, new, 1)


# 1. a second cache, keyed by the generated source text.
sub("""  auto it = s_shader_cache.find(hash);
  if (it != s_shader_cache.end()) {
    s_active_shader = it->second;""",
    """  // """ + MARK + """: one program per distinct generated source.
  static std::unordered_map<uint64_t, GLShader> s_shader_by_src;
  auto it = s_shader_cache.find(hash);
  if (it != s_shader_cache.end()) {
    s_active_shader = it->second;""",
    'secondary cache')

# 2. after generating, reuse a program whose source is identical.
sub("""    auto src = GenerateTEVShader(g_state, cmd.prim_type);""",
    """    auto src = GenerateTEVShader(g_state, cmd.prim_type);
    uint64_t source_key = 1469598103934665603ull;
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
    }""",
    'source hash')

# 3. remember every compiled program by source.
sub("""    s_shader_cache[hash] = sh;
    s_active_shader = sh;""",
    """    s_shader_cache[hash] = sh;
    if (dedupe_sources) s_shader_by_src[source_key] = sh; // """ + MARK
    + """
    s_active_shader = sh;""",
    'remember program')

path.write_text(text)
print('Programs are deduplicated by generated source.')
