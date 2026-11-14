"""Collapse the SDK cache-range loops at their back edge (CotT's optimisation).

The Wii SDK's cache maintenance routines walk a range 32 bytes at a time:

    mtctr  r4
    loop: dcbf 0,r3          ; or dcbi / dcbst
          addi r3, r3, 0x20
          bdnz loop

The cache instruction itself emits no host work (the recompiler turns it into
a comment), but every iteration still costs a guest instruction dispatch, a
poll site and two instruction counts. In this game the two hottest loops were
DCFlushRange (0x8048B068) and DCInvalidateRange (0x8048B03C): together they
were a third of all guest samples, and the poll at the back edge ran seven
million times a second.

At the first back edge the counter still holds the number of lines, so all
but the last two iterations can be applied directly: the pointer advances by
32*(N-2) bytes and the instruction count by 2*(N-2). The two remaining
iterations run normally, so the loop's observable effect (the final pointer
value and the instruction count) is unchanged.

DCZeroRange (0x8048B110) is deliberately not touched: dcbz *zeroes* memory,
so its iterations are not free. (The recompiler still emits it as a comment,
which is a separate issue - see the report.)

This stage edits the generated guest code in export/, so it has to run after
the recompiler regenerates it, exactly like fix_guest_compile_flags.py.
"""
from pathlib import Path
import re

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
gen = port / 'export'
MARKER = 'NATIVE_CACHE_RANGE_COLLAPSE_MOM_V1'

# The MoM cache-range loops, named after the guest's own symbol so the
# generated function carries the address in its name.
TARGETS = [
    'DCInvalidateRange_8048B03C',   # dcbi
    'DCFlushRange_8048B068',        # dcbf
    'DCStoreRange_8048B098',        # dcbst
    'DCFlushRangeNoSync_8048B0C8',  # dcbf without the trailing sync
]

PATCH = ('    // ' + MARKER + ': skip all but the last two iterations at the first back-edge.\n'
         '    if (ctx.ctr > 2 && !nwii::runtime::g_trace_calls) { const uint32_t skip = ctx.ctr - 2;'
         ' ctx.gpr[3] += 32u * skip; ctx.inst_count += 2ull * skip; ctx.ctr = 2; }\n')

BACKEDGE = re.compile(r'\n    ctx\.ctr--;\n    if \(ctx\.ctr != 0\) goto (loc_[0-9A-F]+);\n')

done = []
changed = False
for path in sorted(gen.glob('output_*.cpp')):
    text = path.read_text(errors='surrogateescape')
    file_changed = False
    for name in TARGETS:
        head = 'void %s(' % name
        start = text.find(head)
        if start < 0:
            continue
        stop = text.find('\nvoid ', start + 1)
        if stop < 0:
            stop = len(text)
        body = text[start:stop]
        if MARKER in body:
            done.append(name)
            continue
        match = BACKEDGE.search(body)
        if not match:
            print('skip (no back edge):', name)
            continue
        label = body.index(match.group(1) + ': ;')
        loop = body[label:match.start()]
        poll = loop.find('    ctx.pc = 0x')
        if poll < 0:
            print('skip (no poll site):', name)
            continue
        statements = [line.strip() for line in loop[:poll].splitlines()
                      if line.strip()
                      and not line.strip().startswith('// 0x')
                      and not line.strip().startswith('loc_')]
        ok = (statements[:1] == ['// sync/cache instruction']
              and any(s.startswith('ctx.gpr[3] = ctx.gpr[3] + 32;') for s in statements)
              and statements.count('++ctx.inst_count;') == 2
              and len(statements) == 4)
        if not ok:
            print('skip (unexpected body):', name, statements)
            continue
        body = body[:match.start() + 1] + PATCH + body[match.start() + 1:]
        text = text[:start] + body + text[stop:]
        file_changed = True
        done.append(name)
    if file_changed:
        path.write_text(text, errors='surrogateescape')
        changed = True

if not changed and not done:
    print('No cache-range loop found in the generated code; run the recompiler first.')
else:
    print('Cache-range loops collapsed:', sorted(set(done)))
