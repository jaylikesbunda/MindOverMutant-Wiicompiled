#pragma once
// NATIVE_MOM_RENDERER_STATE_SPLIT_V1: renderer-owned GXState (tools/stages/fix_renderer_state_split_mom.py), ported from
// the Crash of the Titans port's runtime-overlays/render_state.h.
// The parser owns the CP / VAT / array / default-matrix fields it writes while parsing (ParseSnapshot). Every field the
// renderer derives from BP or XF writes is applied in command order to RenderState() only, so parsing the next drain
// cannot change what the renderer of this drain observes - the precondition for drawing on a separate GL thread.
#include <cstdint>
#include "runtime/gx_state.h"

namespace nwii::runtime::gx {

// Parse-owned fields of one drain, captured right after FifoParser::Parse.
struct ParseSnapshot {
    uint32_t cp[256];
    VATSlot vat[8];
    uint32_t arrayBase[16];
    uint32_t arrayStride[16];
    uint8_t defPosMtxIdx;
    uint8_t defTexMtxIdx[8];
};

// Renderer-owned state. Written only by the renderer, in command order.
GXState& RenderState();
// Snapshot slot filled by ProcessGXFifo after Parse and consumed by RendererGL::Render.
ParseSnapshot& RenderBatchSnapshot();
// Copies the parse-owned view of snap into RenderState() before the batch is applied.
void RenderStateBegin(const ParseSnapshot& snap);
// Savestates still serialise one GXState: export/import keep it equal to the split state.
void RenderStateExportToParse();   // RenderState() -> g_state (parse-owned fields of g_state are kept)
void RenderStateImportFromParse(); // g_state -> RenderState()

// Parser side (defined in fifo_parser.cpp).
void NativeTakeParseSnapshot(ParseSnapshot& out);
const uint32_t* NativeParseXfCommitted();
void ApplyBPRegisterTo(GXState& st, uint8_t reg, uint32_t val);

} // namespace nwii::runtime::gx
