r"""Renderer-owned GX state for Mind over Mutant: RendererGL applies BP/XF writes to RenderState(), not to the parser's g_state.

Marker: NATIVE_MOM_RENDERER_STATE_SPLIT_V1. Port of the Crash of the Titans stage fix_renderer_state_split.py (MoM keeps
std::vector<GXCommand> where CotT has its compact CommandStream, so the walk and anchors are MoM's own).

Before this stage the parser (FifoParser::Parse) and RendererGL::Render share one GXState. Parse writes only the raw
BP words (ParseBP's seeding) and the CP / VAT / array / default-matrix fields; everything derived from BP and XF writes
(TEV, texture stages, z/alpha modes, EFB copy, TLUT, XFB latch, XF words and matrix banks, projection) is applied by
Render in command order, and Render overwrites g_state.bp with its BP shadow at each batch start
(NativeRestoreBpSnapshot). This stage moves that derived state into RenderState(), a GXState owned by the renderer:

  * the parse-owned fields are captured in a ParseSnapshot right after Parse() and applied by RenderStateBegin() at the
    start of the batch that consumes them;
  * the parser keeps its own copy of what the renderer will resolve: the BP shadow / one-shot 0xFE mask (each drain
    starts from it, as it started from the renderer's restored words before) and the XF words the display-list cache
    key reads (g_parse_xf_committed), both advanced by walking the drain's commands in order;
  * GX_GetXfb / GX_GetClearColor, the FRAMESIG and F-key diagnostics and the texture cache read RenderState();
  * savestates still serialise one GXState: RenderStateExportToParse() before writing, ImportFromParse() after reading.

Single-threaded this changes no rendered pixel; it is the precondition for drawing on a separate GL thread (the render
queue), where Parse of drain N+1 runs while drain N is drawn.

Run after fix_savestate_mom.py and fix_early_pe_mom.py (it edits text they installed). --restore puts every touched
file back exactly as it was before this stage (kept under logs/stage-backups/), and refuses if a file changed since.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_RENDERER_STATE_SPLIT_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_renderer_state_split_mom'
OVERLAYS = ROOT / 'overlays'
INSTALLS = [('render_state_mom.h', 'include/runtime/gx/render_state.h'),
            ('render_state_mom.inc', 'src/hle/gx/render_state_mom.inc')]
FP, GX, RG, TC, MAIN = ('src/hle/gx/fifo_parser.cpp', 'src/hle/gx.cpp', 'src/hle/gx/renderer_gl.cpp',
                        'src/hle/texture_cache_gl.cpp', 'src/core/main.cpp')
INC = '#include "runtime/gx/render_state.h" // ' + MARK + '\n'


class Skip(Exception):
    pass


def swap(text, old, new, where):
    if text.count(old) != 1:
        raise Skip('%s: expected one occurrence of %r, found %d' % (where, old.strip()[:80], text.count(old)))
    return text.replace(old, new, 1)


WALK = r'''    // NATIVE_MOM_RENDERER_STATE_SPLIT_V1: XF words the renderer will have applied after the previous drain (display-list
    // cache key), and the BP words as RendererGL resolves them (shadow + one-shot 0xFE mask, NativeBpRegisterValue's rule).
    // The renderer used to overwrite g_state.bp with its shadow at each batch start; the parser now starts each drain
    // from this copy of that shadow instead.
    uint32_t g_parse_xf_pending[256] = {};
    uint32_t g_parse_xf_committed[256] = {};
    uint32_t g_parse_bp_shadow[256] = {};
    uint32_t g_parse_bp_mask = 0xffffff;
    void NativeRendererWalk(const std::vector<GXCommand>& commands) {
        for (const auto& c : commands) {
            if (c.type == GXCommandType::BPRegister) {
                const uint8_t reg = uint8_t(c.reg);
                const uint32_t resolved = ((g_parse_bp_shadow[reg] & ~g_parse_bp_mask) | (c.val & g_parse_bp_mask)) & 0xffffff;
                g_parse_bp_shadow[reg] = resolved;
                g_parse_bp_mask = reg == 0xfe ? resolved : 0xffffff;
                continue;
            }
            if (c.type != GXCommandType::XFRegister) continue;
            for (size_t i = 0; i < c.payload.size(); ++i) {
                const uint32_t a = (uint32_t)c.reg + (uint32_t)i;
                if (a >= 0x1000 && a < 0x1100) std::memcpy(&g_parse_xf_pending[a - 0x1000], &c.payload[i], 4);
            }
        }
    }
    void ApplyBPRegisterImpl(GXState& g_state, uint8_t reg, uint32_t val);
'''
WRAPPERS = r'''void ApplyBPRegister(uint8_t reg, uint32_t val) { ApplyBPRegisterImpl(g_state, reg, val); }
// NATIVE_MOM_RENDERER_STATE_SPLIT_V1: the renderer applies BP writes to RenderState(); the parser's g_state keeps raw BP words.
void ApplyBPRegisterTo(GXState& st, uint8_t reg, uint32_t val) { ApplyBPRegisterImpl(st, reg, val); }
const uint32_t* NativeParseXfCommitted() { return g_parse_xf_committed; }
void NativeTakeParseSnapshot(ParseSnapshot& out) {
    std::memcpy(out.cp, g_state.cp, sizeof(out.cp));
    std::memcpy(out.vat, g_state.vat, sizeof(out.vat));
    std::memcpy(out.arrayBase, g_state.arrayBase, sizeof(out.arrayBase));
    std::memcpy(out.arrayStride, g_state.arrayStride, sizeof(out.arrayStride));
    out.defPosMtxIdx = g_state.defPosMtxIdx;
    std::memcpy(out.defTexMtxIdx, g_state.defTexMtxIdx, sizeof(out.defTexMtxIdx));
}'''


def fp(t):
    t = swap(t, '#include "runtime/gx/fifo_parser.h"\n', '#include "runtime/gx/fifo_parser.h"\n' + INC, FP)
    t = swap(t, '    void ApplyBPRegisterImpl(uint8_t reg, uint32_t val);\n', WALK, FP)
    t = swap(t, '    void ApplyBPRegisterImpl(uint8_t reg, uint32_t val) {\n        g_state.bp[reg] = val;',
             '    void ApplyBPRegisterImpl(GXState& g_state, uint8_t reg, uint32_t val) {\n        g_state.bp[reg] = val;', FP)
    t = swap(t, 'void ApplyBPRegister(uint8_t reg, uint32_t val) { ApplyBPRegisterImpl(reg, val); }', WRAPPERS, FP)
    t = swap(t, '    h ^= g_state.xf[0x00E]; h *= 1099511628211ull;', '    h ^= g_parse_xf_committed[0x00E]; h *= 1099511628211ull;', FP)
    t = swap(t, '    h ^= g_state.xf[0x00F]; h *= 1099511628211ull;', '    h ^= g_parse_xf_committed[0x00F]; h *= 1099511628211ull;', FP)
    t = swap(t, '    size_t offset = 0;\n    ParseStream(fifo, offset, commands, 0);\n',
             '    std::memcpy(g_parse_xf_committed, g_parse_xf_pending, sizeof(g_parse_xf_committed)); // ' + MARK + '\n'
             '    std::memcpy(g_state.bp, g_parse_bp_shadow, sizeof(g_state.bp));\n'
             '    size_t offset = 0;\n    ParseStream(fifo, offset, commands, 0);\n'
             '    NativeRendererWalk(commands); // ' + MARK + '\n', FP)
    return t.rstrip('\n') + '\n\n#include "render_state_mom.inc" // ' + MARK + '\n'


def gx(t):
    t = INC + t
    t = swap(t, '    nwii::runtime::gx::FifoParser::Parse(s_parse_buf, commands);\n',
             '    nwii::runtime::gx::FifoParser::Parse(s_parse_buf, commands);\n'
             '    nwii::runtime::gx::NativeTakeParseSnapshot(nwii::runtime::gx::RenderBatchSnapshot()); // ' + MARK + '\n', GX)
    t = swap(t, '    s.pod(gx::g_state);\n',
             '    if (!s.loading) gx::RenderStateExportToParse(); // ' + MARK + '\n'
             '    s.pod(gx::g_state);\n'
             '    if (s.loading) gx::RenderStateImportFromParse(); // ' + MARK + '\n', GX)
    # GX_GetClearColor, GX_GetXfb and the drain diagnostics read the renderer's state
    return re.sub(r'(?<![\w:.])g_state\b', 'nwii::runtime::gx::RenderState()', t)


def rg(t):
    t = swap(t, '#include "runtime/gx/fifo_parser.h"\n', '#include "runtime/gx/fifo_parser.h"\n' + INC, RG)
    t = swap(t, '} else ApplyBPRegister((uint8_t)cmd.reg, bp_value);',
             '} else ApplyBPRegisterTo(RenderState(), (uint8_t)cmd.reg, bp_value); // ' + MARK, RG)
    t = swap(t, '    NativeRestoreBpSnapshot();\n',
             '    RenderStateBegin(RenderBatchSnapshot()); // ' + MARK + ': parse-owned view of this batch\n'
             '    NativeRestoreBpSnapshot();\n', RG)
    t = re.sub(r'gx::g_state\b', 'gx::RenderState()', t)
    return re.sub(r'(?<![\w:.])g_state\b', 'RenderState()', t)


def tc(t):
    t = INC + t
    t = swap(t, 'using gx::g_state;', 'using gx::RenderState; // ' + MARK, TC)
    t = re.sub(r'gx::g_state\b', 'gx::RenderState()', t)
    return re.sub(r'(?<![\w:.])g_state\b', 'RenderState()', t)


def main_cpp(t):
    t = INC + t
    return re.sub(r'nwii::runtime::gx::g_state\b', 'nwii::runtime::gx::RenderState()', t)


EDITS = {FP: fp, GX: gx, RG: rg, TC: tc, MAIN: main_cpp}
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
    print('Renderer state split installed: %d file(s) changed.' % len(plan))


def restore():
    manifest_path = BACKUP / 'manifest.json'
    if not manifest_path.exists():
        print('Renderer state split: nothing to restore.')
        return
    manifest = json.loads(manifest_path.read_text())
    for key, digest in manifest.items():
        tree, rel = key.split('/', 1)
        path = TREES[tree] / rel
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Skip(key + ' changed since this stage wrote it; restore by hand from ' + str(BACKUP / tree / rel))
    for key in manifest:
        tree, rel = key.split('/', 1)
        shutil.copyfile(BACKUP / tree / rel, TREES[tree] / rel)
    for base in TREES.values():
        for _, rel in INSTALLS:
            if (base / rel).exists():
                (base / rel).unlink()
    shutil.rmtree(BACKUP)
    print('Renderer state split removed: %d file(s) restored.' % len(manifest))


if __name__ == '__main__':
    try:
        restore() if '--restore' in sys.argv else apply()
    except Skip as reason:
        print('fix_renderer_state_split_mom: ' + str(reason))
        sys.exit(1)
