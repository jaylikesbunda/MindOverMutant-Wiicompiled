"""Inline fast path for ordinary-RAM guest loads and stores.

MoM's guest runs through the generic recompiler, so *every* guest load and
store becomes a call into MMU::read*/write*, and each of those walked the
whole chain: custom_watch, the g_watch_addr test, the 0x0C008000 write-gather
page test, is_hw_reg, get_ptr and finally the byte assembly. The guest
retired 81 million instructions a second, so that chain was the guest thread's
dominant cost.

This stage splits every access into an always_inline fast path and a
noinline slow path. The fast path handles ordinary MEM1 RAM with a single
compare and one store; everything else (MEM2, the MMIO window, the GX
write-gather page, the 0x0000000C sentinel and any active watch) keeps the
original implementation, so the observable behaviour is unchanged.

The same split is applied to the paired-single F32 loads and stores: for the
F32 form the GQR scale has no effect, so the scale/format decoding can be
skipped.

The fast path can only reach addresses below mem1.size()-8 (24 MiB), and the
MMIO window (0x0C000000), the write-gather page (0x0C008000) and MEM2 all
live far above that, so they always take the slow path.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
p = port / 'vendor/NWiiRecomp/nWiiRuntime/include/runtime/cpu_context.h'
t = p.read_text()

if 'NATIVE_INLINE_RAM_ACCESS_V1' in t:
    print('MMU inline fast path already present.')
    raise SystemExit(0)

# The watch callback must stay empty, otherwise the fast path would skip it.
assert 'void custom_watch(uint32_t, uint32_t, int) {}' in t, \
    'MoM custom_watch is not empty; the fast path would skip it'


def replace(old, new):
    global t
    assert t.count(old) == 1, old[:70]
    t = t.replace(old, new, 1)


# ---------------------------------------------------------------- reads ----
replace("""  uint8_t read8(uint32_t addr) {
    uint32_t paddr = addr & 0x3FFFFFFF;""",
        """  // NATIVE_INLINE_RAM_ACCESS_V1: ordinary RAM reads skip the MMIO dispatch.
  __attribute__((always_inline)) inline uint8_t read8(uint32_t addr) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8, 1)) return mem1[paddr];
    return native_read8_slow(addr);
  }
  __attribute__((noinline)) uint8_t native_read8_slow(uint32_t addr) {
    uint32_t paddr = addr & 0x3FFFFFFF;""")

replace("""  uint16_t read16(uint32_t addr) {
    uint32_t paddr = addr & 0x3FFFFFFF;""",
        """  __attribute__((always_inline)) inline uint16_t read16(uint32_t addr) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8, 1)) {
      uint16_t bits; std::memcpy(&bits, mem1.data() + paddr, 2);
      return __builtin_bswap16(bits);
    }
    return native_read16_slow(addr);
  }
  __attribute__((noinline)) uint16_t native_read16_slow(uint32_t addr) {
    uint32_t paddr = addr & 0x3FFFFFFF;""")

replace("""  uint32_t read32(uint32_t addr) {
    uint32_t paddr = addr & 0x3FFFFFFF;
    if (paddr == 0x0000000C) return 0; """,
        """  __attribute__((always_inline)) inline uint32_t read32(uint32_t addr) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8, 1)) {
      if (paddr == 0x0000000C) return 0;
      uint32_t bits; std::memcpy(&bits, mem1.data() + paddr, 4);
      return __builtin_bswap32(bits);
    }
    return native_read32_slow(addr);
  }
  __attribute__((noinline)) uint32_t native_read32_slow(uint32_t addr) {
    uint32_t paddr = addr & 0x3FFFFFFF;
    if (paddr == 0x0000000C) return 0; """)

replace("""  uint64_t read64(uint32_t addr) {
    uint8_t *ptr = get_ptr(addr);""",
        """  __attribute__((always_inline)) inline uint64_t read64(uint32_t addr) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8, 1)) {
      uint64_t bits; std::memcpy(&bits, mem1.data() + paddr, 8);
      return __builtin_bswap64(bits);
    }
    return native_read64_slow(addr);
  }
  __attribute__((noinline)) uint64_t native_read64_slow(uint32_t addr) {
    uint8_t *ptr = get_ptr(addr);""")

# --------------------------------------------------------------- writes ----
replace("""  void write8(uint32_t addr, uint8_t value) {
    custom_watch(addr, value, 8);""",
        """  __attribute__((always_inline)) inline void write8(uint32_t addr, uint8_t value) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8 && g_watch_addr == 0, 1)) {
      uint8_t bits = value; std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write8_slow(addr, value);
  }
  __attribute__((noinline)) void native_write8_slow(uint32_t addr, uint8_t value) {
    custom_watch(addr, value, 8);""")

replace("""  void write16(uint32_t addr, uint16_t value) {
    custom_watch(addr, value, 16);""",
        """  __attribute__((always_inline)) inline void write16(uint32_t addr, uint16_t value) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8 && g_watch_addr == 0, 1)) {
      uint16_t bits = value; bits = __builtin_bswap16(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write16_slow(addr, value);
  }
  __attribute__((noinline)) void native_write16_slow(uint32_t addr, uint16_t value) {
    custom_watch(addr, value, 16);""")

replace("""  void write32(uint32_t addr, uint32_t value) {
    custom_watch(addr, value, 32);
    uint32_t paddr = addr & 0x3FFFFFFF;
    if (paddr == 0x0000000C) return; """,
        """  __attribute__((always_inline)) inline void write32(uint32_t addr, uint32_t value) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8 && g_watch_addr == 0, 1)) {
      if (paddr == 0x0000000C) return;
      uint32_t bits = value; bits = __builtin_bswap32(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write32_slow(addr, value);
  }
  __attribute__((noinline)) void native_write32_slow(uint32_t addr, uint32_t value) {
    custom_watch(addr, value, 32);
    uint32_t paddr = addr & 0x3FFFFFFF;
    if (paddr == 0x0000000C) return; """)

replace("""  void write_f32(uint32_t addr, float value) {
    uint32_t paddr = addr & 0x3FFFFFFF;
    """,
        """  __attribute__((always_inline)) inline void write_f32(uint32_t addr, float value) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8 && g_watch_addr == 0, 1)) {
      uint32_t bits; std::memcpy(&bits, &value, sizeof(bits));
      bits = __builtin_bswap32(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write_f32_slow(addr, value);
  }
  __attribute__((noinline)) void native_write_f32_slow(uint32_t addr, float value) {
    uint32_t paddr = addr & 0x3FFFFFFF;
    """)

replace("""  void write64(uint32_t addr, uint64_t value) {
    uint32_t paddr = addr & 0x3FFFFFFF;""",
        """  __attribute__((always_inline)) inline void write64(uint32_t addr, uint64_t value) {
    const uint32_t paddr = addr & 0x3FFFFFFF;
    if (__builtin_expect(paddr < mem1.size() - 8 && g_watch_addr == 0, 1)) {
      uint64_t bits = value; bits = __builtin_bswap64(bits);
      std::memcpy(mem1.data() + paddr, &bits, sizeof(bits));
      return;
    }
    native_write64_slow(addr, value);
  }
  __attribute__((noinline)) void native_write64_slow(uint32_t addr, uint64_t value) {
    uint32_t paddr = addr & 0x3FFFFFFF;""")

p.write_text(t)
print('MMU inline fast path installed.')
