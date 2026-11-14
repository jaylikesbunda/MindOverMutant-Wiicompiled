"""Coarsen the shader cache key: stop hashing GL-only state.

The generated TEV program is looked up by ``GXState::GetShaderHash()``
(wrapped by ``NativeTevShaderHash()`` in ``src/hle/gx/renderer_gl.cpp``).
The key folded in four BP registers that never reach the generated GLSL:

    add_u32(bp[0x40]);   // ZMode: depth test enable/func/update
    add_u32(bp[0x41]);   // CMODE0: blend enable, factors, colour mask
    add_u32(bp[0x42]);   // CMODE1 (read by nothing in this runtime)
    add_u32(bp[0x43]);   // destination-alpha enable

Evidence that they are GL state and not shader source
-----------------------------------------------------
``src/hle/gx/tev_shader_gen.cpp`` (the only producer of GLSL in this tree)
reads exactly these BP registers:

    bp[0x00]          number of TEV stages, indirect unit count
    bp[0x06..0x0B]    indirect matrix coefficients
    bp[0x10+stage]    per-stage indirect configuration
    bp[0x25+sel/2]    indirect scale
    bp[0x27]          indirect order
    bp[0x28..0x2F]    per-stage texture map / texcoord / colour channel
    bp[0xC0..0xDF]    TEV stage combiners (colour/alpha inputs and ops)
    bp[0xEE..0xF2]    fog (range, colour, control)
    bp[0xF3]          alpha test
    bp[0xF6..0xFD]    konst selectors and the RGBA swizzle tables

It never reads BP 0x40..0x43.  Those four are consumed by
``ApplyZMode()`` / ``ApplyColorMask()`` / ``ApplyBlend()``
(``src/hle/gx/renderer_gl.cpp``), which set GL depth-test, colour-mask and
blend state, and by ``NativeClearEfb()``.  Two draws that differ only in
depth-test or blend therefore produced two *identical* programs.

What is deliberately NOT removed
--------------------------------
BP 0xC0..0xDF are **not** the TEV colour registers on this hardware: in
``src/hle/gx/fifo_parser.cpp`` they are decoded into
``g_state.tevStages[stage]`` as the TEV *stage combiners*

    int stage = (reg - 0xC0) / 2;
    s.colorInD = val & 0xF;  s.colorInA = (val >> 12) & 0xF;
    s.colorBias = (val >> 16) & 0x3;  s.colorOp = (val >> 18) & 0x1; ...

and ``tev_shader_gen.cpp`` bakes those straight into the fragment source
(``cIn[..] = ...``, ``cOut = cIn[3] + mix(...)``, ``tevReg[..] = ...``).
``tev_shader_gen.cpp`` also reads ``bp[0xc1 + 2*i]`` for the RAS/TEV swap.
Dropping that range from the key would let two different combiners share one
program and would draw the wrong colour, so it stays.

The TEV *colour* registers (the ones uploaded as the ``uTevColor`` /
``uTevKColor`` uniforms) are BP 0xE0..0xE7
(``NativeTevRegisterWrite()`` / ``s_native_tev_banks``) and were never part
of the key, so nothing had to be removed for them.

Kill switch
-----------
``NWII_NO_COARSE_SHADER_KEY=1`` restores the previous key exactly (the four
words are folded in again, in the same order), so one binary can be A/B'd.
The flag is read once, at first use.

The stage also installs an opt-in fingerprint printer
(``NWII_SHKEY_TRACE=1``) that writes one line per compiled program with a
hash per register group, which is how the remaining key churn was measured.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_COARSE_SHADER_KEY_V1'

# ---- 1. the key ------------------------------------------------------------
header = runtime / 'include/runtime/gx_state.h'
text = header.read_text()
if MARK not in text:
    old_include = """#pragma once
#include <array>
#include <cstdint>
"""
    assert text.count(old_include) == 1, 'gx_state.h include anchor'
    new_include = """#pragma once
#include <array>
#include <cstdint>
#include <cstdlib>
"""
    text = text.replace(old_include, new_include, 1)

    old_helper = """  uint64_t GetShaderHash(uint8_t prim_type) const {
"""
    assert text.count(old_helper) == 1, 'gx_state.h hash anchor'
    new_helper = """  // """ + MARK + """: true keeps the fine (pre-stage) shader key, false uses the
  // coarse one that omits the GL-only registers.  Read once, so the hot path
  // pays a single predictable branch.
  static bool CoarseShaderKey() {
      static const bool coarse = []() {
          const char* v = std::getenv("NWII_NO_COARSE_SHADER_KEY");
          return v != nullptr && v[0] != '\\0' && v[0] != '0';
      }();
      return coarse;
  }

  uint64_t GetShaderHash(uint8_t prim_type) const {
"""
    text = text.replace(old_helper, new_helper, 1)

    old_key = """      add_u32(bp[0x00]); 
      add_u32(bp[0x40]); 
      add_u32(bp[0x41]); 
      add_u32(bp[0x42]); 
      add_u32(bp[0x43]); 
      add_u32(bp[0xF3]); 
"""
    assert text.count(old_key) == 1, 'gx_state.h key-body anchor'
    new_key = """      add_u32(bp[0x00]); 
      // """ + MARK + """: BP 0x40 (ZMode) and BP 0x41..0x43 (blend / destination
      // alpha) are applied as GL state by ApplyZMode/ApplyColorMask/ApplyBlend
      // and are never read by the shader generator, so folding them into the
      // key compiled a new program for every distinct depth/blend setting
      // while the generated GLSL stayed byte-identical.  Everything that does
      // reach the generated source (genMode, alpha test, the TEV stage
      // combiners in 0xC0..0xDF, the indirect and fog registers) stays in the
      // key.  NWII_NO_COARSE_SHADER_KEY=1 folds the four words back in.
      if (CoarseShaderKey()) {
        add_u32(bp[0x40]); 
        add_u32(bp[0x41]); 
        add_u32(bp[0x42]); 
        add_u32(bp[0x43]); 
      }
      add_u32(bp[0xF3]); 
"""
    text = text.replace(old_key, new_key, 1)
    header.write_text(text)

# ---- 2. the opt-in fingerprint --------------------------------------------
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK not in text:
    old_hash = """  for (unsigned reg = 0xf6; reg <= 0xfd; ++reg)
    for (unsigned shift = 0; shift < 32; shift += 8)
      hash = (hash ^ ((state.bp[reg] >> shift) & 0xff)) * 1099511628211ull;
  return hash;
}
"""
    assert text.count(old_hash) == 1, 'renderer_gl.cpp hash anchor'
    new_hash = old_hash + """
// """ + MARK + """: one line per compiled program with a fingerprint per register
// group of the shader key.  Off by default; NWII_SHKEY_TRACE=1 turns it on.
// It is how the remaining key churn was attributed to a group.
static void NativeShaderKeyFingerprint(const GXState &state) {
  static const bool on = std::getenv("NWII_SHKEY_TRACE") != nullptr;
  if (!on) return;
  const uint64_t seed = 14695981039346656037ull;
  auto fold = [](uint64_t h, uint32_t w) {
      h = (h ^ (w & 0xFF)) * 1099511628211ull;
      h = (h ^ ((w >> 8) & 0xFF)) * 1099511628211ull;
      h = (h ^ ((w >> 16) & 0xFF)) * 1099511628211ull;
      h = (h ^ ((w >> 24) & 0xFF)) * 1099511628211ull;
      return h;
  };
  auto range = [&](unsigned lo, unsigned hi) {
      uint64_t h = seed;
      for (unsigned r = lo; r <= hi; ++r) h = fold(h, state.bp[r]);
      return h;
  };
  uint64_t gen = fold(seed, state.bp[0x00]);
  uint64_t zb = range(0x40, 0x43);
  uint64_t at = fold(seed, state.bp[0xF3]);
  uint64_t tev = range(0xC0, 0xDF);
  uint64_t map = range(0x28, 0x2F);
  uint64_t ind = range(0x06, 0x27);
  uint64_t fog = range(0xEE, 0xF2);
  uint64_t ksel = range(0xF6, 0xFD);
  uint64_t vtx = seed;
  vtx = fold(vtx, state.cp[0x50]);
  vtx = fold(vtx, state.cp[0x60]);
  vtx = fold(vtx, state.xf[0x3F]);
  for (unsigned t = 0; t < 8; ++t) vtx = fold(vtx, state.xf[0x40 + t]);
  uint64_t fmt = seed;
  for (unsigned t = 0; t < 8; ++t) {
      fmt = fold(fmt, state.texStages[t].format);
      fmt = fold(fmt, state.texStages[t].tlut_format);
  }
  printf("[SHKEY] gen=%08llx zb=%08llx at=%08llx tev=%08llx map=%08llx "
         "ind=%08llx fog=%08llx ksel=%08llx vtx=%08llx fmt=%08llx\\n",
         (unsigned long long)gen, (unsigned long long)zb,
         (unsigned long long)at, (unsigned long long)tev,
         (unsigned long long)map, (unsigned long long)ind,
         (unsigned long long)fog, (unsigned long long)ksel,
         (unsigned long long)vtx, (unsigned long long)fmt);
}
"""
    text = text.replace(old_hash, new_hash, 1)

    old_call = """    GLShader sh = {};
    sh.uProjMtx = -1;
"""
    assert text.count(old_call) == 1, 'renderer_gl.cpp compile-site anchor'
    new_call = """    NativeShaderKeyFingerprint(g_state); // """ + MARK + """
    GLShader sh = {};
    sh.uProjMtx = -1;
"""
    text = text.replace(old_call, new_call, 1)
    renderer.write_text(text)

print('The shader cache key no longer hashes the GL-only BP 0x40..0x43 registers.')
