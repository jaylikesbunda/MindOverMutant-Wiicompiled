"""Wrap the guest's per-frame minimum-dt load with a host FPS override (MoM).

Marker: NATIVE_MOM_FRAME_GATE_V1.

reference/mom-frame-gate.json (lead-authored, verified against the MoM DOL):
the game's frame gate loads 1/60 (bits 0x3C888889, address 0x805D8F98) at
0x8029E23C (`lfs f0,-20360(r2)`, word 0xC002B078) inside the function at
0x8029E1AC and skips the update when elapsed < threshold.  This stage wraps
ONLY that generated load with native_frame_limit::MinimumUpdateSeconds():

* NWII_FPS_LIMIT parsed with strtod end-to-end: unset, empty, trailing
  characters, non-finite or outside 10..1000 -> original returned unchanged.
* NWII_NO_GUEST_FRAME_LIMIT present -> original returned unchanged.
* Anything but the exact original bits 0x3C888889 (including -0.0, NaN
  patterns, a patched constant) -> original returned bit-preserved.
* Valid override -> float(1.0 / parsed_fps).
* The same constant loaded at 0x8029E1F0 (debug single-step dt) is never
  touched; elapsed f31, the [r29+152] accumulator and the max-delta clamp
  0x3DCCCCCD are untouched; FP exception gates and inst_count are unchanged.

The stage refuses to write unless the DOL hash and the generated context both
match the spec, so a regenerated tree that lost the site fails loudly instead
of patching the wrong load.

Idempotent and reversible via exact inverse pairs (--restore).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

MARKER = 'NATIVE_MOM_FRAME_GATE_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
LIMIT = RUNTIME + '/include/runtime/native_frame_limit.h'
SPEC = 'reference/mom-frame-gate.json'
DOL = 'dump/sys/main.dol'

HELPER_PAIR = (
    '''// The same cap expressed in guest timebase ticks.
''',
    '''// ''' + MARKER + ''': the guest's frame-gate threshold load.  Returns the
// original value bit-preserved unless it is exactly the stock 1/60 constant
// and NWII_FPS_LIMIT parses to a finite fps inside 10..1000 (strtod, full
// string) without NWII_NO_GUEST_FRAME_LIMIT.  No fixed-dt speed hack: the
// override only changes the cap the game already applies to itself.
inline float MinimumUpdateSeconds(float original) {
    static const float override_dt = []() {
        if (std::getenv("NWII_NO_GUEST_FRAME_LIMIT")) return 0.f;
        const char* e = std::getenv("NWII_FPS_LIMIT");
        if (!e || !*e) return 0.f;
        char* end = nullptr;
        const double fps = std::strtod(e, &end);
        if (end == e || *end || !std::isfinite(fps) || fps < 10.0 || fps > 1000.0)
            return 0.f;
        return float(1.0 / fps);
    }();
    if (!override_dt) return original;
    uint32_t bits;
    std::memcpy(&bits, &original, 4);
    return bits == 0x3C888889u ? override_dt : original;
}

// The same cap expressed in guest timebase ticks.
''')

INCLUDE_PAIR = (
    '''#include <cstdint>
''',
    '''#include <cstdint>
#include <cstring>
''')


def read(path):
    return Path(path).read_text(encoding='utf-8', errors='surrogateescape').replace('\r\n', '\n')


def write(path, text):
    with open(path, 'rb') as h:
        crlf = b'\r\n' in h.read(4096)
    data = text.replace('\n', '\r\n') if crlf else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as h:
        h.write(data)


def validate(root, spec):
    dol = root / DOL
    if not dol.is_file():
        raise SystemExit('%s: %s missing - refusing to write without the spec DOL'
                         % (MARKER, DOL))
    data = dol.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec['dol_sha256']:
        raise SystemExit('%s: DOL sha256 %s != spec %s - refusing to write'
                         % (MARKER, digest[:16], spec['dol_sha256'][:16]))
    def dol_read(address, size):
        for count, offsets, addresses, sizes in ((7, 0, 0x48, 0x90),
                                                 (11, 0x1c, 0x64, 0xac)):
            for i in range(count):
                off, base, length = (struct.unpack_from('>I', data, field + i * 4)[0]
                                     for field in (offsets, addresses, sizes))
                if base <= address and address + size <= base + length:
                    return data[off + address - base:off + address - base + size]
        raise SystemExit('%s: spec address outside DOL sections' % MARKER)
    context = bytes.fromhex(spec['context_hex'])
    actual = dol_read(int(spec['context_address'], 16), len(context))
    if actual != context or hashlib.sha256(actual).hexdigest() != spec['context_sha256']:
        raise SystemExit('%s: frame-gate context mismatch' % MARKER)
    for address, expected in ((spec['threshold_load_pc'], spec['threshold_load_word']),
                              (spec['threshold_address'], spec['threshold_bits'])):
        if dol_read(int(address, 16), 4) != int(expected, 16).to_bytes(4, 'big'):
            raise SystemExit('%s: threshold instruction/constant mismatch' % MARKER)
    gpath = root / spec['generated_file']
    if not gpath.is_file():
        raise SystemExit('%s: %s missing - regenerate before staging'
                         % (MARKER, spec['generated_file']))
    text = read(gpath)
    loc = 'loc_%s: ;' % spec['threshold_load_pc'][2:].upper()
    word = '// %s: %s' % (spec['threshold_load_pc'],
                          spec['threshold_load_word'].upper().replace('0X', ''))
    if loc not in text or word not in text:
        raise SystemExit('%s: threshold load site not found in %s'
                         % (MARKER, spec['generated_file']))
    expr = spec['original_expression']
    # The load at threshold_load_pc is the only site wrapped; the same
    # expression at the debug-step load (0x8029E1F0) stays literal.
    site = word + '\n    if (ctx.require_fpu(' + spec['threshold_load_pc'] + ')) return; // MSR.FP exception\n    ctx.fpr[0] = ctx.ps1[0] = ' + expr + '; // lfs'
    if site not in text and spec['replacement_expression'] not in text:
        raise SystemExit('%s: generated threshold-load line shape changed - '
                         'refusing to guess' % MARKER)
    return gpath, text, site, expr, spec['replacement_expression']


def apply(root):
    spec = json.loads(read(root / SPEC))
    # Validate EVERYTHING before a single write: spec DOL identity, the
    # generated context/site shape, and the header anchors.  A failure leaves
    # the tree untouched.
    gpath, text, site, expr, repl = validate(root, spec)
    lpath = root / LIMIT
    ltext = read(lpath)
    for old, new in (INCLUDE_PAIR, HELPER_PAIR):
        if new in ltext:
            continue
        if ltext.count(old) != 1:
            raise SystemExit('%s: %s anchor matched %d times'
                             % (MARKER, LIMIT, ltext.count(old)))
        ltext = ltext.replace(old, new, 1)
    if repl in text:
        # already wrapped; make sure the include is there too (mixed trees)
        inc_old = '#include "runtime/cpu_context.h"'
        if 'runtime/native_frame_limit.h' not in text:
            if text.count(inc_old) != 1:
                raise SystemExit('%s: helper include anchor missing' % MARKER)
            text = text.replace(inc_old, inc_old +
                                '\n#include "runtime/native_frame_limit.h" // ' + MARKER, 1)
        if read(lpath) != ltext:
            write(lpath, ltext)
        if read(gpath) != text:
            write(gpath, text)
        print('%s: threshold load already wrapped' % MARKER)
        return
    # include the helper header once
    inc_old = '#include "runtime/cpu_context.h"'
    inc_new = inc_old + '\n#include "runtime/native_frame_limit.h" // ' + MARKER
    if 'runtime/native_frame_limit.h' not in text:
        if text.count(inc_old) != 1:
            raise SystemExit('%s: include anchor missing in %s'
                             % (MARKER, spec['generated_file']))
        text = text.replace(inc_old, inc_new, 1)
    text = text.replace(site, site.replace(expr, repl), 1)
    write(lpath, ltext)   # all validation passed: commit both files
    write(gpath, text)
    print('%s staged: %s threshold load gated on NWII_FPS_LIMIT '
          '(NWII_NO_GUEST_FRAME_LIMIT opts out)' % (MARKER, spec['generated_file']))


def restore(root):
    spec = json.loads(read(root / SPEC))
    gpath = root / spec['generated_file']
    if gpath.is_file():
        text = read(gpath)
        text = text.replace(spec['replacement_expression'],
                            spec['original_expression'])
        text = text.replace('\n#include "runtime/native_frame_limit.h" // '
                            + MARKER, '')
        write(gpath, text)
    lpath = root / LIMIT
    if lpath.is_file():
        ltext = read(lpath)
        ltext = ltext.replace(HELPER_PAIR[1], HELPER_PAIR[0])
        ltext = ltext.replace(INCLUDE_PAIR[1], INCLUDE_PAIR[0])
        write(lpath, ltext)
    print('%s restored' % MARKER)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
