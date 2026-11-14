r"""Save states for Mind over Mutant: F1 writes a snapshot, NWII_LOAD_STATE=<file> starts the game from one.

Marker: NATIVE_SAVESTATE_V1 (the same format and orchestration as the Crash of the Titans port, whose
native-port/tools/fix_savestate.py this follows; both games run the same runtime family).

A snapshot is taken at the generated run_game loop head, where callbacks and thread switches have unwound every host
frame, so the CPU context and guest memory are complete. Each device module serializes its own file-scope state through
native_ss::Stream (one function saves and loads) and re-arms the scheduler events its registers imply; ios.cpp
orchestrates (overlays/native_savestate_ios.inc). Saving waits for a quiet point (no callback, IPC, disc or ARAM transfer
in flight, no DSP mail scheduled, GX FIFO drained). Loading happens at the first dispatcher step after a normal boot. The
GX register file is restored and replayed to the renderer as ordinary BP/CP/XF commands, so the parser and renderer
shadows are rebuilt the normal way.

What differs from the CotT stage, all because MoM's runtime differs:
  * the AX protocol runs synchronously on the guest thread (no AX worker), so the DSP is quiet when no mail is scheduled
    and no ARAM DMA is counting;
  * MoM's GX front end has no early-PE BP shadow and no FIFO-ok flag, and requests a drain with native_gx_request_drain();
  * ProcessGXFifo's static s_parse_buf moves to file scope so a snapshot can carry a partial command;
  * F1 and the thumbnail sit next to MoM's own dev HUD (NATIVE_MOM_DEV_HUD_V1);
  * the emulated remote keeps one connection flag rather than one per channel.

Edits the vendored runtime (vendor/NWiiRecomp/nWiiRuntime), mirrors every file it touched into export/nWiiRuntime, and
adds the request check to export/main_output.cpp (regeneration rewrites that file: re-run this stage afterwards).
Run it after tools/apply_perf_stages.py, which restores and replays its own stages over gx.cpp, ios.cpp and main.cpp.

All anchors are checked before anything is written. Idempotent; --restore removes every edit byte for byte.

    python tools/stages/fix_savestate_mom.py [--restore] [--root <dir holding nWiiRuntime/ and main_output.cpp>]
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_SAVESTATE_V1'
ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / 'vendor/NWiiRecomp/nWiiRuntime'
EXPORT = ROOT / 'export/nWiiRuntime'
GENERATED = ROOT / 'export/main_output.cpp'
OVERLAYS = ROOT / 'overlays'
INC = '#include "runtime/native_savestate.h" // ' + MARK + '\n'
BEGIN = '// ' + MARK + ' BEGIN (tools/stages/fix_savestate_mom.py)\n'
END = '// ' + MARK + ' END\n'
CRLF, LF = '\r\n', '\n'
INSTALLS = [('native_savestate.h', 'include/runtime/native_savestate.h'),
            ('native_savestate_ios.inc', 'src/hle/native_savestate_ios.inc')]


class Skip(Exception):
    pass


HOOKS = {
    'src/hw/hw_ai.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_ai(native_ss::Stream& s) {
  s.tag("AI01"); s.pod(ai_control); s.pod(ai_volume); s.pod(ai_count); s.pod(ai_target); s.pod(ai_fraction);
  if (s.loading) { ai_last = nwii::runtime::get_os_time(); ai_irq(); }   // ai_last is host time: restart the sample clock now
}
}
''',
    'src/hw/hw_vi.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_vi(native_ss::Stream& s) { s.tag("VI01"); s.pod(vi_regs); s.pod(g_vi_top_field_base); s.pod(g_vi_btm_field_base); }
}
''',
    'src/hw/hw_pi.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_pi(native_ss::Stream& s) {
  s.tag("PI01"); s.atom(pi_intsr); s.atom(pi_intmr); s.pod(pi_fifo_base); s.pod(pi_fifo_end); s.pod(pi_fifo_wptr);
}
}
''',
    'src/hw/hw_ipc.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_ipc(native_ss::Stream& s) {
  s.tag("IPC1"); s.pod(ipc_arm_msg); s.pod(ipc_arm_ctrl); s.pod(ipc_ppc_ctrl); s.pod(ipc_ppc_msg); s.deq(ipc_replies);
}
bool native_ss_ipc_quiet() { return ipc_replies.empty(); }
}
''',
    'src/hw/hw_pe.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_pe(native_ss::Stream& s) {
  std::lock_guard<std::mutex> lock(g_pe_transaction_mutex);
  s.tag("PE01"); s.atom(g_pe_ctrl); s.atom(g_pe_token_pending); s.atom(g_pe_finish_pending); s.atom(g_pe_sr); s.atom(g_pe_token);
}
}
''',
    'src/hw/hw_cp.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_cp(native_ss::Stream& s) {
  s.tag("CP01"); s.pod(g_cp_cr); s.pod(g_cp_sr); s.pod(g_cp_clear); s.pod(g_cp_fifo_base); s.pod(g_cp_fifo_end); s.pod(g_cp_fifo_hi);
  s.pod(g_cp_fifo_lo); s.pod(g_cp_fifo_rw_dist); s.pod(g_cp_fifo_wp); s.pod(g_cp_fifo_rp);
}
}
''',
    'src/hw/hw_di.cpp': r'''
namespace nwii::runtime::hw {
void native_ss_di(native_ss::Stream& s) {
  s.tag("DI01"); s.pod(di_cmd); s.pod(di_mar); s.pod(di_len); s.pod(di_cr); s.pod(di_imm); s.pod(di_sr); s.pod(di_cvr);
  s.pod(di_stream_playing); s.pod(g_di_interrupt_delay);
}
// A transfer in flight completes through a scheduler event, which a snapshot cannot carry.
bool native_ss_di_quiet() { return (di_cr & 1u) == 0 && g_di_interrupt_delay == 0; }
}
''',
    'src/hw/hw_dsp.cpp': r'''
namespace nwii::runtime::hw {
// MoM runs AX command lists synchronously on the guest thread: the DSP is quiet when no reply mail is scheduled and no
// ARAM DMA is counting.
bool native_ss_dsp_quiet() { return !native_mail_scheduled && ar_cnt == 0; }
void native_ss_dsp(native_ss::Stream& s) {
  s.tag("DSP1");
  s.pod(dsp_mbox_cpu_hi); s.pod(dsp_mbox_cpu_lo); s.pod(dsp_mbox_dsp_hi); s.pod(dsp_mbox_dsp_lo);
  s.pod(ar_mmaddr); s.pod(ar_araddr); s.pod(ar_cnt); s.pod(dsp_control); s.pod(ar_size); s.pod(ar_mode); s.pod(ar_refresh);
  s.vec(native_dsp_mails); s.pod(adma_addr); s.pod(adma_control);
  s.tag("AXP1"); native_ax().Serialize(s);
  if (!s.loading) return;
  { std::lock_guard<std::mutex> lock(adma_mutex); adma_ring.clear(); }
  if (s_adma_event_id) { EventScheduler::get().cancel(s_adma_event_id); s_adma_event_id = 0; }
  if (adma_control & 0x8000)
    s_adma_event_id = EventScheduler::get().schedule_recurring(
        std::max<uint64_t>(1, g_ctx_ptr->tb_freq * (uint64_t(adma_control & 0x7fff) * 8) / 32000), adma_event_cb);
  native_mail_scheduled = false;
  native_dsp_schedule_mail();
  dsp_update_pi();
}
}
''',
    'src/hle/devices/fs_device.cpp': r'''
namespace nwii::runtime::devices { // NAND files are reopened at their saved position
void native_ss_fs(native_ss::Stream& s) {
  s.tag("NFS1");
  for (auto& h : g_handles) {
    uint8_t open = h.open ? 1 : 0; s.pod(open);
    std::string host = h.host.string(); s.str(host); s.pod(h.pos);
    if (!s.loading) continue;
    h.stream.reset(); h.open = false; h.host = host;
    if (!open) continue;
    auto st = std::make_unique<std::fstream>(h.host, std::ios::in | std::ios::out | std::ios::binary);
    if (!st->is_open()) st = std::make_unique<std::fstream>(h.host, std::ios::in | std::ios::binary);
    if (!st->is_open()) throw std::runtime_error("save state: cannot reopen NAND file " + host);
    h.stream = std::move(st); h.open = true;
  }
}
}
''',
}

# (path, [(old, new), ...]) replacements; restore swaps them back.
PROTOCOL_OLD = 'private:\n  enum class State{Rom,Commands,Address,Next,Upload};'
PROTOCOL_NEW = '''  // NATIVE_SAVESTATE_V1: the protocol state; the microcode object is rebuilt from its identity, as Load() builds it.
  template<class Stream> void Serialize(Stream& s){
    uint8_t has_ucode=ax?1:0; s.pod(has_ucode);
    s.pod(crc); s.pod(command_frames); s.pod(state); s.pod(rom_command); s.pod(code_address); s.pod(code_size);
    s.pod(command_size); s.pod(upload); s.pod(upload_index); s.vec(dsp.mail.pending);
    u16 compressor=(!s.loading&&ax)?ax->CompressorPos():0; s.pod(compressor);
    if(!s.loading)return;
    if(has_ucode){
      ax=std::make_unique<DSP::HLE::AXWiiUCode>(&dsp,crc);
      ax->LoadCoefficients(coefficients.data(),coefficients.size());ax->CompressorPos()=compressor;
    }else ax.reset();
  }
''' + PROTOCOL_OLD
BASE_OLD = 'class AXUCode {\nprotected:\n'
BASE_NEW = 'class AXUCode {\npublic:\n  u16& CompressorPos(){return m_compressor_pos;} // NATIVE_SAVESTATE_V1\nprotected:\n'
KERNEL_INC_OLD = '#include <memory>\n'
KERNEL_INC_NEW = '#include <memory>\n#include <stdexcept> // NATIVE_SAVESTATE_V1\n#include <string> // NATIVE_SAVESTATE_V1\n'
KERNEL_OLD = '    int32_t ioctlv(CPUContext& ctx, const IpcRequest& req);\n'
KERNEL_NEW = KERNEL_OLD + '''    // NATIVE_SAVESTATE_V1: the descriptor table for save states; devices are matched by name.
    template <class Stream> void native_ss_fds(Stream& s) {
        s.tag("IOSK");
        uint32_t n = uint32_t(m_fds.size()); s.pod(n);
        if (s.loading) { if (n > 4096) throw std::runtime_error("save state fd table too large"); m_fds.assign(n, FdEntry{}); }
        for (auto& e : m_fds) {
            std::string name = e.device ? e.device->get_name() : ""; s.str(name);
            s.pod(e.internal_fd); s.pod(e.in_use);
            if (!s.loading) continue;
            e.device = nullptr;
            for (const auto& d : m_devices) if (name == d->get_name()) e.device = d.get();
            if (!name.empty() && !e.device) throw std::runtime_error("save state names an IOS device this runtime lacks: " + name);
        }
    }
'''
GX_HEAD_OLD = 'void ProcessGXFifo() {\n'
GX_HEAD_NEW = ('static std::vector<uint8_t> s_parse_buf; // NATIVE_SAVESTATE_V1: file scope so a snapshot can carry a partial command\n'
               'static std::atomic<int> s_native_ss_gx_busy{0}; // NATIVE_SAVESTATE_V1\n'
               'void ProcessGXFifo() {\n'
               '    s_native_ss_gx_busy.fetch_add(1, std::memory_order_acq_rel); // NATIVE_SAVESTATE_V1\n'
               '    struct NativeSsBusy { ~NativeSsBusy() { s_native_ss_gx_busy.fetch_sub(1, std::memory_order_acq_rel); } } native_ss_busy;\n')
GX_LOCAL_OLD = '    static std::vector<uint8_t> s_parse_buf;\n'
GX_LOCAL_NEW = '    // NATIVE_SAVESTATE_V1: s_parse_buf is at file scope\n'
GX_TAIL = r'''
namespace nwii::runtime {
// Guest thread: publish everything staged and wait until the host has consumed it and is idle.
bool native_ss_gx_sync(int timeout_ms) {
    const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    for (;;) {
        native_gx_publish();
        bool empty;
        { nwii::runtime::NativeFifoLockGuard lock(g_fifo_mutex); empty = g_hw_fifo.empty(); }
        if (empty && s_native_ss_gx_busy.load(std::memory_order_acquire) == 0) {
            // ProcessGXFifo may have swapped the FIFO out just before the busy check; confirm once more.
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            { nwii::runtime::NativeFifoLockGuard lock(g_fifo_mutex); empty = g_hw_fifo.empty(); }
            if (empty && s_native_ss_gx_busy.load(std::memory_order_acquire) == 0) return true;
        }
        if (std::chrono::steady_clock::now() >= until) return false;
        nwii::runtime::native_gx_request_drain();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

static void native_ss_put32(std::vector<uint8_t>& o, uint32_t v) { o.push_back(uint8_t(v >> 24)); o.push_back(uint8_t(v >> 16)); o.push_back(uint8_t(v >> 8)); o.push_back(uint8_t(v)); }
static void native_ss_xf_floats(std::vector<uint8_t>& o, uint32_t address, const float* values, uint32_t count) {
    o.push_back(0x10); native_ss_put32(o, ((count - 1) << 16) | address);
    for (uint32_t i = 0; i < count; ++i) { uint32_t w; std::memcpy(&w, &values[i], 4); native_ss_put32(o, w); }
}
// The register file as GX commands. BP trigger registers (draw done, tokens, EFB copy, TLUT/TMEM loads, cache invalidate,
// the one-shot mask) are left out; everything else is configuration and idempotent.
static std::vector<uint8_t> native_ss_replay_stream(const gx::GXState& g) {
    std::vector<uint8_t> o;
    for (uint32_t r : {0x30u, 0x40u, 0x50u, 0x60u}) { o.push_back(0x08); o.push_back(uint8_t(r)); native_ss_put32(o, g.cp[r]); }
    for (uint32_t r = 0x70; r < 0xC0; ++r) { if ((r & 0xF) >= 8 && r < 0xA0) continue; o.push_back(0x08); o.push_back(uint8_t(r)); native_ss_put32(o, g.cp[r]); }
    {   // XF registers 0x1000-0x1057
        o.push_back(0x10); native_ss_put32(o, ((0x58u - 1) << 16) | 0x1000u);
        for (uint32_t i = 0; i < 0x58; ++i) native_ss_put32(o, g.xf[i]);
    }
    native_ss_xf_floats(o, 0x000, g.posMatrices, 256);
    native_ss_xf_floats(o, 0x400, g.normalMatrices, 96);
    native_ss_xf_floats(o, 0x500, g.postMatrices, 256);
    native_ss_xf_floats(o, 0x600, &g.lights[0][0], 128);
    for (uint32_t r = 0; r < 0x100; ++r) {
        switch (r) { case 0x45: case 0x47: case 0x48: case 0x52: case 0x63: case 0x64: case 0x65: case 0x66: case 0x67: case 0xFE: continue; }
        o.push_back(0x61); native_ss_put32(o, (r << 24) | (g.bp[r] & 0xFFFFFF));
    }
    return o;
}

void native_ss_gx(native_ss::Stream& s) {
    s.tag("GX01");
    s.pod(gx::g_state);
    s.vec(s_parse_buf);
    s.pod(g_wpar_cur); s.pod(g_wpar_fill); s.pod(g_wpar_redirect);
    if (!s.loading) return;
    std::vector<uint8_t> stream = native_ss_replay_stream(gx::g_state);
    stream.insert(stream.end(), s_parse_buf.begin(), s_parse_buf.end());
    s_parse_buf.clear();
    {
        nwii::runtime::NativeFifoLockGuard lock(g_fifo_mutex);
        stream.insert(stream.end(), g_hw_fifo.begin(), g_hw_fifo.end());
        g_hw_fifo.swap(stream);
        g_gx_pending_bytes.store(g_hw_fifo.size(), std::memory_order_relaxed);
    }
    g_cpu_fifo.clear();
    native_gather_invalidate();
    nwii::runtime::native_gx_request_drain();
}
}
'''
MAIN_KEY_OLD = '          if (e.type == SDL_KEYDOWN && !e.key.repeat && native_dev_hud::key(e.key.keysym.sym)) {'
MAIN_KEY_NEW = '''          if (e.type == SDL_KEYDOWN && !e.key.repeat && e.key.keysym.sym == SDLK_F1) { // NATIVE_SAVESTATE_V1: save a state
            int expected = 0;
            if (native_ss::request.compare_exchange_strong(expected, 1)) {
              { std::lock_guard<std::mutex> lock(native_ss::thumb.mutex); native_ss::thumb.want = true; native_ss::thumb.ready = false; native_ss::thumb.bmp.clear(); }
              std::cout << "[SaveState] requested" << std::endl;
            }
          }
          else ''' + MAIN_KEY_OLD.lstrip()
MAIN_PRESENT_OLD = '        if (!present_offscreen) native_dev_hud::draw(draw_w, draw_h); // NATIVE_MOM_DEV_HUD_V1\n'
MAIN_PRESENT_NEW = '''        { // NATIVE_SAVESTATE_V1: thumbnail of the presented frame (before the HUD), 256 pixels wide, 24-bit BMP
          bool want = false;
          { std::lock_guard<std::mutex> lock(native_ss::thumb.mutex); want = native_ss::thumb.want; }
          if (want && draw_w > 0 && draw_h > 0) {
            std::vector<uint8_t> px(size_t(draw_w) * draw_h * 3);
            GLint read_fb = 0; glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &read_fb);
            glBindFramebuffer(GL_READ_FRAMEBUFFER, 0); glReadBuffer(GL_BACK);
            glPixelStorei(GL_PACK_ALIGNMENT, 1);
            glReadPixels(0, 0, draw_w, draw_h, GL_BGR, GL_UNSIGNED_BYTE, px.data());
            glBindFramebuffer(GL_READ_FRAMEBUFFER, read_fb);
            const int tw = 256, th = std::max(1, int(int64_t(draw_h) * tw / draw_w)), row = (tw * 3 + 3) & ~3;
            std::vector<uint8_t> bmp(54 + size_t(row) * th);
            auto put = [&](size_t at, uint32_t v, int n) { for (int i = 0; i < n; ++i) bmp[at + i] = uint8_t(v >> (8 * i)); };
            bmp[0] = 'B'; bmp[1] = 'M'; put(2, uint32_t(bmp.size()), 4); put(10, 54, 4); put(14, 40, 4); put(18, tw, 4); put(22, th, 4);
            put(26, 1, 2); put(28, 24, 2); put(34, uint32_t(row) * th, 4);
            for (int y = 0; y < th; ++y)   // box filter; GL rows are bottom-up like BMP
              for (int x = 0; x < tw; ++x) {
                const int x0 = x * draw_w / tw, x1 = std::max(x0 + 1, (x + 1) * draw_w / tw);
                const int y0 = y * draw_h / th, y1 = std::max(y0 + 1, (y + 1) * draw_h / th);
                uint32_t sum[3] = {0, 0, 0}, n = 0;
                for (int yy = y0; yy < y1; yy += 2) for (int xx = x0; xx < x1; xx += 2) {
                  const uint8_t* q = &px[(size_t(yy) * draw_w + xx) * 3]; sum[0] += q[0]; sum[1] += q[1]; sum[2] += q[2]; ++n;
                }
                for (int c = 0; c < 3; ++c) bmp[54 + size_t(y) * row + x * 3 + c] = uint8_t(sum[c] / std::max(1u, n));
              }
            std::lock_guard<std::mutex> lock(native_ss::thumb.mutex);
            native_ss::thumb.bmp.swap(bmp); native_ss::thumb.ready = true; native_ss::thumb.want = false;
          }
        }
''' + MAIN_PRESENT_OLD
MAIN_THREAD_OLD = '  std::thread cpu_thread(cpu_thread_func, ctx.get());\n'
MAIN_THREAD_NEW = ('  if (const char* load_state = std::getenv("NWII_LOAD_STATE"); load_state && *load_state) native_ss::request.store(2); // '
                   + MARK + '\n' + MAIN_THREAD_OLD)
RUN_GAME = 'extern "C" void run_game(nwii::runtime::CPUContext& ctx) {'
LOOP_HEAD = '    while (ctx.is_running) {\n'
HOOK_LINE = ('      if (__builtin_expect(native_ss::request.load(std::memory_order_relaxed) != 0, 0)) nwii::runtime::native_ss_service(ctx); // '
             + MARK + '\n')
GEN_PRELUDE = INC + 'namespace nwii::runtime { struct CPUContext; void native_ss_service(CPUContext&); } // ' + MARK + '\n'

# Every runtime edit: relative path -> (prefix with INC?, [(old, new)], suffix block or None)
EDITS = {rel: (True, [], body) for rel, body in HOOKS.items()}
EDITS.update({
    'include/runtime/native_ax/protocol.h': (False, [(PROTOCOL_OLD, PROTOCOL_NEW)], None),
    'include/runtime/native_ax/base.h': (False, [(BASE_OLD, BASE_NEW)], None),
    'include/runtime/ios_kernel.h': (False, [(KERNEL_INC_OLD, KERNEL_INC_NEW), (KERNEL_OLD, KERNEL_NEW)], None),
    'src/hle/gx.cpp': (True, [(GX_HEAD_OLD, GX_HEAD_NEW), (GX_LOCAL_OLD, GX_LOCAL_NEW)], GX_TAIL),
    'src/hle/ios.cpp': (True, [], '#include "native_savestate_ios.inc"\n'),
    'src/core/main.cpp': (True, [(MAIN_KEY_OLD, MAIN_KEY_NEW), (MAIN_PRESENT_OLD, MAIN_PRESENT_NEW), (MAIN_THREAD_OLD, MAIN_THREAD_NEW)], None),
})
# Names the hooks rely on; checked before anything is written so a moved symbol fails loudly instead of at compile time.
NEEDS = {
    'src/hle/gx.cpp': ['std::vector<uint8_t> g_hw_fifo;', 'std::vector<uint8_t> g_cpu_fifo;', 'static uint32_t g_wpar_cur',
                       'static unsigned g_wpar_fill', 'static bool g_wpar_redirect', 'native_gather_invalidate()', 'native_gx_request_drain()'],
    'src/hle/ios.cpp': ['static std::vector<PendingCallback> g_pending_callbacks;', 'static uint32_t ipc_pool_next', 'int g_ipc_interrupt_delay',
                        'static NativeWpadState& native_wpad_state()', 'static uint32_t ios_guest_heap_ptr', 'bool g_ipc_reply_pending'],
    'src/hw/hw_dsp.cpp': ['static bool native_mail_scheduled', 'static uint64_t s_adma_event_id', 'static void adma_event_cb('],
    'src/hle/devices/fs_device.cpp': ['Handle g_handles[FD_MAX];', 'uint32_t pos = 0;'],
}


def read(path):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    return raw.replace(CRLF, LF), CRLF in raw


def write(path, text, crlf):
    path.write_bytes((text.replace(LF, CRLF) if crlf else text).encode('utf-8', errors='surrogateescape'))


def once(text, old, where):
    if text.count(old) != 1:
        raise Skip('%s: expected one occurrence of %r, found %d' % (where, old.strip()[:70], text.count(old)))


def apply_text(rel, text):
    prefix, pairs, suffix = EDITS[rel]
    if MARK in text:
        return text                                   # installed already
    for name in NEEDS.get(rel, []):
        if name not in text:
            raise Skip('%s: %s not found' % (rel, name))
    for old, new in pairs:
        once(text, old, rel)
        text = text.replace(old, new, 1)
    if prefix:
        text = INC + text
    if suffix:
        text = text.rstrip(LF) + LF + LF + BEGIN + suffix.lstrip(LF) + END
    return text


def restore_text(rel, text):
    prefix, pairs, suffix = EDITS[rel]
    if MARK not in text:
        return text
    if suffix:
        a = text.rfind(LF + LF + BEGIN)
        if a < 0 or not text.endswith(END):
            raise Skip(rel + ': appended block not found intact')
        text = text[:a] + LF
    if prefix:
        if not text.startswith(INC):
            raise Skip(rel + ': include line not at the top')
        text = text[len(INC):]
    for old, new in reversed(pairs):
        once(text, new, rel)
        text = text.replace(new, old, 1)
    if MARK in text:
        raise Skip(rel + ': marker left after restore')
    return text


def generated_apply(text):
    if MARK in text:
        return text
    a = text.find(RUN_GAME)
    if a < 0 or text.count(RUN_GAME) != 1:
        raise Skip('main_output.cpp: run_game not found once')
    b = text.find(LOOP_HEAD, a)
    if b < 0:
        raise Skip('main_output.cpp: run_game loop head not found')
    b += len(LOOP_HEAD)
    return GEN_PRELUDE + text[:b] + HOOK_LINE + text[b:]


def generated_restore(text):
    if MARK not in text:
        return text
    if not text.startswith(GEN_PRELUDE) or text.count(HOOK_LINE) != 1:
        raise Skip('main_output.cpp: hook not found intact')
    return text[len(GEN_PRELUDE):].replace(HOOK_LINE, '', 1)


def run(restore, roots, generated):
    plan = []                                         # (path, new text, crlf); computed before anything is written
    for base in roots:
        for rel in EDITS:
            path = base / rel
            if not path.is_file():
                raise Skip('missing ' + str(path))
            text, crlf = read(path)
            new = restore_text(rel, text) if restore else apply_text(rel, text)
            if new != text:
                plan.append((path, new, crlf))
    if generated.is_file():
        text, crlf = read(generated)
        new = generated_restore(text) if restore else generated_apply(text)
        if new != text:
            plan.append((generated, new, crlf))
    for path, text, crlf in plan:
        write(path, text, crlf)
    for name, rel in INSTALLS:
        for base in roots:
            dst = base / rel
            if restore:
                if dst.exists():
                    dst.unlink()
            elif not dst.exists() or dst.read_bytes() != (OVERLAYS / name).read_bytes():
                shutil.copyfile(OVERLAYS / name, dst)
    return len(plan)


def main(argv):
    restore = '--restore' in argv
    roots, generated = [VENDOR, EXPORT], GENERATED
    if '--root' in argv:                              # test hook: a copied tree
        r = Path(argv[argv.index('--root') + 1])
        roots, generated = [r / 'nWiiRuntime'], r / 'main_output.cpp'
    roots = [b for b in roots if b.is_dir()]
    if not roots:
        raise Skip('no runtime tree found')
    for name, _ in INSTALLS:
        if not (OVERLAYS / name).is_file():
            raise Skip('missing overlay ' + name)
    n = run(restore, roots, generated)
    print('Save states %s: %d file(s) changed.' % ('removed' if restore else 'installed', n))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Skip as reason:
        print('fix_savestate_mom: nothing written: ' + str(reason))
        sys.exit(1)
