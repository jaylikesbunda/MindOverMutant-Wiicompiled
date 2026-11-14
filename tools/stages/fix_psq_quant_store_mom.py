r"""Quantized and single-element psq_st without per-element MMU calls (MoM; CotT fix_quant_pair_stores.py / fix_psq_store_pair.py).

Marker: NATIVE_MOM_PSQ_QUANT_STORE_V1.

psq_store_slow (and its store_element lambda) were ~2.5% of the guest thread in profile prof7: every psq_st that is not a
float W==0 pair (single floats with W==1, and the u8/u16/s8/s16 GQR forms) wrote each element through a separate MMU call.
At the top of psq_store_slow these forms now take one native_mom_lane::Span proof for the whole access (refused when a
watch is set, exactly like the inline float-pair path) and store each element with the same conversion store_element
uses: (float)val for F32, (uint8_t)(val*scale), (uint16_t)(val*scale), (int8_t)(val*scale), (int16_t)(val*scale), with
the scale built from the GQR exactly as before, big-endian. W==1 stores only the first element. Anything else (GQR types
1-3, MMIO, watched or non-RAM addresses) runs the original code.

Edits include/runtime/cpu_context.h in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_PSQ_QUANT_STORE_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/include/runtime/cpu_context.h',
         ROOT / 'export/nWiiRuntime/include/runtime/cpu_context.h']
ANCHOR = '  __attribute__((noinline)) void psq_store_slow(uint32_t frS, uint32_t addr, uint32_t W, uint32_t I) {\n'
BLOCK = r'''    { // NATIVE_MOM_PSQ_QUANT_STORE_V1: single floats and u8/u16/s8/s16 forms in ordinary RAM, as store_element converts them
      const uint32_t q_gqr = gqr[I];
      const uint32_t q_type = q_gqr & 0x7u;
      if ((q_type == 0 && W != 0) || q_type >= 4) {
        const uint32_t elem = q_type == 0 ? 4u : ((q_type == 4 || q_type == 6) ? 1u : 2u);
        if (uint8_t* p = native_mom_lane::Span(mmu, addr, W ? elem : elem * 2u, g_watch_addr != 0)) {
          const uint32_t q_scale = (q_gqr >> 8) & 0x3Fu;
          const int q_signed = (q_scale & 32u) ? int(q_scale) - 64 : int(q_scale);
          const uint32_t q_bits = uint32_t(127 + q_signed) << 23;
          float scale; std::memcpy(&scale, &q_bits, sizeof(scale));
          auto put = [&](uint8_t* d, double val) {
            switch (q_type) {
            case 0: { const float f = (float)val; uint32_t u; std::memcpy(&u, &f, 4); u = __builtin_bswap32(u); std::memcpy(d, &u, 4); break; }
            case 4: d[0] = (uint8_t)(val * scale); break;
            case 5: { const uint16_t v = (uint16_t)(val * scale); d[0] = uint8_t(v >> 8); d[1] = uint8_t(v); break; }
            case 6: d[0] = (uint8_t)(int8_t)(val * scale); break;
            default: { const uint16_t v = (uint16_t)(int16_t)(val * scale); d[0] = uint8_t(v >> 8); d[1] = uint8_t(v); break; }
            }
          };
          put(p, fpr[frS]);
          if (W == 0) put(p + elem, ps1[frS]);
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
        if restore:
            n = t.replace(ANCHOR + BLOCK, ANCHOR, 1)
        elif MARK in t:
            continue
        elif t.count(ANCHOR) != 1:
            print('fix_psq_quant_store_mom: anchor not found once in %s' % path); sys.exit(1)
        else:
            n = t.replace(ANCHOR, ANCHOR + BLOCK, 1)
        if n != t:
            path.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    print('psq quantized store %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
