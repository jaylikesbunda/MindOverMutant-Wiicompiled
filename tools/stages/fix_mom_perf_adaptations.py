"""MoM-specific adaptations to the ported CotT performance stages.

CotT's stages were written against CotT's runtime tree. Four of them need a
MoM-specific edit before they can be applied here; each edit is idempotent and
is applied to the *copied* stage script in tools/stages, not to CotT's.

A1  fix_gx_fifo_batch installs the publish-at-poll hook with the probe
    ``if 'native_gx_publish();' not in t:``.  The extern declaration the same
    stage adds just above it already contains that substring, so the probe
    always fails and the call is never installed.  Without it the guest can
    leave up to 4 KiB of FIFO bytes unpublished while it waits on the GPU and
    the renderer never sees them, so the guest can hang.  The probe is
    narrowed to the call site.

A2  fix_clock_cache invalidates the cached guest clock after the OS idle
    park.  Its anchor is CotT's bare ``native_idle::wait(...)`` line; MoM's
    line is ``if (ticks) native_idle::wait(...)``, so the invalidation would
    be skipped and the clock would be stale across every park (the one place
    where guest time jumps without the instruction count moving).

A3  fix_ios_bulk_transfer installs CotT's native_guest_bytes.h, which needs
    MMU::kMem1Limit / kMem2Limit and native_watch_span_may_hit - symbols that
    only exist after CotT's fix_mmu_constant_limits and
    fix_watch_span_guards stages, neither of which applies to MoM.  MoM gets
    its own overlay that derives the RAM extents from the live vectors.

A4  MoM has no native_trace namespace (CotT's frame-trace instrumentation).
    The calls are pure diagnostics, so they are stripped.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
stages = port / 'tools' / 'stages'


def edit(name, old, new, count=1):
    path = stages / (name + '.py')
    text = path.read_text()
    if new in text:
        return False
    assert text.count(old) == count, (name, text.count(old), old[:60])
    path.write_text(text.replace(old, new, count))
    return True


# --- A1 ---------------------------------------------------------------------
edit('fix_gx_fifo_batch',
     "if 'native_gx_publish();' not in t:",
     "# NATIVE_MOM_ADAPT_A1: the extern declaration added just above contains the\n"
     "# substring 'native_gx_publish();', so the original probe always failed and\n"
     "# the call was never installed. Probe the call site instead.\n"
     "if '\\n  native_gx_publish();' not in t:")

# --- A2 ---------------------------------------------------------------------
edit('fix_clock_cache',
     "IOS_OLD = '        native_idle::wait(ticks, ctx.tb_freq, pending);'",
     "# NATIVE_MOM_ADAPT_A2: MoM's park line is 'if (ticks) native_idle::wait(...)'.\n"
     "IOS_OLD = '        if (ticks) native_idle::wait(ticks, ctx.tb_freq, pending);'")

# --- A4 ---------------------------------------------------------------------
edit('fix_program_binary_cache',
     'if(const GLuint cached=cache.load(cache_key)){native_trace::mark("shader.binary_hit");return cached;}',
     'if(const GLuint cached=cache.load(cache_key)){return cached;} // NATIVE_MOM_ADAPT_A4')
edit('fix_used_texture_binding',
     "s=s.replace('if(!(used&(1u<<i))){native_trace::mark(\"texture.unused_slot\");continue;}', 'if(!(used&(1u<<i)))continue;')",
     "s=s.replace('if(!(used&(1u<<i))){native_trace::mark(\"texture.unused_slot\");continue;}', 'if(!(used&(1u<<i)))continue;')  # NATIVE_MOM_ADAPT_A4")
edit('fix_display_list_gpu_ownership',
     "s=s.replace('snapshot->bytes.clear();native_trace::mark(\"decode.gpu_vertices\",c.vertices.size());','native_trace::mark(\"decode.gpu_vertices\",c.vertices.size());')",
     "s=s.replace('snapshot->bytes.clear();native_trace::mark(\"decode.gpu_vertices\",c.vertices.size());','')  # NATIVE_MOM_ADAPT_A4")
edit('fix_parallel_decode',
     "if (native_trace::enabled()) native_trace::local_buffer();",
     "")
edit('fix_early_gpu_signal',
     "native_trace::mark(\"pe.early_signal\", 0x45, resolved);",
     "")
edit('fix_early_gpu_signal',
     "native_trace::mark(\"pe.early_signal\", reg, resolved & 0xffffu);",
     "")

# --- A3: the MoM replacement overlay ----------------------------------------
overlay = port / 'overlays' / 'native_guest_bytes.h'
overlay.write_text('''#pragma once
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
''')
print('MoM adaptations applied to the ported CotT performance stages.')
