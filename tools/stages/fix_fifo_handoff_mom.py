"""Move the GX FIFO handoffs off per-byte copies (MoM variant).

Markers: NATIVE_MOM_FIFO_HANDOFF_V1, NATIVE_MOM_FIFO_DRAIN_UNLOCKED_V1.

MoM's FIFO shape matches the problem CotT's ``fix_fifo_ownership_transfer.py``
solved, with MoM's own pieces (``NativeFifoLock`` SRW instead of std::mutex,
``std::vector`` cpu staging buffer, shared ``g_hw_fifo``, parser that carries
partial commands in ``s_parse_buf``):

* Guest publish: ``g_cpu_fifo``'s bytes were appended into ``g_hw_fifo`` with
  ``insert``.  When the destination is empty - the common case after a drain -
  an O(1) ``swap`` hands the whole buffer over instead.  The published byte
  count is taken *before* the append, because a swap leaves the source empty.
* Host drain: ``ProcessGXFifo`` copied ``g_hw_fifo`` into ``s_parse_buf``
  while holding the SRW lock, so every guest publish blocked on the render
  thread's copy.  The drain now swaps the pending FIFO into a local buffer
  under the same lock, resets ``g_gx_pending_bytes`` under the lock, and
  appends/carries into ``s_parse_buf`` after unlocking.  The parse buffer and
  its partial-command carry are render-thread-owned, so nothing else needs
  the lock, and byte order is the exact concatenation it was before.

No new lock is introduced and no lock is held across the parse, so there is
no lock-order change to invert.

The append helper lives in ``include/runtime/native_fifo_lock.h`` so the
tools/tests fixture exercises the same function the runtime compiles.

Idempotent and reversible: every edit is an exact inverse pair
(``--restore`` puts the tree back byte for byte).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_FIFO_HANDOFF_V1'
DRAIN_MARKER = 'NATIVE_MOM_FIFO_DRAIN_UNLOCKED_V1'

ROOT = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
LOCK = RUNTIME + '/include/runtime/native_fifo_lock.h'
GX = RUNTIME + '/src/hle/gx.cpp'

# ---------------------------------------------------------------------------
# native_fifo_lock.h: the shared append.
# ---------------------------------------------------------------------------

LOCK_PAIRS = [
    ('''#pragma once
''',
     '''#pragma once
#include <cstdint>
#include <vector>
'''),
    ('''} // namespace nwii::runtime
''',
     '''// ''' + MARKER + ''': append source to destination, transferring the whole
// buffer with one swap when the destination is empty.  Callers hold the FIFO
// lock (or own both buffers); byte order is the plain concatenation either way.
inline void NativeAppendFifo(std::vector<uint8_t>& destination,
                             std::vector<uint8_t>& source) {
  if (destination.empty()) destination.swap(source);
  else { destination.insert(destination.end(), source.begin(), source.end());
         source.clear(); }
}
} // namespace nwii::runtime
'''),
]

# ---------------------------------------------------------------------------
# gx.cpp: helper use sites.
# ---------------------------------------------------------------------------

GX_PAIRS = [
    # Guest publish: count first (a swap empties the source), then hand the
    # buffer over under the same lock, then publish the byte count.
    ('''        nwii::runtime::NativeFifoLockGuard lk(g_fifo_mutex);
        g_hw_fifo.insert(g_hw_fifo.end(), g_cpu_fifo.begin(), g_cpu_fifo.end());
        g_gx_pending_bytes.fetch_add(g_cpu_fifo.size(), std::memory_order_relaxed);
''',
     '''        nwii::runtime::NativeFifoLockGuard lk(g_fifo_mutex);
        // ''' + MARKER + ''': the size is captured before the append because a
        // swap leaves g_cpu_fifo empty; the pending-byte count is published
        // before the source is cleared, under the same lock as the transfer.
        const size_t published = g_cpu_fifo.size();
        nwii::runtime::NativeAppendFifo(g_hw_fifo, g_cpu_fifo);
        g_gx_pending_bytes.fetch_add(published, std::memory_order_relaxed);
'''),

    # Host drain: swap the pending FIFO out under the lock, append outside it.
    ('''    {
        nwii::runtime::NativeFifoLockGuard lock(g_fifo_mutex);
        if (!g_hw_fifo.empty()) {
            s_parse_buf.insert(s_parse_buf.end(), g_hw_fifo.begin(),
                               g_hw_fifo.end());
            g_hw_fifo.clear();
        }
        g_gx_pending_bytes.store(0, std::memory_order_relaxed);
    }
''',
     '''    std::vector<uint8_t> native_drain_buf; // ''' + DRAIN_MARKER + '''
    {
        nwii::runtime::NativeFifoLockGuard lock(g_fifo_mutex);
        if (!g_hw_fifo.empty()) native_drain_buf.swap(g_hw_fifo);
        g_gx_pending_bytes.store(0, std::memory_order_relaxed);
    }
    // ''' + DRAIN_MARKER + ''': s_parse_buf and the carried partial command are
    // render-thread-owned, so the append - and the partial-command carry it
    // extends - needs no lock.  The concatenated bytes are identical.
    if (!native_drain_buf.empty())
        nwii::runtime::NativeAppendFifo(s_parse_buf, native_drain_buf);
'''),
]


def read(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline='') as handle:
        return handle.read().replace('\r\n', '\n')


def write(path, text):
    with open(path, 'rb') as handle:
        crlf = b'\r\n' in handle.read(4096)
    data = text.replace('\n', '\r\n') if crlf else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as handle:
        handle.write(data)


def apply_pairs(text, pairs, tag):
    for old, new in pairs:
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:70]))
        text = text.replace(old, new, 1)
    return text


def restore_pairs(text, pairs, tag):
    for old, new in reversed(pairs):
        found = text.count(new)
        if found == 0:
            continue
        if found != 1:
            raise SystemExit('%s: %s restore matched %d times (expected 1)'
                             % (MARKER, tag, found))
        text = text.replace(new, old, 1)
    return text


EDITS = {LOCK: LOCK_PAIRS, GX: GX_PAIRS}


def apply(root):
    changed = []
    for rel, pairs in EDITS.items():
        target = root / rel
        if not target.is_file():
            raise SystemExit('%s: %s is missing' % (MARKER, rel))
        text = read(target)
        updated = apply_pairs(restore_pairs(text, pairs, rel), pairs, rel)
        if updated != text:
            write(target, updated)
            changed.append(rel)
    print('%s staged: %d file(s); CPU->shared publish swaps when empty and the '
          'host drain appends outside the SRW lock (order, carries and pending '
          'count unchanged)' % (MARKER, len(changed)))


def restore(root):
    changed = []
    for rel, pairs in ((GX, GX_PAIRS), (LOCK, LOCK_PAIRS)):
        target = root / rel
        if not target.is_file():
            continue
        text = read(target)
        updated = restore_pairs(text, pairs, rel)
        if updated != text:
            write(target, updated)
            changed.append(rel)
    print('%s restored: %s' % (MARKER, ', '.join(changed) or 'already pristine'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if args.restore:
        restore(args.root)
    else:
        apply(args.root)


if __name__ == '__main__':
    main()
