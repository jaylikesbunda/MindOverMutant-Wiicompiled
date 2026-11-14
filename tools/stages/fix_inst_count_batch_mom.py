"""One instruction-count update per straight-line run (CotT's fix_codegen_slim, count half).

The generated guest code counts instructions at instruction *boundaries*, not
after the instruction:

    ++ctx.inst_count;
loc_80004004: ;
    // 0x80004004: ...
    ctx.gpr[23] = ...;

so the increment belongs to the edge that falls into the label: it runs when the
previous instruction falls through (and at function entry) and is skipped when the
label is entered by a ``goto`` or by the entry switch.  That makes it exactly one
load-add-store to ``ctx.inst_count`` per guest instruction on the critical path -
1,363,251 of them across MoM's 70 chunks.

Each straight-line run (a run start plus the following instructions that are
neither a ``goto`` target nor a run-ending instruction) keeps the run start's own
``++`` and adds ``ctx.inst_count += N-1;`` right after it, so the counter is
identical whenever control leaves the run or the counter is observed.  A run ends
after any instruction that can leave it or read the count: a goto, a return, a
call to another guest function, polling, ``ctx.pc`` writes, ``interpret_step``,
timebase reads, ``require_fpu``, ``msr`` accesses, tracing.  The count is
therefore exact at every point where anything can observe it; it is only *ahead*
of the original by fewer than one run length inside a run, which is visible only
to a device handler that reads the virtual clock (``NWII_WALLTB`` unset) or to a
guest exception that unwinds with longjmp.  Same contract as CotT's stage.

Measured on MoM: 1,363,251 per-instruction updates become 449,549 run updates
(67% removed; the average run is 3.0 instructions), 216.8 MB -> 204.3 MB of
generated C++, and .text on output_62.cpp 1,451,648 -> 1,413,780 bytes.

The per-call trace test (``if (g_trace_calls && ctx.pc == ADDR) trace_call(...)``)
is deliberately NOT touched: CotT compiled it out behind a build define, but in
MoM ``g_trace_calls`` is a runtime flag set from ``NWII_TRACE_CALLS``, so removing
the test would delete the NWII_TRACE_CALLS/NWII_TRACE_ONLY diagnostics that the
run scripts use.  It costs one load and a compare per guest *call*, not per
instruction.

Run after fix_prune_entry_cases_mom.py (pruning first makes the runs longer) and
after fix_cache_range_loops_mom.py (which anchors on the per-instruction ``++``).
Idempotent: each rewritten chunk carries the marker and is skipped next time.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER = 'NATIVE_MOM_INST_COUNT_BATCH_V1'
FUNC = re.compile(r'^void (\w+)\(CPUContext& ctx\) \{\n', re.M)
UNIT = re.compile(r'    \+\+ctx\.inst_count;\nloc_([0-9A-F]{8}): ;\n')
FPRET = re.compile(r'if \(ctx\.require_fpu\((0x[0-9A-F]{8})\)\) return;')
FPLINE = re.compile(r'^ *if \(ctx\.require_fpu\(0x[0-9A-F]{8}\)\) return;[^\n]*\n', re.M)
LABEL = re.compile(r'loc_[0-9A-F]{8}: ;')

# Anything that can leave the run, observe the counter, or change the FP state.
BREAK = ('goto ', 'return', 'func_', 'native_', 'process_pending', 'interpret',
         'inst_count', 'ctx.pc', 'read_timebase', 'switch', 'trace', 'queue',
         'throw', 'setjmp', 'longjmp', 'msr')
NL = '\n'


def code_of(text):
    return NL.join(line.split('//', 1)[0] for line in text.split(NL))


def unit_kind(text):
    """'plain' stays inside a run, 'fp' is plain except for the FP-unavailable
    early return, 'break' ends the run."""
    code = code_of(text)
    if any(token in code for token in BREAK if token != 'return'):
        return 'break'
    if 'return' not in code:
        return 'plain'
    return 'fp' if 'return' not in FPRET.sub('', code) else 'break'


def batch_function(body):
    units = list(UNIT.finditer(body))
    if len(units) < 2:
        return body, 0, len(units)
    targets = set(re.findall(r'goto loc_([0-9A-F]{8})', body))
    count = len(units)
    starts = [m.start() for m in units] + [len(body)]
    heads = [body[m.start():m.end()] for m in units]
    stmts = [body[m.end():starts[i + 1]] for i, m in enumerate(units)]
    labels = [m[1] for m in units]
    kinds = [unit_kind(stmts[i]) for i in range(count)]
    for i in range(count):
        # A label inside a statement would make the batching non-invertible.
        if re.search(r'^loc_', stmts[i], re.M):
            kinds[i] = 'break'

    out = [body[:starts[0]]]
    runs = 0
    i = 0
    while i < count:
        j = i
        while kinds[j] != 'break' and j + 1 < count and labels[j + 1] not in targets:
            j += 1
        n = j - i + 1
        out.append(heads[i])                    # the run start keeps its own '++'
        if n > 1:
            out.append('    ctx.inst_count += %d;%s' % (n - 1, NL))
        fp_checked = False
        for k in range(n):
            text = stmts[i + k]
            remaining = n - 1 - k
            if kinds[i + k] == 'fp':
                if fp_checked:
                    # MSR.FP cannot change inside a run (no call, poll, msr access
                    # or ctx.pc write), and the first FP instruction of the run
                    # already proved it set: later per-instruction tests are
                    # redundant.
                    text = FPLINE.sub('', text)
                else:
                    fp_checked = True
                    if remaining > 0:
                        text = FPRET.sub(
                            lambda m: 'if (ctx.require_fpu(%s)) { ctx.inst_count -= %d; return; }'
                                      % (m[1], remaining), text)
            if k:
                out.append('loc_%s: ;%s' % (labels[i + k], NL))
            out.append(text)
        runs += 1
        i = j + 1
    return ''.join(out), runs, count


def process(text):
    stats = {'functions': 0, 'units': 0, 'runs': 0}
    pieces = []
    pos = 0
    for m in FUNC.finditer(text):
        stop = text.find(NL + '}' + NL, m.end())
        if stop < 0:
            continue
        body = text[m.end():stop + 1]
        new, runs, units = batch_function(body)
        stats['functions'] += 1
        stats['units'] += units
        stats['runs'] += runs if runs else units
        pieces.append(text[pos:m.end()])
        pieces.append(new)
        pos = stop + 1
    pieces.append(text[pos:])
    return ''.join(pieces), stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=None)
    args = parser.parse_args()

    root = args.root
    if root is None:
        root = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
        if not (root / 'vendor').is_dir():
            root = root.parent
    gen = root / 'export'
    if not gen.is_dir():
        print('Export not generated; instruction-count batching deferred.')
        return 0

    total = {'functions': 0, 'units': 0, 'runs': 0}
    changed = 0
    skipped = 0
    for path in sorted(gen.glob('output_*.cpp')):
        text = path.read_text(errors='surrogateescape')
        if MARKER in text:
            skipped += 1
            continue
        new, stats = process(text)
        for key in total:
            total[key] += stats[key]
        if new != text:
            if MARKER not in new:
                new = ('// ' + MARKER + ': one instruction-count update per straight-line run.\n'
                       '// The counter is exact whenever control leaves a run or anything reads it;\n'
                       '// inside a run it is ahead by fewer than one run length (see the stage docstring).\n'
                       + new)
            path.write_text(new, errors='surrogateescape')
            changed += 1
    if not changed and not skipped:
        print('Instruction-count batching: no per-instruction updates found.')
    else:
        print('Instruction-count batching: %d updates -> %d run updates across %d functions; '
              '%d chunk(s) rewritten, %d already staged.'
              % (total['units'], total['runs'], total['functions'], changed, skipped))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
