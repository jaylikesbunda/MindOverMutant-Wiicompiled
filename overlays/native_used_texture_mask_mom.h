// Conservative used-texture-slot mask for MoM's GenerateTEVShader emission.
// Mirrors the CotT staged helper: primary TEV texMap slots plus indirect
// texture maps the TEV configuration can reach; invalid inputs fail closed to
// 0xff (all slots bound).  Used by the texture-bind loop to skip sampler work
// for slots no generated shader reads, and by the XF boundary check.
#pragma once
#include "runtime/gx_state.h"

namespace nwii::runtime::gx {

// Settled conservative mask:
//  - one bit per TEV stage's primary texMap (0xff decodes as map 0);
//  - indirect texture bits for each configured stage whose selected indirect
//    order is within the available count and has a matrix or bump scale;
//  - any invalid map (>=8) or count (>16) fails closed to all slots;
//  - a USED slot whose texStage base_addr is 0 keeps its bit (it may still
//    sample the unit's existing bound texture or unit 0 state).
inline unsigned NativeUsedTextureMask(const GXState& state) {
  unsigned mask = 0;
  unsigned count = state.numTevStages ? state.numTevStages : 1;
  if (count > 16) return 0xff;
  for (unsigned i = 0; i < count; ++i) {
    const unsigned map = state.tevStages[i].texMap;
    if (map != 0xff && map >= 8) return 0xff;
    mask |= 1u << (map == 0xff ? 0 : map);
    const uint32_t config = state.bp[0x10 + i];
    const unsigned selected = config & 3;
    if (selected < ((state.bp[0] >> 16) & 7) &&
        ((config >> 9) & 3 || (config >> 7) & 3))
      mask |= 1u << ((state.bp[0x27] >> (selected * 6)) & 7);
  }
  for (unsigned unit = 1; unit < 8; ++unit)
    if ((mask >> unit) & 1 && state.texStages[unit].base_addr == 0)
      mask |= 1u; // keep unit 0 as the fallback alias - a used slot with no
    // base_addr still samples whatever is bound there, so keep slot 0 bound
  return mask;
}

} // namespace nwii::runtime::gx
