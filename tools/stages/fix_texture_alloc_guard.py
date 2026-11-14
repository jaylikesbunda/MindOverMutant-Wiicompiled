"""Keep a bogus guest texture size from destroying the host heap.

The texture decoder sized its image buffer with `width * height * sizeof(Color)`
in `int` arithmetic and never checked the result:

    img.data = malloc(width * height * sizeof(Color));
    for (int i = 0; i < width*height; i++) ((Color*)img.data)[i] = color;

A guest texture size of 0x10000 x 0x10000 overflows that product to zero, so
malloc(0) returned a tiny block and the fill loop then wrote 4 GiB through it.
That is the host-side std::bad_alloc / heap corruption seen when the game
streamed a new area (a load zone) and the runtime was handed a texture whose
dimensions it could not make sense of.

This stage validates the size before allocating, keeps a bounded image for
an invalid one, and checks the allocation. It also reports an uncaught host
exception with the guest's program counter instead of letting std::terminate
print a bare `std::bad_alloc`.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'


def edit(path, old, new, marker):
    text = path.read_text()
    if marker in text:
        return False
    assert text.count(old) == 1, (str(path), old[:70])
    path.write_text(text.replace(old, new, 1))
    return True


# ---- the allocation itself -------------------------------------------------
header = runtime / 'include/runtime/texture_cache_gl.h'
edit(header,
     """inline Image GenImageColor(int width, int height, Color color) {
    Image img;
    img.width = width;
    img.height = height;
    img.data = malloc(width * height * sizeof(Color));
    for (int i=0; i<width*height; i++) ((Color*)img.data)[i] = color;
    return img;
}""",
     """// NATIVE_TEXTURE_ALLOC_GUARD_V1: size the image in 64-bit and refuse a
// request the host cannot serve. A guest texture size is 16-bit per axis, so
// 0x10000 x 0x10000 is representable in the guest and overflows the old
// `int` product; that produced a zero-sized allocation that the fill loop
// then wrote 4 GiB through.
inline Image GenImageColor(int width, int height, Color color) {
    Image img;
    img.width = width > 0 ? width : 1;
    img.height = height > 0 ? height : 1;
    const uint64_t pixels = (uint64_t)img.width * (uint64_t)img.height;
    const uint64_t bytes = pixels * sizeof(Color);
    const uint64_t limit = 64ull << 20;   // 64 MiB of texels
    if (bytes > limit) {
        std::cout << "[TextureCache] refusing " << img.width << "x" << img.height
                  << " image (" << bytes << " bytes)" << std::endl;
        img.width = 1; img.height = 1;
        img.data = malloc(sizeof(Color));
        if (img.data) ((Color*)img.data)[0] = color;
        return img;
    }
    img.data = malloc((size_t)bytes);
    if (!img.data) {
        img.width = 1; img.height = 1;
        img.data = malloc(sizeof(Color));
        if (img.data) ((Color*)img.data)[0] = color;
        return img;
    }
    for (uint64_t i = 0; i < pixels; ++i) ((Color*)img.data)[i] = color;
    return img;
}""",
     'NATIVE_TEXTURE_ALLOC_GUARD_V1')

# ---- the decode entry point ------------------------------------------------
source = runtime / 'src/hle/texture_cache_gl.cpp'
edit(source,
     """Image TextureCache::decode_texture(CPUContext& ctx, const gx::TexStage& stage) {
    uint32_t width = stage.width, height = stage.height;
    Image img = GenImageColor(width, height, BLANK);""",
     """Image TextureCache::decode_texture(CPUContext& ctx, const gx::TexStage& stage) {
    uint32_t width = stage.width, height = stage.height;
    // NATIVE_TEXTURE_ALLOC_GUARD_V1: a guest size the host cannot decode is
    // reported once and replaced by a 4x4 marker instead of a huge allocation.
    if (width == 0 || height == 0 || width > 8192 || height > 8192 ||
        (uint64_t)width * height > (4096ull * 4096ull)) {
        std::cout << "[TextureCache] invalid texture size " << width << "x" << height
                  << " addr=0x" << std::hex << stage.base_addr << std::dec
                  << " format=" << (int)stage.format << std::endl;
        width = 4; height = 4;
    }
    Image img = GenImageColor(width, height, BLANK);""",
     'NATIVE_TEXTURE_ALLOC_GUARD_V1')

# ---- report an uncaught host exception with the guest's PC -----------------
main = runtime / 'src/core/main.cpp'
edit(main,
     """  run_game(*ctx);
""",
     """  // NATIVE_GUEST_EXCEPTION_REPORT_V1: a host-side allocation failure used
  // to end the process with a bare std::bad_alloc and no clue which guest
  // code was running. Report the guest's program counter instead.
  try {
    run_game(*ctx);
  } catch (const std::exception& e) {
    std::cout << "[Thread] host exception while running the guest: " << e.what()
              << " pc=0x" << std::hex << ctx->pc << " lr=0x" << ctx->lr << std::dec
              << std::endl;
    ctx->is_running = false;
  } catch (...) {
    std::cout << "[Thread] unknown host exception while running the guest: pc=0x"
              << std::hex << ctx->pc << " lr=0x" << ctx->lr << std::dec << std::endl;
    ctx->is_running = false;
  }
""",
     'NATIVE_GUEST_EXCEPTION_REPORT_V1')
print('Texture allocation guarded; guest exception reporting added.')
