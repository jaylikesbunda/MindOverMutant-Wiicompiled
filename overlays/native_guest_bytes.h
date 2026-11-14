#pragma once
#include "runtime/cpu_context.h"
#include <algorithm>
#include <vector>
#include <new>
#include <cstring>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif
// NATIVE_MOM_ADAPT_A3: MoM's replacement for CotT's native_guest_bytes.h.
// CotT's version needs MMU::kMem1Limit / kMem2Limit and
// native_watch_span_may_hit, which only exist after CotT's
// fix_mmu_constant_limits and fix_watch_span_guards stages. Neither applies
// to MoM, so the RAM extents are taken from the live vectors instead:
// MEM1 = 24 MiB + 8, MEM2 = 64 MiB + 8.
namespace native_guest_bytes {
using nwii::runtime::MMU;
inline bool Enabled() {
  static const bool enabled = std::getenv("NWII_NO_IOS_BULK_TRANSFER") == nullptr;
  return enabled;
}
inline uint8_t* OrdinarySpan(MMU& memory, uint32_t address, uint32_t bytes) {
  if (!Enabled() || !bytes) return nullptr;
  if (nwii::runtime::g_watch_addr) return nullptr;
  const uint32_t physical = address & 0x3fffffffu;
  const size_t mem1_limit = memory.mem1.size() - 8;
  if (physical < mem1_limit && bytes <= mem1_limit - physical)
    return memory.mem1.data() + physical;
  if (physical >= 0x10000000u) {
    const uint32_t offset = physical - 0x10000000u;
    const size_t mem2_limit = memory.mem2.size() - 8;
    if (offset < mem2_limit && bytes <= mem2_limit - offset)
      return memory.mem2.data() + offset;
  }
  return nullptr;
}
inline void Copy(MMU& memory, uint32_t address, const uint8_t* bytes, uint32_t count) {
  if (auto* target = OrdinarySpan(memory, address, count)) {
    std::memcpy(target, bytes, count); return;
  }
  for (uint32_t i = 0; i < count; ++i) memory.write8(address + i, bytes[i]);
}
inline void Zero(MMU& memory, uint32_t address, uint32_t count) {
  if (auto* target = OrdinarySpan(memory, address, count)) {
    std::memset(target, 0, count); return;
  }
  for (uint32_t i = 0; i < count; ++i) memory.write8(address + i, 0);
}
struct ScratchState { std::vector<uint8_t> bytes; bool busy = false; };
#ifdef _WIN32
inline void NTAPI ReleaseScratch(void* pointer) { delete static_cast<ScratchState*>(pointer); }
inline ScratchState* LocalScratch() {
  static const DWORD index = FlsAlloc(ReleaseScratch);
  if (index == FLS_OUT_OF_INDEXES) return nullptr;
  if (auto* state = static_cast<ScratchState*>(FlsGetValue(index))) return state;
  auto* state = new (std::nothrow) ScratchState;
  if (!state) return nullptr;
  if (!FlsSetValue(index, state)) { delete state; return nullptr; }
  return state;
}
#else
inline ScratchState* LocalScratch() { static thread_local ScratchState state; return &state; }
#endif
class ReadBuffer {
  std::vector<uint8_t> local;
  ScratchState* owner = nullptr;
  std::vector<uint8_t>* buffer = &local;
public:
  explicit ReadBuffer(uint32_t count) {
    if (count && Enabled() && count <= 8u * 1024u * 1024u) {
      if (auto* state = LocalScratch(); state && !state->busy) { owner = state; buffer = &state->bytes; }
    }
    const size_t dirty = std::min<size_t>(buffer->size(), count);
    buffer->resize(count);
    std::fill(buffer->begin(), buffer->begin() + dirty, uint8_t(0));
    if (owner) owner->busy = true;
  }
  ~ReadBuffer() { if (owner) owner->busy = false; }
  ReadBuffer(const ReadBuffer&) = delete;
  ReadBuffer& operator=(const ReadBuffer&) = delete;
  uint8_t* data() { return buffer->data(); }
};
}
