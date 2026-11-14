r"""Inline fast path for the common paired-single load/store: float GQR, both halves, ordinary RAM.

Marker: NATIVE_MOM_PSQ_INLINE_V1. (The Crash of the Titans port inlines its psq instructions the same way:
NATIVE_PSQ_INSTRUCTION_INLINE_V1 / NATIVE_PSQ_QUANT_INLINE_V1.)

The generated code calls ctx.psq_load / ctx.psq_store for every psq_l / psq_st. Both are member functions with a lambda
and a quantisation-type switch, which GCC does not inline, and in MoM gameplay they cost ~5% of the guest thread
(psq_load, its lambda, psq_store's lambda). Almost every call is a stack save/restore of a paired register: GQR type 0
(plain float), W == 0 (both halves). This stage renames the existing bodies to psq_load_slow / psq_store_slow (unchanged,
noinline) and puts an always-inline front in their place that handles exactly that case with one 8-byte access, through
native_mom_lane::Span - the same ordinary-RAM proof the memory lane uses (refuses the first 16 physical bytes, MMIO,
anything outside MEM1/MEM2, and stores while a watch is armed). Every other case calls the unchanged slow body, so the
values written to fpr/ps1 and to guest RAM are bit-identical to the old path.

Edits include/runtime/cpu_context.h in the vendored runtime and in export/; every generated chunk includes it, so the
whole guest recompiles. --restore puts the header back byte for byte (backups under logs/stage-backups/).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_PSQ_INLINE_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_psq_inline_mom'
REL = 'include/runtime/cpu_context.h'
CRLF, LF = '\r\n', '\n'

LOAD_OLD = '  void psq_load(uint32_t frD, uint32_t addr, uint32_t W, uint32_t I) {\n'
LOAD_NEW = ('  // ' + MARK + ': float GQR, both halves, ordinary RAM -> one 8-byte load; everything else -> psq_load_slow.\n'
            '  __attribute__((always_inline)) inline void psq_load(uint32_t frD, uint32_t addr, uint32_t W, uint32_t I) {\n'
            '    if (__builtin_expect(W == 0 && (gqr[I] & 0x70000u) == 0, 1)) {\n'
            '      if (const uint8_t* p = native_mom_lane::Span(mmu, addr, 8u, false)) {\n'
            '        uint64_t raw; std::memcpy(&raw, p, 8);\n'
            '        const uint32_t l0 = __builtin_bswap32(uint32_t(raw)), l1 = __builtin_bswap32(uint32_t(raw >> 32));\n'
            '        float a, b; std::memcpy(&a, &l0, 4); std::memcpy(&b, &l1, 4);\n'
            '        fpr[frD] = a; ps1[frD] = b;\n'
            '        return;\n'
            '      }\n'
            '    }\n'
            '    psq_load_slow(frD, addr, W, I);\n'
            '  }\n'
            '  __attribute__((noinline)) void psq_load_slow(uint32_t frD, uint32_t addr, uint32_t W, uint32_t I) {\n')
STORE_OLD = '  void psq_store(uint32_t frS, uint32_t addr, uint32_t W, uint32_t I) {\n'
STORE_NEW = ('  // ' + MARK + ': float GQR, both halves, ordinary RAM, no watch -> one 8-byte store; else psq_store_slow.\n'
             '  __attribute__((always_inline)) inline void psq_store(uint32_t frS, uint32_t addr, uint32_t W, uint32_t I) {\n'
             '    if (__builtin_expect(W == 0 && (gqr[I] & 7u) == 0, 1)) {\n'
             '      if (uint8_t* p = native_mom_lane::Span(mmu, addr, 8u, g_watch_addr != 0)) {\n'
             '        const float a = (float)fpr[frS], b = (float)ps1[frS];\n'
             '        uint32_t l0, l1; std::memcpy(&l0, &a, 4); std::memcpy(&l1, &b, 4);\n'
             '        const uint64_t raw = uint64_t(__builtin_bswap32(l0)) | (uint64_t(__builtin_bswap32(l1)) << 32);\n'
             '        std::memcpy(p, &raw, 8);\n'
             '        return;\n'
             '      }\n'
             '    }\n'
             '    psq_store_slow(frS, addr, W, I);\n'
             '  }\n'
             '  __attribute__((noinline)) void psq_store_slow(uint32_t frS, uint32_t addr, uint32_t W, uint32_t I) {\n')


class Skip(Exception):
    pass


def transform(t):
    for old, new in ((LOAD_OLD, LOAD_NEW), (STORE_OLD, STORE_NEW)):
        if t.count(old) != 1:
            raise Skip('cpu_context.h: expected one %r, found %d' % (old.strip(), t.count(old)))
        t = t.replace(old, new, 1)
    return t


def main():
    restore = '--restore' in sys.argv
    manifest_path = BACKUP / 'manifest.json'
    if restore:
        if not manifest_path.exists():
            print('psq inline: nothing to restore.')
            return
        manifest = json.loads(manifest_path.read_text())
        for key, digest in manifest.items():
            if hashlib.sha256((TREES[key] / REL).read_bytes()).hexdigest() != digest:
                raise Skip(key + ' changed since this stage wrote it')
        for key in manifest:
            shutil.copyfile(BACKUP / key / REL, TREES[key] / REL)
        shutil.rmtree(BACKUP)
        print('psq inline removed: %d file(s) restored.' % len(manifest))
        return
    plan = []
    for key, base in TREES.items():
        raw = (base / REL).read_bytes()
        text = raw.decode('utf-8', errors='surrogateescape')
        crlf = CRLF in text
        text = text.replace(CRLF, LF)
        if MARK in text:
            continue
        new = transform(text)
        plan.append((key, base, raw, (new.replace(LF, CRLF) if crlf else new).encode('utf-8', errors='surrogateescape')))
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for key, base, raw, new in plan:
        dst = BACKUP / key / REL
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        (base / REL).write_bytes(new)
        manifest[key] = hashlib.sha256(new).hexdigest()
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print('psq inline installed: %d file(s) changed.' % len(plan))


if __name__ == '__main__':
    try:
        main()
    except Skip as reason:
        print('fix_psq_inline_mom: ' + str(reason))
        sys.exit(1)
