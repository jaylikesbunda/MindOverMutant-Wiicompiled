"""Port CotT's cheap guest-CPU memory work into MoM's guest memory lane.

Marker: NATIVE_MOM_GUEST_MEMORY_LANE_V1.  Everything here is host-side: the same
values are stored, every poll still runs, every watch still fires, and each item
is independently switchable at run time through one environment variable.

Reference stages (read for the *idea*; none of them applies to this tree because
they patch CotT's ``build/nwii-full-source`` and its page-table MMU):

  native-port/tools/fix_multiple_word_spans.py    one RAM proof per lmw/stmw
  native-port/tools/fix_broad_memory_decode.py    one index-domain proof per draw
  native-port/tools/fix_page_zero_reads.py        sentinel page inline reads
  native-port/tools/fix_mem2_inline.py            MEM2 gets MEM1's inline path
  native-port/tools/fix_guest_fill_blocks.py      one proof per fill iteration
  native-port/tools/fix_psq_pair_f32.py           one guard per F32 pair

What this tree already had (so those items are *not* ported here):

  * Page zero / the 0x0C sentinel: already inline.  MoM has no page table and no
    ``page_bias`` (CotT's fix_page_zero_reads anchors on it), so CotT's mechanism
    does not exist here at all; instead ``read32``/``write32`` already special-case
    physical 0x0C inside the always-inline MEM1 fast path
    (include/runtime/cpu_context.h:183 and :206).
  * Ordinary MEM1 scalar reads/writes: NATIVE_INLINE_RAM_ACCESS_V1
    (include/runtime/cpu_context.h:92-188), i.e. CotT's fix_broad_memory_decode
    first half.
  * The whole-draw ``Direct`` vertex specialisation: NATIVE_DIRECT_VERTEX_DECODE_V1
    (src/hle/gx/fifo_parser.cpp:929-938), i.e. the second half of the same stage.
  * PSQ paired-single aliasing and the F32 GQR type split:
    NATIVE_PS_BROADCAST_ALIAS_V1 / NATIVE_GQR_EXPONENT_V1
    (include/runtime/cpu_context.h:513-584): psq_load/psq_store already avoid the
    scale decode for type 0.

What this tree genuinely lacks, and what this stage adds:

  1. ``words``   lmw/stmw: one ordinary-RAM proof per run instead of one per
                 register.  216 sites in export/ (122 lmw, 94 stmw); the reference
                 amortises only over >= 16 words because the proof loses on
                 one- and two-word transfers, which selects 62 of them here.
  2. ``decode``  the indexed vertex arrays: one index-domain proof per draw
                 instead of one ``get_ptr`` per attribute per vertex.
                 ``ReadVectorAttr``/``ReadColorAttr`` (src/hle/gx/fifo_parser.cpp:381,
                 :484) resolved the bank per vertex; the domain is fixed for the
                 whole draw, so it is proved once and the per-vertex lookup becomes
                 pointer arithmetic.  Deliberately *not* ported from the same
                 reference stage: replacing ``reserve(count)`` + ``push_back`` with
                 ``resize(count)`` + a reference.  MoM's decoder returns early in
                 the middle of a vertex, so with an up-front resize a failed draw
                 would report ``count`` vertices where it reports fewer today, and
                 ``c.vertices.empty()`` is what the parser uses to detect that.
  3. ``mem2``    MEM2 had no inline scalar path at all: every MEM2 load or store
                 fell out of the MEM1 test into an out-of-line call, which is why
                 ``MMU::native_read32_slow`` is 2.49% of the guest thread
                 (logs/play-cpuprof.cpuprofile.md:31).  Same guarded shape as the
                 MEM1 path, same ``size()-8`` padding bound, same watch rule.
  4. ``fill``    one fill iteration's stores combined into one proof.  CotT's
                 fix_guest_fill_blocks anchors on one iteration of a loop at
                 0x80006004; MoM's word-fill routine is func_8040E71C, whose bdnz
                 body is the same eight stores (0x8040E738..0x8040E754, with the
                 poll and the back edge at 0x8040E75C).  The stage finds that shape
                 wherever it appears -- 209 runs of >= 8 consecutive ``write32`` to
                 one base with a stepping offset, 2341 words: loop bodies like that
                 one and straight-line clears like the 60-word block in
                 func_800AC44C -- and combines each run, keeping every label, poll
                 and instruction-count line exactly where it was.
  5. ``psq``     a W==0 plain-F32 psq pair did two independent 4-byte accesses with
                 two address proofs; one 8-byte access in ordinary RAM, everything
                 else unchanged.

Switch: ``NWII_MOM_LANE`` is a comma-separated subset of
``words,decode,mem2,fill,psq``; unset (or ``all``) means all five, ``none`` means
none.  Examples: ``NWII_MOM_LANE=mem2`` for one item alone, ``NWII_MOM_LANE=none``
for the reference build of the whole lane.

Idempotent: every patch records what it replaced (the vendor edits as an exact
inverse pair, the generated-code edits base64-encoded in the marker comment), so
``apply`` starts from ``restore`` and a second run is a byte-identical no-op.
``--restore`` puts the tree back exactly as it was.

The stage edits export/, so it must run after every stage that rewrites the
generated guest code (fix_prune_entry_cases_mom, fix_inst_count_batch_mom,
fix_dispatcher_o2_mom, fix_dispatch_hot_cache_mom, promote_regs_mom) and after the
fifo_parser/cpu_context stages; see the registration note in the report.
"""
from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

MARKER = 'NATIVE_MOM_GUEST_MEMORY_LANE_V1'

ROOT = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
CONTEXT = RUNTIME + '/include/runtime/cpu_context.h'
LANE = RUNTIME + '/include/runtime/native_mom_lane.h'
PARSER = RUNTIME + '/src/hle/gx/fifo_parser.cpp'

# ---------------------------------------------------------------------------
# The runtime header this stage installs.
#
# One header, included from cpu_context.h after <cstdlib>: it declares the
# switches and the span proofs.  The span proofs are templates on the memory
# type, because they are used by MMU's own always-inline fast paths, by
# CPUContext::psq_load/psq_store and by the generated guest code -- all of which
# are compiled *after* `struct MMU` is complete, while this header is parsed
# before it.
# ---------------------------------------------------------------------------
LANE_HEADER = r'''#pragma once
// NATIVE_MOM_GUEST_MEMORY_LANE_V1 -- measured, switchable host-side memory work.
//
// Installed by tools/stages/fix_guest_memory_lane_mom.py.  Read that file's
// docstring for the evidence behind each item.
//
// Switches (read once, at static-initialisation time):
//     NWII_MOM_LANE=words,decode,mem2,fill,psq    the five items by name
//     NWII_MOM_LANE=all / unset                  all five (default)
//     NWII_MOM_LANE=none                         none of them
// An unrecognised name selects nothing, so a typo leaves the reference lane.
//
// Nothing here can change a guest-visible value:
//   * a span proof is refused for every address the per-word MMU path has to
//     handle itself (see Span()), and the caller then runs the original
//     statement sequence, which is still in the generated code;
//   * a refused proof is the *whole* run: the items never split a run between
//     the two paths, so ordering, watches and the 0x0C sentinel stay exact.
#include <cstdint>
#include <cstdlib>
#include <cstring>

namespace nwii::runtime {
// Declared (and defined) by cpu_context.h, which includes this header before the
// MMU.  Forward-declared here so the span proofs can read the live watch state:
// a store run must keep the per-word path whenever a watch is armed.
extern uint32_t g_watch_addr;
}

namespace nwii::runtime::native_mom_lane {

// ---------------------------------------------------------------------------
// Switches.  One dynamically initialised object, so no path pays a
// thread-safe-static guard and there is no initialisation order to reason about
// between five separate flags.
// ---------------------------------------------------------------------------
enum Item : unsigned {
    kWords  = 1u << 0,
    kDecode = 1u << 1,
    kMem2   = 1u << 2,
    kFill   = 1u << 3,
    kPsq    = 1u << 4,
    kAll    = kWords | kDecode | kMem2 | kFill | kPsq,
};

inline unsigned Parse(const char *text) {
    if (!text || !*text) return kAll;
    unsigned mask = 0;
    for (const char *p = text; *p;) {
        while (*p == ',' || *p == ' ' || *p == '\t') ++p;
        const char *begin = p;
        while (*p && *p != ',' && *p != ' ' && *p != '\t') ++p;
        const unsigned length = unsigned(p - begin);
        if (length == 4 && std::memcmp(begin, "none", 4) == 0) return 0;
        else if (length == 3 && std::memcmp(begin, "all", 3) == 0) return kAll;
        else if (length == 5 && std::memcmp(begin, "words", 5) == 0) mask |= kWords;
        else if (length == 6 && std::memcmp(begin, "decode", 6) == 0) mask |= kDecode;
        else if (length == 4 && std::memcmp(begin, "mem2", 4) == 0) mask |= kMem2;
        else if (length == 4 && std::memcmp(begin, "fill", 4) == 0) mask |= kFill;
        else if (length == 3 && std::memcmp(begin, "psq", 3) == 0) mask |= kPsq;
    }
    return mask;
}

struct Switches { unsigned mask; };
inline Switches Mix() { Switches s; s.mask = Parse(std::getenv("NWII_MOM_LANE")); return s; }
inline const Switches g_switches = Mix();

inline bool On(Item item) { return (g_switches.mask & unsigned(item)) != 0; }
inline bool WordsOn()  { return On(kWords); }
inline bool DecodeOn() { return On(kDecode); }
inline bool Mem2On()   { return On(kMem2); }
inline bool FillOn()   { return On(kFill); }
inline bool PsqOn()    { return On(kPsq); }

// ---------------------------------------------------------------------------
// One ordinary-RAM proof for a whole run of words.
//
// Returns a host pointer only when every byte of [address, address+bytes) maps
// to the same ordinary bank, with the *same* bounds the per-word fast path uses
// (`size() - 8`).  It is refused -- and the caller keeps the per-word path --
// for:
//
//   * an armed watch: a watch must see every individual write, in order;
//   * the first 16 physical bytes, which contain the 0x0C sentinel word that
//     read32 zeroes and write32 drops;
//   * MEM2 outside its padding limit, the MMIO windows, the GX write-gather page
//     (none of them is inside either bank, so the bank tests refuse them);
//   * a run that wraps.  A run never wraps here: a run that would cross 2^32 (or
//     the 0x3FFFFFFF address mask) needs `address > 0xFFFFFFE0`, i.e.
//     `physical > 0x3FFFFFE0`, which neither bank limit accepts.
//
// `Memory` is nwii::runtime::MMU; it is a template so this header can be parsed
// before `struct MMU` is defined.
template <class Memory>
inline uint8_t *Span(Memory &memory, uint32_t address, uint32_t bytes, bool watched) {
    if (watched || bytes == 0) return nullptr;
    const uint32_t physical = address & 0x3fffffffu;
    if (physical < 0x10u) return nullptr;
    const uint64_t mem1_limit = memory.mem1.size() - 8;
    if (uint64_t(physical) + bytes <= mem1_limit) return memory.mem1.data() + physical;
    const uint64_t mem2_limit = memory.mem2.size() - 8;
    if (physical >= 0x10000000u &&
        uint64_t(physical) - 0x10000000u + bytes <= mem2_limit)
        return memory.mem2.data() + (physical - 0x10000000u);
    return nullptr;
}

// Guest words are big-endian; one swap per word, no per-word address work.
inline uint32_t Load(const uint8_t *pointer) {
    uint32_t bits;
    std::memcpy(&bits, pointer, 4);
    return __builtin_bswap32(bits);
}
inline void Store(uint8_t *pointer, uint32_t value) {
    const uint32_t bits = __builtin_bswap32(value);
    std::memcpy(pointer, &bits, 4);
}

// lmw / stmw: the proof is only worth its own compare for >= 16 words (the
// reference measured the break-even; below it the per-word path wins).
template <class Memory>
inline uint8_t *SpanWordsLoad(Memory &memory, uint32_t address, uint32_t bytes) {
    if (!WordsOn()) return nullptr;
    return Span(memory, address, bytes, false);
}
template <class Memory>
inline uint8_t *SpanWordsStore(Memory &memory, uint32_t address, uint32_t bytes) {
    if (!WordsOn()) return nullptr;
    return Span(memory, address, bytes, nwii::runtime::g_watch_addr != 0);
}

// The same guest word repeated `words` times, in ascending address order.
inline void FillWords(uint8_t *destination, unsigned words, uint32_t value) {
    if (value == (value & 255u) * 0x01010101u) {   // 0x00000000, 0xVVVVVVVV
        std::memset(destination, int(value & 255u), size_t(words) * 4u);
        return;
    }
    const uint32_t big_endian = __builtin_bswap32(value);
    const uint32_t pattern[4] = {big_endian, big_endian, big_endian, big_endian};
    unsigned offset = 0;
    const unsigned total = words * 4u;
    for (; offset + 16u <= total; offset += 16u)
        std::memcpy(destination + offset, pattern, 16);
    for (; offset < total; offset += 4u)
        std::memcpy(destination + offset, &big_endian, 4);
}

// One straight-line fill run: prove the span, write every word, and report
// whether the caller may skip its own per-word stores.  Every word is a store,
// so an armed watch refuses it.  Returns false (caller stores per word) for
// anything Span() refuses.
template <class Memory>
inline bool FillRun(Memory &memory, uint32_t address, unsigned words, uint32_t value) {
    if (!FillOn() || words == 0) return false;
    uint8_t *span = Span(memory, address, words * 4u, nwii::runtime::g_watch_addr != 0);
    if (!span) return false;
    FillWords(span, words, value);
    return true;
}

// psq: a W==0 plain-F32 pair is two adjacent floats.
template <class Memory>
inline uint8_t *SpanPairLoad(Memory &memory, uint32_t address) {
    if (!PsqOn()) return nullptr;
    return Span(memory, address, 8u, false);
}
template <class Memory>
inline uint8_t *SpanPairStore(Memory &memory, uint32_t address) {
    if (!PsqOn()) return nullptr;
    return Span(memory, address, 8u, nwii::runtime::g_watch_addr != 0);
}

}  // namespace nwii::runtime::native_mom_lane
'''

# ---------------------------------------------------------------------------
# cpu_context.h.  Each edit is one exact (old, new) pair, so `restore` is the
# exact inverse and `apply` is a pure function of the restored text.
# ---------------------------------------------------------------------------

MEM1_READ8 = """    if (__builtin_expect(paddr < mem1.size() - 8, 1)) return mem1[paddr];
    return native_read8_slow(addr);"""

MEM2_READ8 = """    if (__builtin_expect(paddr < mem1.size() - 8, 1)) return mem1[paddr];
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): same bounded ordinary-RAM path.
    if (native_mom_lane::Mem2On()) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) return mem2[m2];
    }
    return native_read8_slow(addr);"""

MEM1_READ16 = """      uint16_t bits; std::memcpy(&bits, mem1.data() + paddr, 2);
      return __builtin_bswap16(bits);
    }
    return native_read16_slow(addr);"""

MEM2_READ16 = """      uint16_t bits; std::memcpy(&bits, mem1.data() + paddr, 2);
      return __builtin_bswap16(bits);
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): same bounded ordinary-RAM path.
    if (native_mom_lane::Mem2On()) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint16_t bits; std::memcpy(&bits, mem2.data() + m2, 2);
        return __builtin_bswap16(bits);
      }
    }
    return native_read16_slow(addr);"""

MEM1_READ32 = """      if (paddr == 0x0000000C) return 0;
      uint32_t bits; std::memcpy(&bits, mem1.data() + paddr, 4);
      return __builtin_bswap32(bits);
    }
    return native_read32_slow(addr);"""

MEM2_READ32 = """      if (paddr == 0x0000000C) return 0;
      uint32_t bits; std::memcpy(&bits, mem1.data() + paddr, 4);
      return __builtin_bswap32(bits);
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): same bounded ordinary-RAM path.
    if (native_mom_lane::Mem2On()) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint32_t bits; std::memcpy(&bits, mem2.data() + m2, 4);
        return __builtin_bswap32(bits);
      }
    }
    return native_read32_slow(addr);"""

MEM1_READ64 = """      uint64_t bits; std::memcpy(&bits, mem1.data() + paddr, 8);
      return __builtin_bswap64(bits);
    }
    return native_read64_slow(addr);"""

MEM2_READ64 = """      uint64_t bits; std::memcpy(&bits, mem1.data() + paddr, 8);
      return __builtin_bswap64(bits);
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): same bounded ordinary-RAM path.
    if (native_mom_lane::Mem2On()) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint64_t bits; std::memcpy(&bits, mem2.data() + m2, 8);
        return __builtin_bswap64(bits);
      }
    }
    return native_read64_slow(addr);"""

MEM1_WRITE8 = """      uint8_t bits = value; std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write8_slow(addr, value);"""

MEM2_WRITE8 = """      uint8_t bits = value; std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): the watch test above still keeps
    // every watched store on the byte path.
    if (native_mom_lane::Mem2On() && g_watch_addr == 0) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint8_t bits = value; std::memcpy(mem2.data() + m2, &bits, sizeof(bits));
        return;
      }
    }
    native_write8_slow(addr, value);"""

MEM1_WRITE16 = """      uint16_t bits = value; bits = __builtin_bswap16(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write16_slow(addr, value);"""

MEM2_WRITE16 = """      uint16_t bits = value; bits = __builtin_bswap16(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): the watch test above still keeps
    // every watched store on the byte path.
    if (native_mom_lane::Mem2On() && g_watch_addr == 0) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint16_t bits = value; bits = __builtin_bswap16(bits);
        std::memcpy(mem2.data() + m2, &bits, sizeof(bits));
        return;
      }
    }
    native_write16_slow(addr, value);"""

MEM1_WRITE32 = """      uint32_t bits = value; bits = __builtin_bswap32(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write32_slow(addr, value);"""

MEM2_WRITE32 = """      uint32_t bits = value; bits = __builtin_bswap32(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): the watch test above still keeps
    // every watched store on the byte path.
    if (native_mom_lane::Mem2On() && g_watch_addr == 0) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint32_t bits = value; bits = __builtin_bswap32(bits);
        std::memcpy(mem2.data() + m2, &bits, sizeof(bits));
        return;
      }
    }
    native_write32_slow(addr, value);"""

MEM1_WRITE_F32 = """      uint32_t bits; std::memcpy(&bits, &value, sizeof(bits));
      bits = __builtin_bswap32(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write_f32_slow(addr, value);"""

MEM2_WRITE_F32 = """      uint32_t bits; std::memcpy(&bits, &value, sizeof(bits));
      bits = __builtin_bswap32(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): the watch test above still keeps
    // every watched store on the byte path.
    if (native_mom_lane::Mem2On() && g_watch_addr == 0) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint32_t bits; std::memcpy(&bits, &value, sizeof(bits));
        bits = __builtin_bswap32(bits);
        std::memcpy(mem2.data() + m2, &bits, sizeof(bits));
        return;
      }
    }
    native_write_f32_slow(addr, value);"""

MEM1_WRITE64 = """      uint64_t bits = value; bits = __builtin_bswap64(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write64_slow(addr, value);"""

MEM2_WRITE64 = """      uint64_t bits = value; bits = __builtin_bswap64(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): the watch test above still keeps
    // every watched store on the byte path.
    if (native_mom_lane::Mem2On() && g_watch_addr == 0) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint64_t bits = value; bits = __builtin_bswap64(bits);
        std::memcpy(mem2.data() + m2, &bits, sizeof(bits));
        return;
      }
    }
    native_write64_slow(addr, value);"""

# write_f64 has no fast path at all today: it always walks get_ptr.
WRITE_F64_OLD = """    uint64_t v;
    std::memcpy(&v, &value, sizeof(double));"""

WRITE_F64_NEW = """    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): one proof for MEM1 and MEM2.  The
    // watch test below only fires for a watched address, which is exactly the
    // condition under which the mixed fast path is skipped.
    if (native_mom_lane::Mem2On() && g_watch_addr == 0) {
      const uint32_t m2 = paddr - 0x10000000u;
      if (__builtin_expect(m2 < mem2.size() - 8, 0)) {
        uint64_t bits; std::memcpy(&bits, &value, sizeof(bits));
        bits = __builtin_bswap64(bits);
        std::memcpy(mem2.data() + m2, &bits, sizeof(bits));
        return;
      }
    }
    uint64_t v;
    std::memcpy(&v, &value, sizeof(double));"""

READ_F64_OLD = """  double read_f64(uint32_t addr) { 
      uint8_t *ptr = get_ptr(addr);
      if (ptr) {
          uint64_t val = ((uint64_t)ptr[0] << 56) | ((uint64_t)ptr[1] << 48) | ((uint64_t)ptr[2] << 40) | ((uint64_t)ptr[3] << 32) |
                         ((uint64_t)ptr[4] << 24) | ((uint64_t)ptr[5] << 16) | ((uint64_t)ptr[6] << 8) | ptr[7];
          double f; std::memcpy(&f, &val, 8); return f;
      }
      return 0.0;
  }"""

# Same bytes: read64 is the same big-endian assembly, and both return 0.0 for an
# address outside both banks.  read64 has no 0x0C sentinel, and neither had this.
READ_F64_NEW = """  // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (mem2): bit-identical to the byte assembly
  // this replaces, through the scalar path that now covers MEM1 and MEM2.
  __attribute__((always_inline)) inline double read_f64(uint32_t addr) {
      uint64_t value = read64(addr);
      double result; std::memcpy(&result, &value, 8); return result;
  }"""

READ_F32_OLD = """  float read_f32(uint32_t addr) { uint32_t val = read32(addr); float f; std::memcpy(&f, &val, 4); return f; }"""
READ_F32_NEW = """  __attribute__((always_inline)) inline float read_f32(uint32_t addr) { uint32_t val = read32(addr); float f; std::memcpy(&f, &val, 4); return f; }"""

PSQ_LOAD_OLD = """    fpr[frD] = load_element(addr);"""
PSQ_LOAD_NEW = """    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (psq): one proof and one 8-byte load for a
    // plain-F32 W==0 pair in ordinary RAM; every other form keeps both reads.
    if (ld_type == 0 && W == 0) {
      if (const uint8_t* native_pair = native_mom_lane::SpanPairLoad(mmu, addr)) {
        uint64_t raw; std::memcpy(&raw, native_pair, 8);
        const uint32_t lane0 = __builtin_bswap32(uint32_t(raw));
        const uint32_t lane1 = __builtin_bswap32(uint32_t(raw >> 32));
        float first, second;
        std::memcpy(&first, &lane0, 4);
        std::memcpy(&second, &lane1, 4);
        fpr[frD] = first;
        ps1[frD] = second;
        return;
      }
    }
    fpr[frD] = load_element(addr);"""

PSQ_STORE_OLD = """    store_element(addr, fpr[frS]);"""
PSQ_STORE_NEW = """    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (psq): the same two values, stored as one
    // 8-byte pair when both words are proven ordinary RAM; W!=0 stores one.
    if (st_type == 0 && W == 0) {
      if (uint8_t* native_pair = native_mom_lane::SpanPairStore(mmu, addr)) {
        float first = (float)fpr[frS], second = (float)ps1[frS];
        uint32_t lane0, lane1;
        std::memcpy(&lane0, &first, 4);
        std::memcpy(&lane1, &second, 4);
        native_mom_lane::Store(native_pair, lane0);
        native_mom_lane::Store(native_pair + 4, lane1);
        return;
      }
    }
    store_element(addr, fpr[frS]);"""

CONTEXT_EDITS = [
    # The header itself.
    ('#include <cstdlib>\n// NATIVE_DIAGNOSTIC_CACHE_BEGIN',
     '#include <cstdlib>\n#include "runtime/native_mom_lane.h" // ' + MARKER +
     '\n// NATIVE_DIAGNOSTIC_CACHE_BEGIN'),
    # mem2: every scalar width, reads and writes, plus the two f64 forms.
    (MEM1_READ8, MEM2_READ8),
    (MEM1_READ16, MEM2_READ16),
    (MEM1_READ32, MEM2_READ32),
    (MEM1_READ64, MEM2_READ64),
    (MEM1_WRITE8, MEM2_WRITE8),
    (MEM1_WRITE16, MEM2_WRITE16),
    (MEM1_WRITE32, MEM2_WRITE32),
    (MEM1_WRITE_F32, MEM2_WRITE_F32),
    (MEM1_WRITE64, MEM2_WRITE64),
    (WRITE_F64_OLD, WRITE_F64_NEW),
    (READ_F64_OLD, READ_F64_NEW),
    (READ_F32_OLD, READ_F32_NEW),
    # psq: one access per F32 pair.
    (PSQ_LOAD_OLD, PSQ_LOAD_NEW),
    (PSQ_STORE_OLD, PSQ_STORE_NEW),
]

# ---------------------------------------------------------------------------
# fifo_parser.cpp: the draw-local index-domain proof.
# ---------------------------------------------------------------------------

RESOLVER = '''    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (decode): the index domain of an indexed
    // attribute is fixed for the whole draw, so prove it once instead of running
    // a RAM dispatch per attribute per vertex.  A refused proof leaves that draw
    // on the per-vertex path.
    struct NativeIndexSpan {
        const uint8_t* data = nullptr;
        uint32_t stride = 0;
    };
    NativeIndexSpan ResolveIndexSpan(VtxAttrMask mask, uint32_t array_base,
                                     uint32_t array_stride, uint32_t element_bytes) {
        NativeIndexSpan span;
        if (!native_mom_lane::DecodeOn() || !nwii::runtime::g_mmu) return span;
        if (mask != VtxAttrMask::Index8 && mask != VtxAttrMask::Index16) return span;
        const uint64_t last = uint64_t(mask == VtxAttrMask::Index8 ? 255u : 65535u) * array_stride;
        if (uint64_t(array_base) + last + element_bytes > 0x100000000ull) return span;
        const uint32_t physical = array_base & 0x3fffffffu;
        if (uint64_t(physical) + last + element_bytes > 0x40000000ull) return span;
        nwii::runtime::MMU& memory = *nwii::runtime::g_mmu;
        const uint64_t mem1_limit = memory.mem1.size() - 8;
        const uint64_t mem2_limit = memory.mem2.size() - 8;
        if (uint64_t(physical) + last + element_bytes <= mem1_limit) {
            span.data = memory.mem1.data() + physical;
        } else if (physical >= 0x10000000u &&
                   uint64_t(physical) - 0x10000000u + last + element_bytes <= mem2_limit) {
            span.data = memory.mem2.data() + (physical - 0x10000000u);
        }
        span.stride = array_stride;
        return span;
    }

'''

VEC_BODY_OLD = ('        if (!nwii::runtime::g_mmu) return true;\n'
                '        uint32_t base = array_base + index * array_stride;')
VEC_BODY_NEW = ('        const uint8_t* p;\n'
                '        if (native_span) {\n'
                '            p = native_span + (size_t)index * native_span_stride;\n'
                '        } else {\n'
                '        if (!nwii::runtime::g_mmu) return true;\n'
                '        uint32_t base = array_base + index * array_stride;')

GETPTR_OLD = ('        const uint8_t* p = nwii::runtime::g_mmu->get_ptr(base);\n'
              '        if (!p) return true;')
GETPTR_NEW = ('        p = nwii::runtime::g_mmu->get_ptr(base);\n'
              '        if (!p) return true;\n'
              '        } // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (decode)')

VEC_SIG_OLD = ('                        uint32_t array_base, uint32_t array_stride, int ncomp, float* out) {')
VEC_SIG_NEW = ('                        uint32_t array_base, uint32_t array_stride, int ncomp, float* out,\n'
               '                        const uint8_t* native_span = nullptr,\n'
               '                        uint32_t native_span_stride = 0u) { // ' + MARKER)
CLR_SIG_OLD = ('                       uint32_t array_base, uint32_t array_stride, float out[4]) {')
CLR_SIG_NEW = ('                       uint32_t array_base, uint32_t array_stride, float out[4],\n'
               '                       const uint8_t* native_span = nullptr,\n'
               '                       uint32_t native_span_stride = 0u) { // ' + MARKER)

SPAN_RESOLVE = '''    // NATIVE_MOM_GUEST_MEMORY_LANE_V1 (decode): one index-domain proof per draw.
    const NativeIndexSpan native_pos_span = ResolveIndexSpan(
        r.vat.posMask, r.arrayBase[0], r.arrayStride[0],
        uint32_t(TypeSize(r.vat.posType)) * (r.vat.posElements ? 3u : 2u));
    const NativeIndexSpan native_nrm_span = ResolveIndexSpan(
        r.vat.nrmMask, r.arrayBase[1], r.arrayStride[1],
        uint32_t(TypeSize(r.vat.nrmType)) * (r.vat.nrmElements ? 9u : 3u));
    const NativeIndexSpan native_clr_span[2] = {
        ResolveIndexSpan(r.vat.clrMask[0], r.arrayBase[2], r.arrayStride[2],
                         uint32_t(ColorBytes(r.vat.clrType[0]))),
        ResolveIndexSpan(r.vat.clrMask[1], r.arrayBase[3], r.arrayStride[3],
                         uint32_t(ColorBytes(r.vat.clrType[1])))};
    NativeIndexSpan native_tex_span[8];
    for (unsigned native_slot = 0; native_slot < 8; ++native_slot) {
        native_tex_span[native_slot] = ResolveIndexSpan(
            r.vat.texMask[native_slot], r.arrayBase[4 + native_slot],
            r.arrayStride[4 + native_slot],
            uint32_t(TypeSize(r.vat.texType[native_slot])) *
                (r.vat.texElements[native_slot] ? 2u : 1u));
    }
'''

POS_CALL_OLD = '''        if (!ReadVectorAttr(buf, off, buf_size, r.vat.posMask, r.vat.posType,
                            r.vat.posShift, r.arrayBase[0], r.arrayStride[0],
                            r.vat.posElements ? 3 : 2, vtx.pos))'''
POS_CALL_NEW = '''        if (!ReadVectorAttr(buf, off, buf_size, r.vat.posMask, r.vat.posType,
                            r.vat.posShift, r.arrayBase[0], r.arrayStride[0],
                            r.vat.posElements ? 3 : 2, vtx.pos,
                            native_pos_span.data, native_pos_span.stride)) // ''' + MARKER

NRM_CALL_OLD = '''            if (!ReadVectorAttr(buf, off, buf_size, r.vat.nrmMask, r.vat.nrmType,
                                NativeNormalFracBits(r.vat.nrmType), // NATIVE_MOM_NORMAL_SCALE_V1
                                r.arrayBase[1], r.arrayStride[1],
                                r.vat.nrmElements ? 9 : 3, nbt))'''
NRM_CALL_NEW = '''            if (!ReadVectorAttr(buf, off, buf_size, r.vat.nrmMask, r.vat.nrmType,
                                NativeNormalFracBits(r.vat.nrmType), // NATIVE_MOM_NORMAL_SCALE_V1
                                r.arrayBase[1], r.arrayStride[1],
                                r.vat.nrmElements ? 9 : 3, nbt,
                                native_nrm_span.data, native_nrm_span.stride)) // ''' + MARKER

CLR0_CALL_OLD = '''            if (!ReadColorAttr(buf, off, buf_size, r.vat.clrMask[0],
                               r.vat.clrType[0], r.arrayBase[2],
                               r.arrayStride[2], col))'''
CLR0_CALL_NEW = '''            if (!ReadColorAttr(buf, off, buf_size, r.vat.clrMask[0],
                               r.vat.clrType[0], r.arrayBase[2],
                               r.arrayStride[2], col,
                               native_clr_span[0].data, native_clr_span[0].stride)) // ''' + MARKER

CLR1_CALL_OLD = '''            if (!ReadColorAttr(buf,off,buf_size,r.vat.clrMask[1],r.vat.clrType[1],r.arrayBase[3],r.arrayStride[3],vtx.color1)) return;'''
CLR1_CALL_NEW = '''            if (!ReadColorAttr(buf,off,buf_size,r.vat.clrMask[1],r.vat.clrType[1],r.arrayBase[3],r.arrayStride[3],vtx.color1,
                               native_clr_span[1].data, native_clr_span[1].stride)) return; // ''' + MARKER

TEX_CALL_OLD = '''            if (!ReadVectorAttr(buf,off,buf_size,r.vat.texMask[t],r.vat.texType[t],r.vat.texShift[t],r.arrayBase[4+t],r.arrayStride[4+t],r.vat.texElements[t]?2:1,vtx.tex[t])) return;'''
TEX_CALL_NEW = '''            if (!ReadVectorAttr(buf,off,buf_size,r.vat.texMask[t],r.vat.texType[t],r.vat.texShift[t],r.arrayBase[4+t],r.arrayStride[4+t],r.vat.texElements[t]?2:1,vtx.tex[t],
                                native_tex_span[t].data, native_tex_span[t].stride)) return; // ''' + MARKER

RESOLVE_OLD = '''    size_t off = 0;
    c.vertices.reserve(r.count);'''
RESOLVE_NEW = ('''    size_t off = 0;
''' + SPAN_RESOLVE + '''    c.vertices.reserve(r.count);''')

VEC_HEAD_OLD = '    bool ReadVectorAttr(const std::vector<uint8_t>& fifo, size_t& fifo_offset, size_t fifo_size,\n'

PARSER_EDITS = [
    (VEC_HEAD_OLD, RESOLVER + VEC_HEAD_OLD),
    # Both readers: signature, span branch, and one decorative `//` for the
    # closing brace (the marker itself lives on the signature line).
    (VEC_SIG_OLD, VEC_SIG_NEW),
    (CLR_SIG_OLD, CLR_SIG_NEW),
    (VEC_BODY_OLD, VEC_BODY_NEW),      # exactly twice: ReadVectorAttr and ReadColorAttr
    (GETPTR_OLD, GETPTR_NEW),          # exactly twice
    # One resolution per draw in NativeDecodeDraw.
    (RESOLVE_OLD, RESOLVE_NEW),
    (POS_CALL_OLD, POS_CALL_NEW),
    (NRM_CALL_OLD, NRM_CALL_NEW),
    (CLR0_CALL_OLD, CLR0_CALL_NEW),
    (CLR1_CALL_OLD, CLR1_CALL_NEW),
    (TEX_CALL_OLD, TEX_CALL_NEW),
]

# Edits that occur exactly twice in the file (the two readers share the shape).
PARSER_DOUBLE = {VEC_BODY_OLD, GETPTR_OLD}

# ---------------------------------------------------------------------------
# Generated guest code (export/*.cpp).
# ---------------------------------------------------------------------------

# lmw/stmw, verbatim from the reference stage (MoM emits the same shape).
WORD_PATTERN = re.compile(
    r'    \{\n'
    r'        uint32_t addr = (?P<address>[^;\n]+);\n'
    r'        for \(int i = (?P<first>\d+); i <= 31; \+\+i\) \{\n'
    r'            (?P<operation>ctx\.gpr\[i\] = ctx\.mmu\.read32\(addr\);'
    r'|ctx\.mmu\.write32\(addr, ctx\.gpr\[i\]\);)\n'
    r'            addr \+= 4;\n'
    r'        \}\n'
    r'    \}')

WORD_ENCODED = re.compile(
    r'    /\* ' + MARKER + r'_(?P<kind>WORDS|FILL) (?P<data>[A-Za-z0-9+/=]+) \*/\n'
    r'.*?'
    r'    /\* END_' + MARKER + r'_(?P=kind) \*/', re.S)

# Runs shorter than this are left alone: the reference measured that the extra
# proof only pays for itself from 16 words up.
WORD_MIN_WORDS = 16

# A fill run: consecutive 32-bit stores, one base register, one value expression,
# offsets stepping by 4, with only comments/labels between the stores.
FILL_STORE = re.compile(
    r'^(    )ctx\.mmu\.write32\(ctx\.gpr\[(\d+)\] \+ (-?\d+), (ctx\.gpr\[\d+\]|0|-?\d+)\);')
FILL_LABEL = re.compile(r'^loc_([0-9A-Fa-f]{8}): ;')
FILL_ADDRESS = re.compile(r'^// 0x([0-9A-Fa-f]{8}):')
FILL_MIN_WORDS = 8


# ---------------------------------------------------------------------------
# File I/O that preserves the file's own line endings byte for byte.
# ---------------------------------------------------------------------------

def read(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline='') as handle:
        return handle.read().replace('\r\n', '\n')


def write(path, text):
    with open(path, 'rb') as handle:
        crlf = b'\r\n' in handle.read(4096)
    data = text.replace('\n', '\r\n') if crlf else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as handle:
        handle.write(data)


def encode(text):
    return base64.b64encode(text.encode('utf-8', 'surrogateescape')).decode('ascii')


def decode(data):
    return base64.b64decode(data.encode('ascii')).decode('utf-8', 'surrogateescape')


def apply_pairs(text, pairs, tag):
    """old -> new for every pair; `new` is unique because it carries the marker."""
    for old, new in pairs:
        expected = 2 if old in PARSER_DOUBLE else 1
        found = text.count(old)
        if found != expected:
            raise SystemExit('%s: %s anchor matched %d times (expected %d): %r'
                             % (MARKER, tag, found, expected, old[:70]))
        text = text.replace(old, new)
    return text


def restore_pairs(text, pairs, tag):
    for old, new in reversed(pairs):
        expected = 2 if old in PARSER_DOUBLE else 1
        found = text.count(new)
        if found == 0:
            continue                      # pristine tree: nothing to undo
        if found != expected:
            raise SystemExit('%s: %s restore matched %d times (expected %d)'
                             % (MARKER, tag, found, expected))
        text = text.replace(new, old)
    return text


def patch_words(text):
    """One proof per lmw/stmw run instead of one per register."""
    changes = []
    for match in WORD_PATTERN.finditer(text):
        first = int(match['first'])
        if first > WORD_MIN_WORDS:
            continue
        words = 32 - first
        load = match['operation'].startswith('ctx.gpr')
        if load:
            fast = ('                ctx.gpr[i] = native_mom_lane::Load('
                    'native_span + 4u * (i - %du));' % first)
            span = 'SpanWordsLoad'
            name = 'lmw'
        else:
            fast = ('                native_mom_lane::Store(native_span + 4u * (i - %du), '
                    'ctx.gpr[i]);' % first)
            span = 'SpanWordsStore'
            name = 'stmw'
        fallback = ('            for (int i = %du; i <= 31; ++i) {\n'
                    '                %s\n'
                    '                addr += 4;\n'
                    '            }' % (first, match['operation']))
        replacement = (
            '    /* %s_WORDS %s */\n'
            '    {\n'
            '        uint32_t addr = %s;\n'
            '        // %s: one ordinary-RAM proof for the whole %s run; the\n'
            '        // per-register path below is the fallback.\n'
            '        if (uint8_t* native_span = native_mom_lane::%s(ctx.mmu, addr, %uu)) {\n'
            '            for (int i = %du; i <= 31; ++i)\n'
            '%s\n'
            '        } else {\n'
            '%s\n'
            '        }\n'
            '    }\n'
            '    /* END_%s_WORDS */'
            % (MARKER, encode(match.group(0)), match['address'], MARKER, name,
               span, 4 * words, first, fast, fallback, MARKER))
        changes.append((match.start(), match.end(), replacement))
    for start, end, replacement in reversed(changes):
        text = text[:start] + replacement + text[end:]
    return text, len(changes)


def patch_fill(text):
    """One proof + one combined write per straight-line fill run."""
    lines = text.split('\n')
    runs = []
    index = 0
    while index < len(lines):
        head = FILL_STORE.match(lines[index])
        if not head:
            index += 1
            continue
        indent = head.group(1)
        base = int(head.group(2))
        offset = int(head.group(3))
        value = head.group(4)
        members = [index]
        probe = index + 1
        expected = offset + 4
        while probe < len(lines):
            nxt = FILL_STORE.match(lines[probe])
            if (nxt and nxt.group(1) == indent and nxt.group(4) == value
                    and int(nxt.group(2)) == base and int(nxt.group(3)) == expected):
                members.append(probe)
                expected += 4
                probe += 1
                continue
            bare = lines[probe].strip()
            if not bare or bare.startswith('//') or FILL_LABEL.match(bare):
                probe += 1
                continue
            break
        if len(members) >= FILL_MIN_WORDS and indent == '    ':
            runs.append((members, base, offset, value))
            index = members[-1] + 1
        else:
            index += 1
    if not runs:
        return text, 0, 0

    blocks = []
    for members, base, offset, value in runs:
        first_line, last_line = members[0], members[-1]
        addresses = []
        for line in members:
            found = FILL_ADDRESS.match(lines[line - 1].strip()) if line else None
            addresses.append(int(found.group(1), 16) if found else None)
        if any(address is None for address in addresses):
            continue
        # The enclosing function, for the interior-entry test.  The combined write
        # sits at the top of the block, so a jump into the middle of a run would
        # skip the proof and store the remaining words one by one -- exactly what
        # the original did, and legal because the block declares nothing.  The
        # reference refuses such runs anyway; keep that (it costs 0 of the 209 runs
        # here and removes any argument about an entry that expects earlier stores).
        start = 0
        for probe in range(first_line, -1, -1):
            if lines[probe].startswith('void '):
                start = probe
                break
        end = len(lines)
        for probe in range(last_line + 1, len(lines)):
            if lines[probe].startswith('void '):
                end = probe
                break
        body = '\n'.join(lines[start:end])
        if any(('goto loc_%08X;' % address) in body
               or ('case 0x%08X:' % address) in body
               or ('ctx.pc = 0x%08X;' % address) in body
               for address in addresses[1:]):
            continue
        region = lines[first_line:last_line + 1]
        # One `if` around the whole run, with every store inside it.  The form is
        # a block with no declarations in it, so the interior labels stay legal
        # jump targets (`case 0x...: goto loc_...` at the top of the function is a
        # forward jump past this whole statement, and a jump *into* the run must
        # still run the stores that the original ran from that point on).
        combined = [
            '    // %s: one ordinary-RAM proof and one combined write for this fill run.'
            % MARKER,
            '    if (!native_mom_lane::FillRun(ctx.mmu, ctx.gpr[%d] + %d, %du, %s)) {'
            % (base, offset, len(members), value),
        ]
        for line in region:
            combined.append('    ' + line if line.strip() else line)
        combined.append('    }')
        block = ('    /* %s_FILL %s */\n%s\n    /* END_%s_FILL */'
                 % (MARKER, encode('\n'.join(region)), '\n'.join(combined), MARKER))
        blocks.append((first_line, last_line, block, len(members)))
    for first_line, last_line, block, _count in reversed(blocks):
        lines[first_line:last_line + 1] = [block]
    return '\n'.join(lines), len(blocks), sum(item[3] for item in blocks)


def restore_generated(text):
    """Undo every marked region in a generated chunk, byte-exactly."""
    while True:
        updated = WORD_ENCODED.sub(lambda match: decode(match['data']), text)
        if updated == text:
            return text
        text = updated


def patch_generated(text):
    text = restore_generated(text)
    text, words = patch_words(text)
    text, fills, fill_words = patch_fill(text)
    return text, words, fills, fill_words


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def apply(port):
    context = port / CONTEXT
    lane = port / LANE
    parser = port / PARSER

    # Already staged: do not re-derive the anchors.  A later stage in the
    # pipeline (the GPU vertex pipeline) rewrites the decode region this stage
    # anchors on, so re-applying from the current text would fail even though
    # the tree is exactly what this stage produces.
    if context.exists() and parser.exists():
        if MARKER in read(context) and MARKER in read(parser):
            print('%s already staged.' % MARKER)
            return

    # 1. The runtime header (content-compared, so a second run writes nothing).
    header = LANE_HEADER.encode('utf-8')
    if not lane.exists() or lane.read_bytes() != header:
        lane.parent.mkdir(parents=True, exist_ok=True)
        with open(lane, 'wb') as handle:
            handle.write(header)

    # 2. cpu_context.h: the include, MEM2, the two f64 forms, the psq pairs.
    text = read(context)
    updated = apply_pairs(restore_pairs(text, CONTEXT_EDITS, 'cpu_context.h'),
                          CONTEXT_EDITS, 'cpu_context.h')
    if updated != text:
        write(context, updated)
    mem2_sites = sum(1 for _old, new in CONTEXT_EDITS if 'Mem2On' in new)

    # 3. fifo_parser.cpp: one index-domain proof per draw.
    text = read(parser)
    updated = apply_pairs(restore_pairs(text, PARSER_EDITS, 'fifo_parser.cpp'),
                          PARSER_EDITS, 'fifo_parser.cpp')
    if updated != text:
        write(parser, updated)

    # 4. The generated guest code: lmw/stmw runs and fill runs.
    words = fills = fill_words = chunks = 0
    for path in sorted((port / 'export').glob('output_*.cpp')):
        text = read(path)
        updated, chunk_words, chunk_fills, chunk_fill_words = patch_generated(text)
        if updated != text:
            write(path, updated)
            chunks += 1
        words += chunk_words
        fills += chunk_fills
        fill_words += chunk_fill_words
    print('%s staged: mem2 %d sites, psq 2 pairs, words %d runs (>=%d words), '
          'fill %d runs / %d words, %d chunk(s); switch NWII_MOM_LANE='
          'words,decode,mem2,fill,psq (unset = all, none = reference)'
          % (MARKER, mem2_sites, words, WORD_MIN_WORDS, fills, fill_words, chunks))


def restore(port):
    changed = 0
    for path, pairs, tag in ((port / CONTEXT, CONTEXT_EDITS, 'cpu_context.h'),
                             (port / PARSER, PARSER_EDITS, 'fifo_parser.cpp')):
        text = read(path)
        updated = restore_pairs(text, pairs, tag)
        if updated != text:
            write(path, updated)
            changed += 1
    for path in sorted((port / 'export').glob('output_*.cpp')):
        text = read(path)
        updated = restore_generated(text)
        if updated != text:
            write(path, updated)
            changed += 1
    print('%s restored: %d file(s); the installed header stays in place (it is '
          'inert once the include is gone).' % (MARKER, changed))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if args.restore:
        restore(args.port)
    else:
        apply(args.port)


if __name__ == '__main__':
    main()
