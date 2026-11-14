// NATIVE_MOM_RENDERER_STATE_SPLIT_V1: definitions for runtime/gx/render_state.h, included at the end of fifo_parser.cpp
// (MoM's export build lists its runtime sources itself, so no new translation unit is added).
namespace nwii::runtime::gx {
namespace {
void NativeParseOwnedOut(ParseSnapshot& s, const GXState& g) {
    std::memcpy(s.cp, g.cp, sizeof(s.cp));
    std::memcpy(s.vat, g.vat, sizeof(s.vat));
    std::memcpy(s.arrayBase, g.arrayBase, sizeof(s.arrayBase));
    std::memcpy(s.arrayStride, g.arrayStride, sizeof(s.arrayStride));
    s.defPosMtxIdx = g.defPosMtxIdx;
    std::memcpy(s.defTexMtxIdx, g.defTexMtxIdx, sizeof(s.defTexMtxIdx));
}
void NativeParseOwnedIn(GXState& g, const ParseSnapshot& s) {
    std::memcpy(g.cp, s.cp, sizeof(g.cp));
    std::memcpy(g.vat, s.vat, sizeof(g.vat));
    std::memcpy(g.arrayBase, s.arrayBase, sizeof(g.arrayBase));
    std::memcpy(g.arrayStride, s.arrayStride, sizeof(g.arrayStride));
    g.defPosMtxIdx = s.defPosMtxIdx;
    std::memcpy(g.defTexMtxIdx, s.defTexMtxIdx, sizeof(g.defTexMtxIdx));
}
} // namespace

GXState& RenderState() {
    static GXState* state = new GXState();
    return *state;
}
ParseSnapshot& RenderBatchSnapshot() {
    static ParseSnapshot snap{};
    return snap;
}
void RenderStateBegin(const ParseSnapshot& snap) { NativeParseOwnedIn(RenderState(), snap); }
void RenderStateExportToParse() {
    ParseSnapshot keep;
    NativeParseOwnedOut(keep, g_state);
    std::memcpy(&g_state, &RenderState(), sizeof(GXState));
    NativeParseOwnedIn(g_state, keep);
}
void RenderStateImportFromParse() { std::memcpy(&RenderState(), &g_state, sizeof(GXState)); }
} // namespace nwii::runtime::gx
