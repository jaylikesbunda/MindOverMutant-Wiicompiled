"""Carry CotT's EFB texture freshness checks into MoM without its cache rewrites.

An EFB image is usable only while its destination RAM remains unchanged.
Snapshot the actual tiled copy footprint (CotT fix_efb_copy_guard.py), compare
every byte (fix_exact_texture_cache.py), and retain the full physical address
so MEM1 and MEM2 cannot share an entry. No guest bytes are modified.
NWII_LEGACY_EFB_CACHE bypasses freshness checks for a controlled comparison.
NWII_EFB_CACHE_PROBE prints bounded observations of hit/fallback decisions.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
path = port / "vendor/NWiiRecomp/nWiiRuntime/src/hle/texture_cache_gl.cpp"
text = path.read_text()
if "NATIVE_MOM_EFB_FRESHNESS_V1" not in text:
    anchor = "using gx::g_state;"
    helper = r'''

// NATIVE_MOM_EFB_FRESHNESS_V1: CotT's copy footprint and exact RAM validation.
struct NativeEfbGuest { std::vector<uint8_t> bytes; };
static std::unordered_map<uint32_t, NativeEfbGuest> s_efb_guest;
static uint32_t NativeEfbCopyBytes(uint32_t copy, uint32_t w, uint32_t h, uint32_t stride) {
    if (copy & 0x200u) { w = (w + 1) / 2; h = (h + 1) / 2; }
    const uint32_t tpf = (copy >> 3) & 15u, fmt = tpf / 2 + (tpf & 1u) * 8;
    uint32_t tw = 4, th = 4, bpp = 16;
    switch (fmt) {
        case 0: tw = 8; th = 8; bpp = 4; break;
        case 1: case 2: case 7: case 8: case 9: case 10: tw = 8; th = 4; bpp = 8; break;
        case 6: bpp = 32; break;
    }
    const uint32_t rows = (h + th - 1) / th;
    const uint32_t row_bytes = (w + tw - 1) / tw * tw * th * bpp / 8;
    return rows && row_bytes ? (rows - 1) * std::max(stride, row_bytes) + row_bytes : 0;
}
static bool NativeEfbSpan(CPUContext& ctx, uint32_t address, size_t size) {
    const uint32_t p = address & 0x3fffffffu;
    return (p < 0x01800000u && size <= 0x01800000u - p) ||
           (p >= 0x10000000u && p < 0x14000000u && size <= 0x14000000u - p);
}
static bool NativeEfbGuestUnchanged(CPUContext& ctx, uint32_t address) {
    static const bool legacy = std::getenv("NWII_LEGACY_EFB_CACHE") != nullptr;
    if (legacy) return true;
    auto it = s_efb_guest.find(address & 0x3fffffffu);
    return it != s_efb_guest.end() && !it->second.bytes.empty() &&
           NativeEfbSpan(ctx, address, it->second.bytes.size()) &&
           std::memcmp(ctx.mmu.get_ptr(address), it->second.bytes.data(), it->second.bytes.size()) == 0;
}
'''
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + helper, 1)
    text = text.replace("#include <cstring>", "#include <cstring>\n#include <algorithm>\n#include <vector>\nnamespace nwii::runtime { extern CPUContext* g_ctx_ptr; }", 1)
    old = "    efb_textures[addr & 0x01FFFFFF] = tex;"
    new = """    efb_textures[addr & 0x3fffffff] = tex;
    auto& snapshot = s_efb_guest[addr & 0x3fffffff].bytes;
    snapshot.clear();
    const uint32_t size = NativeEfbCopyBytes(g_state.bp[0x52], g_state.efbW, g_state.efbH, g_state.efbCopyStride);
    if (g_ctx_ptr && size && size <= (4u << 20) && NativeEfbSpan(*g_ctx_ptr, addr, size)) {
        const auto* bytes = g_ctx_ptr->mmu.get_ptr(addr);
        snapshot.assign(bytes, bytes + size);
    }"""
    assert text.count(old) == 1
    text = text.replace(old, new, 1)
    text = text.replace("efb_textures.find(addr & 0x01FFFFFF)", "efb_textures.find(addr & 0x3fffffff)", 1)
    old = "    if (GLuint efb = find_efb_texture(stage.base_addr))\n        return efb;"
    new = """    if (GLuint efb = find_efb_texture(stage.base_addr)) {
        const bool unchanged = NativeEfbGuestUnchanged(ctx, stage.base_addr);
        static const bool probe = std::getenv("NWII_EFB_CACHE_PROBE") != nullptr;
        static unsigned shown = 0;
        if (probe && shown < 100) {
            ++shown;
            std::cout << "[EFB cache] address=" << std::hex << stage.base_addr << std::dec
                      << " size=" << stage.width << "x" << stage.height << " format=" << unsigned(stage.format)
                      << " unchanged=" << unchanged << std::endl;
        }
        if (unchanged) return efb;
    }"""
    assert text.count(old) == 1
    text = text.replace(old, new, 1)
    path.write_text(text)
print("EFB textures validate destination RAM and distinguish MEM1 from MEM2.")
