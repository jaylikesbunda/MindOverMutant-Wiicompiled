#include "runtime/native_ram_write_watch_mom.h"
#include <cstdio>
using nwii::runtime::native_ram_watch::query;
int main() {
  constexpr size_t size = 4 * 4096;
  auto* ram = static_cast<unsigned char*>(VirtualAlloc(nullptr, size,
      MEM_RESERVE | MEM_COMMIT | MEM_WRITE_WATCH, PAGE_READWRITE));
  if (!ram) { std::puts("FAIL allocation"); return 1; }
  auto first = query(ram, size, 0, size);
  ram[4097] = 42;
  auto overlap = query(ram, size, 4096, 4096);
  auto whole = query(ram, size, 0, size);
  bool ok = first.ok && overlap.ok && whole.ok &&
            overlap.newest > first.generation && whole.newest == overlap.newest;
  auto clean = query(ram, size, 0, 4096);
  ok = ok && clean.ok && clean.newest <= first.generation;
  ram[1] = 3;
  auto changed = query(ram, size, 0, 2);
  ok = ok && changed.ok && changed.newest > clean.generation;
  auto invalid = query(ram, size, size - 1, 2);
  ok = ok && !invalid.ok;
  auto* ordinary = static_cast<unsigned char*>(VirtualAlloc(nullptr, size,
      MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
  if (!ordinary) ok = false;
  else {
    ok = ok && !query(ordinary, size, 0, size).ok;
    VirtualFree(ordinary, 0, MEM_RELEASE);
  }
  VirtualFree(ram, 0, MEM_RELEASE);
  std::puts(ok ? "PASS" : "FAIL");
  return ok ? 0 : 1;
}
