#include "runtime/cpu_context.h"
#include <iostream>

// These checks use RAM only; any MMIO access is a test failure.
namespace nwii::runtime {
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

int main() {
  nwii::runtime::CPUContext ctx;
  constexpr uint32_t a=0x80010000, b=0x80011000;
  ctx.mmu.write32(0x800000D4,a);
  ctx.msr=0;
  if (ctx.require_fpu(0x8001000)) return 2;
  ctx.fpr[2]=4503599627370496.0;
  ctx.ps1[2]=13.0;
  ctx.fpscr=0x12345678;
  ctx.mmu.write32(0x800000D4,b);
  ctx.msr &= ~0x2000u;
  if (ctx.require_fpu(0x8001004)) return 2;
  ctx.fpr[2]=-37.0;
  ctx.ps1[2]=17.0;
  ctx.fpscr=0x87654321;
  ctx.mmu.write32(0x800000D4,a);
  ctx.msr &= ~0x2000u;
  if (ctx.require_fpu(0x8001008)) return 2;
  bool ok=ctx.fpr[2]==4503599627370496.0 && ctx.ps1[2]==13.0 && ctx.fpscr==0x12345678;
  ctx.mmu.write32(0x800000D4,b);
  ctx.msr &= ~0x2000u;
  if (ctx.require_fpu(0x800100c)) return 2;
  ok &= ctx.fpr[2]==-37.0 && ctx.ps1[2]==17.0 && ctx.fpscr==0x87654321;
  ok &= ctx.mmu.read32(0x800000D8)==b;
  // Synthetic callbacks can overwrite the physical owner's registers without
  // switching contexts. Returning must reload the interrupted snapshot anyway.
  ctx.save_fp_context(b);
  ctx.fpr[2]=99.0;
  ctx.ps1[2]=101.0;
  ctx.fpscr=0;
  ctx.acquire_fp_context(b,true);
  ok &= ctx.fpr[2]==-37.0 && ctx.ps1[2]==17.0 && ctx.fpscr==0x87654321;
  std::cout << "FP ownership switch/restore: " << (ok ? "PASS" : "FAIL") << '\n';
  return ok ? 0 : 1;
}
