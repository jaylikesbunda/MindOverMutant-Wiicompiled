"""Perform SDK lazy FP ownership transfers natively instead of skipping them.

OSSetCurrentContext clears MSR[FP] when switching away from 800000D8's owner.
Granting FP without transferring registers lets the next thread overwrite the
previous thread's live FPRs. The unavailable vector is outside translated text,
so implement its save/load operation with the verified retail OSContext layout.
Synthetic interrupts use the same helpers, and never save another owner's FP
registers into a context which was interrupted with FP disabled.
"""
import re
from pathlib import Path

port = Path(__file__).resolve().parents[1]
runtime = port / "build/nwii-full-source/nWiiRuntime"
path = runtime / "include/runtime/cpu_context.h"
text = path.read_text()
if "NATIVE_FP_OWNERSHIP_V1" not in text:
    new = """  // NATIVE_FP_OWNERSHIP_V1: retail OSContext FP ownership without vector 0x800.
  static bool fp_context_ram(uint32_t c) {
    return (c >= 0x80000000u && c <= 0x817FFD38u) ||
           (c >= 0x90000000u && c <= 0x93FFFD38u);
  }
  void save_fp_context(uint32_t c) {
    for (unsigned i = 0; i < 32; ++i) {
      mmu.write_f64(c + 0x90 + i * 8, fpr[i]);
      mmu.write_f32(c + 0x1C8 + i * 8, float(fpr[i]));
      mmu.write_f32(c + 0x1CC + i * 8, float(ps1[i]));
    }
    mmu.write32(c + 0x190, 0);
    mmu.write32(c + 0x194, fpscr);
    mmu.write16(c + 0x1A2, mmu.read16(c + 0x1A2) | 1u);
  }
  void acquire_fp_context(uint32_t c, bool reload = false) {
    if (!fp_context_ram(c)) return;
    const uint32_t owner = mmu.read32(0x800000D8);
    if (owner != c && fp_context_ram(owner)) save_fp_context(owner);
    if ((reload || owner != c) && (mmu.read16(c + 0x1A2) & 1u)) {
      for (unsigned i = 0; i < 32; ++i) {
        fpr[i] = mmu.read_f64(c + 0x90 + i * 8);
        ps1[i] = mmu.read_f32(c + 0x1CC + i * 8);
      }
      fpscr = mmu.read32(c + 0x194);
    }
    mmu.write32(0x800000D8, c);
    msr |= 0x2000u;
  }
  bool require_fpu(uint32_t fault_pc) {
    if (msr & 0x2000u) return false;
    (void)fault_pc;
    acquire_fp_context(mmu.read32(0x800000D4));
    msr |= 0x2000u;
    return false;
  }"""
    text, count = re.subn(r"  bool require_fpu\(uint32_t fault_pc\) \{.*?\n  \}",
                          lambda _: new, text, count=1, flags=re.S)
    assert count == 1
    path.write_text(text)

path = runtime / "src/hle/ios.cpp"
text = path.read_text()
if "NATIVE_HLE_FP_OWNERSHIP_V1" not in text:
    start = text.index("  // NATIVE_HLE_FP_CONTEXT_V1:")
    end = text.index("\n", text.index("ctx.mmu.write16(c + 0x1A2,", start))
    text = text[:start] + """  // NATIVE_HLE_FP_OWNERSHIP_V1: disabled FP may belong to another thread.
  if (ctx.msr & 0x2000u) {
    ctx.save_fp_context(c);
    ctx.mmu.write32(0x800000D8, c);
  }
""" + text[end:]
    start = text.index("  // Restore only initialized FP contexts;")
    end = text.index("\n  }", start) + len("\n  }")
    text = text[:start] + text[end:]
    start = text.index("  // NATIVE_FP_ALWAYS_AVAILABLE_V1:")
    end = text.index("\n", text.index("  ctx.msr |= 0x2000u;", start))
    text = text[:start] + """  // Restore initialized FP state after loading MSR; first use claims new contexts.
  if (ctx.mmu.read16(c + 0x1A2) & 1u) ctx.acquire_fp_context(c, true);
  else ctx.msr &= ~0x2000u;
""" + text[end:]
    path.write_text(text)
print("Native lazy FP ownership and synthetic context restore are coherent.")
