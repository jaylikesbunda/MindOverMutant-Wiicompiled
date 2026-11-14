// Used-texture-mask soundness: exact cases plus a property check that no
// generated shader references a sampler outside the mask.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <regex>
#include <string>
#include "runtime/native_used_texture_mask_mom.h"
#include "runtime/gx/tev_shader_gen.h"

using namespace nwii::runtime::gx;

static int fails = 0;
#define CK(cond, name) do { if (!(cond)) { ++fails; printf("FAIL %s\n", name); } } while (0)

static GXState mk(unsigned count = 1, bool all_addr = true) {
  GXState s{}; // zero-initialise; texStage addresses default 0
  s.numTevStages = count;
  for (unsigned i = 0; i < 8; ++i) s.texStages[i].base_addr = all_addr ? 0x8000000 + i * 0x1000 : 0;
  for (unsigned i = 0; i < 16; ++i) s.tevStages[i].texMap = 0xff;
  return s;
}

int main() {
  // settled cases
  {
    GXState s = mk(); s.tevStages[0].texMap = 3;
    CK(NativeUsedTextureMask(s) == 0x08, "only map3 -> 0x08");
  }
  {
    GXState s = mk(); // texMap 0xff -> map 0, no indirect
    CK(NativeUsedTextureMask(s) == 0x01, "disabled FF no indirect -> 0x01");
  }
  {
    GXState s = mk(2); s.tevStages[0].texMap = 3; s.tevStages[1].texMap = 5;
    CK(NativeUsedTextureMask(s) == 0x28, "maps 3+5 -> 0x28");
  }
  {
    GXState s = mk(8);
    for (int i = 0; i < 8; ++i) s.tevStages[i].texMap = i;
    CK(NativeUsedTextureMask(s) == 0xff, "8 stages maps 0..7 -> 0xff");
  }
  {
    // primary map2 + indirect: selected=1 < available=2, matrix+order bits set -> map6
    GXState s = mk(); s.tevStages[0].texMap = 2;
    s.bp[0] = (2u << 16);            // 2 indirect textures available
    s.bp[0x10] = 1 | (1u << 9) | (1u << 7); // selected=1, matrix+bump set
    s.bp[0x27] = (6u << 6);          // order[1].map = 6
    CK(NativeUsedTextureMask(s) == 0x44, "primary2 + indirect map6 -> 0x44");
  }
  {
    GXState s = mk(); s.tevStages[0].texMap = 0; // selected=1 but only 1 available
    s.bp[0] = (1u << 16); s.bp[0x10] = 1 | (1u << 9); s.bp[0x27] = (6u << 6);
    CK(NativeUsedTextureMask(s) == 0x01, "selected>=available -> primary only");
  }
  {
    GXState s = mk(); s.tevStages[0].texMap = 3; s.texStages[3].base_addr = 0;
    CK(NativeUsedTextureMask(s) == 0x09, "used slot3 addr0 -> 0x09");
  }
  {
    GXState s = mk(); s.tevStages[0].texMap = 2;
    s.bp[0] = 2u << 16; s.bp[0x10] = 1 | (1u << 7);
    s.bp[0x27] = 6u << 6;
    CK(NativeUsedTextureMask(s) == 0x44, "bump-only indirect -> 0x44");
    s = mk(0); s.tevStages[0].texMap = 3;
    CK(NativeUsedTextureMask(s) == 0x08, "count zero uses first stage");
  }
  {
    GXState s = mk(); s.tevStages[0].texMap = 8;
    CK(NativeUsedTextureMask(s) == 0xff, "invalid map8 -> 0xff");
    GXState s2 = mk(17);
    CK(NativeUsedTextureMask(s2) == 0xff, "count 17 -> 0xff");
  }

  // property: generated shader never references a sampler outside the mask
  const std::regex ref_re(R"((?:textureSize|textureGrad|texture)\s*\(\s*uTex([0-7]))");
  auto references = [&](const std::string& text) {
    unsigned bits = 0;
    for (std::sregex_iterator m(text.begin(), text.end(), ref_re), e; m != e; ++m)
      bits |= 1u << std::stoi((*m)[1]);
    return bits;
  };
  {
    GXState s = mk(); s.tevStages[0].texMap = 3;
    CK(references(GenerateTEVShader(s, 0x90).fragment_source) == 0x08,
       "regex observes production map3 reference");
    for (unsigned count : {0u, 2u, 16u}) {
      s = mk(count);
      for (unsigned i = 0; i < (count ? count : 1); ++i)
        s.tevStages[i].texMap = (i * 3) & 7;
      const unsigned refs = references(GenerateTEVShader(s, 0x90).fragment_source);
      CK(refs != 0 && (refs & ~NativeUsedTextureMask(s)) == 0,
         "boundary stage counts cover generated references");
    }
  }
  unsigned covered = 0;
  for (unsigned prim : {0x90u}) {
    for (unsigned selected = 0; selected < 4; ++selected)
      for (unsigned available : {0u, 1u, 4u})
        for (unsigned matrix = 0; matrix < 2; ++matrix)
          for (unsigned bump = 0; bump < 2; ++bump)
            for (unsigned imap = 0; imap < 8; ++imap)
              for (unsigned pmap = 0; pmap <= 8; ++pmap) {
                const unsigned map = pmap == 8 ? 0xff : pmap;
                GXState s = mk(); s.tevStages[0].texMap = map;
                if (map != 0xff && ((selected + available + matrix + bump + imap) & 1))
                  s.texStages[map].base_addr = 0;
                s.bp[0] = available << 16;
                s.bp[0x10] = selected | (matrix << 9) | (bump << 7);
                s.bp[0x27] = imap << (selected * 6);
                const unsigned mask = NativeUsedTextureMask(s);
                auto src = GenerateTEVShader(s, prim);
                unsigned referenced = 0;
                for (std::sregex_iterator m(src.vertex_source.begin(), src.vertex_source.end(), ref_re),
                     e; m != e; ++m) referenced |= 1u << std::stoi((*m)[1]);
                for (std::sregex_iterator m(src.fragment_source.begin(), src.fragment_source.end(), ref_re),
                     e; m != e; ++m) referenced |= 1u << std::stoi((*m)[1]);
                if (referenced && (referenced & ~mask) == 0) {
                  ++covered;
                  continue;
                }
                ++fails;
                printf("FAIL mask miss map=%u sel=%u avail=%u mtx=%u bmp=%u "
                       "imap=%u refs=%02x mask=%02x\n",
                       map, selected, available, matrix, bump, imap,
                       referenced, mask);
                if (fails > 20) { printf("%s (%d failures)\n", "FAIL", fails); return fails; }
              }
  }
  printf("property cases: %u\n", covered);
  printf("%s (%d failures)\n", fails ? "FAIL" : "PASS", fails);
  return fails;
}
