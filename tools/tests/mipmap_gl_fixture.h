#pragma once
#include "runtime/frame_trace.h"
#include <array>
#include <unordered_set>
#include <utility>

// The runner extracts the production method; only its cache storage is mocked.
class TextureCache {
public:
  struct Key { uint32_t width, height; };
  struct Entry { GLuint tex; size_t bytes; };
  std::vector<std::pair<Key, Entry>> cache;
  std::unordered_set<GLuint> mip_ready;
  size_t cache_bytes = 64;
  void ensure_mips(GLuint tex);
};
#include "mipmap_gl_body.inc"

static void mip_require(bool ok, const char* message) {
  if (!ok) {
    std::fprintf(stderr, "FAIL mipmap: %s\n", message);
    std::exit(1);
  }
}

static void test_mipmap_state() {
  const bool disabled = std::getenv("NWII_NO_MIPMAPS") != nullptr;
  for (unsigned unit : {3u, 0u, 7u}) {
    GLuint names[4];
    glGenTextures(4, names);
    const unsigned char colors[4][4] = {
        {255, 0, 0, 255}, {0, 255, 0, 255},
        {0, 0, 255, 255}, {64, 128, 192, 128}};
    for (unsigned n = 0; n < 4; ++n) {
      std::array<unsigned char, 64> pixels{};
      for (unsigned i = 0; i < 16; ++i)
        std::copy(colors[n], colors[n] + 4, pixels.begin() + i * 4);
      glBindTexture(GL_TEXTURE_2D, names[n]);
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 4, 4, 0,
                   GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    }
    constexpr unsigned units[] = {0, 3, 7};
    for (unsigned i = 0; i < 3; ++i) {
      glActiveTexture(GL_TEXTURE0 + units[i]);
      glBindTexture(GL_TEXTURE_2D, names[i]);
    }
    glActiveTexture(GL_TEXTURE0 + unit);
    TextureCache cache;
    cache.cache.push_back({{4, 4}, {names[3], 64}});
    cache.ensure_mips(names[3]);
    GLint active = 0;
    glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
    GLint bindings[3] = {};
    for (unsigned i = 0; i < 3; ++i) {
      glActiveTexture(GL_TEXTURE0 + units[i]);
      glGetIntegerv(GL_TEXTURE_BINDING_2D, &bindings[i]);
    }
    glActiveTexture(GL_TEXTURE0 + unit);
    GLint bound = 0, widths[3] = {};
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
    glBindTexture(GL_TEXTURE_2D, names[3]);
    for (unsigned level = 0; level < 3; ++level)
      glGetTexLevelParameteriv(GL_TEXTURE_2D, level, GL_TEXTURE_WIDTH,
                              &widths[level]);
    std::array<unsigned char, 4> pixel{};
    if (widths[2] == 1)
      glGetTexImage(GL_TEXTURE_2D, 2, GL_RGBA, GL_UNSIGNED_BYTE, pixel.data());
    glBindTexture(GL_TEXTURE_2D, GLuint(bound));
    std::printf("mip unit=%u disabled=%d active=%d binding0=%d expected0=%u "
                "levels=%d,%d,%d bytes=%zu\n",
                unit, disabled, active, bindings[0], names[0],
                widths[0], widths[1], widths[2], cache.cache_bytes);
    std::fflush(stdout);
    mip_require(active == GLint(GL_TEXTURE0 + unit), "active unit changed");
    for (unsigned i = 0; i < 3; ++i)
      mip_require(GLuint(bindings[i]) == names[i], "a unit binding changed");
    mip_require(widths[0] == 4, "base level missing");
    if (disabled) {
      mip_require(widths[1] == 0 && widths[2] == 0, "disabled generated levels");
      mip_require(cache.cache_bytes == 64 && cache.mip_ready.empty(),
                  "disabled changed accounting");
    } else {
      mip_require(widths[1] == 2 && widths[2] == 1, "mip chain incomplete");
      mip_require(std::equal(pixel.begin(), pixel.end(), colors[3]),
                  "generated mip pixel changed");
      mip_require(cache.cache_bytes == 84 && cache.cache[0].second.bytes == 84,
                  "mip bytes incorrect");
      mip_require(cache.mip_ready.count(names[3]) == 1, "mip not marked ready");
    }
    cache.ensure_mips(names[3]);
    cache.ensure_mips(0);
    mip_require(cache.cache_bytes == (disabled ? 64u : 84u),
                "repeat or null call changed accounting");
    glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
    mip_require(active == GLint(GL_TEXTURE0 + unit), "repeat changed unit");
    for (unsigned i = 0; i < 3; ++i) {
      glActiveTexture(GL_TEXTURE0 + units[i]);
      glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
      mip_require(GLuint(bound) == names[i], "repeat changed binding");
    }
    mip_require(glGetError() == GL_NO_ERROR, "GL error");
    glDeleteTextures(4, names);
    glActiveTexture(GL_TEXTURE0);
  }
  std::puts("PASS mipmap binding, completeness, accounting and repeat checks");
}
