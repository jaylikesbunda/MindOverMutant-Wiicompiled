"""Keep MSR[FP] available: never take the FPU-unavailable trap.

Mind over Mutant creates its threads with MSR[FP] clear (the SDK's lazy-FPU
ownership design: the first FP instruction traps to the FPU-unavailable vector
at 0x800, whose stub grants ownership and returns to the faulting instruction).
That vector cannot run here: the generated dispatcher aliases pc 0x800 to
0x80000800 (`pc |= 0x80000000`), which lies outside recompiled text, so with
NWII_TEXT_GUARD the trap is refused and execution resumes at the faulting
`lfs` forever (boot parks at 0x801F686C with msr=0x0, dispatching 0x801F686C /
0x80000800 alternately). Observed: crt0 enables FP (mfmsr/ori 0x2000/mtmsr at
0x80006594) but the first saved context already shows msr=0x8030, and every
restore after that keeps FP off.

Worse, trapping here does not even retry: require_fpu diverts to the vector
instead of completing the instruction, and the guard's refusal resumes at the
caller's lr. For FPU prologues (e.g. the FPR-save sequences that run before
any millicode save) that means the guarded work is silently skipped while the
frame stays live - downstream code then runs with uninitialized FP state and
uncalibrated control flow (observed: a wild loop bound and a clobbered lr slot
downstream of an FP-trapped region).

This bootstrap stage grants FP in place instead of entering the unavailable
vector. fix_fp_context.py then preserves synthetic interrupt snapshots, and
fix_fp_ownership.py refines both paths to implement the SDK's lazy ownership
transfer natively. Keep those stages together: granting FP alone loses another
thread's live FP registers when OSSetCurrentContext changes the owner.
"""
from pathlib import Path
port=Path(__file__).resolve().parents[1]
# 1) require_fpu: grant FP in place instead of trapping to the vector.
path=port/'build/nwii-full-source/nWiiRuntime/include/runtime/cpu_context.h'
text=path.read_text()
marker='NATIVE_FP_ALWAYS_AVAILABLE_V1'
if marker not in text and 'NATIVE_FP_OWNERSHIP_V1' not in text:
    old='''  bool require_fpu(uint32_t fault_pc) {
    if (msr & 0x2000u) return false;
    // Hardware exception entry: retry faulting instruction after guest rfi.
    srr0=fault_pc; srr1=msr & 0x87C0FFFFu;
    msr=(msr & ~1u) | ((msr>>16)&1u);
    msr &= ~0x04EF36u;
    pc=0x800u;
    return true;
  }'''
    new='''  bool require_fpu(uint32_t fault_pc) {
    if (msr & 0x2000u) return false;
    (void)fault_pc;
    // NATIVE_FP_ALWAYS_AVAILABLE_V1: the FPU-unavailable vector cannot run in
    // this runtime (pc 0x800 aliases outside recompiled text), and diverting
    // there skips the faulting work and leaks the live frame on return.
    // Grant FP in place; the following ownership stage supplies transfers.
    msr |= 0x2000u;
    return false;
  }'''
    assert text.count(old)==1
    path.write_text(text.replace(old,new,1))
# 2) Restored thread contexts keep the crt0-established FP bit as well, so a
# thread that never touches FP needlessly traps on first use after a switch.
path=port/'build/nwii-full-source/nWiiRuntime/src/hle/ios.cpp'
text=path.read_text()
if marker not in text and 'NATIVE_HLE_FP_OWNERSHIP_V1' not in text:
    old='''  ctx.pc = ctx.mmu.read32(c + 0x198);  
  ctx.msr = ctx.mmu.read32(c + 0x19C); 
'''
    new='''  ctx.pc = ctx.mmu.read32(c + 0x198);  
  ctx.msr = ctx.mmu.read32(c + 0x19C); 
  // NATIVE_FP_ALWAYS_AVAILABLE_V1: see cpu_context.h.
  ctx.msr |= 0x2000u;
'''
    assert text.count(old)==1
    path.write_text(text.replace(old,new,1))
print('FPU instructions can run without entering the unavailable vector.')
