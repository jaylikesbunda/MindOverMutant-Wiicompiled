"""Preserve FP state across synthetic interrupts with the always-available FPU.

MoM's animation-key search (80331BE8) keeps the integer conversion bias in f2
across its loop. A synthetic interrupt saved only GPRs; its callback overwrote
f2 and the search resumed with 3c737ebba0000000 instead of 4330000000000000,
so no key could bracket the animation time and boot spun at 80331DA0.

Use the retail OSContext layout, verified in MoM's 8048B44C/8048B6A0 FP
load/save routines: FPRs at 90, FPSCR at 194, paired singles at 1C8, and
state bit 0 at 1A2. This accompanies fix_fp_available.py: lazy ownership
cannot protect interrupted FP registers when the unavailable vector is bypassed.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
path = port / "build/nwii-full-source/nWiiRuntime/src/hle/ios.cpp"
text = path.read_text()
marker = "NATIVE_HLE_FP_CONTEXT_V1"
if marker not in text and "NATIVE_HLE_FP_OWNERSHIP_V1" not in text:
    old = "  ctx.mmu.write32(c + 0x8C, ctx.xer);"
    new = old + """

  // NATIVE_HLE_FP_CONTEXT_V1: eager FP preservation matches require_fpu.
  for (unsigned i = 0; i < 32; ++i) {
    ctx.mmu.write_f64(c + 0x90 + i * 8, ctx.fpr[i]);
    ctx.mmu.write_f32(c + 0x1C8 + i * 8, float(ctx.fpr[i]));
    ctx.mmu.write_f32(c + 0x1CC + i * 8, float(ctx.ps1[i]));
  }
  ctx.mmu.write32(c + 0x190, 0);
  ctx.mmu.write32(c + 0x194, ctx.fpscr);
  ctx.mmu.write16(c + 0x1A2, ctx.mmu.read16(c + 0x1A2) | 1u);
"""
    assert text.count(old) == 1
    text = text.replace(old, new, 1)
    old = "  ctx.xer = ctx.mmu.read32(c + 0x8C);"
    new = old + """

  // Restore only initialized FP contexts; paired lane 0 must not round FPRs.
  if (ctx.mmu.read16(c + 0x1A2) & 1u) {
    for (unsigned i = 0; i < 32; ++i) {
      ctx.fpr[i] = ctx.mmu.read_f64(c + 0x90 + i * 8);
      ctx.ps1[i] = ctx.mmu.read_f32(c + 0x1CC + i * 8);
    }
    ctx.fpscr = ctx.mmu.read32(c + 0x194);
  }
"""
    assert text.count(old) == 1
    text = text.replace(old, new, 1)
    text = text.replace(
        "// NATIVE_GENERAL_CONTEXT_ONLY: original SDK manages lazy physical FP state.",
        "// Synthetic contexts preserve integer and FP state together.",
    )
    path.write_text(text)
print("HLE contexts preserve FPRs, paired singles and FPSCR.")
