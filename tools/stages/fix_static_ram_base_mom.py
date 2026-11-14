r"""Fixed-address guest RAM: MEM1 and MEM2 live at constant host addresses.

Marker: NATIVE_STATIC_RAM_MOM_V1.  (Crash of the Titans does the same thing: native-port/tools/fix_static_ram_base.py
with runtime-overlays/static_ram_base.cpp, behind -DNWII_STATIC_RAM_BASE.  The fixed addresses are the same:
0x20000000 for MEM1 and 0x30000000 for MEM2.)

Why: MMU::mem1 and MMU::mem2 are std::vector<uint8_t>.  Every guest load and store in the generated code goes through
MMU::read8..write64 (and the float forms, the psq inline and the span proofs in native_mom_lane.h), and each one reads
the vector's begin and end pointers from the MMU object to compare the size and then reads begin again for the data.
When the banks are FixedRam objects, data() and size() are compile-time constants, so the same, unchanged accessor
text compiles to one compare against an immediate and one disp32 address.  The fast-path code is not edited; it
simply resolves against a constant.

What it changes (both trees, vendor/NWiiRecomp/nWiiRuntime and export/nWiiRuntime; every edit is checked first):

  include/runtime/native_static_ram_mom.h   NEW.  Copied from overlays/static_ram_mom/native_static_ram_mom.h.
                                            Defines FixedRam<Host, Bytes>, the constants, and Commit(), which
                                            VirtualAlloc()s the bank once at its fixed address (aborts if taken).
  include/runtime/cpu_context.h             (a) adds #include "runtime/native_static_ram_mom.h" after the
                                            native_mom_lane.h include line (anchor: that exact include line, once).
                                            (b) replaces the MMU members and constructor (anchor: the struct MMU
                                            header through its constructor, with the two trailing spaces that the
                                            resize lines carry) with Mem1Ram/Mem2Ram members and two Commit calls.
  src/hw/hw_dsp.cpp                         the AX bank picker bound `auto& bank = cond ? mem2 : mem1`.  With two
                                            different FixedRam types that conditional does not compile, so it is
                                            rewritten to pick a data pointer and a size (anchor: the four lines of
                                            native_ax()'s lambda, once).  Same values for both banks.

Nothing else is touched.  The sentinel (physical 0x0C), MMIO, the write-gather page, watches, the +8 padding, the
bounds tests and every *_slow path keep their text.  The other users of guest RAM (native_mom_lane.h Span,
native_guest_bytes.h, fifo_parser.cpp, gx.cpp, native_savestate_ios.inc, main.cpp) only call .data(), .size() or
index through the same interface, so they need no change; the audit is in logs/agent-reports/static_ram_base.md.

No new translation unit and no CMakeLists.txt edit: Commit() is an inline function in the header, and cpu_context.h
already includes <windows.h> (through native_idle_wait.h).

Usage:
    python tools/stages/fix_static_ram_base_mom.py            install (idempotent: a tree that already has the marker
                                                              is left alone)
    python tools/stages/fix_static_ram_base_mom.py --check    report whether each anchor matches; write nothing
    python tools/stages/fix_static_ram_base_mom.py --restore  put every touched file back byte for byte, delete the
                                                              new header, and refuse if a file changed since install

Backups and the manifest live under logs/stage-backups/fix_static_ram_base_mom/.  The stage refuses (exit 1, nothing
written) if any anchor is missing or appears more than once, so it cannot half-apply.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_STATIC_RAM_MOM_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
OVERLAY = ROOT / 'overlays/static_ram_mom/native_static_ram_mom.h'
BACKUP = ROOT / 'logs/stage-backups/fix_static_ram_base_mom'
MANIFEST = BACKUP / 'manifest.json'
HEADER_REL = 'include/runtime/native_static_ram_mom.h'
CTX_REL = 'include/runtime/cpu_context.h'
DSP_REL = 'src/hw/hw_dsp.cpp'
CRLF, LF = '\r\n', '\n'

# ---------------------------------------------------------------------------
# cpu_context.h
# ---------------------------------------------------------------------------
CTX_INCLUDE_OLD = '#include "runtime/native_mom_lane.h" // NATIVE_MOM_GUEST_MEMORY_LANE_V1\n'
CTX_INCLUDE_NEW = (CTX_INCLUDE_OLD +
                   '#include "runtime/native_static_ram_mom.h" // ' + MARK + '\n')

# The two resize lines end in a space in the tree; the anchor keeps it.
CTX_MMU_OLD = ('struct MMU {\n'
               '  std::vector<uint8_t> mem1;\n'
               '  std::vector<uint8_t> mem2;\n'
               '\n'
               '  MMU() {\n'
               '    mem1.resize(24 * 1024 * 1024 + 8, 0); \n'
               '    mem2.resize(64 * 1024 * 1024 + 8, 0); \n'
               '  }\n')
CTX_MMU_NEW = ('struct MMU {\n'
               '  // ' + MARK + ': MEM1 at host 0x20000000 and MEM2 at host 0x30000000, committed once in the\n'
               '  // constructor (see native_static_ram_mom.h).  Sizes, padding and every bounds check are unchanged.\n'
               '  native_static_ram_mom::Mem1Ram mem1;\n'
               '  native_static_ram_mom::Mem2Ram mem2;\n'
               '\n'
               '  MMU() {\n'
               '    native_static_ram_mom::Commit(native_static_ram_mom::kMem1Host, native_static_ram_mom::kMem1Bytes);\n'
               '    native_static_ram_mom::Commit(native_static_ram_mom::kMem2Host, native_static_ram_mom::kMem2Bytes);\n'
               '  }\n')

CTX_EDITS = ((CTX_INCLUDE_OLD, CTX_INCLUDE_NEW, 'cpu_context.h include line'),
             (CTX_MMU_OLD, CTX_MMU_NEW, 'cpu_context.h MMU members and constructor'))

# ---------------------------------------------------------------------------
# hw_dsp.cpp  (native_ax() lambda: the bank picker)
# ---------------------------------------------------------------------------
DSP_OLD = ('        auto& bank=p>=0x10000000u?g_mmu->mem2:g_mmu->mem1;\n'
           '        size_t offset=p>=0x10000000u?p-0x10000000u:p;\n'
           '        size_t limit=bank.size()-8;\n'
           '        return offset<=limit&&size<=limit-offset?bank.data()+offset:nullptr;\n')
DSP_NEW = ('        // ' + MARK + ': the banks are different types now, so pick a data pointer and a size, not a reference.\n'
           '        const bool second_bank=p>=0x10000000u;\n'
           '        uint8_t* const bank_data=second_bank?g_mmu->mem2.data():g_mmu->mem1.data();\n'
           '        size_t offset=second_bank?p-0x10000000u:p;\n'
           '        size_t limit=(second_bank?g_mmu->mem2.size():g_mmu->mem1.size())-8;\n'
           '        return offset<=limit&&size<=limit-offset?bank_data+offset:nullptr;\n')

DSP_EDITS = ((DSP_OLD, DSP_NEW, 'hw_dsp.cpp native_ax bank picker'),)


class Skip(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode(raw: bytes):
    text = raw.decode('utf-8', errors='surrogateescape')
    crlf = CRLF in text
    return text.replace(CRLF, LF), crlf


def encode(text: str, crlf: bool) -> bytes:
    return (text.replace(LF, CRLF) if crlf else text).encode('utf-8', errors='surrogateescape')


def anchored(text: str, old: str, new: str, label: str) -> str:
    found = text.count(old)
    if found != 1:
        raise Skip('%s: expected one anchor, found %d' % (label, found))
    return text.replace(old, new, 1)


def plan_tree(key: str, base: Path):
    """Return [(rel, before_bytes_or_None, after_bytes)] for one tree.  Raises Skip on any anchor problem."""
    changes = []
    header_dst = base / HEADER_REL
    header_bytes = OVERLAY.read_bytes()
    if header_dst.exists():
        if header_dst.read_bytes() != header_bytes:
            raise Skip('%s: %s exists and is not the overlay copy' % (key, HEADER_REL))
    else:
        changes.append((HEADER_REL, None, header_bytes))

    for rel, edits in ((CTX_REL, CTX_EDITS), (DSP_REL, DSP_EDITS)):
        raw = (base / rel).read_bytes()
        text, crlf = decode(raw)
        if MARK in text:
            continue
        for old, new, label in edits:
            text = anchored(text, old, new, '%s %s' % (key, label))
        changes.append((rel, raw, encode(text, crlf)))
    return changes


def check_all():
    """Plan every tree before anything is written, so a skip leaves both trees untouched."""
    if not OVERLAY.exists():
        raise Skip('overlay missing: %s' % OVERLAY)
    return {key: plan_tree(key, base) for key, base in TREES.items()}


def load_manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def install():
    plans = check_all()
    if not any(plans.values()):
        print('static RAM base (MoM): already installed in both trees.')
        return
    manifest = load_manifest()
    for key, changes in plans.items():
        for rel, before, after in changes:
            if before is not None:
                backup = BACKUP / key / rel
                if not backup.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(before)
            manifest.setdefault(key, {})[rel] = {'created': before is None, 'sha256': sha(after)}
    for key, changes in plans.items():
        base = TREES[key]
        for rel, before, after in changes:
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(after)
    BACKUP.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1))
    print('static RAM base (MoM) installed: %d file(s) changed.' % sum(len(c) for c in plans.values()))


def restore():
    if not MANIFEST.exists():
        print('static RAM base (MoM): nothing to restore.')
        return
    manifest = load_manifest()
    for key, files in manifest.items():
        for rel, info in files.items():
            dst = TREES[key] / rel
            if not dst.exists():
                if info['created']:
                    continue
                raise Skip('%s/%s is missing' % (key, rel))
            if sha(dst.read_bytes()) != info['sha256']:
                raise Skip('%s/%s changed since this stage wrote it' % (key, rel))
    for key, files in manifest.items():
        for rel, info in files.items():
            dst = TREES[key] / rel
            if info['created']:
                if dst.exists():
                    dst.unlink()
            else:
                shutil.copyfile(BACKUP / key / rel, dst)
    shutil.rmtree(BACKUP)
    print('static RAM base (MoM) removed: %d file(s) restored or deleted.' % sum(len(f) for f in manifest.values()))


def check():
    for key, changes in check_all().items():
        print('%s: %d file(s) would change' % (key, len(changes)))
        for rel, before, after in changes:
            print('  %s %s' % ('new' if before is None else 'edit', rel))
    print('all anchors found exactly once (no files written).')


def main():
    if '--restore' in sys.argv:
        restore()
    elif '--check' in sys.argv:
        check()
    else:
        install()


if __name__ == '__main__':
    try:
        main()
    except Skip as reason:
        print('fix_static_ram_base_mom: ' + str(reason))
        sys.exit(1)
