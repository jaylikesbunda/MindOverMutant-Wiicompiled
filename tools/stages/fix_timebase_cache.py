"""NATIVE_TIMEBASE_CACHE_V1: refresh the wall timebase at most once per N guest instructions.

read_timebase() (NWII_WALLTB=1, the launcher default) reads the TSC on every call; the
fresh guest profile shows __rdtsc() as the single hottest line (1.41%). Guest time advances
only as fast as the emulated instruction stream, so the wall timebase can be refreshed every
N instructions (well under a poll's granularity) without changing any observable deadline:
the DEC and event scheduler already tolerate the 16-poll clock throttle. On x86-64 an aligned
uint64 load/store is atomic, so a concurrent reader can only see a slightly stale (monotonic)
value, never a torn one.

MoM adaptation of the CotT stage: CotT caches the already-scaled timebase and returns it.
Here the *elapsed microseconds* are cached and the value is still passed through
native_scale_timebase() on every call, so fix_timebase_scaling.py's applied-probe (exactly
one `return native_scale_timebase(us, tb_freq);`) stays valid and the two stages remain
independently idempotent. The saved work -- the steady_clock read -- is the same; the
scaling is one integer multiply plus an add.

Port notes:
  * MoM only takes this path with NWII_WALLTB set; logs/run-sdk6.ps1 sets it, the default
    launch does not (virtual clock), so this stage costs nothing when WALLTB is off.
  * It uses the same 512-instruction span as NATIVE_CLOCK_CACHE_V1 (get_os_time), so both
    guest clocks share one staleness bound.
  * Must run after fix_timebase_scaling.py (it rewrites the same function).

Build rollback: NWII_BUILD_TIMEBASE_CACHE=0. Runtime: NWII_NO_TIMEBASE_CACHE=1.
Idempotent.
"""
from pathlib import Path
import argparse
import os

MARK = 'NATIVE_TIMEBASE_CACHE_V1'

OLD = """  uint64_t read_timebase() const {
    static const bool wall = std::getenv("NWII_WALLTB") != nullptr;
    if (wall) {
      uint64_t us = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - tb_start)
                        .count();
      return native_scale_timebase(us, tb_freq);
    }
    return inst_count;
  }"""

NEW = """  // NATIVE_TIMEBASE_CACHE_V1: last steady_clock reading, bounded by guest instructions.
  mutable uint64_t tb_cache_inst=0,tb_cache_val=0;
  mutable bool tb_cache_valid=false;
  uint64_t read_timebase() const {
    static const bool wall = std::getenv("NWII_WALLTB") != nullptr;
    if (wall) {
      // NATIVE_TIMEBASE_CACHE_V1: at most one steady_clock read per 512 guest instructions.
      static const bool cache = std::getenv("NWII_NO_TIMEBASE_CACHE") == nullptr;
      if (cache && tb_cache_valid && inst_count - tb_cache_inst < 512u)
        return native_scale_timebase(tb_cache_val, tb_freq);
      uint64_t us = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - tb_start)
                        .count();
      tb_cache_inst=inst_count;tb_cache_val=us;tb_cache_valid=true;
      return native_scale_timebase(us, tb_freq);
    }
    return inst_count;
  }"""


def apply(s):
    if MARK in s:
        return s
    if s.count(OLD) != 1:
        raise SystemExit('timebase anchor matched %d times (expected 1)' % s.count(OLD))
    return s.replace(OLD, NEW, 1)


def revert(s):
    """Undo the previous (scaled-value) form of this stage if a tree carries it."""
    legacy = """  mutable uint64_t tb_cache_inst=0,tb_cache_val=0;
  mutable bool tb_cache_valid=false;
  uint64_t read_timebase() const {
    static const bool wall = std::getenv("NWII_WALLTB") != nullptr;
    if (wall) {
      // NATIVE_TIMEBASE_CACHE_V1: at most one TSC read per 512 guest instructions.
      static const bool cache = std::getenv("NWII_NO_TIMEBASE_CACHE") == nullptr;
      if (cache && tb_cache_valid && inst_count - tb_cache_inst < 512u) return tb_cache_val;
      uint64_t us = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - tb_start)
                        .count();
      tb_cache_inst=inst_count;tb_cache_val=native_scale_timebase(us, tb_freq);tb_cache_valid=true;
      return tb_cache_val;
    }
    return inst_count;
  }"""
    if legacy in s:
        return s.replace(legacy, OLD, 1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args()
    if os.environ.get('NWII_BUILD_TIMEBASE_CACHE', '1') == '0':
        print('timebase cache disabled')
        return
    path = args.port / 'build/nwii-full-source/nWiiRuntime/include/runtime/cpu_context.h'
    before = path.read_text()
    after = apply(before)
    if before != after:
        path.write_text(after)
    print('timebase cache staged')


if __name__ == '__main__':
    main()
