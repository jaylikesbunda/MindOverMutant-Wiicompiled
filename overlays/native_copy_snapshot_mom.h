#pragma once
#include <cstdint>

// ParseBP seeds raw BP registers, not the renderer's derived efb*/clear fields.
// Capture the stream-position register values before later writes replace them.
template<class Command>
inline void NativeCaptureCopyRegisters(Command& c, const uint32_t* bp) {
    c.native_copy_valid = true;
    c.native_copy_src_x = bp[0x49] & 0x3ff;
    c.native_copy_src_y = (bp[0x49] >> 10) & 0x3ff;
    c.native_copy_w = (bp[0x4a] & 0x3ff) + 1;
    c.native_copy_h = ((bp[0x4a] >> 10) & 0x3ff) + 1;
    c.native_copy_dest = (bp[0x4b] & 0xffffff) << 5;
    c.native_copy_stride = (bp[0x4d] & 0x3ff) << 5;
    c.native_copy_zmode = bp[0x40];
    c.native_copy_blend = bp[0x41];
    c.native_copy_clear_ar = bp[0x4f];
    c.native_copy_clear_gb = bp[0x50];
    c.native_copy_clear_depth = bp[0x51];
}
