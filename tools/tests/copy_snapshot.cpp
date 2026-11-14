#include "runtime/gx_command.h"
#include "runtime/native_copy_snapshot_mom.h"
#include <cassert>
#include <cstdio>

int main() {
    uint32_t bp[256] = {};
    bp[0x49] = 17 | (23 << 10);
    bp[0x4a] = 319 | (239 << 10);
    bp[0x4b] = 0x123456;
    bp[0x4d] = 20;
    bp[0x40] = 16;
    bp[0x41] = 24;
    bp[0x4f] = 0xaabb;
    bp[0x50] = 0xccdd;
    bp[0x51] = 0x123456;
    nwii::runtime::gx::GXCommand first, second;
    NativeCaptureCopyRegisters(first, bp);
    bp[0x49] = 7 | (11 << 10);
    bp[0x4a] = 63 | (31 << 10);
    bp[0x4b] = 0x56789;
    bp[0x40] = bp[0x41] = 0;
    bp[0x4f] = bp[0x50] = 0;
    bp[0x51] = 0xffffff;
    NativeCaptureCopyRegisters(second, bp);
    assert(first.native_copy_valid && first.native_copy_src_x == 17);
    assert(first.native_copy_src_y == 23 && first.native_copy_w == 320);
    assert(first.native_copy_h == 240 && first.native_copy_dest == (0x123456u << 5));
    assert(first.native_copy_stride == 640 && first.native_copy_zmode == 16);
    assert(first.native_copy_blend == 24 && first.native_copy_clear_ar == 0xaabb);
    assert(first.native_copy_clear_gb == 0xccdd && first.native_copy_clear_depth == 0x123456);
    assert(second.native_copy_src_x == 7 && second.native_copy_src_y == 11);
    assert(second.native_copy_w == 64 && second.native_copy_h == 32);
    assert(second.native_copy_clear_depth == 0xffffff && second.native_copy_blend == 0);
    bp[0x49] = bp[0x4a] = 0xffffff;
    NativeCaptureCopyRegisters(second, bp); // Display-list replay must re-freeze.
    assert(second.native_copy_src_x == 1023 && second.native_copy_w == 1024);
    assert(first.native_copy_src_x == 17 && first.native_copy_w == 320);
    std::puts("PASS copy register snapshots, lifetime and replay");
}
