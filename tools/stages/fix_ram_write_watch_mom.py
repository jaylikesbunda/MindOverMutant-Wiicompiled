"""Track guest RAM writes and skip unchanged EFB snapshot comparisons.

Write-watch allocation falls back to normal fixed RAM if unsupported. All
query failures fall back to the existing full memcmp. Each snapshot records
the watch generation *before* copying its bytes; overlapping snapshots share
per-page generations. Idempotent; --restore reverses anchored edits.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
TREES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', ROOT / 'export/nWiiRuntime']
RESTORE = '--restore' in sys.argv

alloc_old = '  void *mapped = VirtualAlloc(reinterpret_cast<void *>(base), bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);'
alloc_new = '''  // NATIVE_MOM_RAM_WRITE_WATCH_V1: allocation falls back without tracking.
  void *mapped = VirtualAlloc(reinterpret_cast<void *>(base), bytes, MEM_RESERVE | MEM_COMMIT | MEM_WRITE_WATCH, PAGE_READWRITE);
  if (!mapped)
    mapped = VirtualAlloc(reinterpret_cast<void *>(base), bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);'''
include_old = '#include "runtime/frame_trace.h" // NATIVE_MOM_MIP_STATE_TRACE_V1'
include_new = include_old + '\n#include "runtime/native_ram_write_watch_mom.h" // NATIVE_MOM_RAM_WRITE_WATCH_V1'
snapshot_old = 'struct NativeEfbGuest { std::vector<uint8_t> bytes; };'
snapshot_new = 'struct NativeEfbGuest { std::vector<uint8_t> bytes; uint64_t verified_generation = 0; }; // NATIVE_MOM_RAM_WRITE_WATCH_V1'
check_old = '''    auto it = s_efb_guest.find(address & 0x3fffffffu);
    return it != s_efb_guest.end() && !it->second.bytes.empty() &&
           NativeEfbSpan(ctx, address, it->second.bytes.size()) &&
           std::memcmp(ctx.mmu.get_ptr(address), it->second.bytes.data(), it->second.bytes.size()) == 0;'''
check_new = '''    auto it = s_efb_guest.find(address & 0x3fffffffu);
    if (it == s_efb_guest.end() || it->second.bytes.empty() ||
        !NativeEfbSpan(ctx, address, it->second.bytes.size())) return false;
    auto& entry = it->second;
    const auto watch = NativeEfbWatch(ctx, address, entry.bytes.size());
    if (watch.ok && entry.verified_generation && watch.newest <= entry.verified_generation)
        return true;
    if (std::memcmp(ctx.mmu.get_ptr(address), entry.bytes.data(), entry.bytes.size()) != 0)
        return false;
    entry.verified_generation = watch.ok ? watch.generation : 0;
    return true;'''
before_check_old = 'static bool NativeEfbGuestUnchanged(CPUContext& ctx, uint32_t address) {'
before_check_new = '''// NATIVE_MOM_RAM_WRITE_WATCH_V1: use physical bank boundaries; aliases share
// generations. Watch is an optimization only, never a correctness fallback.
static native_ram_watch::Result NativeEfbWatch(CPUContext& ctx, uint32_t address, size_t size) {
    static const bool enabled = std::getenv("NWII_NO_WRITE_WATCH") == nullptr;
    if (!enabled) return {false, 0, 0};
    const uint32_t p = address & 0x3fffffffu;
    if (p < 0x01800000u)
        return native_ram_watch::query(ctx.mmu.mem1.data(), ctx.mmu.mem1.size(), p, size);
    if (p >= 0x10000000u && p < 0x14000000u)
        return native_ram_watch::query(ctx.mmu.mem2.data(), ctx.mmu.mem2.size(), p - 0x10000000u, size);
    return {false, 0, 0};
}
''' + before_check_old
reg_old = '''    auto& snapshot = s_efb_guest[addr & 0x3fffffff].bytes;
    snapshot.clear();'''
reg_new = '''    auto& entry = s_efb_guest[addr & 0x3fffffff];
    auto& snapshot = entry.bytes;
    snapshot.clear();
    entry.verified_generation = 0;'''
copy_old = '''        const auto* bytes = g_ctx_ptr->mmu.get_ptr(addr);
        snapshot.assign(bytes, bytes + size);'''
copy_new = '''        const auto watch = NativeEfbWatch(*g_ctx_ptr, addr, size);
        const auto* bytes = g_ctx_ptr->mmu.get_ptr(addr);
        snapshot.assign(bytes, bytes + size);
        entry.verified_generation = watch.ok ? watch.generation : 0;'''

def replace(s, old, new, label):
    if RESTORE:
        old, new = new, old
        if s.count(old) == 1: return s.replace(old, new, 1)
    if new in s: return s
    if s.count(old) != 1: raise ValueError((label, s.count(old)))
    return s.replace(old, new, 1)

def edit(path, changes):
    raw = path.read_bytes().decode('utf-8', 'surrogateescape')
    crlf = '\r\n' in raw
    s = raw.replace('\r\n', '\n')
    for old, new in changes: s = replace(s, old, new, str(path))
    path.write_bytes((s.replace('\n', '\r\n') if crlf else s).encode('utf-8', 'surrogateescape'))

for path in [ROOT/'overlays/static_ram_mom/native_static_ram_mom.h'] + [t/'include/runtime/native_static_ram_mom.h' for t in TREES]:
    edit(path, [(alloc_old, alloc_new)])
for tree in TREES:
    edit(tree/'src/hle/texture_cache_gl.cpp', [(include_old,include_new), (snapshot_old,snapshot_new),
         (before_check_old,before_check_new), (check_old,check_new), (reg_old,reg_new), (copy_old,copy_new)])
    target = tree/'include/runtime/native_ram_write_watch_mom.h'
    if RESTORE:
        if target.exists():
            if target.read_bytes() != (ROOT/'overlays/native_ram_write_watch_mom.h').read_bytes():
                raise ValueError('watch header differs; refusing to remove')
            target.unlink()
    else:
        content = (ROOT/'overlays/native_ram_write_watch_mom.h').read_bytes()
        if target.exists() and target.read_bytes() != content: raise ValueError('watch header differs')
        target.write_bytes(content)
print('RAM write watch ' + ('removed' if RESTORE else 'installed'))
