"""Give the shader lane its own key cache and take its filesystem work out of the draw.

`gl_render` is normally 0.0x ms but spikes to 20-120 ms about 9-13 times per
second, and the spikes are TEV program creation inside the draw call.  A 240 s
session asks for 2,270 distinct state keys but generates only ~885 distinct
GLSL sources, so most of those spikes are a link plus driver JIT for a program
whose source already exists.  `fix_shader_src_dedup_mom.py` (applied before this
stage) reuses the program in that case.  What is left on the draw path is the
key itself and the program-binary write, and this stage removes both.

Part 1 - key memoisation (`NWII_NO_SHADER_KEY_MEMO=1` disables)
    `NativeTevShaderHash` is a pure function of a fixed set of `GXState` words.
    It folds them byte by byte through an FNV chain, i.e. four *dependent*
    64-bit multiplies per word, ~470 for a full call: a ~1.5k cycle serial
    chain, paid on every batch topology change and on every DRAWLOG line.  The
    stage splits the function into `NativeTevShaderHashUncached` (the previous
    body, byte for byte) and a wrapper that gathers the consumed words into one
    array and memcmp's them against the previous call's array.

    Ported from CotT's `native-port/tools/fix_state_epochs.py`, but the *cheap
    self-invalidating variant*, not the epoch protocol.  CotT's epoch counts on
    an `Invalidate()` call at every writer of the words the key reads.  Those
    writers in this tree are:

        fifo_parser.cpp:76        ParseBP()             g_state.bp[reg]
        fifo_parser.cpp:80        ApplyBPRegisterImpl() g_state.bp[reg], and it
                                  *derives* numTevStages (bp[0x00]), cullMode,
                                  tevStages[].texMap/texCoord/colorChan
                                  (bp[0x28..0x2F]), tevStages[] combiners
                                  (bp[0xC0..0xDF]), alphaTest (bp[0xF3]),
                                  texStages[] (bp[0x88.. / 0xA8..)
        fifo_parser.cpp:228       CP 0x50/0x60
        renderer_gl.cpp:2017      Render's XF loop -> g_state.xf[]
        renderer_gl.cpp:896       NativeRestoreBpSnapshot() memcpy's all 256
                                  BP words before every Render pass, without
                                  re-deriving the fields above

    An epoch protocol would have to be correct at all five, and at the
    derivation sites, and across the restore.  A memcmp cannot be wrong: the
    cached hash is reused only when every input word is bit-identical, so the
    result is exactly the uncached result.  The gathered set is a superset of
    what the hash reads in either prune setting (see part 3), so the two
    switches never have to agree.

Part 2 - program-binary writes off the renderer (`NWII_NO_SHADER_ASYNC_STORE=1`)
    `store()` copied the binary out of the driver, then did create/truncate,
    write and rename - three filesystem operations, ~25 KB each, inside
    CompileShader, inside a draw.  The stage keeps the extraction on the GL
    thread (it is a GL call) and queues the finished record; the queue is
    drained by `flush_pending()` at the end of the Render pass, after the batch
    flush, and from the cache's destructor.  `load()` stays synchronous: it is
    the call that turns a recompile into a load, so deferring it would only
    move the stall.  The queue is capped at 32 MiB (~1,300 binaries) and
    drained early rather than dropped if the cap is ever reached.

Part 3 - key pruning (`NWII_NO_SHADER_KEY_PRUNE=1`)
    `GenerateTEVShader` (src/hle/gx/tev_shader_gen.cpp) reads exactly:

        state.bp[0x00]                 num TEV stages / ind tex count / texgens
        state.bp[0x06..0x0E]           indirect matrix coefficients + exponents
                                       (base = 6 + (matrix-1)*3, up to base+2)
        state.bp[0x10 + stage]         per-TEV-stage indirect config
        state.bp[0x25..0x27]           indirect scale and command/order words
        state.bp[0x28..0x2F]           *via ApplyBPRegisterImpl* -> tevStages[]
                                       .texMap/.texCoord/.colorChan
        state.bp[0xC0..0xDF]           *via ApplyBPRegisterImpl* -> tevStages[]
                                       combiners, scale, clamp, reg ids
        state.bp[0xEE..0xF2]           fog A/B/C/control/colour
        state.bp[0xF3]                 *via ApplyBPRegisterImpl* -> alphaTest
        state.bp[0xF6..0xFD]           konst selectors and RGBA swap tables
        state.numTevStages             (derived from bp[0x00])
        state.tevStages[]              (derived from the two ranges above)
        state.alphaTest                (derived from bp[0xF3])
        state.xf[0x40..0x47]           bits 4-6 = texgen type per texcoord

    It reads nothing else: no `cp[]`, no other `xf[]`, no `texStages[]`, and
    it never looks at `prim_type`.  Against `GetShaderHash`, that makes these
    hash inputs pollution:

        cp[0x50], cp[0x60]      VCD/VAT: host-side vertex decode only
        xf[0x103F - 0x1000]     XF colour-channel count; the vertex shader emits
                                both channels and every attribute unconditionally
        texStages[t].format,
        .tlut_format (x8)       sampler/TLUT decode, host-side; the fragment
                                shader declares `uniform sampler2D` per unit
        bp[0xE9..0xED]          fog starts at 0xEE; unread
        bp[0x0F]                IND_IMASK; no code in this tree decodes it
        bp[0x20..0x24]          raster line/point sizes (0x22 is read by
                                NativeEmitLinesPoints), not shader state
        prim_type               unused parameter, and both call sites pass 0x90

    The four BP 0x40..0x43 words were the same kind of pollution and
    `fix_shader_key_mom.py` already removed them, so they are left alone.
    `bp[0x28..0x2F]` is *not* pollution: it decodes into the TEV stage texmap /
    texcoord / colour-channel fields the generator bakes in, and it is hashed
    for that reason.  Neither is any part of bp[0x06..0x27] or bp[0xC0..0xF2]
    apart from the two ranges named above, and nothing the generator reads is
    missing from the key: 0x10..0x1F (indirect config) comes in through the
    renderer's 0x06..0x27 range, 0x28..0x2F and 0xC0..0xDF through
    GetShaderHash, the fog words through the renderer's 0xEE..0xF2 range, the
    konst/swizzle words through 0xF6..0xFD, and the texgen nibbles through
    xf[0x40..0x47].  `prim_type` stays as well: it is a constant at both call
    sites, so it costs no keys, and the parameter is what the caller's
    signature promises.

    The prune is a statement about today's generator.  If a later change makes
    GenerateTEVShader read a pruned word, that change has to revisit this list;
    the list above is the contract, and NWII_NO_SHADER_KEY_PRUNE=1 is the
    one-flag rollback.

    Pruning changes the *value* of the key, and that costs nothing on disk: the
    program-binary cache is keyed by the generated source text
    (`NativeProgramCache::key()`), not by the state key, so every binary it
    already holds still loads.  Only the in-memory state-key map starts cold,
    and a miss there now costs one `GenerateTEVShader` plus the source hash
    that the src-dedup stage already computes for every miss, not a link.

Ordering
    Must run after `fix_shader_key_mom.py` (its key body and `CoarseShaderKey()`
    are the anchors for part 3), after `fix_program_store_mom.py` (its drain of
    the GL error queue stays at the head of `store()`, which this stage only
    rewrites from the record-building line down) and after
    `fix_shader_src_dedup_mom.py` (that stage's `s_shader_by_src` reuse is what
    makes the memoised key worth having, and the two must both be in place
    before either is measured).  Register it last in `tools/apply_stages.py`.

Idempotence
    A second run finds MARK in both files, verifies every anchor of the
    canonical text and exits without writing anything.
"""
from pathlib import Path
import re

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_SHADER_LANE_V1'

HEADER = runtime / 'include/runtime/gx_state.h'
RENDERER = runtime / 'src/hle/gx/renderer_gl.cpp'

# Canonical fragments a finished application must contain, one list per file.
# They double as the partial-application guard: MARK in only one file, or MARK
# in both with a fragment missing, is an error rather than a silent no-op.
HEADER_NEEDLES = (
    'static bool PruneShaderKey() {',
    'if (PruneShaderKey()) add_u32(bp[0xEE]);',
    'else for (int i = 0xE9; i <= 0xEE; ++i) add_u32(bp[i]);',
    'if (!PruneShaderKey())\n      for (int i = 0; i < 8; ++i) {',
    'if (!PruneShaderKey()) {\n          add_u32(cp[0x50]);',
)
RENDERER_NEEDLES = (
    'static unsigned NativeShaderKeyGather(',
    'static uint64_t NativeTevShaderHashUncached(',
    'if (have && std::memcmp(previous, words, sizeof words) == 0) {',
    'std::printf("[SHKEYMEMO] hits=%llu misses=%llu\\n",',
    '[SHADERIO] binaries=%llu',
    'if (GXState::PruneShaderKey()) {',
    'for(unsigned reg=0x25;reg<=0x27;++reg)',
    'pending.back().blob.swap(blob);',
    'NativeProgramBinaryCache().flush_pending();\n    native_batch_census::MaybeReport();',
)


def function_block(text, signature):
    """(start, end) of the definition whose head is |signature|, braces matched."""
    a = text.index(signature)
    assert text.count(signature) == 1, 'anchor is not unique: %s' % signature
    index = text.index('{', a)
    depth = 1
    index += 1
    while depth:
        depth += (text[index] == '{') - (text[index] == '}')
        index += 1
    return a, index


def substitute(text, old, new, what):
    assert text.count(old) == 1, 'anchor: %s (%d)' % (what, text.count(old))
    return text.replace(old, new, 1)


def rebuild(text, pattern, make, what):
    """Replace the single line-block matching |pattern| with make(indent, match)."""
    hits = list(re.finditer(pattern, text, re.M))
    assert len(hits) == 1, 'anchor: %s (%d)' % (what, len(hits))
    match = hits[0]
    return text[:match.start()] + make(match.group(1), match) + text[match.end():]


# ---- part 3: the key in GXState::GetShaderHash ------------------------------

PRUNE_HELPER = '''  // {mark}: the key selects a compiled program, and a program is a function of
  // the generated GLSL only.  Inputs GenerateTEVShader never reads split one
  // program into several keys (see tools/stages/fix_shader_lane_mom.py for the
  // generator's full read-set), so they leave the key by default.
  // NWII_NO_SHADER_KEY_PRUNE=1 folds them all back in, word for word.
  static bool PruneShaderKey() {{
      static const bool prune = []() {{
          const char* v = std::getenv("NWII_NO_SHADER_KEY_PRUNE");
          return !(v != nullptr && v[0] != '\\0' && v[0] != '0');
      }}();
      return prune;
  }}

'''.format(mark=MARK)

HEADER_EDITS = (
    # fog: only bp[0xEE] is read by NativeWriteFog; 0xE9..0xED are not read.
    (r'^([ \t]*)for \(int i = 0xE9; i <= 0xEE; \+\+i\) add_u32\(bp\[i\]\);[ \t]*$',
     lambda indent, _m: (
         indent + '// ' + MARK + ': the fog block the generator reads is bp[0xEE..0xF2\n'
         + indent + '// (NativeWriteFog); 0xE9..0xED are never read by it, so folding them in\n'
         + indent + '// only split one program into several keys.\n'
         + indent + 'if (PruneShaderKey()) add_u32(bp[0xEE]);\n'
         + indent + 'else for (int i = 0xE9; i <= 0xEE; ++i) add_u32(bp[i]);'),
     'fog range'),
    # texture format / TLUT format: host-side sampler state, not source.
    (r'^([ \t]*)for \(int i = 0; i < 8; \+\+i\) \{\n'
     r'[ \t]*add_u8\(texStages\[i\]\.format\);\n'
     r'[ \t]*add_u8\(texStages\[i\]\.tlut_format\);\n'
     r'[ \t]*\}[ \t]*$',
     lambda indent, _m: (
         indent + '// ' + MARK + ': the sampler path decodes these host-side and the generated\n'
         + indent + '// fragment shader declares `uniform sampler2D` per unit regardless, so a\n'
         + indent + '// format change compiled a byte-identical program under a new key.\n'
         + indent + 'if (!PruneShaderKey())\n'
         + indent + 'for (int i = 0; i < 8; ++i) {\n'
         + indent + '    add_u8(texStages[i].format);\n'
         + indent + '    add_u8(texStages[i].tlut_format);\n'
         + indent + '}'),
     'texture formats'),
    # VCD/VAT and the XF colour-channel count.
    (r'^([ \t]*)add_u32\(cp\[0x50\]\);[ \t]*\n'
     r'[ \t]*add_u32\(cp\[0x60\]\);[ \t]*\n'
     r'[ \t]*\n'
     r'[ \t]*add_u32\(xf\[0x103F - 0x1000\]\);[ \t]*$',
     lambda indent, _m: (
         indent + '// ' + MARK + ': CP 0x50/0x60 (VCD/VAT) drive the host-side vertex decoder and\n'
         + indent + '// XF 0x103F is the colour-channel count; the vertex shader emits every\n'
         + indent + '// attribute and both colour channels unconditionally.\n'
         + indent + 'if (!PruneShaderKey()) {\n'
         + indent + '    add_u32(cp[0x50]);\n'
         + indent + '    add_u32(cp[0x60]);\n'
         + indent + '    add_u32(xf[0x103F - 0x1000]);\n'
         + indent + '}'),
     'vertex-descriptor words'),
)


def apply_header(text):
    text = substitute(text, '  uint64_t GetShaderHash(uint8_t prim_type) const {\n',
                      PRUNE_HELPER + '  uint64_t GetShaderHash(uint8_t prim_type) const {\n',
                      'GetShaderHash head')
    for pattern, make, what in HEADER_EDITS:
        text = rebuild(text, pattern, make, what)
    return text


# ---- part 1: the memoised key ----------------------------------------------

KEY_PREAMBLE = '''// {mark}: memoise the key.  The key is a pure function of a fixed word set -
// GXState::GetShaderHash() folds bp[0x00], bp[0x28..0x2F], bp[0xC0..0xDF],
// bp[0xE9..0xEE], bp[0xF3], cp[0x50], cp[0x60] and the eight texture formats,
// and this function adds bp[0x06..0x27], bp[0xEE..0xF2], bp[0xF6..0xFD] and the
// texgen type nibbles of xf[0x40..0x47].  Folding ~118 words costs four
// dependent 64-bit multiplies each, so one call is a ~1.5k cycle serial chain,
// paid on every batch topology change and every DRAWLOG line.
//
// This is CotT's fix_state_epochs mechanism as its self-invalidating variant:
// the previous call's inputs are kept verbatim and memcmp'd, instead of an
// epoch bumped by every writer of g_state.bp/xf/cp (ParseBP,
// ApplyBPRegisterImpl and its derived TEV/alpha/tex fields, Render's XF loop,
// and the whole-array memcpy in NativeRestoreBpSnapshot).  Reusing the hash
// only when every input word is bit-identical makes a stale hit impossible,
// and the result is exactly the uncached result.
//
// The gathered set is a superset of what the hash reads with or without
// NWII_NO_SHADER_KEY_PRUNE, so the two switches stay independent.
// NWII_NO_SHADER_KEY_MEMO=1 recomputes on every call.
static constexpr unsigned kNativeShaderKeyWords =
    1 + 1 + 4 + 1 + 32 + 8 + 10 + 34 + 8 + 2 + 1 + 8 + 8; // = 118

// Writes the consumed words in one fixed order and returns how many.
static unsigned NativeShaderKeyGather(const GXState &state, uint8_t primitive,
                                     uint32_t *words) {{
  unsigned n = 0;
  words[n++] = primitive;
  words[n++] = state.bp[0x00];
  for (unsigned reg = 0x40; reg <= 0x43; ++reg) words[n++] = state.bp[reg];
  words[n++] = state.bp[0xF3];
  for (unsigned reg = 0xC0; reg <= 0xDF; ++reg) words[n++] = state.bp[reg];
  for (unsigned reg = 0x28; reg <= 0x2F; ++reg) words[n++] = state.bp[reg];
  for (unsigned reg = 0xE9; reg <= 0xF2; ++reg) words[n++] = state.bp[reg];
  for (unsigned reg = 0x06; reg <= 0x27; ++reg) words[n++] = state.bp[reg];
  for (unsigned reg = 0xF6; reg <= 0xFD; ++reg) words[n++] = state.bp[reg];
  words[n++] = state.cp[0x50];
  words[n++] = state.cp[0x60];
  words[n++] = state.xf[0x3F];
  for (unsigned reg = 0x40; reg <= 0x47; ++reg) words[n++] = state.xf[reg];
  for (unsigned t = 0; t < 8; ++t)
    words[n++] = uint32_t(state.texStages[t].format) |
                 (uint32_t(state.texStages[t].tlut_format) << 8);
  return n;
}}

'''.format(mark=MARK)

# The renderer's own blanket 0x06..0x27 range hides two more unread words.
KEY_LOOP_OLD = """  for(unsigned reg=6;reg<=0x27;++reg)
    for(unsigned shift=0;shift<32;shift+=8)
      hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;"""

KEY_LOOP_NEW = """  // """ + MARK + """: the blanket 0x06..0x27 range also covered bp[0x0F]
  // (IND_IMASK, which this runtime never decodes) and bp[0x20..0x24] (raster
  // line/point sizes; 0x22 is read by NativeEmitLinesPoints).  The generator
  // reads the indirect matrices (0x06..0x0E), the per-TEV-stage indirect
  // configs (0x10..0x1F) and the indirect scale/command words (0x25..0x27).
  if (GXState::PruneShaderKey()) {
    for(unsigned reg=0x06;reg<=0x0E;++reg)
      for(unsigned shift=0;shift<32;shift+=8)
        hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;
    for(unsigned reg=0x10;reg<=0x1F;++reg)
      for(unsigned shift=0;shift<32;shift+=8)
        hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;
    for(unsigned reg=0x25;reg<=0x27;++reg)
      for(unsigned shift=0;shift<32;shift+=8)
        hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;
  } else {
    for(unsigned reg=6;reg<=0x27;++reg)
      for(unsigned shift=0;shift<32;shift+=8)
        hash=(hash ^ ((state.bp[reg]>>shift)&255))*1099511628211ull;
  }"""

KEY_WRAPPER = '''static uint64_t NativeTevShaderHash(const GXState &state, uint8_t primitive) {
  static const bool memo = std::getenv("NWII_NO_SHADER_KEY_MEMO") == nullptr;
  if (!memo) return NativeTevShaderHashUncached(state, primitive);
  // NWII_SHADER_LANE_STATS=1 prints one line at exit, so a run can show that
  // the memo is hit rather than merely present.
  struct Stats {
    uint64_t hits = 0, misses = 0;
    ~Stats() {
      if (std::getenv("NWII_SHADER_LANE_STATS") == nullptr) return;
      std::printf("[SHKEYMEMO] hits=%llu misses=%llu\\n",
                  (unsigned long long)hits, (unsigned long long)misses);
    }
  };
  static Stats stats;
  static uint32_t previous[kNativeShaderKeyWords];
  static uint64_t cached = 0;
  static bool have = false;
  uint32_t words[kNativeShaderKeyWords];
  const unsigned n = NativeShaderKeyGather(state, primitive, words);
  // A gather that no longer fills the array disables the memo instead of
  // comparing a prefix: the answer stays exact under any future edit here.
  if (n != kNativeShaderKeyWords) return NativeTevShaderHashUncached(state, primitive);
  if (have && std::memcmp(previous, words, sizeof words) == 0) {
    ++stats.hits;
    return cached;
  }
  ++stats.misses;
  std::memcpy(previous, words, sizeof words);
  have = true;
  return cached = NativeTevShaderHashUncached(state, primitive);
} // ''' + MARK


def apply_key(text):
    start, end = function_block(
        text, 'static uint64_t NativeTevShaderHash(const GXState &state, uint8_t primitive) {')
    body = text[start:end]
    uncached = substitute(
        body,
        'static uint64_t NativeTevShaderHash(const GXState &state, uint8_t primitive) {',
        'static uint64_t NativeTevShaderHashUncached(const GXState &state, uint8_t primitive) {',
        'uncached body')
    uncached = substitute(uncached, KEY_LOOP_OLD, KEY_LOOP_NEW, 'indirect register range')
    return text[:start] + KEY_PREAMBLE + uncached + '\n\n' + KEY_WRAPPER + text[end:]


# ---- part 2: deferred program-binary writes --------------------------------

CACHE_MEMBERS_ANCHOR = """  std::filesystem::path directory;std::string salt;uint64_t hits=0,stores=0,rejected=0;
  NativeProgramCache() {"""

CACHE_MEMBERS = """  std::filesystem::path directory;std::string salt;uint64_t hits=0,stores=0,rejected=0;
  // """ + MARK + """: a program binary is ~25 KB and writing it is
  // create/truncate + write + rename, which used to run inside CompileShader,
  // i.e. inside a draw.  The bytes are already copied out of the driver on the
  // GL thread, so only the write moves: queue the record, drain once per
  // Render pass.  load() stays synchronous on purpose - it is the call that
  // turns a recompile into a load, so deferring it only moves the stall.
  // NWII_NO_SHADER_ASYNC_STORE=1 writes from store() again.
  struct PendingWrite {std::filesystem::path target;std::vector<char> blob;};
  std::vector<PendingWrite> pending;
  size_t pending_bytes=0;
  static constexpr size_t kPendingCap=32u<<20; // ~1,300 binaries, then drain early
  bool write_blob(const std::filesystem::path &target,const std::vector<char> &blob) {
    auto temporary=target;temporary+=".tmp";
    {std::ofstream out(temporary,std::ios::binary|std::ios::trunc);if(!out)return false;
     out.write(blob.data(),std::streamsize(blob.size()));if(!out)return false;}
    std::error_code error;std::filesystem::rename(temporary,target,error);
    if(error){std::filesystem::remove(temporary,error);return false;}
    return true;
  }
  // End of the Render pass and teardown.  No GL, so it is safe with no
  // context current; every caller already has the program's bytes.
  void flush_pending() {
    if(pending.empty())return;
    for(const auto &write : pending) if(write_blob(write.target,write.blob))++stores;
    pending.clear();pending_bytes=0;
  }
  ~NativeProgramCache(){
    flush_pending();
    // NWII_SHADER_LANE_STATS=1: binaries written, for the program-cache count.
    if(std::getenv("NWII_SHADER_LANE_STATS")!=nullptr && stores)
      std::printf("[SHADERIO] binaries=%llu\\n",(unsigned long long)stores);
  }
  NativeProgramCache() {"""

STORE_TAIL = """    const auto final_path=path(value);auto temporary=final_path;temporary+=".tmp";
    {std::ofstream out(temporary,std::ios::binary|std::ios::trunc);if(!out)return;
     out.write(reinterpret_cast<const char*>(&header),sizeof header);out.write(data.data(),written);if(!out)return;}
    std::error_code error;std::filesystem::rename(temporary,final_path,error);
    if(error){std::filesystem::remove(temporary,error);return;}
    ++stores;
  }"""

STORE_TAIL_NEW = """    // """ + MARK + """: hand the finished record to the frame-end writer.
    std::vector<char> blob(sizeof header+size_t(written));
    std::memcpy(blob.data(),&header,sizeof header);
    std::memcpy(blob.data()+sizeof header,data.data(),size_t(written));
    static const bool async_store=std::getenv("NWII_NO_SHADER_ASYNC_STORE")==nullptr;
    if(async_store){
      if(pending_bytes+blob.size()>kPendingCap)flush_pending();
      pending_bytes+=blob.size();
      pending.resize(pending.size()+1);
      pending.back().target=path(value);
      pending.back().blob.swap(blob);
      return;
    }
    if(write_blob(path(value),blob))++stores;
  }"""

RENDER_DRAIN_ANCHOR = """    native_batch_census::MaybeReport();
  } // NATIVE_SUBMISSION_BATCH_MOM_V1_DRAIN"""

RENDER_DRAIN = """    // """ + MARK + """: the batch is closed, so this is outside every draw.
    NativeProgramBinaryCache().flush_pending();
    native_batch_census::MaybeReport();
  } // NATIVE_SUBMISSION_BATCH_MOM_V1_DRAIN"""


def apply_store(text):
    text = substitute(text, CACHE_MEMBERS_ANCHOR, CACHE_MEMBERS, 'program cache members')
    text = substitute(text, STORE_TAIL, STORE_TAIL_NEW, 'store() record tail')
    text = substitute(text, RENDER_DRAIN_ANCHOR, RENDER_DRAIN, 'Render drain')
    return text


def main():
    header, renderer = HEADER.read_text(), RENDERER.read_text()
    done = (MARK in header, MARK in renderer)
    if any(done):
        assert all(done), 'partial application of %s: missing in %s' % MARK
        for needles, text, name in ((HEADER_NEEDLES, header, 'gx_state.h'),
                                    (RENDERER_NEEDLES, renderer, 'renderer_gl.cpp')):
            missing = [n for n in needles if n not in text]
            assert not missing, '%s: noncanonical %s (missing %r)' % (MARK, name, missing[0])
        print('shader lane stage already present.')
        return

    new_header = apply_header(header)
    new_renderer = apply_store(apply_key(renderer))
    for needles, text, name in ((HEADER_NEEDLES, new_header, 'gx_state.h'),
                                (RENDERER_NEEDLES, new_renderer, 'renderer_gl.cpp')):
        missing = [n for n in needles if n not in text]
        assert not missing, 'post-check %s: missing %r' % (name, missing[0])
    if new_header != header:
        HEADER.write_text(new_header)
    if new_renderer != renderer:
        RENDERER.write_text(new_renderer)
    print('Shader keys are memoised on their inputs, pruned to the generator\'s '
          'read-set, and program binaries are written after the draw.')


if __name__ == '__main__':
    main()
