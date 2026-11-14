r"""Inline quantized and single-element psq_l (MoM counterpart of CotT fix_guest_psq_pairs.py / fix_indexed_psq.py).

Marker: NATIVE_MOM_PSQ_QUANT_INLINE_V1. Run after fix_psq_inline_mom.py.

fix_psq_inline_mom.py inlined only the float GQR W==0 pair. Every other psq_l (a single float with W==1, and the u8/u16/s8/s16
quantized forms the vertex and animation code use) went to psq_load_slow, which reads each element through a separate
MMU call; psq_load_slow was 1.6% of the guest thread in profile prof5. The inline path now also takes those forms when the
bytes lie in ordinary RAM (one native_mom_lane::Span proof for the whole access), computing each element exactly as
psq_load_slow's load_element does: the integer is converted to float, multiplied by the float scale built from the GQR
(same exponent construction), and widened to double; W==1 sets ps1 to 1.0. GQR types 1-3 (undefined) and anything outside
ordinary RAM keep the slow path.

Edits include/runtime/cpu_context.h in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_PSQ_QUANT_INLINE_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/include/runtime/cpu_context.h',
         ROOT / 'export/nWiiRuntime/include/runtime/cpu_context.h']
ANCHOR = ('        fpr[frD] = a; ps1[frD] = b;\n'
          '        return;\n'
          '      }\n'
          '    }\n'
          '    psq_load_slow(frD, addr, W, I);\n')
BLOCK = r'''    { // NATIVE_MOM_PSQ_QUANT_INLINE_V1: single floats and u8/u16/s8/s16 forms in ordinary RAM, as psq_load_slow computes them
      const uint32_t gqr_val = gqr[I];
      const uint32_t ld_type = (gqr_val >> 16) & 0x7u;
      if (ld_type == 0 || ld_type >= 4) {
        const uint32_t elem = ld_type == 0 ? 4u : ((ld_type == 4 || ld_type == 6) ? 1u : 2u);
        if (const uint8_t* p = native_mom_lane::Span(mmu, addr, W ? elem : elem * 2u, false)) {
          const uint32_t ld_scale = (gqr_val >> 24) & 0x3Fu;
          const int signed_scale = (ld_scale & 32u) ? int(ld_scale) - 64 : int(ld_scale);
          const uint32_t scale_bits = uint32_t(127 - signed_scale) << 23;
          float scale; std::memcpy(&scale, &scale_bits, sizeof(scale));
          auto element = [&](const uint8_t* q) -> double {
            switch (ld_type) {
            case 0: { uint32_t v; std::memcpy(&v, q, 4); v = __builtin_bswap32(v); float f; std::memcpy(&f, &v, 4); return f; }
            case 4: return q[0] * scale;
            case 5: return uint16_t((q[0] << 8) | q[1]) * scale;
            case 6: return int8_t(q[0]) * scale;
            default: return int16_t(uint16_t((q[0] << 8) | q[1])) * scale;
            }
          };
          fpr[frD] = element(p);
          ps1[frD] = W ? 1.0 : element(p + elem);
          return;
        }
      }
    }
'''


def main():
    restore = '--restore' in sys.argv
    for path in FILES:
        raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
        crlf = '\r\n' in raw
        t = raw.replace('\r\n', '\n')
        tail = '    psq_load_slow(frD, addr, W, I);\n'
        if restore:
            n = t.replace(BLOCK + tail, tail, 1)
        elif MARK in t:
            continue
        elif t.count(ANCHOR) != 1:
            print('fix_psq_quant_inline_mom: anchor not found once in %s (run fix_psq_inline_mom.py first)' % path); sys.exit(1)
        else:
            n = t.replace(ANCHOR, ANCHOR[:-len(tail)] + BLOCK + tail, 1)
        if n != t:
            path.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('psq quantized inline %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
