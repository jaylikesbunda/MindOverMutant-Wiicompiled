"""Keep the hottest guest registers in host locals inside the hottest generated functions.

The generated guest code keeps the PowerPC register file in ``CPUContext``: every
guest register is a memory operand of the object ``ctx``, and because
``ctx.mmu.write32(...)`` is an opaque call as far as the compiler is concerned, a
guest store forces it to assume ``ctx`` changed.  A guest register therefore
round-trips through memory on almost every guest instruction, and nothing can be
kept in a CPU register across a guest instruction boundary.

This pass keeps ``gpr[0..31]``, ``ctr``, ``xer`` and the 32 CR bits of the
selected functions in C++ locals instead, and writes them back to ``ctx`` exactly
where something else could observe them.  It is a port of the reference port's
``tools/promote_regs.py`` (Crash of the Titans), re-derived for this tree's
generated code: the parser accepts *this* emitter's statement shapes, the
barrier set is derived from *this* runtime's helpers, and the promoted classes
are narrowed to what this runtime can prove (see "What is not promoted").

Model, per function.  Only functions whose every statement has a recognised
shape are touched; anything unusual rejects the whole function and leaves it
byte-identical.

  * promoted state: ``g0..g31`` (gpr), ``ctrv`` (ctr), ``xer``, ``c<f><b>`` (CR
    field f bit b).
  * a *plain* line touches ``ctx`` only through promoted fields, ``ctx.pc``,
    ``ctx.lr``, ``ctx.msr``, ``ctx.mmu``, ``ctx.fpr``/``ctx.ps1``, ``ctx.gqr``,
    ``ctx.require_fpu`` or the FP helpers.  Its promoted references become the
    locals, everything else is emitted unchanged.
  * a *barrier* line passes ``ctx`` to something (a guest callee, the poll, the
    interpreter, an HLE stub, ``handle_syscall``).  It is emitted unchanged, and
    operates on ``ctx`` memory as before:
        spill(dirty) ; line ; reload(live-after)
  * every ``return;`` writes back whatever may be dirty at that point (one
    shared ``exit_N`` block per distinct dirty set).
  * ``dirty`` = may have been written since the last sync (forward dataflow);
    ``live`` = may be read before being rewritten, counting the spills that the
    dirty sets imply (backward dataflow).  Entry reloads (function start and
    every entry-switch case) load only what is live there.

Why the invariant holds.  ``spill(dirty)`` writes the locals into ``ctx`` before
a barrier, so anything that can observe guest state - a callee, the poll, an HLE
stub, a longjmp out of one - sees exactly the values the unpromoted code would
have left there.  ``reload(live-after)`` re-reads what the barrier may have
changed.  For the analysis to be sound, every bit that a spill writes must be
defined on *every* path reaching that spill; the backward pass therefore treats
a barrier's live-in set as its dirty set (``live = dbefore``), which forces those
bits to be must-defined (or reloaded at an entry) along all paths.

Entry trampoline (``NWII_BUILD_PROMOTE_ENTRY_TRAMPOLINE``, default on): cases of
the entry switch whose label has the same live-in set share one materialiser (the
outer switch groups them, the group reloads the set once, a nested switch picks
the target).  Every case still materialises exactly its label's live-in set and
nothing else.  Only taken when the grouped text is strictly shorter, so a group
that would not shrink keeps the flat per-case reload.

What is *not* promoted, and why (all MoM-specific):

  * ``inst_count``.  Unlike the reference, this runtime reads the guest's retired
    instruction counter from outside the generated code through the global
    ``g_ctx_ptr``: ``get_os_time()`` (the clock cache's staleness guard) and
    ``ctx->inst_count`` in the host loop's ``[Sample]`` line.  It is also read by
    the inline ``if ((ctx.inst_count % 10000000) == 0)`` spin trace the emitter
    puts next to every poll, which is a plain line.  Promoting it would need an
    ic-sync category covering all of those, and the counter is already batched to
    one update per straight-line run by ``fix_inst_count_batch_mom.py``, so the
    remaining win is small and the risk is not.
  * ``fpr[]`` / ``ps1[]``.  ``ctx.require_fpu()`` is emitted in front of almost
    every FP instruction and calls ``acquire_fp_context()``, which *reloads*
    ``ctx.fpr[]``/``ctx.ps1[]`` in place from the guest's saved exception context
    when this thread does not own FP yet.  That write would be invisible to a
    local, so the FP register file has to stay in ``ctx``.  The FP helpers
    (``ctx.ps_*``, ``ctx.psq_*``) are left as calls for the same reason.
  * CR field state in functions that index ``ctx.cr[i]`` dynamically (``mfcr`` /
    ``mtcrf`` are emitted as a loop over ``i``).  Such a function keeps its CR in
    ``ctx`` and still gets the GPR/CTR/XER promotion.

Safety, and the one caveat.  Every path that can see promoted state is a barrier
or a member of the whitelist above; the audit behind that statement is in
``logs/cott-promote-regs.md``.  The single exception is diagnostic only, exactly
as in the reference port: the runtime's watch and sample probes read guest
registers through ``g_ctx_ptr`` from outside the generated code
(``watch_hit``'s r1/r3/r4/r24-31 chain walk when ``NWII_WATCH`` is set,
``[Sample]``'s r3, the ``[RunQ]`` r13 read, ``NWII_IPC_PROBE``'s r13).  Those can
print a stale value; they cannot change what the guest computes.  ``[Sample]``'s
``inst=`` field, which the perf measurements use, is not affected because
``inst_count`` is not promoted.

Cost control.  A function is left alone unless the promotion is expected to pay:
the number of promoted references in plain lines has to exceed twice the number
of spill/reload pairs it would add (each pair is two memory operations).  A
function that is mostly barriers is therefore never rewritten.

Environment:
  NWII_BUILD_PROMOTE_REGS=1    enable (off by default: measured neutral here)
  NWII_BUILD_PROMOTE_ENTRY_TRAMPOLINE=0   flat per-case entry reloads
  NWII_BUILD_PROMOTE_CR=0      leave CR state in ctx (diagnostic bisect aid)

Reads tools/hot_functions_mom.txt (regenerate with tools/mom_hot_functions.py)
and rewrites the matching functions in export/output_*.cpp.  Idempotent: a
promoted function carries the marker comment and is skipped on later passes.
"""
import collections
import collections
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    # Run straight out of tools/stages rather than through apply_stages.py.
    ROOT = ROOT.parent
MARKER = 'NATIVE_MOM_PROMOTE_REGS_V1'
HOT_LIST = 'tools/hot_functions_mom.txt'
OFF_ENV = 'NWII_BUILD_PROMOTE_REGS'
TRAMPOLINE_ENV = 'NWII_BUILD_PROMOTE_ENTRY_TRAMPOLINE'
NL = '\n'

FUNC_HEAD = re.compile(r'^void (\w+)\(CPUContext& ctx\) \{$', re.M)
TRACE_PREFIX = '    if (nwii::runtime::g_trace_calls && ctx.pc == '
LABEL = re.compile(r'^loc_([0-9A-F]{8}): ;$')
CASE = re.compile(r'^            case (0x[0-9A-F]+): goto loc_([0-9A-F]{8});$')
GOTO = re.compile(r'goto loc_([0-9A-F]{8})')
ANY_REF = re.compile(r'ctx\.gpr\[(\d+)\]|ctx\.cr\[(\d)\]\.(lt|gt|eq|so)\b|ctx\.ctr\b'
                     r'|ctx\.inst_count\b|ctx\.fpr\[(\d+)\]|ctx\.ps1\[(\d+)\]|ctx\.xer\b')
ASSIGN_AFTER = re.compile(r'\s*(?:=(?!=)|[-+*/%&|^]=|<<=|>>=|\+\+|--)')
INCDEC_BEFORE = re.compile(r'(?:\+\+|--)\s*$')
RETURN = re.compile(r'\breturn\s*;')
WHITELIST_PREFIX = ('ps_', 'psq_')
WHITELIST_EXACT = {'require_fpu'}
# Tokens that mean the function is not shaped the way this pass assumes.  They
# are matched against statement text only, never against the trailing comment
# (the emitter and the stage scripts use ``// NATIVE_...`` markers there).
UNSUPPORTED = ('NATIVE_SQRT', 'NATIVE_PLAIN', 'NATIVE_WPAD', 'NATIVE_CACHE',
               'static ', '__asm', '__builtin', '&ctx.gpr', '&ctx.cr', '&ctx.ctr',
               '&ctx.inst_count', 'setjmp', 'longjmp', 'throw', 'reinterpret_cast')

BIT_CTR = 32
BIT_CR = 33          # 33 + field*4 + bit
BIT_IC = 65
BIT_FPR = 66         # fpr[i]
BIT_PS1 = 98         # ps1[i]
BIT_XER = 130
FP_MASK = ((1 << 64) - 1) << BIT_FPR
CR_MASK = ((1 << 32) - 1) << BIT_CR
CR_BITS = {'lt': 0, 'gt': 1, 'eq': 2, 'so': 3}
PROMOTE_IC = False       # see the docstring
PROMOTE_FP = False       # see the docstring
PROMOTE_CR = os.environ.get('NWII_BUILD_PROMOTE_CR', '1') != '0'


def enabled():
    """Opt-in: NWII_BUILD_PROMOTE_REGS=1.

    The transform is correct (see the module docstring and
    logs/cott-promote-regs.md) but measured neutral on this port: six baseline
    and six staged 90 s runs interleaved on the same machine gave a median of
    5.35 ns per guest instruction before and 5.34 ns after (means 5.45 vs 5.27,
    run-to-run spread +-10%), and a controlled microbenchmark of the rewritten
    functions puts the promoted code 1.7% slower.  It is therefore off by
    default; deleting the entry from STAGES in tools/apply_stages.py is also a
    complete rollback.
    """
    return os.environ.get(OFF_ENV, '0') == '1'


def trampoline_enabled():
    return os.environ.get(TRAMPOLINE_ENV, '1') != '0'


def code_of(line):
    """The line without its trailing // comment (quote aware)."""
    if '//' not in line:
        return line
    in_str = False
    i = 0
    while i < len(line) - 1:
        c = line[i]
        if c == '"' and (i == 0 or line[i - 1] != '\\'):
            in_str = not in_str
        elif not in_str and c == '/' and line[i + 1] == '/':
            return line[:i]
        i += 1
    return line


def bit_of(match):
    text = match.group(0)
    if text == 'ctx.ctr':
        return BIT_CTR
    if text == 'ctx.inst_count':
        return BIT_IC
    if text == 'ctx.xer':
        return BIT_XER
    if match.group(1) is not None:
        return int(match.group(1))
    if match.group(4) is not None:
        return BIT_FPR + int(match.group(4))
    if match.group(5) is not None:
        return BIT_PS1 + int(match.group(5))
    return BIT_CR + int(match.group(2)) * 4 + CR_BITS[match.group(3)]


def local_name(bit):
    if bit < 32:
        return 'g%d' % bit
    if bit == BIT_CTR:
        return 'ctrv'
    if bit == BIT_IC:
        return 'ic'
    if bit == BIT_XER:
        return 'xer'
    if bit >= BIT_PS1:
        return 'p%d' % (bit - BIT_PS1)
    if bit >= BIT_FPR:
        return 'f%d' % (bit - BIT_FPR)
    b = bit - BIT_CR
    return 'c%d%s' % (b // 4, ('lt', 'gt', 'eq', 'so')[b % 4])


def ctx_name(bit):
    if bit < 32:
        return 'ctx.gpr[%d]' % bit
    if bit == BIT_CTR:
        return 'ctx.ctr'
    if bit == BIT_IC:
        return 'ctx.inst_count'
    if bit == BIT_XER:
        return 'ctx.xer'
    if bit >= BIT_PS1:
        return 'ctx.ps1[%d]' % (bit - BIT_PS1)
    if bit >= BIT_FPR:
        return 'ctx.fpr[%d]' % (bit - BIT_FPR)
    b = bit - BIT_CR
    return 'ctx.cr[%d].%s' % (b // 4, ('lt', 'gt', 'eq', 'so')[b % 4])


def bits_of(mask):
    out = []
    bit = 0
    while mask:
        if mask & 1:
            out.append(bit)
        mask >>= 1
        bit += 1
    return out


def spill_stmt(mask):
    return ' '.join('%s = %s;' % (ctx_name(b), local_name(b)) for b in bits_of(mask))


def reload_stmt(mask):
    return ' '.join('%s = %s;' % (local_name(b), ctx_name(b)) for b in bits_of(mask))


class Statement:
    __slots__ = ('text', 'code', 'comment', 'kind', 'uses', 'writes', 'mustdef',
                 'has_return', 'gotos', 'dbefore', 'lafter', 'refs')


DYNAMIC_GPR = re.compile(r'ctx\.gpr\[(?!\d+\])|ctx\.gpr(?!\[)')
FOR_LOOP = re.compile(r'^(\s*)for \(int i = (\d+); i <= (\d+); \+\+i\) \{$')


def unroll_register_loops(body):
    """Expand `stmw`/`lmw`, which the emitter writes as a loop over `ctx.gpr[i]`.

    The one shape it produces is

        {
            uint32_t addr = <expr>;
            for (int i = A; i <= B; ++i) {
                <op(ctx.gpr[i])>;
                addr += 4;
            }
        }

    with literal A and B.  Unrolling it into B-A+1 explicit statements with
    ``ctx.gpr[A]`` ... ``ctx.gpr[B]`` is exactly what the guest instruction does,
    and it lets the register promotion see (and localise) every access.  Anything
    else - a loop bound that is not a literal, a body that uses the index for
    something other than the register - is left alone, and the caller then
    rejects the function because a dynamic index remains.
    """
    out = []
    i = 0
    while i < len(body):
        m = FOR_LOOP.match(body[i])
        if not m:
            out.append(body[i])
            i += 1
            continue
        indent, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        close = indent + '}'
        j = i + 1
        while j < len(body) and body[j] != close:
            j += 1
        if j >= len(body) or lo > hi or hi > 31:
            out.append(body[i])
            i += 1
            continue
        inner = body[i + 1:j]
        text = NL.join(inner)
        if 'ctx.gpr[i]' not in text or re.search(r'\bi\b', text.replace('ctx.gpr[i]', '')):
            out.append(body[i])
            i += 1
            continue
        # the index must not leak into the other statements either
        if re.search(r'\bi\b', re.sub(r'ctx\.gpr\[i\]', '', text).replace('++i', '')):
            out.append(body[i])
            i += 1
            continue
        for reg in range(lo, hi + 1):
            for line in inner:
                out.append(line.replace('ctx.gpr[i]', 'ctx.gpr[%d]' % reg))
        i = j + 1
    return out


def classify(code):
    if DYNAMIC_GPR.search(code):
        # Only reachable if unroll_register_loops did not recognise the shape;
        # the caller rejects such a function, so treat it as a ctx-passing line
        # rather than silently mis-promoting it.
        return 'barrier'
    if re.search(r'\bctx\b(?!\.)', code):
        return 'barrier'
    for m in re.finditer(r'ctx\.(\w+)\s*\(', code):
        name = m.group(1)
        if name in WHITELIST_EXACT:
            continue
        if name.startswith(WHITELIST_PREFIX) and not name.startswith('ps_cmp'):
            continue
        return 'barrier'
    return 'plain'


def analyse_line(text, depth):
    ln = Statement()
    ln.text = text
    ln.code = code_of(text)
    ln.comment = text[len(ln.code):]
    ln.kind = classify(ln.code)
    if ln.kind == 'barrier' and depth > 0:
        # Inside a block the line may not execute, so a spill/reload around it
        # must leave the dirty set unchanged afterwards.
        ln.kind = 'cbarrier'
    ln.has_return = bool(RETURN.search(ln.code))
    ln.gotos = GOTO.findall(ln.code)
    uses = writes = mustdef = 0
    refs = list(ANY_REF.finditer(ln.code))
    ln.refs = len(refs)
    stripped = ln.code.strip()
    if depth == 0 and refs and ln.kind == 'plain' and stripped.count(';') == 1 and stripped.endswith(';'):
        # `a = b = expr;` defines every target of the chain.
        pos = 0
        while True:
            m = ANY_REF.match(stripped, pos)
            if not m:
                break
            a = re.compile(r'\s*=(?!=)').match(stripped, m.end())
            if not a:
                break
            mustdef |= 1 << bit_of(m)
            pos = a.end()
            while pos < len(stripped) and stripped[pos] == ' ':
                pos += 1
            if not ANY_REF.match(stripped, pos):
                break
    for m in refs:
        bit = bit_of(m)
        tail = ln.code[m.end():]
        head = ln.code[:m.start()]
        is_write = bool(ASSIGN_AFTER.match(tail)) or bool(INCDEC_BEFORE.search(head))
        compound = (bool(re.match(r'\s*(?:[-+*/%&|^]=|<<=|>>=|\+\+|--)', tail))
                    or bool(INCDEC_BEFORE.search(head)))
        if is_write:
            writes |= 1 << bit
        if (not is_write) or compound:
            uses |= 1 << bit
    ln.uses, ln.writes, ln.mustdef = uses, writes, mustdef
    if ln.kind != 'plain':
        # A barrier consumes and produces the whole ctx object, so its own text
        # is described by ctx memory, not by the locals.
        ln.uses = ln.writes = ln.mustdef = 0
    return ln


class Unit:
    __slots__ = ('label', 'lines', 'succ', 'falls', 'din', 'dout', 'lin', 'lout')


def parse(text):
    lines = text.split(NL)
    head = FUNC_HEAD.match(lines[0])
    if not head or lines[-1] != '}':
        return None
    name = head.group(1)
    i = 1
    trace = None
    if lines[i].startswith(TRACE_PREFIX):
        trace = lines[i]
        i += 1
    cases = []
    special = []
    entry = None
    if lines[i].startswith('    if (ctx.pc != 0x'):
        m = re.match(r'^    if \(ctx\.pc != 0x([0-9A-F]{8})\) \{$', lines[i])
        if not m:
            return None
        # The dispatch guard is the authoritative entry address.  A function's
        # name is not: several MoM functions are emitted under the name of the
        # *inner* address the dispatcher starts them at while their body begins
        # at an earlier address, and the guard is what the dispatcher relies on.
        entry = int(m.group(1), 16)
        if trace is not None and ('0x%08X' % entry) not in trace:
            return None
        i += 1
        if not lines[i].startswith('        switch (ctx.pc) {'):
            return None
        i += 1
        while not lines[i].startswith('        }'):
            m = CASE.match(lines[i])
            if m:
                cases.append((m.group(1), m.group(2)))
            elif (lines[i].startswith('            case 0x0: return;')
                  or lines[i].startswith('            default: nwii::runtime::interpret_step(ctx); return;')):
                special.append(lines[i])
            else:
                return None
            i += 1
        i += 1
        if lines[i] != '    }':
            return None
        i += 1
    if entry is None:
        if trace is not None:
            m = re.search(r'ctx\.pc == 0x([0-9A-F]{8})', trace)
            if m:
                entry = int(m.group(1), 16)
        if entry is None:
            try:
                entry = int(name.rsplit('_', 1)[1], 16)
            except (IndexError, ValueError):
                return None
    body = lines[i:-1]
    return name, entry, trace, cases, special, body


def promote(text):
    """Return (new_text, stats) or (None, reason)."""
    parsed = parse(text)
    if parsed is None:
        return None, 'unrecognised preamble'
    name, entry, trace, cases, special, body = parsed
    body = unroll_register_loops(body)
    for line in body:
        code = code_of(line)
        for token in UNSUPPORTED:
            if token in code:
                return None, 'unsupported token ' + token
    joined = NL.join(code_of(l) for l in body)
    if DYNAMIC_GPR.search(joined):
        # A guest register indexed by a runtime value: the promotion cannot name
        # it, so leave the whole function alone rather than guess.
        return None, 'dynamic gpr index'
    dynamic_cr = (bool(re.search(r'ctx\.cr\[(?!\d\])', joined))
                  or bool(re.search(r'ctx\.cr\[\d\](?!\.(?:lt|gt|eq|so))', joined)))
    if re.search(r'ctx\.(?:fpr|ps1)\[(?!\d+\])', joined):
        return None, 'dynamic fp index'
    use_cr = PROMOTE_CR and not dynamic_cr
    # --- split into units ---------------------------------------------------
    units = []
    cur = Unit()
    cur.label = None
    cur.lines = []
    depth = 0
    for text_line in body:
        m = LABEL.match(text_line)
        if m:
            if depth != 0:
                return None, 'label inside block'
            units.append(cur)
            cur = Unit()
            cur.label = m.group(1)
            cur.lines = []
            continue
        ln = analyse_line(text_line, depth)
        cur.lines.append(ln)
        depth += ln.code.count('{') - ln.code.count('}')
    units.append(cur)
    if depth != 0:
        return None, 'unbalanced braces'
    index = {u.label: k for k, u in enumerate(units) if u.label}
    for u in units:
        for ln in u.lines:
            for g in ln.gotos:
                if g not in index:
                    return None, 'goto to unknown label'
            if ln.kind != 'plain' and ln.gotos:
                return None, 'barrier line with goto'
    for _, label in cases:
        if label not in index:
            return None, 'case label missing'
    # --- classes the promotion cannot carry --------------------------------
    for u in units:
        for ln in u.lines:
            ln.uses &= ~FP_MASK
            ln.writes &= ~FP_MASK
            ln.mustdef &= ~FP_MASK
            if not PROMOTE_IC:
                bit = 1 << BIT_IC
                ln.uses &= ~bit
                ln.writes &= ~bit
                ln.mustdef &= ~bit
            if not use_cr:
                ln.uses &= ~CR_MASK
                ln.writes &= ~CR_MASK
                ln.mustdef &= ~CR_MASK
    # --- control flow ------------------------------------------------------
    for k, u in enumerate(units):
        succ = set()
        falls = True
        last = None
        for ln in u.lines:
            for g in ln.gotos:
                succ.add(index[g])
            if ln.code.strip():
                last = ln.code.strip()
        if last is not None:
            if re.match(r'^goto loc_[0-9A-F]{8};$', last) or re.match(r'^(?:ctx\.pc = [^;]+; )?return;$', last):
                falls = False
        if falls and k + 1 < len(units):
            succ.add(k + 1)
        u.succ = sorted(succ)
        u.falls = falls
    n = len(units)
    preds = [[] for _ in range(n)]
    for k, u in enumerate(units):
        for s in u.succ:
            preds[s].append(k)
    entries = {0} | {index[l] for _, l in cases}
    # --- forward may-dirty -------------------------------------------------
    for u in units:
        u.din = 0
        u.dout = 0

    def sweep_forward(u, d):
        for ln in u.lines:
            ln.dbefore = d
            if ln.kind == 'barrier':
                d = 0
            elif ln.kind == 'cbarrier':
                pass
            else:
                d |= ln.writes
        return d

    work = list(range(n))
    inq = set(work)
    while work:
        k = work.pop(0)
        inq.discard(k)
        u = units[k]
        din = 0
        for p in preds[k]:
            din |= units[p].dout
        u.din = din
        dout = sweep_forward(u, din)
        if dout != u.dout:
            u.dout = dout
            for s in u.succ:
                if s not in inq:
                    work.append(s)
                    inq.add(s)
    for u in units:
        u.dout = sweep_forward(u, u.din)
    # --- backward liveness (a spill of a dirty bit counts as a use) --------
    for u in units:
        u.lin = 0
        u.lout = 0

    def sweep_backward(u, live):
        for ln in reversed(u.lines):
            if ln.kind == 'barrier':
                ln.lafter = live
                # The spill writes exactly dbefore, so every one of those bits
                # must be defined on every path: force them live-in.
                live = ln.dbefore
            elif ln.kind == 'cbarrier':
                ln.lafter = live
                live = live | ln.dbefore
            else:
                ln.lafter = live
                after = live
                if ln.has_return:
                    after |= ln.dbefore | ln.writes
                live = (after & ~ln.mustdef) | ln.uses
        return live

    work = list(range(n - 1, -1, -1))
    inq = set(work)
    while work:
        k = work.pop(0)
        inq.discard(k)
        u = units[k]
        lout = 0
        for s in u.succ:
            lout |= units[s].lin
        u.lout = lout
        lin = sweep_backward(u, lout)
        if lin != u.lin:
            u.lin = lin
            for p in preds[k]:
                if p not in inq:
                    work.append(p)
                    inq.add(p)
    for u in units:
        sweep_backward(u, u.lout)
    referenced = 0
    for u in units:
        for ln in u.lines:
            referenced |= ln.uses | ln.writes
    if not referenced:
        return None, 'no promotable references'

    def sanitise(mask):
        return mask & referenced

    # --- emission ----------------------------------------------------------
    out = [text.split(NL, 1)[0], '    // ' + MARKER]
    if trace:
        out.append(trace)
    decl = []
    gprs = [b for b in bits_of(referenced) if b < 32]
    if gprs:
        decl.append('    uint32_t ' + ', '.join('g%d' % b for b in gprs) + ';')
    if referenced >> BIT_CTR & 1:
        decl.append('    uint32_t ctrv;')
    if referenced >> BIT_IC & 1:
        decl.append('    uint64_t ic;')
    if referenced >> BIT_XER & 1:
        decl.append('    uint32_t xer;')
    crs = [b for b in bits_of(referenced) if BIT_CR <= b < BIT_CR + 32]
    if crs:
        decl.append('    bool ' + ', '.join(local_name(b) for b in crs) + ';')
    out.extend(decl)
    stats = dict(name=name, units=len(units), barriers=0, returns=0, exits=0,
                 spill_bits=0, reload_bits=0, promoted=bin(referenced).count('1'),
                 cr=len(crs), gain=0, cost=0, entry_cases=len(cases))

    def flat_case_lines(live, members):
        return ['            case %s: %sgoto loc_%s;'
                % (addr, (reload_stmt(live) + ' ') if live else '', label)
                for addr, label in members]

    def grouped_case_lines(live, members):
        if not live or len(members) == 1:
            return flat_case_lines(live, members)
        lines = ['            ' + ' '.join('case %s:' % a for a, _ in members),
                 '                ' + reload_stmt(live),
                 '                switch (ctx.pc) {']
        lines += ['                    case %s: goto loc_%s;' % (addr, label)
                  for addr, label in members]
        lines.append('                    default: goto loc_%s;' % members[-1][1])
        lines.append('                }')
        return lines

    def emit_entry_switch():
        grouped = {}
        order = []
        for addr, label in cases:
            live = sanitise(units[index[label]].lin)
            grouped.setdefault(live, []).append((addr, label))
            if len(grouped[live]) == 1:
                order.append(live)
        assert sum(len(m) for m in grouped.values()) == len(cases)
        for live in order:
            members = grouped[live]
            flat = flat_case_lines(live, members)
            if not trampoline_enabled():
                out.extend(flat)
                continue
            assert all(sanitise(units[index[l]].lin) == live for _, l in members)
            shared = grouped_case_lines(live, members)
            out.extend(shared if sum(map(len, shared)) < sum(map(len, flat)) else flat)

    if cases or special:
        out.append('    if (ctx.pc != 0x%08X) {' % entry)
        out.append('        switch (ctx.pc) {')
        emit_entry_switch()
        out.extend(special)
        out.append('        }')
        out.append('    }')
    live0 = sanitise(units[0].lin)
    if live0:
        out.append('    ' + reload_stmt(live0))
        stats['reload_bits'] += bin(live0).count('1')
    body_start = len(out)
    exits = {}

    def subst(code):
        def rep(m):
            bit = bit_of(m)
            if not PROMOTE_IC and bit == BIT_IC:
                return m.group(0)
            # Only fpr[0..31] and ps1[0..31] live in this range; xer is above it.
            if not PROMOTE_FP and BIT_FPR <= bit < BIT_XER:
                return m.group(0)
            if not use_cr and BIT_CR <= bit < BIT_CR + 32:
                return m.group(0)
            return local_name(bit)
        return ANY_REF.sub(rep, code)

    def unsubstituted(code):
        """A reference to a promoted field that `subst` left alone.

        Everything left in `referenced` after the class strip is declared as a
        local and written back by the spill/reload blocks, so a plain line must
        not still touch it through `ctx`: the spill would then push a stale
        local over the value the line just stored.
        """
        for m in ANY_REF.finditer(code):
            if (1 << bit_of(m)) & referenced:
                return m.group(0)
        return None

    for u in units:
        if u.label:
            out.append('loc_%s: ;' % u.label)
        for ln in u.lines:
            if ln.kind in ('barrier', 'cbarrier'):
                sp = sanitise(ln.dbefore)
                rl = sanitise(ln.lafter)
                if sp:
                    out.append('    ' + spill_stmt(sp))
                out.append(ln.text)
                if rl:
                    out.append('    ' + reload_stmt(rl))
                stats['barriers'] += 1
                stats['spill_bits'] += bin(sp).count('1')
                stats['reload_bits'] += bin(rl).count('1')
                continue
            # Reference occurrences in this plain line.  This counts every
            # promoted-class reference, including inst_count, which the class
            # strip above removed from `referenced` - so the gate is a little
            # permissive.  Tightening it to `& referenced` would reject three
            # more functions (func_80005F34, func_8029E1AC, func_8041F5D4); see
            # logs/cott-promote-regs.md section 2.
            stats['gain'] += ln.refs
            code = subst(ln.code)
            left = unsubstituted(code)
            if left is not None:
                return None, 'promoted reference left in a plain line: ' + left
            if ln.has_return:
                dirty = sanitise(ln.dbefore | ln.writes)
                if dirty:
                    exit_id = exits.setdefault(dirty, len(exits))
                    code = RETURN.sub(lambda m, k=exit_id: 'goto exit_%d;' % k, code)
                    stats['returns'] += 1
            out.append(code + ln.comment)
    last = units[-1]
    tail = sanitise(last.dout) if (last.falls or not last.lines) else 0
    if last.falls or not last.lines:
        out.append('    ' + (spill_stmt(tail) + ' ' if tail else '') + 'return;')
        stats['spill_bits'] += bin(tail).count('1')
    for dirty, k in sorted(exits.items(), key=lambda kv: kv[1]):
        out.append('exit_%d: ;' % k)
        out.append('    ' + spill_stmt(dirty) + ' return;')
        stats['spill_bits'] += bin(dirty).count('1')
    out.append('}')
    stats['exits'] = len(exits)
    stats['cost'] = stats['spill_bits'] + stats['reload_bits']
    if stats['promoted'] < 2:
        return None, 'too few promoted registers'
    if stats['gain'] <= stats['cost']:
        # One promoted reference saves one memory access; one spill bit or reload
        # bit adds one.  Only rewrite when the accesses removed outnumber those
        # added, so a function that is mostly barriers is never touched.
        return None, 'would not pay (gain %d vs cost %d)' % (stats['gain'], stats['cost'])
    new_text = NL.join(out)
    before = invariant_tokens(NL.join(body))
    after = invariant_tokens(NL.join(out[body_start:]))
    if sorted(before) != sorted(after):
        missing = sorted((collections.Counter(before) - collections.Counter(after)).elements())
        extra = sorted((collections.Counter(after) - collections.Counter(before)).elements())
        return None, 'invariant mismatch: lost %s gained %s' % (missing[:6], extra[:6])
    return new_text, stats


def find_span(text, start):
    end = text.find(NL + '}' + NL, start)
    if end < 0:
        return None
    return start, end + len(NL + '}')


TOKEN = re.compile(r'ctx\.\w+|0x[0-9A-Fa-f]+|\b\d+\b|goto loc_[0-9A-F]{8}'
                   r'|loc_[0-9A-F]{8}:')
PROMOTED = re.compile(r'ctx\.gpr\[\d+\]|ctx\.cr\[\d\]\.(?:lt|gt|eq|so)\b'
                      r'|ctx\.ctr\b|ctx\.xer\b')


def invariant_tokens(text):
    """Everything the transform must not change: every reference to a field it
    does not promote, every constant, and every guest label.

    Promotion replaces promoted ``ctx.<field>`` references with locals and adds
    spill/reload/exit blocks; the rest of each statement has to survive verbatim,
    so the two multisets must be equal.  A mismatch means the rewrite dropped,
    duplicated or reordered something - the function is then left untouched.
    """
    return TOKEN.findall(PROMOTED.sub(' ', text))


def main():
    if not enabled():
        print('Guest register promotion disabled (%s=0).' % OFF_ENV)
        return 0
    hot_path = ROOT / HOT_LIST
    gen = ROOT / 'export'
    if not gen.is_dir():
        print('Export not generated; guest register promotion deferred.')
        return 0
    if not hot_path.is_file():
        print('Missing %s - run tools/mom_hot_functions.py first.' % HOT_LIST)
        return 1
    hot = [l.strip() for l in hot_path.read_text().split(NL)
           if l.strip() and not l.strip().startswith('#')]
    rank = {name: i for i, name in enumerate(hot)}
    files = sorted(gen.glob('output_*.cpp')) + sorted(gen.glob('output_gap_*.cpp'))
    promoted = []
    skipped = {}
    for path in files:
        text = path.read_text(errors='surrogateescape')
        if not text:
            continue
        pieces = []
        pos = 0
        changed = False
        for match in FUNC_HEAD.finditer(text):
            name = match.group(1)
            if name not in rank:
                continue
            span = find_span(text, match.start())
            if span is None:
                skipped['no end of function'] = skipped.get('no end of function', 0) + 1
                continue
            a, b = span
            if MARKER in text[a:b]:
                skipped['already promoted'] = skipped.get('already promoted', 0) + 1
                continue
            new, stats = promote(text[a:b])
            if new is None:
                skipped[stats] = skipped.get(stats, 0) + 1
                continue
            pieces.append(text[pos:a])
            pieces.append(new)
            pos = b
            changed = True
            promoted.append(stats)
        if not changed:
            continue
        pieces.append(text[pos:])
        out = ''.join(pieces)
        # The rewrite must not add or lose a function definition.
        if len(FUNC_HEAD.findall(out)) != len(FUNC_HEAD.findall(text)):
            raise SystemExit('%s: function count changed, refusing to write' % path)
        path.write_text(out, errors='surrogateescape')
    promoted.sort(key=lambda s: rank[s['name']])
    for s in promoted:
        print('   %-46s units=%3d barriers=%3d entries=%3d promoted=%2d cr=%2d '
              'spill=%4d reload=%4d gain=%5d cost=%4d exits=%d'
              % (s['name'], s['units'], s['barriers'], s['entry_cases'],
                 s['promoted'], s['cr'], s['spill_bits'], s['reload_bits'],
                 s['gain'], s['cost'], s['exits']))
    total = dict(functions=len(promoted), barriers=sum(s['barriers'] for s in promoted),
                 spill=sum(s['spill_bits'] for s in promoted),
                 reload=sum(s['reload_bits'] for s in promoted),
                 gain=sum(s['gain'] for s in promoted),
                 cost=sum(s['cost'] for s in promoted))
    print('Guest register promotion: %d of %d hot functions promoted '
          '(%d barriers, %d spill bits, %d reload bits, gain %d vs cost %d); '
          'left alone: %s'
          % (total['functions'], len(hot), total['barriers'], total['spill'],
             total['reload'], total['gain'], total['cost'], skipped or 'none'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
