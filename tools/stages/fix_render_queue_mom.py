r"""GL render queue for Mind over Mutant: a GL thread owns the context and draws; the main thread parses, decodes, signals.

Marker: NATIVE_MOM_RENDER_QUEUE_V1. Port of the Crash of the Titans stage fix_render_queue.py (Titans measured +13.5%
over PGO with identical rendering). Requires fix_renderer_state_split_mom.py (the renderer must not share GX state with
the parser) and is meant to run with fix_early_pe_mom.py.

Why: with the save state outside Coco's house and the left stick held, the host thread spent ~49% of wall time in
gl_render and ~20% in parse/decode, and the guest idled ~27% waiting for it. Rendering drain N on its own thread lets the
main thread parse, decode and signal drain N+1 at once.

What moves to the GL thread: RendererGL::Render of each drain (a batch: the command vector plus its parse snapshot) and
the frame's GL work (XFB latch read, composite, probes, screenshots, savestate thumbnail, dev HUD, swap), posted as a
task after the batches before it. The main thread keeps SDL events, input, the drain servicing, the early PE signal and
the field pacing; it applies the window title the GL thread hands over. Savestates flush the GL thread before reading
renderer state. The periodic [STAT] EFB read-back is skipped while the queue runs.

Opt-in: NWII_RENDER_QUEUE=1 turns it on; it stays off when headless or with NWII_NO_RENDER_QUEUE, NWII_XFB or
NWII_SYNC_GPU set. NWII_RENDER_QUEUE_DEPTH (1-8, default 3) sets how many items may be in flight.

Measured (same save, left stick held, 120 FPS cap; machine partly loaded): no gain. The GL thread becomes the critical
path - gl_render takes 61% of wall time on it and batch submission grows from 5% to 17% of wall once GL runs off the
main thread - while the guest still idles ~33%. MoM's per-frame renderer cost is about one frame long, so overlap has
nothing to hide until that cost comes down; the queue is kept for then.

Known hazard (shared with early PE): textures are read from guest RAM when the GL thread draws, so a texture the guest
rewrites after its batch was queued can show the new contents. The Titans texture CPU/GL split closes it.

--restore puts every touched file back exactly as before (backups under logs/stage-backups/) and refuses if a file
changed since this stage wrote it.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_RENDER_QUEUE_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_render_queue_mom'
OVERLAYS = ROOT / 'overlays'
INSTALLS = [('native_render_queue.h', 'include/runtime/native_render_queue.h'),
            ('render_queue_glue_mom.inc', 'src/hle/render_queue_glue_mom.inc')]
GX, MAIN = 'src/hle/gx.cpp', 'src/core/main.cpp'
Q = 'nwii::runtime::render_queue::'


class Skip(Exception):
    pass


def swap(text, old, new, where, count=1):
    if text.count(old) != count:
        raise Skip('%s: expected %d occurrence(s) of %r, found %d' % (where, count, old.strip()[:80], text.count(old)))
    return text.replace(old, new)


def gx(t):
    if 'NATIVE_MOM_RENDERER_STATE_SPLIT_V1' not in t:
        raise Skip('gx.cpp: run fix_renderer_state_split_mom.py first')
    t = swap(t, 'uint64_t g_stat_render_us = 0;\n',
             'uint64_t g_stat_render_us = 0;\n#include "render_queue_glue_mom.inc" // ' + MARK + '\n', GX)
    t = swap(t, '    nwii::runtime::gx::NativeTakeParseSnapshot(nwii::runtime::gx::RenderBatchSnapshot());',
             '    nwii::runtime::gx::NativeTakeParseSnapshot(' + Q + 'Active() ? ' + Q + 'FrontSnapshot() : '
             'nwii::runtime::gx::RenderBatchSnapshot()); // ' + MARK + ': the GL thread owns the batch slot', GX)
    t = swap(t, '    if (g_window) {\n        if (!g_renderer) {\n            g_renderer = nwii::runtime::gx::IRenderer::Create();\n'
                '            g_renderer->Initialize(nullptr);\n        }\n        if (g_renderer) {\n            native_perf_phase::Scope',
             '    if (g_window && ' + Q + 'Active()) {\n'
             '        ' + Q + 'SubmitBatch(frame); // ' + MARK + ': the GL thread renders it, in publish order\n'
             '    } else if (g_window) {\n        if (!g_renderer) {\n            g_renderer = nwii::runtime::gx::IRenderer::Create();\n'
             '            g_renderer->Initialize(nullptr);\n        }\n        if (g_renderer) {\n            native_perf_phase::Scope', GX)
    # Early threshold drains (NATIVE_MOM_GX_OVERLAP_V1, opt-in until now) go with the queue: the main thread is free to parse
    # while the GL thread draws, so drain in 8 KiB steps instead of waiting for GXDrawDone. NWII_NO_GX_EARLY_DRAIN opts out.
    t = swap(t, 'static const bool g_gx_early_drain = std::getenv("NWII_GX_EARLY_DRAIN") != nullptr && std::getenv("NWII_NO_GX_EARLY_DRAIN") == nullptr;',
             'static const bool g_gx_early_drain = (std::getenv("NWII_GX_EARLY_DRAIN") != nullptr || std::getenv("NWII_NO_RENDER_QUEUE") == nullptr) && '
             'std::getenv("NWII_NO_GX_EARLY_DRAIN") == nullptr; // ' + MARK, GX)
    t = swap(t, '    if (v <= 0) return size_t(16384);', '    if (v <= 0) return size_t(8192); // ' + MARK + ': was 16384', GX)
    t = swap(t, '            if (empty && s_native_ss_gx_busy.load(std::memory_order_acquire) == 0) return true;\n',
             '            if (empty && s_native_ss_gx_busy.load(std::memory_order_acquire) == 0) { ' + Q +
             'FlushForSavestate(); return true; } // ' + MARK + '\n', GX)
    return t


DECLS = ('#include <functional> // ' + MARK + '\n#include <string> // ' + MARK + '\n'
         'namespace nwii::runtime::render_queue { // ' + MARK + '\n'
         'bool Active();\n'
         'void Start(std::function<void()> make_current, std::function<void()> release, std::function<void()> bind_frame);\n'
         'void Stop();\n'
         'void Post(std::function<void()> task);\n'
         'void SetTitle(const std::string& title);\n'
         'bool TakeTitle(std::string& out);\n'
         '}\n')
XFB_OLD = ('      extern void GX_GetXfb(uint32_t*, unsigned*, unsigned*, unsigned*);\n'
           '      uint32_t xfb_addr; unsigned xw, xh, xstride;\n'
           '      GX_GetXfb(&xfb_addr, &xw, &xh, &xstride);\n'
           '      if (nwii::runtime::hw::g_vi_top_field_base != 0) {\n'
           '        xfb_addr = nwii::runtime::hw::g_vi_top_field_base;\n'
           '      }\n')
XFB_NEW = ('      auto native_read_xfb_latch = [](uint32_t& a, unsigned& w, unsigned& h, unsigned& s) { // ' + MARK + '\n'
           '        extern void GX_GetXfb(uint32_t*, unsigned*, unsigned*, unsigned*);\n'
           '        GX_GetXfb(&a, &w, &h, &s);\n'
           '        if (nwii::runtime::hw::g_vi_top_field_base != 0) a = nwii::runtime::hw::g_vi_top_field_base;\n'
           '      };\n'
           '      uint32_t xfb_addr = 0; unsigned xw = 0, xh = 0, xstride = 0;\n'
           '      if (!' + Q + 'Active()) native_read_xfb_latch(xfb_addr, xw, xh, xstride); // the GL thread reads it in queue mode\n')
START = ('  // ' + MARK + ': the GL thread takes the context. Off when headless or with\n'
         '  // NWII_NO_RENDER_QUEUE, NWII_XFB or NWII_SYNC_GPU set.\n'
         '  if (!headless && window && gl_ctx && !std::getenv("NWII_NO_RENDER_QUEUE") &&\n'
         '      !std::getenv("NWII_XFB") &&\n'
         '      !std::getenv("NWII_SYNC_GPU")) {\n'
         '    SDL_GL_MakeCurrent(window, nullptr);\n'
         '    ' + Q + 'Start(\n'
         '        [window, gl_ctx] { SDL_GL_MakeCurrent(window, gl_ctx); },\n'
         '        [window] { SDL_GL_MakeCurrent(window, nullptr); },\n'
         '        [&efb_fbo] { glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo); glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight()); });\n'
         '  }\n')


def main_cpp(t):
    first = '#include "runtime/gx/render_state.h" // NATIVE_MOM_RENDERER_STATE_SPLIT_V1\n'
    t = swap(t, first, first + DECLS, MAIN)
    t = swap(t, '        if (!headless) { glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo); glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight()); }\n',
             '        if (!headless && !' + Q + 'Active()) { glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo); glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight()); } // ' + MARK + '\n',
             MAIN, count=2)
    t = swap(t, '      nwii::runtime::input::InputManager::get().update();\n\n      if (!headless) {\n'
                '        glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo);\n        glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight());\n      }\n',
             '      nwii::runtime::input::InputManager::get().update();\n'
             '      { std::string title; if (window && ' + Q + 'TakeTitle(title)) SDL_SetWindowTitle(window, title.c_str()); } // ' + MARK + '\n\n'
             '      if (!headless && !' + Q + 'Active()) {\n'
             '        glBindFramebuffer(GL_FRAMEBUFFER, efb_fbo);\n        glViewport(0, 0, NativeEfbWidth(), NativeEfbHeight());\n      }\n', MAIN)
    t = swap(t, XFB_OLD, XFB_NEW, MAIN)
    t = swap(t, '      if (!headless) {\n        static auto t0 = std::chrono::steady_clock::now();\n',
             '      if (!headless && !' + Q + 'Active()) { // ' + MARK + ': the [STAT] EFB read-back needs the GL thread\n'
             '        static auto t0 = std::chrono::steady_clock::now();\n', MAIN)
    t = swap(t, '      if (!headless) {\n        native_perf_phase::Scope native_perf_phase_present(',
             '      if (!headless) {\n'
             '      auto frame_work = [&, xfb_addr, xw, xh, xstride, native_read_xfb_latch]() mutable { // ' + MARK + ': a GL-thread task in queue mode\n'
             '        if (' + Q + 'Active()) native_read_xfb_latch(xfb_addr, xw, xh, xstride);\n'
             '        native_perf_phase::Scope native_perf_phase_present(', MAIN)
    t = swap(t, '            pm_presents=pm_new=0;pm_copies0=copies;pm_t0=pm_now;}\n        }\n      }\n\n      native_trace::boundary(0);',
             '            pm_presents=pm_new=0;pm_copies0=copies;pm_t0=pm_now;}\n        }\n      };\n'
             '      if (' + Q + 'Active()) ' + Q + 'Post(frame_work); else frame_work(); // ' + MARK + '\n'
             '      }\n\n      native_trace::boundary(0);', MAIN)
    t = swap(t, '            if(window)SDL_SetWindowTitle(window,title);',
             '            if(window){if(' + Q + 'Active())' + Q + 'SetTitle(title);else SDL_SetWindowTitle(window,title);} // ' + MARK, MAIN)
    t = swap(t, '  std::thread cpu_thread(cpu_thread_func, ctx.get());\n',
             '  std::thread cpu_thread(cpu_thread_func, ctx.get());\n' + START, MAIN)
    t = swap(t, '      nwii::runtime::NativePersistentReleaseAll();\n',
             '      ' + Q + 'Stop(); SDL_GL_MakeCurrent(window, gl_ctx); // ' + MARK + ': join the GL thread, take the context back\n'
             '      nwii::runtime::NativePersistentReleaseAll();\n', MAIN)
    return t


EDITS = {GX: gx, MAIN: main_cpp}
CRLF, LF = '\r\n', '\n'


def read(path):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    return raw.replace(CRLF, LF), CRLF in raw


def write(path, text, crlf):
    path.write_bytes((text.replace(LF, CRLF) if crlf else text).encode('utf-8', errors='surrogateescape'))


def apply():
    plan = []
    for tree, base in TREES.items():
        for rel, fn in EDITS.items():
            path = base / rel
            text, crlf = read(path)
            if MARK in text:
                continue
            plan.append((tree, rel, path, path.read_bytes(), fn(text), crlf))
    manifest_path = BACKUP / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for tree, rel, path, before, new, crlf in plan:
        dst = BACKUP / tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(before)
        write(path, new, crlf)
        manifest[tree + '/' + rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    for base in TREES.values():
        for name, rel in INSTALLS:
            (base / rel).write_bytes((OVERLAYS / name).read_bytes())
    print('Render queue installed: %d file(s) changed.' % len(plan))


def restore():
    manifest_path = BACKUP / 'manifest.json'
    if not manifest_path.exists():
        print('Render queue: nothing to restore.')
        return
    manifest = json.loads(manifest_path.read_text())
    for key, digest in manifest.items():
        tree, rel = key.split('/', 1)
        if hashlib.sha256((TREES[tree] / rel).read_bytes()).hexdigest() != digest:
            raise Skip(key + ' changed since this stage wrote it; restore by hand from ' + str(BACKUP / tree / rel))
    for key in manifest:
        tree, rel = key.split('/', 1)
        shutil.copyfile(BACKUP / tree / rel, TREES[tree] / rel)
    for base in TREES.values():
        for _, rel in INSTALLS:
            if (base / rel).exists():
                (base / rel).unlink()
    shutil.rmtree(BACKUP)
    print('Render queue removed: %d file(s) restored.' % len(manifest))


if __name__ == '__main__':
    try:
        restore() if '--restore' in sys.argv else apply()
    except Skip as reason:
        print('fix_render_queue_mom: ' + str(reason))
        sys.exit(1)
