r"""Non-aliasing guest context in the generated code (port of CotT fix_ctx_restrict_variant.py, experiment B).

Marker: NATIVE_MOM_CTX_RESTRICT_V1. Pairs with fix_static_ram_base_mom.py (CotT built the two together).

Every generated function definition `void func_X(CPUContext& ctx) {` in export/output_*.cpp becomes
`void func_X(CPUContext& __restrict ctx) {`, so GCC may keep guest registers in host registers across guest memory
accesses inside a function (guest RAM is separate memory, never the context). Out-of-line callees receive ctx or a member of
it, which forces the usual flush and reload. The only calls without a ctx-derived argument in the inline MMU accessors are
the five write-gather handlers (GX_WGPIPE_Write8/16/32/F32/F64); as in CotT they get a compiler memory barrier first.
CotT measured 6,958 -> 5,579 host instructions on one routine; the frame-level effect is measured here by A/B.

Edits export/output_*.cpp and include/runtime/cpu_context.h (vendor + export). Idempotent; --restore is exact.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_CTX_RESTRICT_V1'
ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / 'export'
HEADERS = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/include/runtime/cpu_context.h',
           EXPORT / 'nWiiRuntime/include/runtime/cpu_context.h']
DEF_OLD = re.compile(r'^(void func_[0-9A-F]{8})\(CPUContext& ctx\) \{', re.M)
DEF_NEW = re.compile(r'^(void func_[0-9A-F]{8})\(CPUContext& __restrict ctx\) \{', re.M)
TAG = '// ' + MARK + ': generated definitions take CPUContext& __restrict ctx\n'
BARRIER = 'asm volatile("" ::: "memory"); /* ' + MARK + ' */ '
GATHER = re.compile(r'\{ (GX_WGPIPE_Write(?:8|16|32|F32|F64)\(value\); return; \})')


def rw(path, fn):
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    n = fn(t)
    if n != t:
        path.write_bytes((n.replace('\n', '\r\n') if crlf else n).encode('utf-8', errors='surrogateescape'))
    return n


def main():
    outputs = sorted(EXPORT.glob('output_*.cpp'))
    if '--restore' in sys.argv:
        for p in outputs:
            rw(p, lambda t: DEF_NEW.sub(r'\1(CPUContext& ctx) {', t.replace(TAG, '', 1)))
        for h in HEADERS:
            rw(h, lambda t: t.replace(BARRIER, ''))
        print('ctx restrict removed.')
        return
    for h in HEADERS:
        t = h.read_bytes().decode('utf-8', errors='surrogateescape')
        if MARK not in t and len(GATHER.findall(t)) != 5:
            print('fix_ctx_restrict_mom: expected 5 gather calls in %s, found %d' % (h, len(GATHER.findall(t)))); sys.exit(1)
    for h in HEADERS:
        rw(h, lambda t: t if MARK in t else GATHER.sub(lambda m: '{ ' + BARRIER + m.group(1), t))
    total = 0
    for p in outputs:
        def edit(t):
            if TAG in t:
                return t
            n, k = DEF_OLD.subn(r'\1(CPUContext& __restrict ctx) {', t)
            return TAG + n if k else t
        total += len(DEF_NEW.findall(rw(p, edit)))
    print('ctx restrict installed: %d function definitions.' % total)


if __name__ == '__main__':
    main()
