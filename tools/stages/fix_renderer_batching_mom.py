"""Keep one GL batch open across adjacent draws, as the CotT renderer does.

Ported from the Crash of the Titans port (tools/fix_renderer_submission.py and
tools/fix_flush_reason_census.py, re-derived against this tree).

This renderer closes the open batch on *every* non-draw command
(renderer_gl.cpp, Render loop), so a frame of ~84 draws is ~84 buffer uploads,
~84 draw calls and ~84 SetupDrawState passes. Two things make that unnecessary:

  * most state writes between adjacent draws repeat state that is already in
    force and cannot change what the queued draw consumes;
  * indexed quads, strips and fans all submit ordered GL_TRIANGLES, so they can
    share one batch instead of closing it on every topology change.

What is installed here, all of it game-independent:

  NativeBatchTopology     keys the batch on the canonical topology
  NativeRedundantBp       a BP write that resolves to the shadowed value
  NativeRedundantXf       an XF write whose every word is already in force
  NativeBatchContinues    draws/CP and redundant state continue the batch
  EmitPrimitive prefix    a batch that mixes array and indexed runs back-fills
                          the index prefix, so no run is dropped silently
  native_batch_census     opt-in count of flush requests, empty requests,
                          submissions and vertices, one [BATCH] line per 3 s

Boundaries that stay boundaries, deliberately:

  * BP 0x52 (EFB copy / XFB latch / copy clear) - it reads and writes the EFB;
  * BP 0x45/0x47/0x48 (PE token/finish) - the guest must never be signalled
    while draws are still queued behind the token;
  * BP 0x80-0x9f and 0xa0-0xbf (texture image/sampler) - rebinding an identical
    texture register can expose changed guest texels, and this renderer binds
    textures only when the batch is closed;
  * BP 0x64/0x65 (TLUT) - same class, and the TLUT copy runs at parse time;
  * any XF write that actually changes matrix/projection/light state, and every
    XF write while a textured draw is possible (this runtime has no
    texture-used mask, so the test is the conservative one);
  * a draw whose shader is not ready yet, and the end-of-stream drain.

Every part is individually defeatable so the same binary can be A/B'd:
    NWII_NO_BATCH_MERGE=1   close the batch on every non-draw command (today)
    NWII_NO_BATCH_CANON=1   do not canonicalise the batch topology
    NWII_BATCH_CENSUS=1     print the [BATCH] census line (default off)

Idempotency: every edit carries its own marker and is skipped when that marker
is present. That matters here because fix_indexed_batches.py, which runs before
this stage, unconditionally splices a canonical EmitPrimitive back in and would
otherwise drop the mixed-index prefix fix on the second run. Anchors are
asserted, so a renderer that has moved is reported instead of half-patched.

Measured on this title (boot, logos, title, name entry, intro movie, the ~84
draws/frame phase): submissions == decoded draws in every 3 s window, in both
A/B arms (per_draw = 1.000, 162 964 of 162 964 and 186 299 of 186 299). The
stream writes draw-visible state before essentially every draw - 44% texture
image registers (0x80-0x87), 26% BP 0x40 ZMode, 8% TLUT, 6% PE finish - and of
the texture writes only ~29% repeat a value already in force, so there is
nothing left to merge without relaxing a boundary that exists for correctness.
The machinery is therefore neutral on this title; it is kept because it is
correct and costs nothing when a state write is real, and because the census it
installs is what showed this. Re-measure with NWII_BATCH_CENSUS=1 on a scene
with repeated state (or after a stage that stops rebinding unchanged textures).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / "vendor").is_dir():
    # Run directly from tools/stages/: the repo root is one level up. The
    # runner (tools/apply_stages.py) rewrites the line above to the repo root.
    port = Path(__file__).resolve().parents[2]

runtime = port / "vendor/NWiiRecomp/nWiiRuntime"
renderer = runtime / "src/hle/gx/renderer_gl.cpp"
shader = runtime / "src/hle/gx/tev_shader_gen.cpp"

MARK = "NATIVE_SUBMISSION_BATCH_MOM_V1"
CENSUS = "NATIVE_BATCH_CENSUS_V1"
PREFIX = "NATIVE_MIXED_INDEX_PREFIX_V1"

text = renderer.read_text()

# The generated shader source does not depend on the primitive type: prim_type
# only enters GXState::GetShaderHash, which is a cache key. Sharing one key for
# every topology is only valid while that holds.
shader_text = shader.read_text()
assert shader_text.count("prim_type") == 1, (
    "prim_type is used in shader generation; topology cannot be dropped from "
    "the shader key")

applied = []


def edit(marker, old, new):
    """Install `new` in place of `old`, once, guarded by `marker`."""
    global text
    if marker in text:
        return
    count = text.count(old)
    assert count == 1, ("%s: anchor matched %d times: %r" % (marker, count, old[:90]))
    assert marker in new, "edit %s does not carry its marker" % marker
    text = text.replace(old, new)
    applied.append(marker)


# --- the census, installed before FlushBatch needs it -----------------------
edit(
    CENSUS + "_INCLUDE",
    "#include <cstring>\n",
    "#include <cstring>\n"
    "#include <chrono> // " + CENSUS + "_INCLUDE\n",
)

edit(
    CENSUS + "_DEFS",
    "static void FlushBatch() {\n"
    "  if (s_batch.empty())\n"
    "    return;\n",
    "// " + CENSUS + ": why the open batch closed, and how many GL submissions\n"
    "// and vertices that produced. Counted only when NWII_BATCH_CENSUS is set,\n"
    "// and only on the host render thread, so the counters need no atomics.\n"
    "namespace native_batch_census {\n"
    "enum Reason { Opening = 0, Topology, Bp, Xf, Other, Drain, ReasonCount };\n"
    "inline bool Enabled() {\n"
    "  static const bool on = std::getenv(\"NWII_BATCH_CENSUS\") != nullptr;\n"
    "  return on;\n"
    "}\n"
    "inline const char *Name(unsigned reason) {\n"
    "  static const char *const names[ReasonCount] = {\"opening\", \"topology\", \"bp\",\n"
    "                                               \"xf\", \"other\", \"drain\"};\n"
    "  return reason < ReasonCount ? names[reason] : \"unknown\";\n"
    "}\n"
    "inline uint64_t total_draws = 0, total_requests = 0, total_empty = 0;\n"
    "inline uint64_t total_submits = 0, total_vertices = 0;\n"
    "inline uint64_t window_draws = 0, window_requests = 0, window_empty = 0;\n"
    "inline uint64_t window_submits = 0, window_vertices = 0;\n"
    "inline uint64_t window_req[ReasonCount] = {};\n"
    "inline uint64_t window_emp[ReasonCount] = {};\n"
    "inline uint64_t window_sub[ReasonCount] = {};\n"
    "inline uint64_t window_vert[ReasonCount] = {};\n"
    "inline uint64_t report_last_ns = 0, report_start_ns = 0;\n"
    "inline uint64_t Now() {\n"
    "  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(\n"
    "                      std::chrono::steady_clock::now().time_since_epoch())\n"
    "                      .count());\n"
    "}\n"
    "inline void Request(bool on, unsigned reason, bool empty) {\n"
    "  if (!on) return;\n"
    "  ++total_requests; ++window_requests; ++window_req[reason];\n"
    "  if (empty) { ++total_empty; ++window_empty; ++window_emp[reason]; }\n"
    "}\n"
    "inline void Submit(bool on, unsigned reason, uint64_t vertices) {\n"
    "  if (!on) return;\n"
    "  ++total_submits; ++window_submits; ++window_sub[reason];\n"
    "  total_vertices += vertices; window_vertices += vertices;\n"
    "  window_vert[reason] += vertices;\n"
    "}\n"
    "inline void Draw() { ++total_draws; ++window_draws; }\n"
    "inline void MaybeReport() {\n"
    "  if (!Enabled()) return;\n"
    "  const uint64_t now = Now();\n"
    "  if (!report_last_ns) { report_last_ns = now; report_start_ns = now; return; }\n"
    "  const double window_s = double(now - report_last_ns) / 1e9;\n"
    "  if (window_s < 3.0) return;\n"
    "  const double total_s = double(now - report_start_ns) / 1e9;\n"
    "  std::printf(\"[BATCH] t=%.1fs window=%.1fs draws=%llu submissions=%llu \"\n"
    "              \"per_draw=%.3f merge=%.2fx verts=%llu req=%llu empty=%llu | \"\n"
    "              \"run draws=%llu submissions=%llu per_draw=%.3f verts=%llu\\n\",\n"
    "              total_s, window_s, (unsigned long long)window_draws,\n"
    "              (unsigned long long)window_submits,\n"
    "              window_draws ? double(window_submits) / double(window_draws) : 0.0,\n"
    "              window_submits ? double(window_draws) / double(window_submits) : 0.0,\n"
    "              (unsigned long long)window_vertices,\n"
    "              (unsigned long long)window_requests, (unsigned long long)window_empty,\n"
    "              (unsigned long long)total_draws, (unsigned long long)total_submits,\n"
    "              total_draws ? double(total_submits) / double(total_draws) : 0.0,\n"
    "              (unsigned long long)total_vertices);\n"
    "  std::printf(\"[BATCH]  by reason\");\n"
    "  for (unsigned r = 0; r < unsigned(ReasonCount); ++r)\n"
    "    std::printf(\" %s=%llu/%llu/%llu\", Name(r),\n"
    "                (unsigned long long)window_req[r],\n"
    "                (unsigned long long)window_emp[r],\n"
    "                (unsigned long long)window_sub[r]);\n"
    "  std::printf(\" (requests/empty/submitted)\\n\");\n"
    "  std::fflush(stdout);\n"
    "  for (unsigned r = 0; r < unsigned(ReasonCount); ++r)\n"
    "    window_req[r] = window_emp[r] = window_sub[r] = window_vert[r] = 0;\n"
    "  window_draws = window_requests = window_empty = 0;\n"
    "  window_submits = window_vertices = 0;\n"
    "  report_last_ns = now;\n"
    "}\n"
    "} // namespace native_batch_census // " + CENSUS + "_DEFS\n"
    "\n"
    "static void FlushBatch(native_batch_census::Reason reason =\n"
    "                           native_batch_census::Other) {\n"
    "  const bool census = native_batch_census::Enabled();\n"
    "  native_batch_census::Request(census, reason, s_batch.empty());\n"
    "  if (s_batch.empty())\n"
    "    return;\n",
)

edit(
    CENSUS + "_SUBMIT",
    "  s_batch.clear();\n"
    "  s_indices.clear();\n"
    "}\n",
    "  native_batch_census::Submit(census, reason, s_batch.size());\n"
    "  s_batch.clear();\n"
    "  s_indices.clear();\n"
    "} // " + CENSUS + "_SUBMIT\n",
)

# Which BP register closed the batch: the census has to name the boundary
# before the boundary can be argued about. Top eight per report window.
edit(
    CENSUS + "_BPREG_DEFS",
    "inline void Draw() { ++total_draws; ++window_draws; }\n",
    "inline void Draw() { ++total_draws; ++window_draws; }\n"
    "inline uint64_t bp_boundary[256] = {};\n"
    "inline void BpBoundary(unsigned reg) { ++bp_boundary[reg & 0xffu]; }\n"
    "inline void PrintBpBoundaries() {\n"
    "  bool printed = false;\n"
    "  for (unsigned shown = 0; shown < 8; ++shown) {\n"
    "    unsigned best = 0;\n"
    "    uint64_t best_hits = 0;\n"
    "    for (unsigned reg = 0; reg < 256; ++reg)\n"
    "      if (bp_boundary[reg] > best_hits) { best_hits = bp_boundary[reg]; best = reg; }\n"
    "    if (!best_hits) break;\n"
    "    std::printf(\"%s0x%02x=%llu\", printed ? \" \" : \"\", best,\n"
    "                (unsigned long long)best_hits);\n"
    "    bp_boundary[best] = 0;\n"
    "    printed = true;\n"
    "  }\n"
    "  if (printed) std::printf(\"\\n\");\n"
    "} // " + CENSUS + "_BPREG_DEFS\n",
)

edit(
    CENSUS + "_BPREG_REPORT",
    "  std::printf(\" (requests/empty/submitted)\\n\");\n",
    "  std::printf(\" (requests/empty/submitted)\\n\");\n"
    "  std::printf(\"[BATCH]  bp boundaries\");\n"
    "  PrintBpBoundaries(); // " + CENSUS + "_BPREG_REPORT\n",
)

# --- the batch decision helpers --------------------------------------------
# Placed after the BP shadow they read and before DrawPrimitive uses them.
edit(
    MARK + "_HELPERS",
    "static void NativeRestoreBpSnapshot() {\n"
    "  std::memcpy(g_state.bp, s_native_bp_shadow, sizeof(s_native_bp_shadow));\n"
    "}\n",
    "static void NativeRestoreBpSnapshot() {\n"
    "  std::memcpy(g_state.bp, s_native_bp_shadow, sizeof(s_native_bp_shadow));\n"
    "}\n"
    "\n"
    "// " + MARK + ": one GL batch stays open across adjacent draws, and only a\n"
    "// state write that can change what the pending draw consumes closes it.\n"
    "// Both halves are separately defeatable so the same binary can be A/B'd:\n"
    "//   NWII_NO_BATCH_MERGE=1  close the batch on every non-draw (old behaviour)\n"
    "//   NWII_NO_BATCH_CANON=1  key the batch on the guest primitive type\n"
    "static bool NativeBatchMerge() {\n"
    "  static const bool enabled = std::getenv(\"NWII_NO_BATCH_MERGE\") == nullptr;\n"
    "  return enabled;\n"
    "}\n"
    "static bool NativeBatchCanonical() {\n"
    "  static const bool enabled = std::getenv(\"NWII_NO_BATCH_CANON\") == nullptr;\n"
    "  return enabled;\n"
    "}\n"
    "\n"
    "// Indexed quads, strips and fans all submit ordered triangles, so they can\n"
    "// share one GL_TRIANGLES batch; a non-multiple-of-three list cannot.\n"
    "static uint8_t NativeBatchTopology(uint8_t primitive, size_t count) {\n"
    "  if (!NativeBatchCanonical()) return primitive;\n"
    "  if (primitive == 0x80 || primitive == 0x98 || primitive == 0xA0 ||\n"
    "      (primitive == 0x90 && count % 3 == 0))\n"
    "    return 0x80;\n"
    "  return primitive;\n"
    "}\n"
    "\n"
    "// True when this BP write cannot change what a pending draw consumes. The\n"
    "// texture registers (0x80-0x9f, 0xa0-0xbf), the TLUT (0x64/0x65) and the PE\n"
    "// token/finish registers (0x45/0x47/0x48) are deliberately absent: rebinding\n"
    "// an identical texture can expose changed guest texels, and the PE registers\n"
    "// must flush before they are signalled.\n"
    "static bool NativeRedundantBp(const GXCommand &cmd) {\n"
    "  if (cmd.type != GXCommandType::BPRegister)\n"
    "    return false;\n"
    "  const unsigned reg = cmd.reg;\n"
    "  // Deferred writes only prepare the command that follows them (the copy\n"
    "  // rectangle and clear colour for BP 0x52, the one-shot 0xfe write mask).\n"
    "  if (reg == 0xfe || reg == 0x49 || reg == 0x4a || reg == 0x4b ||\n"
    "      reg == 0x4d || reg == 0x4f || reg == 0x50)\n"
    "    return true;\n"
    "  const bool ordinary =\n"
    "      reg == 0x00 || reg == 0x0f || reg == 0xe8 ||\n"
    "      (reg >= 0x20 && reg <= 0x2f) || (reg >= 0x40 && reg <= 0x43) ||\n"
    "      (reg >= 0xc0 && reg <= 0xe7) || (reg >= 0xe9 && reg <= 0xfd);\n"
    "  if (!ordinary)\n"
    "    return false;\n"
    "  const uint32_t resolved =\n"
    "      ((s_native_bp_shadow[reg] & ~s_native_bp_mask) |\n"
    "       (cmd.val & s_native_bp_mask)) &\n"
    "      0xffffffu;\n"
    "  return resolved == s_native_bp_shadow[reg];\n"
    "}\n"
    "\n"
    "// True only when every word of this XF write already holds the value it\n"
    "// would store. A word the renderer does not model counts as a change.\n"
    "static bool NativeRedundantXf(const GXCommand &cmd) {\n"
    "  if (cmd.type != GXCommandType::XFRegister)\n"
    "    return false;\n"
    "  // This runtime has no texture-used mask, so XF state that a textured draw\n"
    "  // would consume stays a boundary.\n"
    "  for (unsigned i = 0; i < 8; ++i) {\n"
    "    const auto &stage = g_state.texStages[i];\n"
    "    if (stage.base_addr != 0 && stage.width > 0 && stage.height > 0)\n"
    "      return false;\n"
    "  }\n"
    "  const int base = cmd.reg;\n"
    "  for (size_t i = 0; i < cmd.payload.size(); ++i) {\n"
    "    const int addr = base + int(i);\n"
    "    uint32_t bits = 0;\n"
    "    std::memcpy(&bits, &cmd.payload[i], 4);\n"
    "    bool known = false;\n"
    "    if (addr >= 0x1000 && addr < 0x1100) {\n"
    "      known = true;\n"
    "      if (g_state.xf[addr - 0x1000] != bits)\n"
    "        return false;\n"
    "    }\n"
    "    if (addr >= 0x0000 && addr <= 0x00ff) {\n"
    "      known = true;\n"
    "      if (std::memcmp(&g_state.posMatrices[addr], &cmd.payload[i], 4) != 0)\n"
    "        return false;\n"
    "    } else if (addr >= 0x0400 && addr <= 0x045f) {\n"
    "      known = true;\n"
    "      if (std::memcmp(&g_state.normalMatrices[addr - 0x0400], &cmd.payload[i], 4) != 0)\n"
    "        return false;\n"
    "    } else if (addr >= 0x0500 && addr < 0x0600) {\n"
    "      known = true;\n"
    "      if (std::memcmp(&g_state.postMatrices[addr - 0x0500], &cmd.payload[i], 4) != 0)\n"
    "        return false;\n"
    "    } else if (addr >= 0x0600 && addr <= 0x067f) {\n"
    "      known = true;\n"
    "      if (std::memcmp(&g_state.lights[(addr - 0x0600) / 16][(addr - 0x0600) % 16],\n"
    "                      &cmd.payload[i], 4) != 0)\n"
    "        return false;\n"
    "    } else if (addr >= 0x1020 && addr <= 0x1026) {\n"
    "      known = true;\n"
    "      if (std::memcmp(&g_state.projection[addr - 0x1020], &cmd.payload[i], 4) != 0)\n"
    "        return false;\n"
    "    }\n"
    "    if (!known)\n"
    "      return false;\n"
    "  }\n"
    "  return true;\n"
    "}\n"
    "\n"
    "// NWII_NO_BATCH_MERGE=1 disables continuation entirely, CP included, which is\n"
    "// exactly the pre-stage behaviour.\n"
    "static bool NativeBatchContinues(const GXCommand &cmd) {\n"
    "  if (!NativeBatchMerge())\n"
    "    return false;\n"
    "  if (cmd.type == GXCommandType::DrawPrimitive ||\n"
    "      cmd.type == GXCommandType::CPRegister)\n"
    "    return true;\n"
    "  if (cmd.type == GXCommandType::BPRegister) {\n"
    "    const unsigned reg = cmd.reg;\n"
    "    // PE copy / XFB latch / copy clear, PE token and PE finish (hard bound).\n"
    "    if (reg == 0x52 || reg == 0x45 || reg == 0x47 || reg == 0x48)\n"
    "      return false;\n"
    "    return NativeRedundantBp(cmd);\n"
    "  }\n"
    "  if (cmd.type == GXCommandType::XFRegister)\n"
    "    return NativeRedundantXf(cmd);\n"
    "  return false;\n"
    "} // " + MARK + "_HELPERS\n",
)

# --- DrawPrimitive: canonical topology key ---------------------------------
edit(
    MARK + "_DRAW",
    "  if (!s_batch_open || s_batch_prim != (uint8_t)cmd.prim_type) {\n"
    "    FlushBatch();\n"
    "    SetupDrawState(cmd);\n"
    "    s_batch_open = true;\n"
    "    s_batch_prim = (uint8_t)cmd.prim_type;\n"
    "  }\n",
    "  native_batch_census::Draw();\n"
    "  const uint8_t topology = NativeBatchTopology((uint8_t)cmd.prim_type, n);\n"
    "  if (!s_batch_open || s_batch_prim != topology) {\n"
    "    FlushBatch(s_batch_open ? native_batch_census::Topology\n"
    "                            : native_batch_census::Opening);\n"
    "    SetupDrawState(cmd);\n"
    "    s_batch_open = true;\n"
    "    s_batch_prim = topology;\n"
    "  } // " + MARK + "_DRAW\n",
)

# --- the shader key no longer includes the topology ------------------------
edit(
    MARK + "_HASH",
    "  uint64_t hash = NativeTevShaderHash(g_state, cmd.prim_type);\n",
    "  // " + MARK + ": the generated vertex/fragment source is topology-\n"
    "  // independent, so every topology shares one program.\n"
    "  uint64_t hash = NativeTevShaderHash(g_state, 0x90); // " + MARK + "_HASH\n",
)
edit(
    MARK + "_HASHLOG",
    "             (unsigned long long)NativeTevShaderHash(g_state, cmd.prim_type),\n",
    "             (unsigned long long)NativeTevShaderHash(g_state, 0x90), // " + MARK + "_HASHLOG\n",
)

# --- EmitPrimitive: array/index run prefix ---------------------------------
edit(
    PREFIX + "_LINES",
    "  if (cmd.prim_type>=0xA8 && cmd.prim_type<=0xB8) {\n"
    "    NativeEmitLinesPoints(cmd);\n"
    "    return;\n"
    "  }\n",
    "  if (cmd.prim_type>=0xA8 && cmd.prim_type<=0xB8) {\n"
    "    NativeEmitLinesPoints(cmd);\n"
    "    // " + PREFIX + ": the expansion pushed unindexed vertices. An array\n"
    "    // batch still covers them; an indexed batch needs indices.\n"
    "    if(!s_indices.empty())\n"
    "      for(uint32_t i=base;i<uint32_t(s_batch.size());++i)s_indices.push_back(i); // " + PREFIX + "_LINES\n"
    "    return;\n"
    "  }\n",
)
edit(
    PREFIX + "_BACKFILL",
    "  for(size_t i=0;i<count;++i) EmitVertex(v[i]);\n"
    "  switch(cmd.prim_type) {\n",
    "  if(!count)return;\n"
    "  for(size_t i=0;i<count;++i) EmitVertex(v[i]);\n"
    "  // " + PREFIX + ": a GL draw is either indexed or not, so a batch that\n"
    "  // joins both kinds back-fills the vertices the array path drew.\n"
    "  if(cmd.prim_type!=0x90 && s_indices.empty())\n"
    "    for(uint32_t i=0;i<base;++i)s_indices.push_back(i); // " + PREFIX + "_BACKFILL\n"
    "  switch(cmd.prim_type) {\n",
)
edit(
    PREFIX + "_LIST90",
    "  case 0x90:\n"
    "    break;\n",
    "  case 0x90:\n"
    "    // " + PREFIX + ": an indexed batch needs indices for this list too, or\n"
    "    // its triangles are never submitted.\n"
    "    if(!s_indices.empty())\n"
    "      for(uint32_t i=0;i<count;++i)s_indices.push_back(base+i); // " + PREFIX + "_LIST90\n"
    "    break;\n",
)

# --- the Render loop boundary ---------------------------------------------
edit(
    MARK + "_LOOP",
    "      if (cmd.type != GXCommandType::DrawPrimitive && s_batch_open) {\n"
    "        FlushBatch();\n"
    "        s_batch_open = false;\n"
    "      }\n",
    "      // " + MARK + ": a state write closes the open batch only when it can\n"
    "      // change what the pending draw consumes. Draws, ignored CP commands\n"
    "      // and state that is already in force continue it.\n"
    "      if (s_batch_open && !NativeBatchContinues(cmd)) { // " + MARK + "_LOOP\n"
    "        FlushBatch(cmd.type == GXCommandType::BPRegister\n"
    "                       ? native_batch_census::Bp\n"
    "                       : cmd.type == GXCommandType::XFRegister\n"
    "                             ? native_batch_census::Xf\n"
    "                             : native_batch_census::Other);\n"
    "        s_batch_open = false;\n"
    "      }\n",
)

# --- name the BP boundary in the census -----------------------------------
# Runs after the loop edit, so it anchors on that edit's text.
edit(
    CENSUS + "_BPREG_CALL",
    "      if (s_batch_open && !NativeBatchContinues(cmd)) { // " + MARK + "_LOOP\n"
    "        FlushBatch(cmd.type == GXCommandType::BPRegister\n",
    "      if (s_batch_open && !NativeBatchContinues(cmd)) { // " + MARK + "_LOOP\n"
    "        if (cmd.type == GXCommandType::BPRegister) // " + CENSUS + "_BPREG_CALL\n"
    "          native_batch_census::BpBoundary(cmd.reg);\n"
    "        FlushBatch(cmd.type == GXCommandType::BPRegister\n",
)

# --- PE completion ordering ------------------------------------------------
edit(
    MARK + "_PE",
    "        // NATIVE_GPU_COMPLETION_HOOK: prior batch flushed at stream boundary.\n"
    "        NativeGpuCompletion((uint8_t)cmd.reg,bp_value,true);\n",
    "        // " + MARK + ": the loop boundary above already closed the batch for\n"
    "        // the PE registers; this keeps the token/finish strictly behind any\n"
    "        // batch that is somehow still open.\n"
    "        if ((cmd.reg == 0x45 || cmd.reg == 0x47 || cmd.reg == 0x48) && s_batch_open) { // " + MARK + "_PE\n"
    "          FlushBatch(native_batch_census::Bp);\n"
    "          s_batch_open = false;\n"
    "        }\n"
    "        NativeGpuCompletion((uint8_t)cmd.reg,bp_value,true);\n",
)

# --- the end-of-stream drain ----------------------------------------------
edit(
    MARK + "_DRAIN",
    "    if (s_batch_open) {\n"
    "      FlushBatch();\n"
    "      s_batch_open = false;\n"
    "    }\n"
    "  }\n",
    "    if (s_batch_open) {\n"
    "      FlushBatch(native_batch_census::Drain);\n"
    "      s_batch_open = false;\n"
    "    }\n"
    "    native_batch_census::MaybeReport();\n"
    "  } // " + MARK + "_DRAIN\n",
)

# --- how much a wider boundary would be worth -----------------------------
# Counts, per boundary write, whether the resolved value was already in force:
# the texture/TLUT registers are boundaries by design, so this is what relaxing
# them later would cost. Instrumentation only; nothing changes behaviour.
edit(
    CENSUS + "_BPSAME_DEFS",
    "inline uint64_t bp_boundary[256] = {};\n"
    "inline void BpBoundary(unsigned reg) { ++bp_boundary[reg & 0xffu]; }\n",
    "inline uint64_t bp_boundary[256] = {};\n"
    "inline uint64_t bp_seen_total = 0, bp_same_total = 0;\n"
    "inline uint64_t tex_seen_total = 0, tex_same_total = 0;\n"
    "inline void BpBoundary(const GXCommand &cmd); // " + CENSUS + "_BPSAME_DEFS\n",
)

edit(
    CENSUS + "_BPSAME_DEFINE",
    "// " + MARK + ": one GL batch stays open across adjacent draws, and only a\n",
    "// " + CENSUS + ": resolved-value identity of the writes that closed the\n"
    "// batch. Defined here because it reads the BP shadow above.\n"
    "namespace native_batch_census {\n"
    "inline void BpBoundary(const GXCommand &cmd) {\n"
    "  const unsigned reg = cmd.reg & 0xffu;\n"
    "  ++bp_boundary[reg];\n"
    "  const uint32_t resolved =\n"
    "      ((s_native_bp_shadow[reg] & ~s_native_bp_mask) |\n"
    "       (cmd.val & s_native_bp_mask)) &\n"
    "      0xffffffu;\n"
    "  const bool same = resolved == s_native_bp_shadow[reg];\n"
    "  ++bp_seen_total;\n"
    "  if (same) ++bp_same_total;\n"
    "  if ((reg >= 0x80 && reg <= 0xbf) || reg == 0x64 || reg == 0x65) {\n"
    "    ++tex_seen_total;\n"
    "    if (same) ++tex_same_total;\n"
    "  }\n"
    "} // " + CENSUS + "_BPSAME_DEFINE\n"
    "} // namespace native_batch_census\n"
    "\n"
    "// " + MARK + ": one GL batch stays open across adjacent draws, and only a\n",
)

edit(
    CENSUS + "_BPSAME_REPORT",
    "  PrintBpBoundaries(); // " + CENSUS + "_BPREG_REPORT\n",
    "  PrintBpBoundaries(); // " + CENSUS + "_BPREG_REPORT\n"
    "  std::printf(\"[BATCH]  bp unchanged value=%llu/%llu tex/tlut=%llu/%llu \"\n"
    "              \"(already-in-force / boundary writes)\\n\",\n"
    "              (unsigned long long)bp_same_total, (unsigned long long)bp_seen_total,\n"
    "              (unsigned long long)tex_same_total, (unsigned long long)tex_seen_total);\n"
    "  bp_seen_total = bp_same_total = tex_seen_total = tex_same_total = 0; // "
    + CENSUS + "_BPSAME_REPORT\n",
)

edit(
    CENSUS + "_BPSAME_CALL",
    "        if (cmd.type == GXCommandType::BPRegister) // " + CENSUS + "_BPREG_CALL\n"
    "          native_batch_census::BpBoundary(cmd.reg);\n",
    "        if (cmd.type == GXCommandType::BPRegister &&\n"
    "            native_batch_census::Enabled()) // " + CENSUS + "_BPREG_CALL\n"
    "          native_batch_census::BpBoundary(cmd); // " + CENSUS + "_BPSAME_CALL\n",
)

if not applied:
    print("Renderer batching already installed (%s)." % MARK)
    raise SystemExit(0)

renderer.write_text(text)
print("Draw batching installed (%d edits): %s. NWII_NO_BATCH_MERGE=1 reverts."
      % (len(applied), MARK))
