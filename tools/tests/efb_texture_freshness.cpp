// Compile with runtime + glad include paths and link the existing libglad.a.
// Exercise the actual cache implementation with GL uploads observed in memory.
#include "../../vendor/NWiiRecomp/nWiiRuntime/src/hle/texture_cache_gl.cpp"
#include <cassert>

namespace nwii::runtime {
CPUContext* g_ctx_ptr=nullptr;
uint32_t g_watch_addr=0;
void watch_hit(uint32_t,uint32_t,int) { std::abort(); }
uint16_t HW_Reg_Read16(uint32_t) { std::abort(); }
uint32_t HW_Reg_Read32(uint32_t) { std::abort(); }
void HW_Reg_Write16(uint32_t,uint16_t) { std::abort(); }
void HW_Reg_Write32(uint32_t,uint32_t) { std::abort(); }
void GX_WGPIPE_Write8(uint8_t) { std::abort(); }
void GX_WGPIPE_Write16(uint16_t) { std::abort(); }
void GX_WGPIPE_Write32(uint32_t) { std::abort(); }
void GX_WGPIPE_WriteF32(float) { std::abort(); }
void GX_WGPIPE_WriteF64(double) { std::abort(); }
}
nwii::runtime::gx::GXState nwii::runtime::gx::g_state{};
static unsigned uploads=0;
static uint8_t observed[128]{};
static void APIENTRY Gen(GLsizei count, GLuint* names) { while(count--) *names++=1000+uploads; }
static void APIENTRY Bind(GLenum,GLuint) {}
static void APIENTRY Param(GLenum,GLenum,GLint) {}
static void APIENTRY Delete(GLsizei,const GLuint*) {}
static void APIENTRY Upload(GLenum,GLint,GLint,GLsizei w,GLsizei h,GLint,GLenum,GLenum,const void* pixels) {
  assert(w==8 && h==4); std::memcpy(observed,pixels,128); ++uploads;
}
int main() {
  using namespace nwii::runtime;
  CPUContext ctx; g_ctx_ptr=&ctx;
  glad_glGenTextures=Gen; glad_glBindTexture=Bind; glad_glTexParameteri=Param;
  glad_glTexImage2D=Upload; glad_glDeleteTextures=Delete;
  auto& cache=hle::TextureCache::get();
  auto& state=gx::g_state;
  state.efbW=8; state.efbH=4; state.efbCopyStride=32;
  state.bp[0x52]=2u<<3; // I8 copy: one 8x4 tile, 32 bytes.
  gx::TexStage stage{}; stage.base_addr=0x80010000; stage.width=8; stage.height=4; stage.format=1;
  std::memset(ctx.mmu.get_ptr(stage.base_addr),9,32);
  cache.register_efb_texture(stage.base_addr,77);
  assert(cache.get_texture(ctx,stage)==77 && uploads==0);
  assert(cache.find_efb_texture(0x90010000)==0); // Same low bits, different RAM bank.
  assert(cache.find_efb_texture(0xC0010000)==77); // Uncached MEM1 alias.
  ctx.mmu.write8(stage.base_addr+17,23); // A byte the old sampled hash would miss.
  assert(cache.get_texture(ctx,stage)!=77 && uploads==1);
  bool saw_changed=false;
  for(unsigned i=0;i<32;++i) saw_changed |= observed[i*4]==23;
  assert(saw_changed);
  cache.register_efb_texture(stage.base_addr,88);
  assert(cache.get_texture(ctx,stage)==88 && uploads==1);
  ctx.mmu.write8(stage.base_addr,41);
  assert(cache.get_texture(ctx,stage)!=88 && uploads==2);
  assert(observed[0]==41 && observed[1]==41 && observed[2]==41);
  std::cout << "EFB freshness: unchanged copies, byte edits, re-registration and RAM-bank identity PASS\n";
}
