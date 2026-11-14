r"""Early PE completion for Mind over Mutant: signal GX tokens/finish after decode, not after GL (on; NWII_NO_EARLY_PE=1 opts out).

Marker: NATIVE_MOM_EARLY_PE_V1. Mechanism ported from the Crash of the Titans stage fix_early_gpu_signal.py (also kept
verbatim in tools/stages/ for reference; its anchors no longer match MoM's GXCommand).

Why: in gameplay (save state outside Coco's house, left stick held) the guest thread is busy ~66% of the time, and only
~240 ms/s of it is translated game code; the rest is waiting. The game synchronises with the GPU (draw-sync tokens,
GXDrawDone), and the renderer raises those only after RendererGL::Render has GL-submitted every earlier draw of the
drain, so the guest waits out the whole render phase at each sync and the two threads cannot overlap.

What: after parse and decode, every PE token / finish of the drain is raised in command order with exactly the value
the renderer would produce (same BP shadow / one-shot 0xFE mask rule as NativeBpRegisterValue), the command is
flagged, and the renderer and the headless path skip flagged commands. TLUT loads (BP 0x65) are copied from guest RAM
at signal time, because the guest may rewrite a palette once released.

Measured: same save, left stick held, 120 FPS cap, two alternating rounds: ~24 FPS without, ~31 with (+29%); a still
frame of the same scene is identical (mean pixel difference 0.48/255).

Known hazard: textures are still read from guest RAM while rendering, so a texture the guest
rewrites between the signal and its draw can show the new contents (MoM's earlier note: "texture-RAM lifetime"). The
EFB-copy half of the old problem (alternating finished / half-rendered frames) is fixed by NATIVE_MOM_GX_OVERLAP_V1's
per-command copy snapshots. Closing the texture half needs the CotT texture CPU/GL split.

NWII_NO_EARLY_PE=1 or NWII_SYNC_GPU=1 keeps the old order.

Edits the vendored runtime and mirrors into export/nWiiRuntime. Anchors checked before writing; idempotent;
--restore removes every edit byte for byte.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_EARLY_PE_V1'
ROOT = Path(__file__).resolve().parents[2]
ROOTS = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', ROOT / 'export/nWiiRuntime']
CRLF, LF = '\r\n', '\n'


class Skip(Exception):
    pass


HELPER = r'''#include "runtime/config.h" // NATIVE_MOM_EARLY_PE_V1
// NATIVE_MOM_EARLY_PE_V1: see tools/stages/fix_early_pe_mom.py. Full BP shadow with the renderer's resolution rule.
static uint32_t s_early_bp_shadow[256] = {};
static uint32_t s_early_bp_mask = 0xffffff;
static void NativeSignalGpuCompletionEarly(std::vector<nwii::runtime::gx::GXCommand>& frame) {
    for (auto& c : frame) {
        if (c.type != nwii::runtime::gx::GXCommandType::BPRegister) continue;
        const uint8_t reg = uint8_t(c.reg);
        const uint32_t resolved = ((s_early_bp_shadow[reg] & ~s_early_bp_mask) | (c.val & s_early_bp_mask)) & 0xffffff;
        s_early_bp_shadow[reg] = resolved;
        s_early_bp_mask = reg == 0xfe ? resolved : 0xffffff;
        if (reg == 0x65) { // TLUT load: copy the source now, exactly as ApplyBPRegister(0x65) would
            uint32_t src = (s_early_bp_shadow[0x64] & 0xFFFFFF) << 5;
            if (nwii::runtime::Config::get().platform == nwii::runtime::Platform::GameCube) src &= 0x01FFFFFF;
            const uint32_t dst = (resolved & 0x3FF) << 9, bytes = ((resolved >> 10) & 0x7FF) * 32;
            if (nwii::runtime::g_mmu && dst + bytes <= sizeof(nwii::runtime::gx::g_state.tlutMem)) {
                c.native_tlut.resize(bytes);
                if (const uint8_t* p = nwii::runtime::g_mmu->get_ptr(src)) { if (bytes) std::memcpy(c.native_tlut.data(), p, bytes); }
                else for (uint32_t i = 0; i < bytes; ++i) c.native_tlut[i] = nwii::runtime::g_mmu->read8(src + i);
                c.native_tlut_snapshot = true;
            }
        }
        if (reg == 0x45 && (resolved & 0xffu) == 2u) {
            nwii::runtime::hw::pe_signal_finish();
            c.native_signalled = true;
        } else if (reg == 0x47 || reg == 0x48) {
            nwii::runtime::hw::pe_signal_token(resolved & 0xffffu, reg == 0x48);
            c.native_signalled = true;
        }
    }
}
// NATIVE_MOM_EARLY_PE_V1 END
'''

EDITS = {
    'include/runtime/gx_command.h': [
        ('    std::vector<float> payload;\n    // NATIVE_MOM_GX_OVERLAP_V1',
         '    std::vector<float> payload;\n'
         '    bool native_signalled = false; // NATIVE_MOM_EARLY_PE_V1: PE token/finish already delivered to the guest\n'
         '    bool native_tlut_snapshot = false; // NATIVE_MOM_EARLY_PE_V1: native_tlut holds the BP 0x65 source read before the signal\n'
         '    std::vector<uint8_t> native_tlut;\n'
         '    // NATIVE_MOM_GX_OVERLAP_V1'),
    ],
    'src/hle/gx.cpp': [
        ('std::vector<uint8_t> g_hw_fifo;\n', 'std::vector<uint8_t> g_hw_fifo;\n' + HELPER),
        ('    if (g_window) {\n        if (!g_renderer) {\n            g_renderer = nwii::runtime::gx::IRenderer::Create();\n            g_renderer->Initialize(nullptr);\n        }\n        if (g_renderer) {\n            native_perf_phase::Scope',
         '    { // NATIVE_MOM_EARLY_PE_V1: the guest sees every token/finish of this drain now; GL submission continues without it\n'
         '        static const bool early_signal = std::getenv("NWII_NO_EARLY_PE") == nullptr && std::getenv("NWII_SYNC_GPU") == nullptr;\n'
         '        if (early_signal) NativeSignalGpuCompletionEarly(frame);\n'
         '    }\n'
         '    if (g_window) {\n        if (!g_renderer) {\n            g_renderer = nwii::runtime::gx::IRenderer::Create();\n            g_renderer->Initialize(nullptr);\n        }\n        if (g_renderer) {\n            native_perf_phase::Scope'),
        ('        for(const auto &c:frame)if(c.type==nwii::runtime::gx::GXCommandType::BPRegister) {\n',
         '        for(const auto &c:frame)if(c.type==nwii::runtime::gx::GXCommandType::BPRegister && !c.native_signalled) { // NATIVE_MOM_EARLY_PE_V1\n'),
    ],
    'src/hle/gx/renderer_gl.cpp': [
        ('        NativeGpuCompletion((uint8_t)cmd.reg,bp_value,true);\n',
         '        if (!cmd.native_signalled) NativeGpuCompletion((uint8_t)cmd.reg,bp_value,true); // NATIVE_MOM_EARLY_PE_V1: already delivered after decode\n'),
        ('        ApplyBPRegister((uint8_t)cmd.reg, bp_value);\n',
         '        if (cmd.native_tlut_snapshot && cmd.reg == 0x65) { // NATIVE_MOM_EARLY_PE_V1: palette bytes read before the early signal\n'
         '          const uint32_t dst = (bp_value & 0x3FF) << 9;\n'
         '          if (!cmd.native_tlut.empty()) std::memcpy(&g_state.tlutMem[dst], cmd.native_tlut.data(), cmd.native_tlut.size());\n'
         '        } else ApplyBPRegister((uint8_t)cmd.reg, bp_value);\n'),
    ],
}


def read(path):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    return raw.replace(CRLF, LF), CRLF in raw


def write(path, text, crlf):
    path.write_bytes((text.replace(LF, CRLF) if crlf else text).encode('utf-8', errors='surrogateescape'))


def transform(rel, text, restore):
    if restore:
        if MARK not in text:
            return text
        for old, new in reversed(EDITS[rel]):
            if text.count(new) != 1:
                raise Skip('%s: applied text not found once: %r' % (rel, new.strip()[:70]))
            text = text.replace(new, old, 1)
        if MARK in text:
            raise Skip(rel + ': marker left after restore')
        return text
    if MARK in text:
        return text
    for old, new in EDITS[rel]:
        if text.count(old) != 1:
            raise Skip('%s: expected one occurrence of %r, found %d' % (rel, old.strip()[:70], text.count(old)))
        text = text.replace(old, new, 1)
    return text


def main(argv):
    restore = '--restore' in argv
    roots = ROOTS
    if '--root' in argv:
        roots = [Path(argv[argv.index('--root') + 1]) / 'nWiiRuntime']
    plan = []
    for base in roots:
        if not base.is_dir():
            continue
        for rel in EDITS:
            path = base / rel
            if not path.is_file():
                raise Skip('missing ' + str(path))
            text, crlf = read(path)
            new = transform(rel, text, restore)
            if new != text:
                plan.append((path, new, crlf))
    for path, text, crlf in plan:
        write(path, text, crlf)
    print('Early PE %s: %d file(s) changed.' % ('removed' if restore else 'installed', len(plan)))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Skip as reason:
        print('fix_early_pe_mom: nothing written: ' + str(reason))
        sys.exit(1)
