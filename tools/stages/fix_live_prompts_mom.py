"""MoM host-only action glyphs; render queue and guest resources remain intact."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
INCLUDE='#include "runtime/native_prompt_textures_mom.h" // NATIVE_MOM_LIVE_PROMPTS_V1\n'
HOOK='''    // NATIVE_MOM_LIVE_PROMPTS_V1: exact decoded originals, with source/palette reuse guards.
    static const bool prompts_enabled=std::getenv("NWII_PROMPT_PACK")!=nullptr;
    if(prompts_enabled && stage.width==32 && stage.height==32) {
        const uint32_t bytes=texture_data_size(32,32,stage.format);
        if(bytes && NativeEfbSpan(ctx,stage.base_addr,bytes)) {
            uint32_t count=stage.format==8?16:stage.format==9?256:stage.format==10?16384:0;
            const uint32_t off=stage.tlut_offset, capacity=sizeof(gx::RenderState().tlutMem);
            const uint32_t palette_bytes=off<capacity?std::min(count*2,capacity-off):0;
            const auto* palette=palette_bytes?gx::RenderState().tlutMem+off:nullptr;
            auto& prompts=native_prompts::mom_textures();
            int glyph=prompts.probe(stage.base_addr,stage.format,stage.tlut_format,ctx.mmu.get_ptr(stage.base_addr),bytes,palette,palette_bytes);
            if(glyph==-2) {
                Image candidate=decode_texture(ctx,stage);
                glyph=prompts.record(stage.base_addr,static_cast<const unsigned char*>(candidate.data));UnloadImage(candidate);
            }
            if(GLuint tex=prompts.texture(glyph))return protect(tex);
        }
    }
'''
CLEAR='    native_prompts::mom_textures().clear(); // NATIVE_MOM_LIVE_PROMPTS_V1\n'
def main():
    restore='--restore' in sys.argv
    for tree in ('export/nWiiRuntime','vendor/NWiiRecomp/nWiiRuntime'):
        root=ROOT/tree;p=root/'src/hle/texture_cache_gl.cpp';s=p.read_text(encoding='utf-8')
        if restore:s=s.replace(INCLUDE,'').replace(HOOK,'').replace(CLEAR,'')
        elif INCLUDE not in s:
            anchor='    uint32_t size = texture_data_size(stage.width, stage.height, stage.format);'
            assert s.count(anchor)==1,p
            # All includes (and GL type declarations) precede this header.
            at='namespace nwii::runtime { extern CPUContext* g_ctx_ptr; }'
            assert s.count(at)==1,p
            s=s.replace(at,INCLUDE+at,1).replace(anchor,HOOK+anchor,1)
            clear='    clear_pending = false;\n';assert s.count(clear)==1,p
            s=s.replace(clear,clear+CLEAR,1)
        else:assert HOOK in s and CLEAR in s,p
        if s!=p.read_text(encoding='utf-8'):p.write_text(s,encoding='utf-8',newline='\n')
        if not restore:
            target=root/'include/runtime/native_prompt_textures_mom.h';data=(ROOT/'overlays/native_prompt_textures_mom.h').read_bytes()
            if not target.exists() or target.read_bytes()!=data:target.write_bytes(data)
    print('MoM live prompts '+('restored' if restore else 'installed'))
if __name__=='__main__':main()
