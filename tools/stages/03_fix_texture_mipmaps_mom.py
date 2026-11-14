"""Mipmapped texture sampling: a minified texture is no longer point-sampled.

Ported from CotT's ``tools/fix_texture_mipmaps.py``, re-derived against this
tree's texture cache and sampler.

Before: the texture cache uploaded level 0 only and the sampler collapsed every
GX min filter to plain LINEAR/NEAREST, so any texture minified on screen (small
or distant objects) was point-sampled from the full-size image: aliasing noise
that gets worse the smaller the object is.  Hardware samples a mip level chosen
from the screen-space derivative, between the sampler's LOD clamps, with a LOD
bias.

Now:

  * a texture the cache uploads starts at level 0 only
    (``GL_TEXTURE_MAX_LEVEL`` 0), so it stays complete and cheap;
  * the first time a texture is drawn with a mip filter, its chain is built
    (``glGenerateMipmap``) and the level cap is lifted.  The chain is built
    lazily because generating it for every upload cost about a millisecond per
    texture;
  * the sampler maps the BP texmode0 min-filter bits (0 near, 1 near/mip-near,
    2 near/mip-lin, 4 linear, 5 lin/mip-near, 6 lin/mip-lin) to the GL
    filters, applies texmode0's LOD bias (signed, 1/32 steps) and texmode1's
    min/max LOD clamps (1/16 steps);
  * an EFB copy texture (a render target) is limited to level 0, so it stays
    complete under a mip filter.

Deliberate deviation from CotT: the LOD clamps are only applied when the
guest actually wrote texmode1 (a non-zero word).  An unwritten texmode1 reads
as 0, which on hardware means "no clamp"; applying a 0/16 clamp would pin every
texture to level 0 and silently disable the whole feature.

``NWII_NO_MIPMAPS=1`` restores the old behaviour for A/B comparison.

Verified by reading the cache upload (``src/hle/texture_cache_gl.cpp``) and the
sampler (``src/hle/gx/renderer_gl.cpp``); the EFB copy texture is created in
``NativeCopyEfbTexture`` and is the one texture that must not get a chain (it is
still attached to the copy framebuffer).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_MIPMAPS_V1'

# ---- header: declare the lazy chain builder --------------------------------
header = runtime / 'include/runtime/texture_cache_gl.h'
text = header.read_text()
if MARK not in text:
    old = ('    void register_efb_texture(uint32_t addr, GLuint tex);\n'
           '    GLuint find_efb_texture(uint32_t addr) const;\n')
    assert text.count(old) == 1, 'texture_cache_gl.h anchor'
    new = old + ('    // ' + MARK + ': build the mip chain the first time a mip filter samples\n'
                 '    // this texture; until then the texture carries level 0 only.\n'
                 '    void ensure_mips(GLuint tex);\n')
    text = text.replace(old, new, 1)
    old = '    std::unordered_map<uint32_t, GLuint> efb_textures;\n'
    assert text.count(old) == 1, 'efb_textures anchor'
    text = text.replace(old, old + '    std::unordered_set<GLuint> mip_ready; // ' + MARK + '\n', 1)
    if '#include <unordered_set>' not in text:
        assert '#include <unordered_map>' in text
        text = text.replace('#include <unordered_map>', '#include <unordered_map>\n#include <unordered_set>', 1)
    header.write_text(text)

# ---- cache: level 0 only, and the lazy chain builder -----------------------
source = runtime / 'src/hle/texture_cache_gl.cpp'
text = source.read_text()
if MARK not in text:
    old = ('    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);\n'
           '    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);\n'
           '    UnloadImage(img);\n')
    assert text.count(old) == 1, 'upload anchor'
    new = ('    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);\n'
           '    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);\n'
           '    // ' + MARK + ': level 0 only until ensure_mips() builds the chain.\n'
           '    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0);\n'
           '    UnloadImage(img);\n')
    text = text.replace(old, new, 1)

    anchor = 'Image TextureCache::decode_texture(CPUContext& ctx, const gx::TexStage& stage) {'
    assert text.count(anchor) == 1, 'decode_texture anchor'
    helper = ('void TextureCache::ensure_mips(GLuint tex) { // ' + MARK + '\n'
              '    static const bool mipmaps = std::getenv("NWII_NO_MIPMAPS") == nullptr;\n'
              '    if (!mipmaps || !tex || mip_ready.count(tex)) return;\n'
              '    mip_ready.insert(tex);\n'
              '    GLint active = 0, bound = 0;\n'
              '    glGetIntegerv(GL_ACTIVE_TEXTURE, &active);\n'
              '    glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);\n'
              '    glActiveTexture(GL_TEXTURE0);\n'
              '    glBindTexture(GL_TEXTURE_2D, tex);\n'
              '    glGenerateMipmap(GL_TEXTURE_2D);\n'
              '    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 1000);\n'
              '    glBindTexture(GL_TEXTURE_2D, GLuint(bound));\n'
              '    glActiveTexture(GLuint(active));\n'
              '}\n'
              '\n')
    text = text.replace(anchor, helper + anchor, 1)
    source.write_text(text)

# ---- renderer: sampler mapping, lazy chain, and the EFB copy level cap ------
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK not in text:
    # The signature and the body are edited separately so that another stage
    # may insert code between them (the anisotropic-filter option does).
    old = 'static void NativeApplyTextureSampler(unsigned unit, uint32_t mode) {\n'
    assert text.count(old) == 1, 'sampler signature anchor'
    text = text.replace(old, 'static void NativeApplyTextureSampler(unsigned unit, uint32_t mode, uint32_t mode1 = 0) {\n', 1)

    old = ('  // NATIVE_SAMPLER_MODE_CACHE_V1: only these bits affect current parameters.\n'
           '  static unsigned previous[8]={};static bool have[8]={};\n'
           '  static const bool cache=std::getenv("NWII_NO_SAMPLER_MODE_CACHE")==nullptr;\n'
           '  const unsigned resolved=mode&0x9fu;\n'
           '  if(!cache || !have[unit] || previous[unit]!=resolved){\n'
           '  auto wrap=[](unsigned value)->GLint {\n'
           '    return value==1?GL_REPEAT:value==2?GL_MIRRORED_REPEAT:GL_CLAMP_TO_EDGE;\n'
           '  };\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_WRAP_S,wrap(mode&3));\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_WRAP_T,wrap((mode>>2)&3));\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_MAG_FILTER,(mode&16)?GL_LINEAR:GL_NEAREST);\n'
           '  // The cache currently uploads level zero only. Preserve the requested texel\n'
           '  // filter without selecting an incomplete mip chain. Mip LOD remains open.\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_MIN_FILTER,(mode&128)?GL_LINEAR:GL_NEAREST);\n'
           '  previous[unit]=resolved;have[unit]=true;\n'
           '  }\n'
           '  glBindSampler(unit,samplers[unit]);\n'
           '}\n')
    assert text.count(old) == 1, 'sampler body anchor'
    new = ('  // NATIVE_SAMPLER_MODE_CACHE_V1: only these bits affect current parameters.\n'
           '  // ' + MARK + ': the min filter (bits 5-7), the LOD bias (bits 9-16) and\n'
           '  // the texmode1 LOD clamps now matter too.\n'
           '  static uint64_t previous[8]={};static bool have[8]={};\n'
           '  static const bool cache=std::getenv("NWII_NO_SAMPLER_MODE_CACHE")==nullptr;\n'
           '  static const bool mipmaps=std::getenv("NWII_NO_MIPMAPS")==nullptr;\n'
           '  const uint64_t resolved=uint64_t(mode&0x1ffffu)|(uint64_t(mode1&0xffffu)<<32);\n'
           '  if(!cache || !have[unit] || previous[unit]!=resolved){\n'
           '  auto wrap=[](unsigned value)->GLint {\n'
           '    return value==1?GL_REPEAT:value==2?GL_MIRRORED_REPEAT:GL_CLAMP_TO_EDGE;\n'
           '  };\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_WRAP_S,wrap(mode&3));\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_WRAP_T,wrap((mode>>2)&3));\n'
           '  glSamplerParameteri(samplers[unit],GL_TEXTURE_MAG_FILTER,(mode&16)?GL_LINEAR:GL_NEAREST);\n'
           '  if(mipmaps) {\n'
           '    // GX min filter: 0 near, 1 near+mip near, 2 near+mip lin, 4 lin,\n'
           '    // 5 lin/mip near, 6 lin/mip lin (3 and 7 are reserved: treat as linear).\n'
           '    const unsigned f=(mode>>5)&7u;\n'
           '    const GLint filter=f==0?GL_NEAREST:f==1?GL_NEAREST_MIPMAP_NEAREST\n'
           '                     :f==2?GL_NEAREST_MIPMAP_LINEAR:f==5?GL_LINEAR_MIPMAP_NEAREST\n'
           '                     :f==6?GL_LINEAR_MIPMAP_LINEAR:GL_LINEAR;\n'
           '    glSamplerParameteri(samplers[unit],GL_TEXTURE_MIN_FILTER,filter);\n'
           '    const int bias=int(int8_t((mode>>9)&0xffu));       // signed, 1/32 LOD steps\n'
           '    glSamplerParameterf(samplers[unit],GL_TEXTURE_LOD_BIAS,float(bias)/32.f);\n'
           '    if (mode1) {                                     // the guest set a LOD clamp\n'
           '      glSamplerParameterf(samplers[unit],GL_TEXTURE_MIN_LOD,float(mode1&0xffu)/16.f);\n'
           '      glSamplerParameterf(samplers[unit],GL_TEXTURE_MAX_LOD,float((mode1>>8)&0xffu)/16.f);\n'
           '    }\n'
           '  } else {\n'
           '    // The cache currently uploads level zero only. Preserve the requested texel\n'
           '    // filter without selecting an incomplete mip chain. Mip LOD remains open.\n'
           '    glSamplerParameteri(samplers[unit],GL_TEXTURE_MIN_FILTER,(mode&128)?GL_LINEAR:GL_NEAREST);\n'
           '  }\n'
           '  previous[unit]=resolved;have[unit]=true;\n'
           '  }\n'
           '  glBindSampler(unit,samplers[unit]);\n'
           '}\n')
    text = text.replace(old, new, 1)

    old = '              NativeApplyTextureSampler(i,g_state.bp[(i<4?0x80:0xa0)+(i%4)]);\n'
    assert text.count(old) == 1, 'sampler call site'
    new = ('              NativeApplyTextureSampler(i,g_state.bp[(i<4?0x80:0xa0)+(i%4)],\n'
           '                                         g_state.bp[(i<4?0x84:0xa4)+(i%4)]); // ' + MARK + '\n'
           '              { // ' + MARK + ': build the chain the first time a mip filter samples it.\n'
           '                const unsigned mip_filter=(g_state.bp[(i<4?0x80:0xa0)+(i%4)]>>5)&7u;\n'
           '                if (mip_filter==1||mip_filter==2||mip_filter==5||mip_filter==6) {\n'
           '                  const GLuint efb = nwii::runtime::hle::TextureCache::get()\n'
           '                                          .find_efb_texture(stage.base_addr);\n'
           '                  if (efb != tex) nwii::runtime::hle::TextureCache::get().ensure_mips(tex);\n'
           '                }\n'
           '              }\n')
    text = text.replace(old, new, 1)

    old = ('  glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);\n'
           '  glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);\n'
           '  glBindFramebuffer(GL_READ_FRAMEBUFFER,GLuint(draw));\n'
           '  glBindFramebuffer(GL_DRAW_FRAMEBUFFER,copy_fbo);\n')
    assert text.count(old) == 1, 'EFB copy texture anchor'
    new = ('  glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);\n'
           '  glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);\n'
           '  glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAX_LEVEL,0); // ' + MARK + ': a copy has no chain\n'
           '  glBindFramebuffer(GL_READ_FRAMEBUFFER,GLuint(draw));\n'
           '  glBindFramebuffer(GL_DRAW_FRAMEBUFFER,copy_fbo);\n')
    text = text.replace(old, new, 1)
    renderer.write_text(text)

print('Minified textures are mip-sampled; the chain is built lazily per texture.')
