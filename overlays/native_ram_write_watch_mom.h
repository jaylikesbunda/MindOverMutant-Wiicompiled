#pragma once
// NATIVE_MOM_RAM_WRITE_WATCH_V1: OS write-watch generations shared by all
// overlapping ranges. A failed query always makes callers compare bytes.
#include <windows.h>
#include <algorithm>
#include <cstdint>
#include <mutex>
#include <vector>

namespace nwii::runtime::native_ram_watch {
struct Result { bool ok; uint64_t newest; uint64_t generation; };
struct Bank {
  const uint8_t* base = nullptr;
  size_t pages = 0;
  bool failed = false;
  std::vector<uint64_t> stamps;
};
inline std::mutex mutex;
inline uint64_t generation = 1;
inline Bank banks[4];

inline Result query(const uint8_t* base, size_t bank_bytes, size_t offset, size_t bytes) {
  if (!base || !bytes || offset >= bank_bytes || bytes > bank_bytes - offset)
    return {false, 0, 0};
  std::lock_guard<std::mutex> lock(mutex);
  Bank* bank = nullptr;
  for (auto& candidate : banks) if (candidate.base == base) { bank = &candidate; break; }
  if (!bank) for (auto& candidate : banks) if (!candidate.base) {
    bank = &candidate; bank->base = base; bank->pages = (bank_bytes + 4095) / 4096;
    bank->stamps.assign(bank->pages, 0); break;
  }
  if (!bank || bank->failed || bank->pages != (bank_bytes + 4095) / 4096)
    return {false, 0, 0};
  const size_t first = offset / 4096, last = (offset + bytes - 1) / 4096;
  static thread_local std::vector<void*> pages;
  pages.resize(last - first + 1);
  ULONG_PTR count = pages.size();
  DWORD granularity = 0;
  if (GetWriteWatch(WRITE_WATCH_FLAG_RESET, const_cast<uint8_t*>(base) + first * 4096,
                    (last - first + 1) * 4096, pages.data(), &count, &granularity) != 0 ||
      granularity != 4096) {
    bank->failed = true;
    return {false, 0, 0};
  }
  if (count) {
    const uint64_t stamp = ++generation;
    for (ULONG_PTR i = 0; i < count; ++i)
      bank->stamps[(static_cast<const uint8_t*>(pages[i]) - base) / 4096] = stamp;
  }
  uint64_t newest = 0;
  for (size_t page = first; page <= last; ++page)
    newest = std::max(newest, bank->stamps[page]);
  return {true, newest, generation};
}
}
