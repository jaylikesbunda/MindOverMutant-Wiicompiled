"""Memoise the dispatcher's target resolution (CotT's fix_measured_gameplay_perf + fix_dispatch_hot_trim).

``find_dispatch_target`` runs on every guest ``bl``/``blr`` that is not a
straight-line goto.  In MoM it is

  * a binary search over ``g_dispatch_overrides`` - 128,850 entries, 2 MB of
    DispatchChoice records, ~17 dependent loads per call;
  * and, on a miss, ``find_func_bound``'s binary search over the 30,676-entry
    bounds table with its 122 KB prefix-max array.

Both tables are immutable once ``main`` starts, so the answer for a given guest pc
is a constant.  This stage renames the resolver to ``find_dispatch_target_slow``
and puts an exact-tag memo in front of it: a hit is one hash, one 16-byte compare
and the load of the cached choice; a miss runs the unchanged resolver and stores
its result.  The tag is the full 32-bit guest pc, so a hit can only ever return
the choice the resolver produced for that exact target - the cache cannot change
what the dispatcher decides, only how long it takes to decide it.  The 4096 slots
(64 KiB) are the size CotT measured as the fastest: ``fix_dispatch_hot_trim``
documents that halving them to 2048 regressed the resolver.

Idempotent: the marker replaces the signature, the call site and the wrapper in
one edit and is a no-op afterwards.

Threading: the memo is a function-local static written only by the guest thread
(``run_game`` is the single call site and the guest runs on one thread), so the
three-field store cannot be observed half-written.

Measured on MoM (with the -O2 pragma stage): the dispatcher's share of the guest
thread falls from 91 + 55 = 146 host instructions of resolver code per dispatch
(17 + 15 binary-search iterations at -O0) to a hash and a 16-byte compare on a
hit. See logs/cott-guest-codegen-perf.md.
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_DISPATCH_HOT_CACHE_V1'
SIGNATURE = 'static DispatchChoice find_dispatch_target(uint32_t target) {\n'
SLOW_SIGNATURE = 'static DispatchChoice find_dispatch_target_slow(uint32_t target) {\n'
# The resolver's own tail, so the memo can be emitted after it is declared.
SLOW_TAIL = '    return {target, fb ? fb->func : nullptr, false};\n}\n'
CALL = 'const auto dispatch = find_dispatch_target(target);'
HOT_CALL = 'const auto dispatch = find_dispatch_target_hot(target);'
INCLUDE_ANCHOR = '#include <cstdlib>\n'

WRAPPER = (
    '// ' + MARKER + ': exact-tag memo in front of the 128,850-entry override\n'
    '// binary search and the 30,676-entry bounds lookup. A hit is one hash, one\n'
    '// 16-byte compare and the load of the cached choice; a miss runs the unchanged\n'
    '// resolver and stores its result. The tag is the full guest pc, so a hit can\n'
    '// only return the value the resolver produced for that exact target.\n'
    'static DispatchChoice find_dispatch_target_hot(uint32_t target) {\n'
    '    static std::array<DispatchChoice, 4096> cache{};\n'
    '    DispatchChoice& slot = cache[((target >> 2) ^ (target >> 14)) & 4095u];\n'
    '    if (slot.pc != target) slot = find_dispatch_target_slow(target);\n'
    '    return slot;\n'
    '}\n'
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=None)
    args = parser.parse_args()

    root = args.root
    if root is None:
        root = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
        if not (root / 'vendor').is_dir():
            root = root.parent
    path = root / 'export' / 'main_output.cpp'
    if not path.is_file():
        print('Export not generated; dispatch hot cache deferred.')
        return 0
    text = path.read_text(errors='surrogateescape')
    if MARKER in text:
        print('Dispatch hot cache already staged.')
        return 0
    if text.count(SIGNATURE) != 1 or text.count(CALL) != 1 or text.count(SLOW_TAIL) != 1:
        print('Dispatch anchors not found exactly once (signature %d, call %d, tail %d); '
              'left unchanged.' % (text.count(SIGNATURE), text.count(CALL), text.count(SLOW_TAIL)))
        return 1
    text = text.replace(SIGNATURE, SLOW_SIGNATURE, 1)
    text = text.replace(SLOW_TAIL, SLOW_TAIL + WRAPPER, 1)
    text = text.replace(CALL, HOT_CALL, 1)
    if '#include <array>' not in text:
        text = text.replace(INCLUDE_ANCHOR, INCLUDE_ANCHOR + '#include <array>\n', 1)
    path.write_text(text, errors='surrogateescape')
    print('Dispatch memo staged: exact-tag cache in front of the override binary search.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
