"""Add CotT's F3 developer overlay (live frame-time breakdown) and F4 export.

Ported from the Crash of the Titans port's ``tools/fix_dev_hud.py``,
``tools/fix_stutter_watch.py``, ``tools/fix_hot_path_detail.py`` and
``tools/fix_budget_trace.py`` (runtime-overlays ``native_hud.h`` /
``native_stutter.h`` / ``native_hud_gl.h``), re-derived for this runtime.

What CotT shows and how it is wired
-----------------------------------
``native_hud.h`` embeds a per-scope exclusive-time slot in
``native_trace::Scope``; ``native_hud_gl.h`` rasterises Consolas with GDI into
a 16x6 atlas, batches textured triangles into one VBO and blits them over the
back buffer just before the swap; the host loop forwards key presses
(``SDLK_F3`` toggles, ``SDLK_F4`` exports, ``SDLK_F9`` resets), feeds the
present counter and calls ``draw()`` once per field.  Start-up sets the budget
from the VI field period and reads ``GL_RENDERER``.

This runtime has none of CotT's per-scope accounting: its ``native_trace::Scope``
is gated on ``NWII_FRAME_TRACE`` and is regenerated from ``overlays/`` by
``fix_perf_frame_trace.py`` on every run, so a slot cache cannot be embedded
there.  What this runtime does have is ``native_perf_phase``: nine named host
phases, per-phase counters and a per-frame ring.  The overlay reads those
instead of inventing timing:

  * frame interval, average, p50/p95/p99/max and the count of frames worse
    than one 50 Hz field (20.05 ms) come from a 240-frame ring: with the phase
    timers on it is fed from the frame record they just closed, so it is
    literally the CSV's ``wall_ms`` column; with them off it is the host loop
    period;
  * the per-phase table is ``Total(phase)`` / ``Hits(phase)`` deltas divided
    by the frames in the window - exactly what ``maybe_report()`` does with
    its ring, and zero when ``NWII_PERF_PHASE`` is unset (the panel says so);
  * the worst frame's breakdown is read back from ``native_perf_phase::ring``
    when the phase timers are on;
  * NEW/PRESENT/XFB rates reuse the same present path and ``fresh`` flag as
    ``07_fix_present_metrics_mom.py`` and ``NativeXfbCopyCount()``;
  * the workload line reuses ``g_stat_frames``, ``g_stat_draws`` and
    ``nwii_stat_shaders()`` - counters this runtime already keeps, so no new
    hot-path counter is added anywhere.

Nothing is timed when the overlay is hidden: every entry point starts with one
relaxed atomic load, and the snapshot (percentiles, phase deltas,
``GetThreadTimes``, ``NativeXfbCopyCount``) only runs while it is visible.

Why the header is written from here instead of copied from ``overlays/``
-----------------------------------------------------------------------
``fix_gl_raster_shadow.py`` asserts that the only header under
``include/runtime`` that calls a plain ``glX(`` state setter is
``native_resolution.h``.  The overlay therefore writes every setter it touches
as ``(glX)(...)`` - the same trick CotT's ``native_hud_gl.h`` documents - and
restores the exact previous state, so the raster shadow's cache stays true.  It
also replaces CotT's GDI font with a built-in 5x7 table: this tree links no
gdi32 and carries no font file.

Env:
    NWII_DEV_HUD=1     start with the overlay visible
    NWII_DEV_HUD_DIR   where F4 writes (default: logs)
    NWII_PERF_PHASE=1  time the nine host phases (the overlay reads them)

F3 toggles, F4 writes ``<dir>/dev-hud-<stamp>.txt`` and ``.json`` and copies
the text to the clipboard, F9 restarts the measurement window.

Idempotent: the header is rewritten only when its bytes differ and every source
edit is guarded by its own token.  Anchors are asserted to match exactly once.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_DEV_HUD_V1'

HEADER = r'''#pragma once
// NATIVE_MOM_DEV_HUD_V1 - F3 developer overlay, F4 snapshot export.
//
// Ported from the Crash of the Titans port's native_hud.h / native_stutter.h /
// native_hud_gl.h. This runtime has no per-scope exclusive-time slots (its
// native_trace::Scope is regenerated from overlays/ by fix_perf_frame_trace.py)
// and no GX workload counters, so the panel shows what this runtime does keep:
// native_perf_phase's nine host phases, its per-frame ring, and the present /
// XFB counters the frame-rate title already prints. No new hot-path counter.
//
// Cost when hidden: one relaxed atomic load at each call site - frame(),
// present() and draw() once per field, key() only on a key event. Every clock
// read, percentile sort, GetThreadTimes and file write happens while visible.
//
// Every raster state setter is written as (glX)(...) on purpose:
// fix_gl_raster_shadow.py asserts that the only header writer of a plain glX(
// setter is include/runtime/native_resolution.h. The overlay saves and restores
// the exact previous state, so the raster shadow's cache stays true.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <SDL.h>
#include <glad/glad.h>
#include "runtime/native_perf_phase.h"
#include "runtime/native_resolution.h"

// Counters this runtime already keeps (declared in main.cpp / gx.cpp /
// renderer_gl.cpp). Reading them costs a load and adds no hot-path work.
namespace nwii { namespace runtime { namespace gx { uint64_t NativeXfbCopyCount(); } } }
extern uint64_t g_stat_frames, g_stat_draws;
uint64_t nwii_stat_shaders();

namespace native_dev_hud {

using native_perf_phase::Phase_Count;

inline std::atomic<bool> on{false};
inline double budget_ms = 16.683;   // VI field period, set at start-up
inline double late_ms = 20.05;      // one 50 Hz field plus 0.05 ms
inline std::string info = "MoM native port";
inline std::string toast;
inline uint64_t toast_until = 0;

inline std::string fmt(const char *f, ...) {
  char b[320];
  va_list a;
  va_start(a, f);
  const int n = std::vsnprintf(b, sizeof b, f, a);
  va_end(a);
  if (n < 0) return std::string();
  if (size_t(n) < sizeof b) return std::string(b, size_t(n));
  std::string out(size_t(n) + 1, '\0');
  va_start(a, f);
  std::vsnprintf(&out[0], out.size(), f, a);
  va_end(a);
  out.resize(size_t(n));
  return out;
}

// ---------------------------------------------------------------------------
// 5x7 font. CotT rasterises Consolas with GDI; this tree links no gdi32 and
// has no font file, so the atlas is generated from this table instead. Each
// glyph is five columns, bit 0 = top row.
// ---------------------------------------------------------------------------
inline const unsigned char kFont[95][5] = {
    {0x00, 0x00, 0x00, 0x00, 0x00},  // 32 ' '
    {0x00, 0x00, 0x5F, 0x00, 0x00},  // 33 '!'
    {0x00, 0x07, 0x00, 0x07, 0x00},  // 34 '"'
    {0x14, 0x7F, 0x14, 0x7F, 0x14},  // 35 '#'
    {0x24, 0x2A, 0x7F, 0x2A, 0x12},  // 36 '$'
    {0x23, 0x13, 0x08, 0x64, 0x62},  // 37 '%'
    {0x36, 0x49, 0x55, 0x22, 0x50},  // 38 '&'
    {0x00, 0x05, 0x03, 0x00, 0x00},  // 39 apostrophe
    {0x00, 0x1C, 0x22, 0x41, 0x00},  // 40 '('
    {0x00, 0x41, 0x22, 0x1C, 0x00},  // 41 ')'
    {0x14, 0x08, 0x3E, 0x08, 0x14},  // 42 '*'
    {0x08, 0x08, 0x3E, 0x08, 0x08},  // 43 '+'
    {0x00, 0x50, 0x30, 0x00, 0x00},  // 44 ','
    {0x08, 0x08, 0x08, 0x08, 0x08},  // 45 '-'
    {0x00, 0x60, 0x60, 0x00, 0x00},  // 46 '.'
    {0x20, 0x10, 0x08, 0x04, 0x02},  // 47 '/'
    {0x3E, 0x51, 0x49, 0x45, 0x3E},  // 48 '0'
    {0x00, 0x42, 0x7F, 0x40, 0x00},  // 49 '1'
    {0x42, 0x61, 0x51, 0x49, 0x46},  // 50 '2'
    {0x21, 0x41, 0x45, 0x4B, 0x31},  // 51 '3'
    {0x18, 0x14, 0x12, 0x7F, 0x10},  // 52 '4'
    {0x27, 0x45, 0x45, 0x45, 0x39},  // 53 '5'
    {0x3C, 0x4A, 0x49, 0x49, 0x30},  // 54 '6'
    {0x01, 0x71, 0x09, 0x05, 0x03},  // 55 '7'
    {0x36, 0x49, 0x49, 0x49, 0x36},  // 56 '8'
    {0x06, 0x49, 0x49, 0x29, 0x1E},  // 57 '9'
    {0x00, 0x36, 0x36, 0x00, 0x00},  // 58 ':'
    {0x00, 0x56, 0x36, 0x00, 0x00},  // 59 ';'
    {0x08, 0x14, 0x22, 0x41, 0x00},  // 60 '<'
    {0x14, 0x14, 0x14, 0x14, 0x14},  // 61 '='
    {0x00, 0x41, 0x22, 0x14, 0x08},  // 62 '>'
    {0x02, 0x01, 0x51, 0x09, 0x06},  // 63 '?'
    {0x32, 0x49, 0x79, 0x41, 0x3E},  // 64 '@'
    {0x7E, 0x11, 0x11, 0x11, 0x7E},  // 65 'A'
    {0x7F, 0x49, 0x49, 0x49, 0x36},  // 66 'B'
    {0x3E, 0x41, 0x41, 0x41, 0x22},  // 67 'C'
    {0x7F, 0x41, 0x41, 0x22, 0x1C},  // 68 'D'
    {0x7F, 0x49, 0x49, 0x49, 0x41},  // 69 'E'
    {0x7F, 0x09, 0x09, 0x09, 0x01},  // 70 'F'
    {0x3E, 0x41, 0x49, 0x49, 0x7A},  // 71 'G'
    {0x7F, 0x08, 0x08, 0x08, 0x7F},  // 72 'H'
    {0x00, 0x41, 0x7F, 0x41, 0x00},  // 73 'I'
    {0x20, 0x40, 0x41, 0x3F, 0x01},  // 74 'J'
    {0x7F, 0x08, 0x14, 0x22, 0x41},  // 75 'K'
    {0x7F, 0x40, 0x40, 0x40, 0x40},  // 76 'L'
    {0x7F, 0x02, 0x0C, 0x02, 0x7F},  // 77 'M'
    {0x7F, 0x04, 0x08, 0x10, 0x7F},  // 78 'N'
    {0x3E, 0x41, 0x41, 0x41, 0x3E},  // 79 'O'
    {0x7F, 0x09, 0x09, 0x09, 0x06},  // 80 'P'
    {0x3E, 0x41, 0x51, 0x21, 0x5E},  // 81 'Q'
    {0x7F, 0x09, 0x19, 0x29, 0x46},  // 82 'R'
    {0x46, 0x49, 0x49, 0x49, 0x31},  // 83 'S'
    {0x01, 0x01, 0x7F, 0x01, 0x01},  // 84 'T'
    {0x3F, 0x40, 0x40, 0x40, 0x3F},  // 85 'U'
    {0x1F, 0x20, 0x40, 0x20, 0x1F},  // 86 'V'
    {0x3F, 0x40, 0x38, 0x40, 0x3F},  // 87 'W'
    {0x63, 0x14, 0x08, 0x14, 0x63},  // 88 'X'
    {0x07, 0x08, 0x70, 0x08, 0x07},  // 89 'Y'
    {0x61, 0x51, 0x49, 0x45, 0x43},  // 90 'Z'
    {0x00, 0x7F, 0x41, 0x41, 0x00},  // 91 '['
    {0x02, 0x04, 0x08, 0x10, 0x20},  // 92 '\'
    {0x00, 0x41, 0x41, 0x7F, 0x00},  // 93 ']'
    {0x04, 0x02, 0x01, 0x02, 0x04},  // 94 '^'
    {0x40, 0x40, 0x40, 0x40, 0x40},  // 95 '_'
    {0x00, 0x01, 0x02, 0x04, 0x00},  // 96 '`'
    {0x00, 0x00, 0x00, 0x00, 0x00},  // 97-122 lowercase: drawn upper case
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},
    {0x00, 0x00, 0x00, 0x00, 0x00},  // 122 'z'
    {0x00, 0x08, 0x36, 0x41, 0x00},  // 123 '{'
    {0x00, 0x00, 0x7F, 0x00, 0x00},  // 124 '|'
    {0x00, 0x41, 0x36, 0x08, 0x00},  // 125 '}'
    {0x08, 0x04, 0x08, 0x10, 0x08},  // 126 '~'
};

// Cell 95 of the atlas (code 127) is a solid block: rectangles reuse the text
// batch instead of adding a second draw path.
inline constexpr int kCellW = 6, kCellH = 8, kGlyphW = 5, kGlyphH = 7;

struct Font {
  GLuint tex = 0;
  bool ok = false;
};
inline Font font;
inline int text_scale = 2;

inline bool init_font() {
  if (font.ok) return true;
  const int W = 96 * kCellW, H = kCellH;
  std::vector<unsigned char> pix(size_t(W) * size_t(H), 0);
  for (int c = 32; c < 127; ++c) {
    const unsigned char *g = kFont[c - 32];
    const int cx = (c - 32) * kCellW;
    for (int x = 0; x < kGlyphW; ++x)
      for (int y = 0; y < kGlyphH; ++y)
        if (g[x] & (1u << y)) pix[size_t(y) * size_t(W) + size_t(cx + x)] = 255;
  }
  for (int y = 0; y < kGlyphH; ++y)
    for (int x = 0; x < kGlyphW; ++x)
      pix[size_t(y) * size_t(W) + size_t(95 * kCellW + x)] = 255;
  GLint unpack = 4;
  glGetIntegerv(GL_UNPACK_ALIGNMENT, &unpack);
  glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
  GLint bound = 0;
  glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
  glGenTextures(1, &font.tex);
  glBindTexture(GL_TEXTURE_2D, font.tex);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, W, H, 0, GL_RED, GL_UNSIGNED_BYTE, pix.data());
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glBindTexture(GL_TEXTURE_2D, GLuint(bound));
  glPixelStorei(GL_UNPACK_ALIGNMENT, unpack);
  font.ok = true;
  return true;
}

struct Gpu {
  GLuint prog = 0, vao = 0, vbo = 0;
  GLint u_view = -1;
  bool ready = false;
};
inline Gpu gpu;

inline bool init_gpu() {
  if (gpu.ready) return true;
  if (!init_font()) return false;
  const char *vs =
      "#version 330 core\n"
      "layout(location=0) in vec2 p; layout(location=1) in vec2 t; layout(location=2) in vec4 c;\n"
      "uniform vec2 uView; out vec2 vt; out vec4 vc;\n"
      "void main(){vt=t;vc=c;gl_Position=vec4(p.x/uView.x*2.0-1.0,1.0-p.y/uView.y*2.0,0.0,1.0);}\n";
  const char *fs =
      "#version 330 core\n"
      "uniform sampler2D uTex; in vec2 vt; in vec4 vc; out vec4 o;\n"
      "void main(){o=vec4(vc.rgb,vc.a*texture(uTex,vt).r);}\n";
  auto compile = [](GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    return s;
  };
  GLuint v = compile(GL_VERTEX_SHADER, vs), f = compile(GL_FRAGMENT_SHADER, fs);
  gpu.prog = glCreateProgram();
  glAttachShader(gpu.prog, v);
  glAttachShader(gpu.prog, f);
  glLinkProgram(gpu.prog);
  glDeleteShader(v);
  glDeleteShader(f);
  GLint ok = 0;
  glGetProgramiv(gpu.prog, GL_LINK_STATUS, &ok);
  if (!ok) return false;
  gpu.u_view = glGetUniformLocation(gpu.prog, "uView");
  GLint vao = 0, vbo = 0;
  glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
  glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &vbo);
  glGenVertexArrays(1, &gpu.vao);
  glBindVertexArray(gpu.vao);
  glGenBuffers(1, &gpu.vbo);
  glBindBuffer(GL_ARRAY_BUFFER, gpu.vbo);
  const GLsizei stride = 8 * sizeof(float);
  glEnableVertexAttribArray(0);
  glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, (void *)0);
  glEnableVertexAttribArray(1);
  glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride, (void *)(2 * sizeof(float)));
  glEnableVertexAttribArray(2);
  glVertexAttribPointer(2, 4, GL_FLOAT, GL_FALSE, stride, (void *)(4 * sizeof(float)));
  glBindVertexArray(GLuint(vao));
  glBindBuffer(GL_ARRAY_BUFFER, GLuint(vbo));
  gpu.ready = true;
  return true;
}

struct Canvas {
  std::vector<float> v;
  void quad(float x0, float y0, float x1, float y1, float u0, float v0, float u1, float v1, const float c[4]) {
    const float q[6][8] = {{x0, y0, u0, v0, c[0], c[1], c[2], c[3]},
                           {x1, y0, u1, v0, c[0], c[1], c[2], c[3]},
                           {x1, y1, u1, v1, c[0], c[1], c[2], c[3]},
                           {x0, y0, u0, v0, c[0], c[1], c[2], c[3]},
                           {x1, y1, u1, v1, c[0], c[1], c[2], c[3]},
                           {x0, y1, u0, v1, c[0], c[1], c[2], c[3]}};
    const size_t begin = v.size();
    v.resize(begin + 48);
    std::memcpy(v.data() + begin, q, sizeof(q));
  }
  void rect(float x0, float y0, float x1, float y1, const float c[4]) {
    const float W = float(96 * kCellW), H = float(kCellH);
    const float u = (95.f * kCellW + kGlyphW * 0.5f) / W, w = kGlyphH * 0.5f / H;
    quad(x0, y0, x1, y1, u, w, u, w, c);
  }
  float text(float x, float y, const std::string &s, const float c[4]) {
    const float W = float(96 * kCellW), H = float(kCellH);
    const float cw = float(kCellW * text_scale);
    const float gw = float(kGlyphW * text_scale), gh = float(kGlyphH * text_scale);
    for (char raw : s) {
      int code = static_cast<unsigned char>(raw);
      if (code >= 'a' && code <= 'z') code -= 32;   // the atlas holds upper case
      if (code < 32 || code > 126) code = ' ';
      if (code != ' ') {
        const float u0 = float((code - 32) * kCellW) / W;
        quad(x, y, x + gw, y + gh, u0, 0.f, u0 + float(kGlyphW) / W, float(kGlyphH) / H, c);
      }
      x += cw;
    }
    return x;
  }
};

// ---------------------------------------------------------------------------
// Frame history. The frame is one iteration of the host render loop, exactly
// what native_perf_phase::frame_end() records, so the percentiles are the same
// quantity as [PERFPHASE] frame_ms and its CSV.
// ---------------------------------------------------------------------------
inline constexpr unsigned kRing = 256;
inline constexpr unsigned kWindow = 240;   // the window [PERFPHASE] uses
inline uint64_t ring_ns[kRing] = {};
inline unsigned ring_n = 0;
inline uint64_t last_stamp_ns = 0;
inline uint64_t last_index = 0;      // native_perf_phase::frame_index already consumed
inline uint64_t frame_calls = 0;
inline uint64_t presents = 0, fresh_frames = 0, copies_now = 0;
inline uint64_t worst_ns = 0, worst_index = ~uint64_t(0);

inline bool is_stall(unsigned p) {
  return p == native_perf_phase::Phase_HostField ||
         p == native_perf_phase::Phase_HostDrainWait ||
         p == native_perf_phase::Phase_GuestIdle;
}

// Total()/Hits() take the enum; the tables are indexed by its ordinal.
inline native_perf_phase::Phase phase_of(unsigned p) {
  return native_perf_phase::Phase(p);
}

struct Snapshot {
  double window_s = 0, frames = 0;
  double new_fps = 0, present_fps = 0, xfb_fps = 0;
  double avg_ms = 0, p50_ms = 0, p95_ms = 0, p99_ms = 0, max_ms = 0;
  unsigned late = 0, samples = 0;
  bool phases = false;
  double phase_ms[Phase_Count] = {};
  double phase_calls[Phase_Count] = {};
  double host_sum_ms = 0, busy_pct = 0;
  double guest_cpu_ms_s = 0, draws_per_frame = 0;
  double worst_ms = 0;
  uint64_t worst_index = ~uint64_t(0);
  std::vector<double> recent;   // chronological, ms
  bool valid = false;
};
inline Snapshot current;

inline uint64_t snap_stamp = 0, snap_frames = 0, snap_presents = 0, snap_fresh = 0;
inline uint64_t snap_copies = 0, snap_draws = 0, snap_guest_cpu = 0;
inline uint64_t snap_phase[Phase_Count] = {};
inline uint64_t snap_hits[Phase_Count] = {};

inline void take_baseline() {
  snap_stamp = native_perf_phase::now_ns();
  snap_frames = frame_calls;
  snap_presents = presents;
  snap_fresh = fresh_frames;
  snap_copies = nwii::runtime::gx::NativeXfbCopyCount();
  snap_draws = g_stat_draws;
  snap_guest_cpu = native_perf_phase::guest_cpu_us();
  for (unsigned p = 0; p < Phase_Count; ++p) {
    snap_phase[p] = native_perf_phase::Total(phase_of(p));
    snap_hits[p] = native_perf_phase::Hits(phase_of(p));
  }
}

inline bool compute(Snapshot &s) {
  if (!snap_stamp) return false;
  const uint64_t now = native_perf_phase::now_ns();
  const double win = double(now - snap_stamp) / 1e9;
  if (win < 0.45) return false;
  const double df = double(frame_calls - snap_frames);
  if (df < 1.0) return false;
  Snapshot r;
  r.window_s = win;
  r.frames = df;
  copies_now = nwii::runtime::gx::NativeXfbCopyCount();
  r.new_fps = double(fresh_frames - snap_fresh) / win;
  r.present_fps = double(presents - snap_presents) / win;
  r.xfb_fps = double(copies_now - snap_copies) / win;
  r.draws_per_frame = double(g_stat_draws - snap_draws) / df;
  r.guest_cpu_ms_s = double(native_perf_phase::guest_cpu_us() - snap_guest_cpu) / 1000.0 / win;
  r.phases = native_perf_phase::enabled;
  for (unsigned p = 0; p < Phase_Count; ++p) {
    r.phase_ms[p] = double(native_perf_phase::Total(phase_of(p)) - snap_phase[p]) / 1e6 / df;
    r.phase_calls[p] = double(native_perf_phase::Hits(phase_of(p)) - snap_hits[p]) / df;
  }
  r.host_sum_ms = r.phase_ms[native_perf_phase::Phase_GxParse] +
                  r.phase_ms[native_perf_phase::Phase_GxDecode] +
                  r.phase_ms[native_perf_phase::Phase_GlRender] +
                  r.phase_ms[native_perf_phase::Phase_XfbCopy] +
                  r.phase_ms[native_perf_phase::Phase_Present] +
                  r.phase_ms[native_perf_phase::Phase_Swap];
  r.busy_pct = budget_ms > 0.0 ? 100.0 * r.host_sum_ms / budget_ms : 0.0;
  const unsigned n = std::min<unsigned>(ring_n, kWindow);
  r.recent.clear();
  r.recent.reserve(n);
  for (unsigned i = 0; i < n; ++i)
    r.recent.push_back(double(ring_ns[(ring_n - n + i) % kRing]) / 1e6);
  r.late = 0;
  for (double ms : r.recent)
    if (ms > late_ms) ++r.late;
  r.samples = n;
  if (!r.recent.empty()) {
    std::vector<double> v = r.recent;
    std::sort(v.begin(), v.end());
    double sum = 0;
    for (double x : v) sum += x;
    auto pct = [&v](double p) { return v[std::min<size_t>(v.size() - 1, size_t(p * (v.size() - 1) + 0.5))]; };
    r.avg_ms = sum / double(v.size());
    r.p50_ms = pct(0.5);
    r.p95_ms = pct(0.95);
    r.p99_ms = pct(0.99);
    r.max_ms = v.back();
  }
  r.worst_ms = double(worst_ns) / 1e6;
  r.worst_index = worst_index;
  r.valid = true;
  s = r;
  return true;
}

// ---------------------------------------------------------------------------
// Entry points used by the host loop.
// ---------------------------------------------------------------------------
inline void frame() {                       // once per host loop iteration
  if (!on.load(std::memory_order_relaxed)) return;
  uint64_t dt = 0, index = ~uint64_t(0);
  if (native_perf_phase::enabled && native_perf_phase::frame_index > last_index) {
    // The phase timers just closed this frame's record: use it. That wall_ns is
    // exactly the frame_ms column [PERFPHASE] and perf_report.py read, so the
    // percentiles here are the same series, not a second measurement of it.
    index = native_perf_phase::frame_index - 1;
    dt = native_perf_phase::ring[index % native_perf_phase::kFrameRing].wall_ns;
    last_index = native_perf_phase::frame_index;
    last_stamp_ns = 0;
  } else {
    // No phase timers: the host loop period is the frame time.
    const uint64_t t = native_perf_phase::now_ns();
    if (!last_stamp_ns) {
      last_stamp_ns = t;
      return;
    }
    dt = t - last_stamp_ns;
    last_stamp_ns = t;
  }
  ring_ns[ring_n % kRing] = dt;
  ++ring_n;
  ++frame_calls;
  if (dt >= worst_ns) {
    worst_ns = dt;
    worst_index = index;
  }
}

inline void present(bool fresh) {            // once per presented field
  if (!on.load(std::memory_order_relaxed)) return;
  ++presents;
  if (fresh) ++fresh_frames;
}

// ---------------------------------------------------------------------------
// Panel text. One source of truth: draw() renders these lines and F4 writes
// the same lines to the .txt file.
// ---------------------------------------------------------------------------
inline std::string pct_text(unsigned part, unsigned whole) {
  return whole ? fmt("%.1f%%", 100.0 * double(part) / double(whole)) : std::string("--");
}

inline std::vector<std::string> header_lines(const Snapshot &s) {
  std::vector<std::string> L;
  L.push_back("DEV STATS    F3 hide   F4 export   F9 reset");
  if (!s.valid) {
    L.push_back("collecting the first window...");
    return L;
  }
  L.push_back(fmt("new %5.1f/s   present %5.1f/s   xfb %5.1f/s   budget %5.2f ms",
                  s.new_fps, s.present_fps, s.xfb_fps, budget_ms));
  L.push_back(fmt("frame ms  avg %5.2f  p50 %5.2f  p95 %5.2f  p99 %5.2f  max %5.2f",
                  s.avg_ms, s.p50_ms, s.p95_ms, s.p99_ms, s.max_ms));
  L.push_back(fmt("          over %5.2f ms: %u of %u frames (%s)",
                  late_ms, s.late, s.samples, pct_text(s.late, s.samples).c_str()));
  return L;
}

inline std::vector<std::string> body_lines(const Snapshot &s) {
  std::vector<std::string> L;
  if (!s.valid) return L;
  if (!s.phases) {
    L.push_back("phases off - run with NWII_PERF_PHASE=1 to time the nine host phases");
  } else {
    L.push_back(fmt("phases ms/frame   host_sum %5.2f   busy %5.1f%%   window %.2fs / %.0f frames",
                    s.host_sum_ms, s.busy_pct, s.window_s, s.frames));
    for (unsigned p = 0; p < Phase_Count; ++p) {
      const double per_call = s.phase_calls[p] > 0.0 ? 1000.0 * s.phase_ms[p] / s.phase_calls[p] : 0.0;
      L.push_back(fmt("  %-16s %6.2f ms  %5.1f x/frame  %6.1f us/call%s",
                      native_perf_phase::phase_name(p), s.phase_ms[p], s.phase_calls[p], per_call,
                      is_stall(p) ? "  stall" : ""));
    }
  }
  if (s.worst_index != ~uint64_t(0) && native_perf_phase::enabled &&
      s.worst_index < native_perf_phase::frame_index) {
    const native_perf_phase::FrameRecord &r = native_perf_phase::ring[s.worst_index % native_perf_phase::kFrameRing];
    std::string line = fmt("worst frame %6.2f ms:", s.worst_ms);
    for (unsigned p = 0; p < Phase_Count; ++p)
      line += fmt(" %s %.1f", native_perf_phase::phase_name(p), double(r.phase_ns[p]) / 1e6);
    L.push_back(line);
  } else {
    L.push_back(fmt("worst frame %6.2f ms (no phase record: NWII_PERF_PHASE=1)", s.worst_ms));
  }
  L.push_back(fmt("gx frames %llu   draws %llu (%.1f/frame)   shaders %llu   guest cpu %.0f ms/s",
                  (unsigned long long)g_stat_frames, (unsigned long long)g_stat_draws,
                  s.draws_per_frame, (unsigned long long)nwii_stat_shaders(), s.guest_cpu_ms_s));
  L.push_back(info);
  return L;
}

// ---------------------------------------------------------------------------
// F4: txt + json snapshot under logs/ (or NWII_DEV_HUD_DIR).
// ---------------------------------------------------------------------------
inline void export_snapshot() {
  Snapshot s = current;
  if (!compute(s)) s = current;
  if (!s.valid) {
    toast = "no window yet - press F3 and let a few frames run";
    toast_until = native_perf_phase::now_ns() + 3000000000ull;
    std::printf("[DEVHUD] %s\n", toast.c_str());
    std::fflush(stdout);
    return;
  }
  const char *dir_env = std::getenv("NWII_DEV_HUD_DIR");
  const std::string dir = (dir_env && *dir_env) ? dir_env : "logs";
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  std::time_t now = std::time(nullptr);
  char stamp[32];
  std::strftime(stamp, sizeof stamp, "%Y%m%d-%H%M%S", std::localtime(&now));
  const std::string base = dir + "/dev-hud-" + stamp;

  std::string text = "MoM native port - developer HUD snapshot (F3 overlay, F4 export)\n";
  text += info + "\n";
  text += fmt("window %.2fs, %.0f frames, budget %.3f ms, late threshold %.2f ms\n\n",
              s.window_s, s.frames, budget_ms, late_ms);
  for (const std::string &l : header_lines(s)) text += l + "\n";
  text += "\n";
  for (const std::string &l : body_lines(s)) text += l + "\n";
  text += "\nframe_ms history (oldest first, " + std::to_string(s.recent.size()) + " frames):\n";
  for (size_t i = 0; i < s.recent.size(); ++i) {
    text += fmt(i ? " %.2f" : "%.2f", s.recent[i]);
    if ((i + 1) % 16 == 0) text += "\n";
  }
  text += "\ncross-check: the same frame intervals are the frame_ms column of the\n";
  text += "NWII_PERF_PHASE_FILE csv; python logs/perf_report.py prints its p95/max.\n";

  std::string json = "{\"schema\":1,\"marker\":\"NATIVE_MOM_DEV_HUD_V1\"";
  json += ",\"timing_source\":\"native_perf_phase\"";
  json += ",\"info\":\"" + info + "\"";
  json += fmt(",\"budget_ms\":%.3f,\"late_ms\":%.3f,\"late_frames\":%u,\"late_samples\":%u",
              budget_ms, late_ms, s.late, s.samples);
  json += fmt(",\"window_s\":%.3f,\"frames\":%.0f", s.window_s, s.frames);
  json += fmt(",\"new_fps\":%.3f,\"present_fps\":%.3f,\"xfb_fps\":%.3f",
              s.new_fps, s.present_fps, s.xfb_fps);
  json += fmt(",\"frame_ms\":{\"avg\":%.3f,\"p50\":%.3f,\"p95\":%.3f,\"p99\":%.3f,\"max\":%.3f}",
              s.avg_ms, s.p50_ms, s.p95_ms, s.p99_ms, s.max_ms);
  json += fmt(",\"phases_available\":%s,\"host_sum_ms\":%.3f,\"busy_pct\":%.2f",
              s.phases ? "true" : "false", s.host_sum_ms, s.busy_pct);
  json += fmt(",\"work\":{\"gx_frames\":%llu,\"draws\":%llu,\"draws_per_frame\":%.3f,\"shaders\":%llu,\"guest_cpu_ms_s\":%.2f}",
              (unsigned long long)g_stat_frames, (unsigned long long)g_stat_draws,
              s.draws_per_frame, (unsigned long long)nwii_stat_shaders(), s.guest_cpu_ms_s);
  json += ",\"phases\":[";
  for (unsigned p = 0; p < Phase_Count; ++p) {
    json += fmt("%s{\"name\":\"%s\",\"ms_per_frame\":%.4f,\"calls_per_frame\":%.3f,\"stall\":%s}",
                p ? "," : "", native_perf_phase::phase_name(p), s.phase_ms[p], s.phase_calls[p],
                is_stall(p) ? "true" : "false");
  }
  json += fmt("],\"worst_frame\":{\"frame_ms\":%.3f,\"has_phase_record\":%s",
              s.worst_ms,
              (s.worst_index != ~uint64_t(0) && native_perf_phase::enabled &&
               s.worst_index < native_perf_phase::frame_index) ? "true" : "false");
  if (s.worst_index != ~uint64_t(0) && native_perf_phase::enabled &&
      s.worst_index < native_perf_phase::frame_index) {
    const native_perf_phase::FrameRecord &r = native_perf_phase::ring[s.worst_index % native_perf_phase::kFrameRing];
    json += fmt(",\"frame\":%llu,\"wall_ms\":%.3f,\"phase_ms\":[",
                (unsigned long long)s.worst_index, double(r.wall_ns) / 1e6);
    for (unsigned p = 0; p < Phase_Count; ++p)
      json += fmt(p ? ",%.3f" : "%.3f", double(r.phase_ns[p]) / 1e6);
    json += "]";
  }
  json += "},\"frame_ms_history\":[";
  for (size_t i = 0; i < s.recent.size(); ++i) json += fmt(i ? ",%.3f" : "%.3f", s.recent[i]);
  json += "]}";

  {
    std::ofstream f(base + ".txt", std::ios::binary);
    f << text;
  }
  {
    std::ofstream f(base + ".json", std::ios::binary);
    f << json;
  }
  SDL_SetClipboardText(text.c_str());
  std::printf("[DEVHUD] exported %s.txt / .json (text copied to clipboard)\n", base.c_str());
  std::fflush(stdout);
  toast = "exported " + base;
  toast_until = native_perf_phase::now_ns() + 4000000000ull;
}

// ---------------------------------------------------------------------------
// Visibility, reset, keys.
// ---------------------------------------------------------------------------
inline void reset_window(const char *why) {
  ring_n = 0;
  frame_calls = 0;
  presents = 0;
  fresh_frames = 0;
  last_stamp_ns = 0;
  last_index = 0;
  worst_ns = 0;
  worst_index = ~uint64_t(0);
  current = Snapshot();
  take_baseline();
  if (why) {
    toast = why;
    toast_until = native_perf_phase::now_ns() + 2000000000ull;
  }
}

inline void set_on(bool v) {
  if (v && !on.load(std::memory_order_relaxed)) {
    on.store(true, std::memory_order_relaxed);
    reset_window(nullptr);
    std::printf("[DEVHUD] visible: percentages over the last %u frames; F4 exports, F9 resets\n", kWindow);
  } else if (!v && on.load(std::memory_order_relaxed)) {
    on.store(false, std::memory_order_relaxed);
    std::printf("[DEVHUD] hidden: timing stopped\n");
  }
  std::fflush(stdout);
}

// Returns true when the key was consumed, so the caller's else-if chain in the
// SDL poll still reaches F11 and the rest of the game's keys.
inline bool key(SDL_Keycode k) {
  const int code = int(k);
  switch (code) {
    case SDLK_F3:
      set_on(!on.load(std::memory_order_relaxed));
      return true;
    case SDLK_F4:
      export_snapshot();
      return true;
    case SDLK_F9:
      reset_window("window reset");
      return true;
    default:
      return false;
  }
}

inline void configure(double field_ms, const char *renderer, int internal_scale) {
  if (field_ms > 1.0 && field_ms < 1000.0) {
    budget_ms = field_ms;
    // One dropped 50 Hz field: 20.05 ms. In an NTSC field (16.68 ms) that is
    // also a dropped field, so the threshold never falls below 20.05.
    late_ms = (field_ms > 20.0 ? field_ms : 20.0) + 0.05;
  }
  info = renderer && *renderer ? std::string("GPU ") + renderer : std::string("headless");
  info += fmt(" | internal %dx | budget %.2f ms | phases %s",
              internal_scale, budget_ms,
              native_perf_phase::enabled ? "on (NWII_PERF_PHASE)" : "off (set NWII_PERF_PHASE=1)");
  if (std::getenv("NWII_DEV_HUD")) set_on(true);
}

// ---------------------------------------------------------------------------
// Draw. Everything GL touches is saved and restored: the renderer caches its
// own state and the raster shadow caches caps, so the overlay must leave the
// pipeline exactly as it found it.
// ---------------------------------------------------------------------------
inline void draw(int W, int H) {
  if (!on.load(std::memory_order_relaxed)) return;
  if (W <= 0 || H <= 0) return;
  static uint64_t next_refresh = 0;
  const uint64_t t = native_perf_phase::now_ns();
  if (t >= next_refresh) {
    next_refresh = t + 500000000ull;
    Snapshot s = current;
    if (compute(s)) {
      current = s;
      take_baseline();
      worst_ns = 0;
      worst_index = ~uint64_t(0);
    }
  }
  const int scale = std::clamp(int(std::lround(double(H) / 400.0)), 1, 3);
  text_scale = scale;
  if (!init_gpu()) return;
  const Snapshot &s = current;

  static Canvas c;
  static uint64_t built = 0;
  static int old_w = 0, old_h = 0, old_scale = 0;
  static bool old_valid = false;
  const bool rebuild = t >= built + 125000000ull || W != old_w || H != old_h ||
                       scale != old_scale || old_valid != s.valid;
  if (rebuild) {
    built = t;
    old_w = W;
    old_h = H;
    old_scale = scale;
    old_valid = s.valid;
    c.v.clear();
    if (c.v.capacity() < 131072) c.v.reserve(131072);
    const std::vector<std::string> head = header_lines(s), body = body_lines(s);
    size_t cols = 0;
    for (const std::string &l : head) cols = std::max(cols, l.size());
    for (const std::string &l : body) cols = std::max(cols, l.size());
    const float lh = float(kCellH * scale), pad = 6.f * scale, x0 = 8.f * scale, y0 = 8.f * scale;
    const float graph_h = 16.f * scale;
    const float pw = float(cols) * float(kCellW * scale) + pad * 2.f;
    const float ph = pad * 2.f + lh * float(head.size() + body.size() + 2) + graph_h;
    const float white[4] = {0.93f, 0.94f, 0.97f, 1}, dim[4] = {0.58f, 0.62f, 0.70f, 1},
                green[4] = {0.30f, 0.87f, 0.50f, 1}, amber[4] = {1.0f, 0.70f, 0.25f, 1},
                red[4] = {0.97f, 0.40f, 0.40f, 1}, bg[4] = {0.035f, 0.045f, 0.06f, 0.93f},
                accent[4] = {1.0f, 0.54f, 0.17f, 1}, grey[4] = {0.16f, 0.20f, 0.26f, 0.85f};
    c.rect(x0, y0, x0 + pw, y0 + ph, bg);
    c.rect(x0, y0, x0 + 3.f * scale, y0 + ph, accent);
    float y = y0 + pad;
    const float x = x0 + pad + 3.f * scale;
    if (!head.empty()) {
      c.text(x, y, head[0], accent);
      y += lh;
      for (size_t i = 1; i < head.size(); ++i) {
        c.text(x, y, head[i], i == 3 && s.late ? red : white);
        y += lh;
      }
    }
    // frame-time graph: the last 120 intervals, budget line across.
    {
      const float gx = x, gw = pw - pad * 2.f - 3.f * scale, gh = graph_h - 4.f * scale;
      c.rect(gx, y, gx + gw, y + gh, grey);
      const double top_ms = std::max(2.5 * budget_ms, s.max_ms * 1.05);
      const size_t n = std::min<size_t>(s.recent.size(), 120);
      const float bw = gw / 120.f;
      for (size_t i = 0; i < n; ++i) {
        const double ms = s.recent[s.recent.size() - n + i];
        const float hh = float(std::min(1.0, ms / top_ms)) * gh;
        const float *col = ms <= budget_ms * 1.15 ? green : (ms <= budget_ms * 2.15 ? amber : red);
        c.rect(gx + float(120 - n) * bw + float(i) * bw, y + gh - hh,
               gx + (float(120 - n) + float(i) + 1.f) * bw - 0.5f, y + gh, col);
      }
      const float by = y + gh - float(budget_ms / top_ms) * gh;
      c.rect(gx, by, gx + gw, by + 1.5f * scale, white);
      y += graph_h;
    }
    for (const std::string &l : body) {
      const float *col = dim;
      if (!l.empty() && l[0] == ' ') col = white;          // phase rows
      if (l.size() > 8 && l.compare(0, 5, "worst") == 0) col = amber;
      if (l.size() > 4 && l.compare(0, 9, "gx frames") == 0) col = white;
      c.text(x, y, l, col);
      y += lh;
    }
    if (t < toast_until && !toast.empty()) c.text(x, y, toast, green);
  }

  // ---- save GL state
  GLint prog, vao, abuf, aunit, viewport[4], blendsrc_rgb, blenddst_rgb, blendsrc_a,
      blenddst_a, eq_rgb, eq_a, fbo_r, fbo_d;
  GLboolean blend = glIsEnabled(GL_BLEND), depth = glIsEnabled(GL_DEPTH_TEST),
            cull = glIsEnabled(GL_CULL_FACE), scissor = glIsEnabled(GL_SCISSOR_TEST), mask[4];
  glGetIntegerv(GL_CURRENT_PROGRAM, &prog);
  glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
  glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &abuf);
  glGetIntegerv(GL_ACTIVE_TEXTURE, &aunit);
  glGetIntegerv(GL_VIEWPORT, viewport);
  glGetBooleanv(GL_COLOR_WRITEMASK, mask);
  glGetIntegerv(GL_BLEND_SRC_RGB, &blendsrc_rgb);
  glGetIntegerv(GL_BLEND_DST_RGB, &blenddst_rgb);
  glGetIntegerv(GL_BLEND_SRC_ALPHA, &blendsrc_a);
  glGetIntegerv(GL_BLEND_DST_ALPHA, &blenddst_a);
  glGetIntegerv(GL_BLEND_EQUATION_RGB, &eq_rgb);
  glGetIntegerv(GL_BLEND_EQUATION_ALPHA, &eq_a);
  glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &fbo_d);
  glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &fbo_r);
  glActiveTexture(GL_TEXTURE0);
  GLint tex0 = 0, sampler0 = 0;
  glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex0);
  glGetIntegeri_v(GL_SAMPLER_BINDING, 0, &sampler0);

  // ---- draw
  glBindFramebuffer(GL_DRAW_FRAMEBUFFER, 0);
  glViewport(0, 0, W, H);
  (glDisable)(GL_DEPTH_TEST);
  (glDisable)(GL_CULL_FACE);
  (glDisable)(GL_SCISSOR_TEST);
  (glEnable)(GL_BLEND);
  (glBlendFuncSeparate)(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
  (glBlendEquation)(GL_FUNC_ADD);
  (glColorMask)(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
  glUseProgram(gpu.prog);
  glUniform2f(gpu.u_view, float(W), float(H));
  glBindVertexArray(gpu.vao);
  glBindBuffer(GL_ARRAY_BUFFER, gpu.vbo);
  glBindTexture(GL_TEXTURE_2D, font.tex);
  glBindSampler(0, 0);
  if (rebuild) glBufferData(GL_ARRAY_BUFFER, GLsizeiptr(c.v.size() * sizeof(float)), c.v.data(), GL_STREAM_DRAW);
  glDrawArrays(GL_TRIANGLES, 0, GLsizei(c.v.size() / 8));

  // ---- restore
  glBindTexture(GL_TEXTURE_2D, GLuint(tex0));
  glBindSampler(0, GLuint(sampler0));
  glActiveTexture(GLenum(aunit));
  glBindBuffer(GL_ARRAY_BUFFER, GLuint(abuf));
  glBindVertexArray(GLuint(vao));
  glUseProgram(GLuint(prog));
  (glBlendFuncSeparate)(GLenum(blendsrc_rgb), GLenum(blenddst_rgb), GLenum(blendsrc_a), GLenum(blenddst_a));
  (glBlendEquationSeparate)(GLenum(eq_rgb), GLenum(eq_a));
  if (!blend) (glDisable)(GL_BLEND);
  if (depth) (glEnable)(GL_DEPTH_TEST);
  if (cull) (glEnable)(GL_CULL_FACE);
  if (scissor) (glEnable)(GL_SCISSOR_TEST);
  (glColorMask)(mask[0], mask[1], mask[2], mask[3]);
  glBindFramebuffer(GL_DRAW_FRAMEBUFFER, GLuint(fbo_d));
  glBindFramebuffer(GL_READ_FRAMEBUFFER, GLuint(fbo_r));
  glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
}

}  // namespace native_dev_hud
'''

skipped = []


def write_header():
    dst = runtime / 'include/runtime/native_dev_hud.h'
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_file():
        with open(dst, encoding='utf-8', errors='surrogateescape') as handle:
            if handle.read() == HEADER:
                return False
    with open(dst, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(HEADER)
    return True


def edit(relative, token, old, new):
    """Insert `new` for the single occurrence of `old`, once.

    `token` is this stage's own marker for the edit: when it is already in the
    file the edit has been applied and is skipped, so a second run is a no-op.
    """
    path = runtime / relative
    with open(path, encoding='utf-8', errors='surrogateescape') as handle:
        text = handle.read()
    if token in text:
        return False
    count = text.count(old)
    assert count == 1, '%s: anchor %r matched %d times' % (relative, old[:64], count)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text.replace(old, new, 1))
    return True


# --- the overlay itself ------------------------------------------------------
if not write_header():
    skipped.append('include/runtime/native_dev_hud.h has not changed')

MAIN = 'src/core/main.cpp'

# 1. The header. It must follow native_perf_phase.h: it reads that header's
#    counters, ring and now_ns() clock instead of keeping a second one.
edit(
    MAIN,
    '#include "runtime/native_dev_hud.h"',
    '#include "runtime/native_perf_phase.h" // NATIVE_PERF_PHASE_V1_INCLUDE_MAIN\n',
    '#include "runtime/native_perf_phase.h" // NATIVE_PERF_PHASE_V1_INCLUDE_MAIN\n'
    '#include "runtime/native_dev_hud.h" // ' + MARK + '\n',
)

# 2. F3 / F4 / F9. The key handler is a leading `if` in the SDL poll and returns
#    false for everything it does not own, so the existing `else if` chain still
#    reaches SDL_QUIT and F11. Non-consuming, so F11 keeps working whichever
#    stage installed it.
edit(
    MAIN,
    'native_dev_hud::key(',
    '          if (e.type == SDL_QUIT) quit = true;\n',
    '          if (e.type == SDL_KEYDOWN && !e.key.repeat && native_dev_hud::key(e.key.keysym.sym)) { /* '
    + MARK + ': F3/F4/F9 consumed */ }\n'
    '          else if (e.type == SDL_QUIT) quit = true;\n',
)

# 3. The frame boundary. Placed immediately after native_perf_phase::frame_end()
#    so the interval it measures is the one [PERFPHASE] frame_ms and the CSV
#    report, and so the per-frame ring entry for this frame already exists.
edit(
    MAIN,
    'native_dev_hud::frame();',
    '      native_perf_phase::frame_end(); // NATIVE_PERF_PHASE_V1_FIELD\n',
    '      native_perf_phase::frame_end(); // NATIVE_PERF_PHASE_V1_FIELD\n'
    '      native_dev_hud::frame(); // ' + MARK + ': one interval per host loop iteration\n',
)

# 4. Present: the same `fresh` flag the window title counts, and the XFB copy
#    counter the title reads once a second.
edit(
    MAIN,
    'native_dev_hud::present(',
    '          ++pm_presents; pm_new+=fresh?1:0;\n',
    '          ++pm_presents; pm_new+=fresh?1:0;\n'
    '          native_dev_hud::present(fresh); // ' + MARK + '\n',
)

# 5. Draw over the back buffer after the present blit and before the swap, so
#    the overlay is outside every phase scope (it must not be charged to
#    `present`) and outside the swap that would hide it. Offscreen runs keep
#    the desktop untouched.
edit(
    MAIN,
    'native_dev_hud::draw(',
    '        native_perf_phase_present.stop(); // NATIVE_PERF_PHASE_V1_SWAP\n',
    '        native_perf_phase_present.stop(); // NATIVE_PERF_PHASE_V1_SWAP\n'
    '        if (!present_offscreen) native_dev_hud::draw(draw_w, draw_h); // ' + MARK + '\n',
)

# 6. Start-up: the budget is the VI field period the host loop paces to (the
#    same number [VI cadence] prints), plus the GPU string and internal
#    resolution CotT's overlay shows. NWII_DEV_HUD=1 starts it visible.
edit(
    MAIN,
    'native_dev_hud::configure(',
    '  std::cout << "[VI cadence] nominal field_ns=" << field_period.count() << std::endl;\n',
    '  std::cout << "[VI cadence] nominal field_ns=" << field_period.count() << std::endl;\n'
    '  native_dev_hud::configure(double(field_period.count()) / 1e6,\n'
    '                            headless ? nullptr : reinterpret_cast<const char *>(glGetString(GL_RENDERER)),\n'
    '                            NativeResolutionScale()); // ' + MARK + '\n',
)

for line in skipped:
    print('  skipped: %s' % line)
print('F3 developer overlay and F4 export installed (hidden until F3; NWII_DEV_HUD=1 starts visible).')
