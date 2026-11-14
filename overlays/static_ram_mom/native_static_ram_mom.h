#pragma once
// NATIVE_STATIC_RAM_MOM_V1 -- guest MEM1 and MEM2 at fixed host virtual addresses.
//
// Installed by tools/stages/fix_static_ram_base_mom.py (report: logs/agent-reports/static_ram_base.md).
//
// MMU::mem1 and MMU::mem2 keep their names and their data()/size()/operator[] interface, but they are
// FixedRam objects whose data() is a compile-time constant:
//     MEM1  host 0x20000000   24 MiB + 8 bytes
//     MEM2  host 0x30000000   64 MiB + 8 bytes
// 0x30000000 - 0x10000000 == 0x20000000, so the MEM2 accessors' `mem2.data() + (paddr - 0x10000000)` folds to
// host [paddr + 0x20000000] as well. The size is a constant too, so a guest access is one bounds compare
// against an immediate and one disp32 address, instead of loading begin, end and data from the MMU object.
// Nothing else changes: the +8 padding, every bounds check, the 0x0C sentinel, MMIO, watches and the
// slow paths keep their existing code.
//
// Both banks are committed once, when the MMU is constructed, with VirtualAlloc(MEM_RESERVE | MEM_COMMIT)
// at the fixed address. The pages are zero-filled, as the old resize(n, 0) was. The address is a
// compile-time choice, so if it is already in use the process prints a message and aborts. This build
// has no run-time fallback. Run the stage with --restore to get the std::vector banks back.
//
// This header is Windows-only (VirtualAlloc). cpu_context.h already includes <windows.h> through
// native_idle_wait.h before it reaches this header, so no extra Windows macros are introduced here.

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

#if !defined(_WIN32)
#error "NATIVE_STATIC_RAM_MOM_V1 needs Windows VirtualAlloc. Restore the std::vector banks on other hosts."
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace nwii::runtime::native_static_ram_mom {

inline constexpr uintptr_t kMem1Host = 0x20000000u;
inline constexpr uintptr_t kMem2Host = 0x30000000u;
inline constexpr size_t kMem1Bytes = 24u * 1024u * 1024u + 8u;
inline constexpr size_t kMem2Bytes = 64u * 1024u * 1024u + 8u;

// A RAM bank whose base is a compile-time constant. Same surface the MMU and the span proofs use on a vector.
template <uintptr_t Host, size_t Bytes>
struct FixedRam {
  uint8_t *data() const { return reinterpret_cast<uint8_t *>(Host); }
  static constexpr size_t size() { return Bytes; }
  uint8_t &operator[](size_t index) const { return data()[index]; }
};

using Mem1Ram = FixedRam<kMem1Host, kMem1Bytes>;
using Mem2Ram = FixedRam<kMem2Host, kMem2Bytes>;

// Commits [base, base + bytes) once per process. A second MMU in the same process (test fixtures) finds its
// bank already committed and reuses it, so the bank is never aliased and never committed twice.
inline void Commit(uintptr_t base, size_t bytes) {
  static uintptr_t placed[4] = {};
  static unsigned count = 0;
  for (unsigned i = 0; i < count; ++i) {
    if (placed[i] == base) return;
  }
  // NATIVE_MOM_RAM_WRITE_WATCH_V1: allocation falls back without tracking.
  void *mapped = VirtualAlloc(reinterpret_cast<void *>(base), bytes, MEM_RESERVE | MEM_COMMIT | MEM_WRITE_WATCH, PAGE_READWRITE);
  if (!mapped)
    mapped = VirtualAlloc(reinterpret_cast<void *>(base), bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
  if (mapped != reinterpret_cast<void *>(base)) {
    const unsigned long error = static_cast<unsigned long>(GetLastError());
    std::fprintf(stderr,
                 "[StaticRAM] cannot commit guest RAM at 0x%llx (error %lu). Another module owns that address and "
                 "the fixed base is chosen at compile time. Run tools/stages/fix_static_ram_base_mom.py --restore "
                 "to return to the dynamic std::vector banks.\n",
                 static_cast<unsigned long long>(base), error);
    std::fflush(stderr);
    std::abort();
  }
  if (count < 4) placed[count++] = base;
}

}  // namespace nwii::runtime::native_static_ram_mom
