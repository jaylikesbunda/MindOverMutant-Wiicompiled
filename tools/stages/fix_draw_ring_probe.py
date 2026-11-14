"""Record the last draws and dump them when a green frame is captured.

The wash is a flat ``(0,151,0)`` over the whole picture with a one-unit vertical
gradient, i.e. a full-screen quad sampling a uniform value - not a torn video
frame, and no texture was uploaded black (the zero probe never fired). To find
the draw that produced it, keep a small ring of the last 64 draws (GX state at
the time: TEV stage count, texture address/format/size, blend and depth modes,
vertex count) and dump it the moment the green probe captures a frame.

This stage also tightens the green test. The previous rule compared green
against red and blue by ratio, which an ordinary dark scene (24,48,36) passes;
a YUV wash with zero chroma drives red and blue to the floor, so the test now
requires both to be under 40 and green above 60.

Observation only: the ring costs a few stores per draw and nothing else changes.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_DRAW_RING_V1'
MARK_GREEN = 'NATIVE_GREEN_WASH_PROBE_V2'

# ---- 1. the ring -----------------------------------------------------------
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK not in text:
    anchor = 'class RendererGL : public IRenderer {'
    assert text.count(anchor) == 1, 'Render anchor'
    ring = '''// ''' + MARK + ''': the last draws, dumped when a green frame is captured.
struct NativeDrawRec {
  uint8_t cmd, tev, fmt;
  uint16_t count, w, h;
  uint32_t t0, gen, blend, zmode, texmask, rgba;
};
static NativeDrawRec s_draw_rec[64];
static unsigned s_draw_rec_n = 0;
extern "C" void NativeDumpDrawRing() {
  const unsigned n = s_draw_rec_n < 64 ? s_draw_rec_n : 64;
  std::cout << "[" << 4 << "DrawRing] last " << n << " draws (oldest first), "
            << s_draw_rec_n << " total:" << std::endl;
  for (unsigned k = 0; k < n; ++k) {
    const NativeDrawRec& r = s_draw_rec[(s_draw_rec_n - n + k) % 64];
    std::cout << "[DrawRing] " << (s_draw_rec_n - n + k) << " cmd=0x" << std::hex
              << unsigned(r.cmd) << " n=" << std::dec << r.count
              << " tev=" << unsigned(r.tev) << " tex=0x" << std::hex << r.t0
              << " fmt=" << std::dec << unsigned(r.fmt) << " " << r.w << "x" << r.h
              << " gen=0x" << std::hex << r.gen << " blend=0x" << r.blend
              << " zmode=0x" << r.zmode << " texmask=0x" << r.texmask
              << " clr0=0x" << r.rgba << std::dec << std::endl;
  }
}

''' + anchor
    text = text.replace(anchor, ring, 1)

    anchor2 = '''      } else if (cmd.type == GXCommandType::DrawPrimitive) {

        if (!s_batch_open && g_ctx_ptr) {'''
    assert text.count(anchor2) == 1, 'draw anchor'
    rec = '''      } else if (cmd.type == GXCommandType::DrawPrimitive) {
        { // ''' + MARK + '''
          NativeDrawRec& r = s_draw_rec[s_draw_rec_n++ % 64];
          r.cmd = uint8_t(cmd.prim_type);
          r.count = uint16_t(cmd.vertices.size());
          r.tev = uint8_t(g_state.numTevStages);
          r.fmt = uint8_t(g_state.texStages[0].format);
          r.w = uint16_t(g_state.texStages[0].width);
          r.h = uint16_t(g_state.texStages[0].height);
          r.t0 = g_state.texStages[0].base_addr;
          r.gen = g_state.bp[0];
          r.blend = g_state.bp[0x41];
          r.zmode = g_state.bp[0x40];
          r.texmask = 0;
          for (int k = 0; k < 8; ++k)
            if (g_state.texStages[k].base_addr) r.texmask |= 1u << k;
          r.rgba = g_state.bp[0xc0]; // TEV colour 0 low: a flat-colour quad's output
        }

        if (!s_batch_open && g_ctx_ptr) {'''
    text = text.replace(anchor2, rec, 1)
    renderer.write_text(text)

# ---- 2. tighten the wash test and dump on capture -------------------------
main = runtime / 'src/core/main.cpp'
text = main.read_text()
if 'NativeDumpDrawRing' not in text:
    anchor = '#include "runtime/gx_state.h"'
    assert text.count(anchor) == 1, 'main include anchor'
    text = text.replace(anchor, anchor + '\nextern "C" void NativeDumpDrawRing(); // ' + MARK, 1)

    old = """              green_band[b] = band[b][1] > 30.0 &&
                              band[b][1] > band[b][0] * 1.25 &&
                              band[b][1] > band[b][2] * 1.25;"""
    assert text.count(old) == 1, 'green band test anchor'
    text = text.replace(old, """              // A YUV wash with zero chroma drives red and blue to the
              // floor; an ordinary dark scene keeps them.
              green_band[b] = band[b][1] > 60.0 && band[b][0] < 40.0 &&
                              band[b][2] < 40.0; // """ + MARK_GREEN, 1)

    old2 = '              std::cout << "[GreenWash] #" << written'
    assert text.count(old2) == 1, 'capture anchor'
    text = text.replace(old2,
                        '              NativeDumpDrawRing();\n'
                        '              std::cout << "[GreenWash] #" << written', 1)
    main.write_text(text)
print('Draw ring installed; selectable green test.')
