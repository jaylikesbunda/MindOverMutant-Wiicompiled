r"""Gather-pipe stores append straight to the CPU FIFO staging buffer (MoM port of CotT fix_inline_gather_mmio.py /
fix_gather_span; CotT's "GX matrix uploads write straight to the GPU FIFO" change was its largest single gain).

Marker: NATIVE_MOM_GATHER_INLINE_V1.

A guest store to the write-gather pipe (0xCC008000) went inline accessor -> MMU::native_write*_slow -> GX_WGPIPE_Write* ->
native_ordinary_fifo_fast -> native_wgp_bulk, i.e. three out-of-line calls per vertex component (GX_WGPIPE_* plus
native_wgp_bulk were ~6.5% of the guest thread in profile prof7; the skinning loops write every vertex this way).

At the gather line of each slow writer (after the unchanged watch and sentinel checks), native_gather_inline_mom now does
what that chain does on its ordinary path, inline: if the ordinary-FIFO predicate holds (the same expression as
native_ordinary_fifo_fast: launch flag, no WPAR redirect, g_mmu set, FIFO base/end test) and the staged bytes stay below
the 4096-byte publish boundary with the buffer already reserved, it appends the big-endian bytes to g_cpu_fifo and returns.
Otherwise it returns false and the original GX_WGPIPE_Write* call runs exactly as before (publish boundaries, redirects,
probes, diagnostics). Bytes, order and publish points are unchanged.

gx.cpp exports two C-linkage mirrors so the header can test them: nwii_mom_gather_enabled (= native_ordinary_fifo_fast_enabled)
and nwii_mom_gather_wpar (= g_wpar_redirect, updated where it is assigned).

Edits include/runtime/cpu_context.h and src/hle/gx.cpp in vendor and export. Idempotent; --restore is exact
(backups under logs/stage-backups/fix_gather_inline_mom).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_GATHER_INLINE_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_gather_inline_mom'

HELPER = r'''// NATIVE_MOM_GATHER_INLINE_V1: the ordinary path of GX_WGPIPE_Write* -> native_wgp_bulk, inline (fix_gather_inline_mom.py).
extern "C" { extern bool nwii_mom_gather_enabled; extern bool nwii_mom_gather_wpar; }
extern std::vector<uint8_t> g_cpu_fifo;
namespace hw { extern uint32_t pi_fifo_base, pi_fifo_end, g_cp_fifo_base; }
struct MMU;
extern MMU *g_mmu;
__attribute__((always_inline)) inline bool native_gather_inline_mom(uint64_t value, unsigned bytes) {
  if (!nwii_mom_gather_enabled || nwii_mom_gather_wpar || !g_mmu) return false;
  const uint32_t base = hw::pi_fifo_base, end = hw::pi_fifo_end;
  if (!((base == 0 && end < 0x04000000u) || (base != 0 && (base & 0x03ffffffu) == (hw::g_cp_fifo_base & 0x03ffffffu)))) return false;
  if (g_cpu_fifo.size() + bytes >= 4096 || g_cpu_fifo.capacity() < 4104) return false;
  const uint64_t be = __builtin_bswap64(value << (8 * (8 - bytes)));
  const uint8_t *b = reinterpret_cast<const uint8_t *>(&be);
  g_cpu_fifo.insert(g_cpu_fifo.end(), b, b + bytes);
  return true;
}
__attribute__((always_inline)) inline uint64_t native_gather_bits_mom(float v) { uint32_t u; std::memcpy(&u, &v, 4); return u; }
__attribute__((always_inline)) inline uint64_t native_gather_bits_mom(double v) { uint64_t u; std::memcpy(&u, &v, 8); return u; }

'''
HDR_ANCHOR = '// Strict Dolphin-accurate MMU\nstruct MMU {\n'
GATHER = re.compile(r'GX_WGPIPE_Write(8|16|32|F32|F64)\(value\); return; \}')
ARGS = {'8': 'value, 1', '16': 'value, 2', '32': 'value, 4', 'F32': 'native_gather_bits_mom(value), 4',
        'F64': 'native_gather_bits_mom(value), 8'}

GX_DEFS_ANCHOR = 'static bool g_wpar_redirect = false;\n'
GX_DEFS = (GX_DEFS_ANCHOR +
           'extern "C" { bool nwii_mom_gather_enabled = false; bool nwii_mom_gather_wpar = false; } // ' + MARK + '\n')
GX_WPAR_OLD = '    g_wpar_redirect = (a & 0x3FFFFFFFu) != 0x0C008000u && (a & 0x3FFFFFFFu) != 0u;\n'
GX_WPAR_NEW = GX_WPAR_OLD + '    nwii_mom_gather_wpar = g_wpar_redirect; // ' + MARK + '\n'
GX_FLAG_OLD = ('static const bool native_ordinary_fifo_fast_enabled = !std::getenv("NWII_GX_REDIRECT_PROBE") &&\n'
               '    !native_wgp_diagnostics_flag && !std::getenv("NWII_NO_GATHER_ORDINARY_FAST");\n')
GX_FLAG_NEW = (GX_FLAG_OLD +
               'static const bool native_gather_inline_mom_init = (nwii_mom_gather_enabled = native_ordinary_fifo_fast_enabled &&\n'
               '    !std::getenv("NWII_NO_GATHER_INLINE"), true); // ' + MARK + '\n')


def header_edit(t):
    if t.count(HDR_ANCHOR) != 1:
        raise SystemExit('fix_gather_inline_mom: MMU anchor not found once')
    n, k = GATHER.subn(lambda m: 'if (!native_gather_inline_mom(%s)) GX_WGPIPE_Write%s(value); return; }' % (ARGS[m[1]], m[1]), t)
    if k != 5:
        raise SystemExit('fix_gather_inline_mom: expected 5 gather lines, found %d' % k)
    return n.replace(HDR_ANCHOR, HELPER + HDR_ANCHOR, 1)


def gx_edit(t):
    for old, new in ((GX_DEFS_ANCHOR, GX_DEFS), (GX_WPAR_OLD, GX_WPAR_NEW), (GX_FLAG_OLD, GX_FLAG_NEW)):
        if t.count(old) != 1:
            raise SystemExit('fix_gather_inline_mom: gx.cpp anchor not found once: %r' % old[:50])
        t = t.replace(old, new, 1)
    return t


EDITS = {'include/runtime/cpu_context.h': header_edit, 'src/hle/gx.cpp': gx_edit}


def main():
    manifest_path = BACKUP / 'manifest.json'
    if '--restore' in sys.argv:
        if not manifest_path.exists():
            print('gather inline: nothing to restore.'); return
        manifest = json.loads(manifest_path.read_text())
        for key, digest in manifest.items():
            if hashlib.sha256((ROOT / key).read_bytes()).hexdigest() != digest:
                raise SystemExit('fix_gather_inline_mom: %s changed since this stage wrote it' % key)
        for key in manifest:
            shutil.copyfile(BACKUP / key, ROOT / key)
        shutil.rmtree(BACKUP)
        print('gather inline removed: %d file(s) restored.' % len(manifest)); return
    plan = []
    for base in TREES.values():
        for rel, fn in EDITS.items():
            path = base / rel
            raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
            if MARK in raw:
                continue
            crlf = '\r\n' in raw
            new = fn(raw.replace('\r\n', '\n'))
            plan.append((path, raw, new.replace('\n', '\r\n') if crlf else new))
    if not plan:
        print('gather inline already installed.'); return
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for path, raw, new in plan:
        key = str(path.relative_to(ROOT)).replace(chr(92), '/')
        dst = BACKUP / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw.encode('utf-8', errors='surrogateescape'))
        path.write_bytes(new.encode('utf-8', errors='surrogateescape'))
        manifest[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print('gather inline installed: %d file(s) changed.' % len(plan))


if __name__ == '__main__':
    main()
