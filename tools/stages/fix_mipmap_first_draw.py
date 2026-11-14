"""Build a texture's mip chain before the sampler can select it.

The mipmap stage made the chain lazy: a texture is uploaded with
``GL_TEXTURE_MAX_LEVEL = 0`` and ``ensure_mips()`` generates the chain the
first time a mip filter samples it.  But at the call site the *sampler* was
updated first and the chain built second:

    NativeApplyTextureSampler(i, texmode0, texmode1);   // min filter = *MIPMAP*
    { ... ensure_mips(tex); }                            // chain built after

A texture whose level cap is still 0 is **incomplete** under a mip filter, and
an incomplete texture samples as (0,0,0,1).  So the first draw of every newly
seen texture was rendered from black - which the movie's YUV conversion turns
into a full-screen green flash, exactly one frame long.  Textures are re-created
whenever their content hash changes, so this fired on scene changes rather than
every frame: "sometimes".

Two fixes:

1.  the chain is built *before* the sampler is pointed at it, so the first draw
    of a texture already samples a complete chain;
2.  ``TextureCache::clear()`` (which runs when the cache passes 512 entries)
    also forgets which texture names have a chain, because GL reuses deleted
    names and a stale entry would skip chain generation for a *different*
    texture.

Neither changes what is drawn for a complete texture: the same chain is
generated, one call earlier.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_MIPMAP_ORDER_V1'

# ---- 1. chain before sampler ----------------------------------------------
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK not in text:
    old = """              NativeApplyTextureSampler(i,g_state.bp[(i<4?0x80:0xa0)+(i%4)],
                                         g_state.bp[(i<4?0x84:0xa4)+(i%4)]); // NATIVE_MOM_MIPMAPS_V1
              { // NATIVE_MOM_MIPMAPS_V1: build the chain the first time a mip filter samples it.
                const unsigned mip_filter=(g_state.bp[(i<4?0x80:0xa0)+(i%4)]>>5)&7u;
                if (mip_filter==1||mip_filter==2||mip_filter==5||mip_filter==6) {
                  const GLuint efb = nwii::runtime::hle::TextureCache::get()
                                          .find_efb_texture(stage.base_addr);
                  if (efb != tex) nwii::runtime::hle::TextureCache::get().ensure_mips(tex);
                }
              }
"""
    assert text.count(old) == 1, 'mipmap call-site anchor'
    new = """              { // """ + MARK + """: the chain must exist before the sampler selects it.
                // A level-0-only texture is *incomplete* under a mip filter and samples
                // as black, which the movie's YUV conversion turns into a green frame.
                const unsigned mip_filter=(g_state.bp[(i<4?0x80:0xa0)+(i%4)]>>5)&7u;
                if (mip_filter==1||mip_filter==2||mip_filter==5||mip_filter==6) {
                  const GLuint efb = nwii::runtime::hle::TextureCache::get()
                                          .find_efb_texture(stage.base_addr);
                  if (efb != tex) nwii::runtime::hle::TextureCache::get().ensure_mips(tex);
                }
              }
              NativeApplyTextureSampler(i,g_state.bp[(i<4?0x80:0xa0)+(i%4)],
                                         g_state.bp[(i<4?0x84:0xa4)+(i%4)]); // NATIVE_MOM_MIPMAPS_V1
"""
    text = text.replace(old, new, 1)
    renderer.write_text(text)

# ---- 2. forget chain state when the cache forgets textures -----------------
source = runtime / 'src/hle/texture_cache_gl.cpp'
text = source.read_text()
if MARK not in text:
    old = """    cache.clear();
"""
    assert text.count(old) == 1, 'cache clear anchor'
    text = text.replace(old, """    cache.clear();
    mip_ready.clear(); // """ + MARK + """: GL reuses deleted names; a stale entry would
                       // skip chain generation for a different texture.
""", 1)
    source.write_text(text)
print('Mip chains are built before the sampler can select them.')
