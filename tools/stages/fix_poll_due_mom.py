"""Poll callbacks every Nth generated backward branch instead of every one.

Marker: NATIVE_MOM_POLL_DUE_V1.

MoM's generated code calls ``process_pending_callbacks(ctx)`` on every backward
branch; the census measured ~7 M polls/s and the CPU profile puts the poll
machinery (plus the clock reads its diagnostics trigger) at the top of guest
CPU even after the fast path.  CotT's ``fix_poll_credit.py`` solves the same
problem; this is the MoM-shaped version of that mechanism (no CotT guest
addresses: the only PC named is MoM's own verified idle loop, 0x80492408).

Mechanism:

* ``native_poll_due(pc)`` in ``cpu_context.h`` decrements a plain
  ``int32_t`` credit (guest-CPU-thread-only, like the poll it gates) and says
  yes when the credit runs out or when a host producer raised
  ``native_idle::attention`` (``native_idle_wait.h`` already sets it from
  ``notify()``: queued callbacks, vblank, PI interrupts, PE signals, the GX
  drain request).
* The credit is reloaded by every *real* poll to ``NWII_POLL_CREDIT`` (default
  64, clamped to 1..1024; ``NWII_NO_POLL_THROTTLE`` selects every-site polling,
  the pre-stage behaviour).  While an IPC or DI interrupt delay is counting
  down the reload is 1, because those delays are counted in polls.
* The same poll consumes ``attention`` *before* processing
  (``store(0)`` first), so a producer that notifies while the poll is running
  leaves the flag set and forces the next real poll - no lost wake.
* The verified idle-loop PC ``0x80492408`` and the sentinel PCs ``0`` and
  ``0xFFFFFFFC`` keep every-site polls: the idle park and the callback-return
  sentinel handling live inside the poll itself, so those sites must reach it
  on every pass.  Interpreter and ``run_game`` call sites are untouched, so
  nothing else about those paths changes.

Every generated ``if (process_pending_callbacks(ctx)) return;`` becomes
``if (nwii::runtime::native_poll_due(ctx.pc) && process_pending_callbacks(ctx))
return;`` and the "Spinning at PC" diagnostic that follows it (a 64-bit modulo
per site) folds into the due branch, so a skipped poll also skips the modulo.
No guest register, ``inst_count`` or ``pc`` value is affected: the gate only
decides whether the poll body runs, and a taken poll runs it unchanged.

Publication progress is preserved: ``wgp_push``/``native_wgp_bulk`` still
publish exactly when the 4,096th staged byte arrives (the bound on staged
data), and every real poll - credit expiry or attention - still runs
``native_gx_publish``.  A time-driven event can therefore be observed up to
credit-1 backward branches late (~10 us at the measured poll rate); it does
not claim identical poll counts.

Idempotent and reversible: the runtime edits are exact inverse pairs
(``--restore`` puts the tree back byte for byte) and the generated rewrite is
skipped wherever the gate is already present, so a second run is a
byte-identical no-op.  The stage rewrites ``export/output_*.cpp``, so it must
run after regeneration and after stages that also rewrite generated chunks
(``fix_guest_memory_lane_mom.py``); the runtime half is independent.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER = 'NATIVE_MOM_POLL_DUE_V1'

ROOT = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
CTX = RUNTIME + '/include/runtime/cpu_context.h'
IOS = RUNTIME + '/src/hle/ios.cpp'
GENERATED = 'export'

# ---------------------------------------------------------------------------
# cpu_context.h
# ---------------------------------------------------------------------------

CTX_PAIR = (
    '''bool process_pending_callbacks(CPUContext &ctx);
''',
    '''bool process_pending_callbacks(CPUContext &ctx);
// ''' + MARKER + ''': generated backward-branch polls are taken every Nth site.
// The credit is a plain int32_t: it is decremented only from generated code on
// the guest CPU thread and reloaded only by process_pending_callbacks on that
// same thread.
extern int32_t g_native_poll_credit;
// The reload every real poll applies.  IPC and DI interrupt delays are counted
// in polls, so while either is nonzero the reload is 1 and polling stays
// every-site - a poll-counted delay may not be stretched by the credit.
inline int32_t native_poll_reload(bool ipc_delay, bool di_delay) {
  if (ipc_delay || di_delay) return 1;
  static const int32_t reload = []() {
    const char *e = std::getenv("NWII_POLL_CREDIT");
    const long v = e ? std::strtol(e, nullptr, 10) : 64;
    if (std::getenv("NWII_NO_POLL_THROTTLE")) return int32_t(1);
    return int32_t(v < 1 ? 1 : (v > 1024 ? 1024 : v));
  }();
  return reload;
}
// Cheap gate: true when this site should run the real poll.  The verified idle
// loop (0x80492408) and the callback-return sentinels keep every-site polls -
// the park and the sentinel paths live inside the poll.  Every other site
// polls when the credit expires or a producer raised attention.
__attribute__((always_inline)) inline bool native_poll_due(uint32_t pc) {
  if (pc == 0x80492408u || pc == 0u || pc == 0xFFFFFFFCu) return true;
  return __builtin_expect(--g_native_poll_credit <= 0 ||
      native_idle::attention.load(std::memory_order_relaxed) != 0, 0);
}
''')

# ---------------------------------------------------------------------------
# ios.cpp
# ---------------------------------------------------------------------------

# Each replacement keeps its anchor verbatim inside the new text, so a re-run
# detects the applied state even when an unrelated stage (the runtime trim)
# inserts its own declarations between the credit and the function header.
FORCE_PAIR = (
    '__attribute__((always_inline)) inline bool native_poll_due(uint32_t pc) {' + chr(10),
    '''// ''' + MARKER + ''': a host countdown becoming nonzero must restart
// polling at the next gate even while a large credit is outstanding; the
// gate then reloads to 1 for the whole countdown (see native_poll_reload).
__attribute__((always_inline)) inline void native_poll_force_due() {
  g_native_poll_credit = 1;
}
__attribute__((always_inline)) inline bool native_poll_due(uint32_t pc) {
''')

HW_IPC = RUNTIME + '/src/hw/hw_ipc.cpp'
HW_DI = RUNTIME + '/src/hw/hw_di.cpp'

# The one arm site inside an unbraced if/else needs braces so the else still
# binds the second statement.
HW_ELSE_PAIR = (
    '''                        g_ipc_interrupt_delay = 50;
                    else
                        g_ipc_interrupt_delay = 0;''',
    '''                        { g_ipc_interrupt_delay = 50; native_poll_force_due(); } // ''' + MARKER + '''
                    else
                        g_ipc_interrupt_delay = 0;''')

import re as _re
_ARM_RE = _re.compile(r'g_(?:ipc|di)_interrupt_delay = [1-9]\d*;(?! native_poll_force_due)')
_ARM_TAG = ' native_poll_force_due(); // ' + MARKER


def _arm_sites(text, forward):
    if forward:
        for old, new in (HW_ELSE_PAIR,):
            if new in text:
                continue
            if old not in text:
                continue
            text = text.replace(old, new, 1)
        text = _ARM_RE.sub(lambda m: m.group(0) + _ARM_TAG, text)
        return text
    text = text.replace(_ARM_TAG, '')
    for old, new in (HW_ELSE_PAIR,):
        text = text.replace(new, old)
    return text


IOS_PAIRS = [
    ('''extern std::condition_variable g_gx_drain_cv;
''',
     '''extern std::condition_variable g_gx_drain_cv;
int32_t g_native_poll_credit = 1; // ''' + MARKER + '''
'''),
    ('''  native_fp_context_probe(ctx,"poll");
''',
     '''  { // ''' + MARKER + ''': reload the site credit, then consume attention
    // *before* processing: a producer that notifies while this poll runs
    // leaves the flag raised and forces the next real poll, so a concurrent
    // notification is never lost.  NWII_NO_POLL_THROTTLE is read inside
    // native_poll_reload.
    g_native_poll_credit = native_poll_reload(g_ipc_interrupt_delay != 0,
                                              hw::g_di_interrupt_delay != 0);
    native_idle::attention.store(0, std::memory_order_relaxed);
  }
  native_fp_context_probe(ctx,"poll");
'''),
]

# ---------------------------------------------------------------------------
# export/output_*.cpp
# ---------------------------------------------------------------------------

SITE_OLD = '\n    if (process_pending_callbacks(ctx)) return;'
SITE_NEW = ('\n    if (nwii::runtime::native_poll_due(ctx.pc) && '
            'process_pending_callbacks(ctx)) return;')

# The "Spinning at PC" diagnostic evaluates a 64-bit modulo on every site;
# fold it into the taken branch so a skipped poll skips the modulo too.
FOLD = re.compile(
    r'if \(nwii::runtime::native_poll_due\(ctx\.pc\) && '
    r'process_pending_callbacks\(ctx\)\) return;\n'
    r'(\s*)(if \(\(ctx\.inst_count % 10000000\) == 0\) \{[^\n]*\})')
FOLD_RE = re.compile(
    r'if \(nwii::runtime::native_poll_due\(ctx\.pc\)\) '
    r'\{ if \(process_pending_callbacks\(ctx\)\) return; '
    r'(if \(\(ctx\.inst_count % 10000000\) == 0\) \{[^\n]*\}) \}')


def fold(text):
    return FOLD.subn(
        lambda m: 'if (nwii::runtime::native_poll_due(ctx.pc)) { '
                  'if (process_pending_callbacks(ctx)) return; ' + m.group(2) + ' }',
        text)


def unfold(text):
    return FOLD_RE.subn(
        lambda m: 'if (process_pending_callbacks(ctx)) return;\n    ' + m.group(1),
        text)


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


def apply_generated(path):
    text = read(path)
    if SITE_OLD not in text and 'native_poll_due(ctx.pc)' not in text:
        return 0, 0
    gated = text.replace(SITE_OLD, SITE_NEW)
    folded, sites = fold(gated)
    if folded != text:
        write(path, folded)
    return text.count(SITE_OLD), sites


def restore_generated(path):
    text = read(path)
    if 'native_poll_due(ctx.pc)' not in text:
        return False
    ungated, _ = unfold(text)
    ungated = ungated.replace(SITE_NEW, SITE_OLD)
    if ungated != text:
        write(path, ungated)
        return True
    return False


def apply(root):
    for rel, pairs in ((CTX, [CTX_PAIR, FORCE_PAIR]), (IOS, IOS_PAIRS)):
        target = root / rel
        if not target.is_file():
            raise SystemExit('%s: %s is missing' % (MARKER, rel))
        text = read(target)
        if rel == IOS:
            # a foreign insert between the g_gx_drain_cv anchor and a
            # previously applied definition makes the pair's `new` unmatched;
            # strip any existing definition line so re-apply can't duplicate it.
            text = '\n'.join(ln for ln in text.split('\n')
                             if ln.strip() != 'int32_t g_native_poll_credit = 1; // ' + MARKER)
        updated = apply_pairs(restore_pairs(text, pairs, rel), pairs, rel)
        if updated != text:
            write(target, updated)
    for rel in (HW_IPC, HW_DI):
        target = root / rel
        if not target.is_file():
            continue
        text = read(target)
        updated = _arm_sites(restore_pairs(_arm_sites(text, False), [], rel), True)
        if updated != text:
            write(target, updated)
    total = files = 0
    generated = root / GENERATED
    if generated.is_dir():
        for path in sorted(generated.glob('output_*.cpp')):
            gated, sites = apply_generated(path)
            total += gated
            files += bool(gated or sites)
        print('%s staged: runtime guard installed; %d generated poll site(s) '
              'gated in %d chunk(s) (NWII_POLL_CREDIT=1 or '
              'NWII_NO_POLL_THROTTLE=1 restores every-site polling)'
              % (MARKER, total, files))
    else:
        print('%s staged: runtime guard installed; export/ absent, generated '
              'rewrite deferred (re-run after regeneration)' % MARKER)


def restore(root):
    changed = []
    for rel in (HW_IPC, HW_DI):
        target = root / rel
        if target.is_file():
            text = read(target)
            updated = _arm_sites(text, False)
            if updated != text:
                write(target, updated)
    for rel, pairs in ((IOS, IOS_PAIRS), (CTX, [CTX_PAIR, FORCE_PAIR])):
        target = root / rel
        if not target.is_file():
            continue
        text = read(target)
        updated = restore_pairs(text, pairs, rel)
        if updated != text:
            write(target, updated)
            changed.append(rel)
    sites = 0
    generated = root / GENERATED
    if generated.is_dir():
        for path in sorted(generated.glob('output_*.cpp')):
            sites += restore_generated(path)
    print('%s restored: %s; %d generated chunk(s) ungated'
          % (MARKER, ', '.join(changed) or 'runtime already pristine', sites))


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
