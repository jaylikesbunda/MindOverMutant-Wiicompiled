r"""Controller mapping for the launcher's Controls page: per-controller layouts, keyboard/mouse rebinding, gyro pointer, motion.

Marker: NATIVE_MOM_INPUT_MAP_V1. Port of the Crash of the Titans input mapping (tools/fix_input_mapping.py and the later
NATIVE_INPUT_MAP_V2/V3 revisions), so the MoM launcher (launcher/, forked from the CotT one) drives MoM the same way:
it writes NWII_INPUT_MAP (player 1) and NWII_INPUT_MAP_DIR/controller-<slot>.txt, and the runtime reloads them when they
change, so a remap applies while playing. Without those variables the previous fixed mapping applies.

Installs the final CotT input files (overlays/input_map_cott/: gamepad_source.cpp/.h, mouse_keyboard_source.cpp,
input_mapping.h, native_prompt_device.h) and makes the two state edits they need:
  * include/input/input_source.h: WiimoteState gains the Nunchuk accelerometer;
  * src/hle/ios.cpp: MoM's emulated WPAD state carries both accelerometers from the physical pad and writes them into the
    packets (204 counts per g, signed 10-bit) instead of reporting a remote at rest, so motion bindings reach the game.
MoM's own raw-joystick fallback (NWII_RAW_PAD_MAP, for controllers SDL does not know as game controllers) is replaced by
the mapping, which covers every SDL game controller.

Edits the vendored runtime and mirrors into export/nWiiRuntime. --restore puts every touched file back exactly as it was
(backups under logs/stage-backups/) and removes the installed headers.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

MARK = 'NATIVE_MOM_INPUT_MAP_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = {'vendor': ROOT / 'vendor/NWiiRecomp/nWiiRuntime', 'export': ROOT / 'export/nWiiRuntime'}
BACKUP = ROOT / 'logs/stage-backups/fix_input_mapping_mom'
SRC = ROOT / 'overlays/input_map_cott'
# overlay file -> runtime path (replaced or added)
FILES = {
    'gamepad_source.cpp': 'src/input/sources/gamepad_source.cpp',
    'gamepad_source.h': 'src/input/sources/gamepad_source.h',
    'mouse_keyboard_source.cpp': 'src/input/sources/mouse_keyboard_source.cpp',
    'input_mapping.h': 'src/input/sources/input_mapping.h',
    'native_prompt_device.h': 'include/runtime/native_prompt_device.h',
}
RAW = ('auto raw=[](float g)->uint16_t{int v=static_cast<int>(g*204.f+(g>=0.f?0.5f:-0.5f)); v=v<-511?-511:v>511?511:v; '
       'return static_cast<uint16_t>(static_cast<int16_t>(v));};')
EDITS = {
    'include/input/input_source.h': [
        ('    int8_t nunchuk_x = 0, nunchuk_y = 0;\n',
         '    int8_t nunchuk_x = 0, nunchuk_y = 0;\n'
         '    float nunchuk_ax = 0, nunchuk_ay = 0, nunchuk_az = 1; // ' + MARK + ': Nunchuk accelerometer, g\n'),
    ],
    'src/hle/ios.cpp': [
        ('  uint16_t buttons=0; int8_t sx=0, sy=0;\n',
         '  uint16_t buttons=0; int8_t sx=0, sy=0;\n'
         '  float ax=0,ay=0,az=1, nax=0,nay=0,naz=1; // ' + MARK + ': accelerometers in g (remote, Nunchuk)\n'),
        ('    s.sx=connected ? state.nunchuk_x : 0; s.sy=connected ? state.nunchuk_y : 0;\n',
         '    s.sx=connected ? state.nunchuk_x : 0; s.sy=connected ? state.nunchuk_y : 0;\n'
         '    s.ax=state.accel_x; s.ay=state.accel_y; s.az=state.accel_z; s.nax=state.nunchuk_ax; s.nay=state.nunchuk_ay; s.naz=state.nunchuk_az; // ' + MARK + '\n'),
        ('  ctx.mmu.write16(p+6,204); // zero-calibrated stationary +1g, SDK calibration fallback\n',
         '  { // ' + MARK + ': remote accelerometer, 204 counts per g, signed 10-bit\n'
         '    auto& ms=native_wpad_state();\n'
         '    ' + RAW + '\n'
         '    ctx.mmu.write16(p+2,raw(ms.ax)); ctx.mmu.write16(p+4,raw(ms.ay)); ctx.mmu.write16(p+6,raw(ms.az));\n'
         '  }\n'),
        ('  if (format>=3 && format<6) ctx.mmu.write16(p+0x2E,204);\n',
         '  if (format>=3 && format<6) { // ' + MARK + ': Nunchuk accelerometer\n'
         '    auto& ms=native_wpad_state();\n'
         '    ' + RAW + '\n'
         '    ctx.mmu.write16(p+0x2A,raw(ms.nax)); ctx.mmu.write16(p+0x2C,raw(ms.nay)); ctx.mmu.write16(p+0x2E,raw(ms.naz));\n'
         '  }\n'),
    ],
}


class Skip(Exception):
    pass


def norm(b):
    return b.decode('utf-8', errors='surrogateescape').replace('\r\n', '\n')


def main():
    manifest_path = BACKUP / 'manifest.json'
    if '--restore' in sys.argv:
        if not manifest_path.exists():
            print('Input mapping: nothing to restore.')
            return
        manifest = json.loads(manifest_path.read_text())
        for key, info in manifest.items():
            tree, rel = key.split('/', 1)
            path = TREES[tree] / rel
            if hashlib.sha256(path.read_bytes()).hexdigest() != info['sha']:
                raise Skip(key + ' changed since this stage wrote it')
        for key, info in manifest.items():
            tree, rel = key.split('/', 1)
            path = TREES[tree] / rel
            if info['existed']:
                shutil.copyfile(BACKUP / tree / rel, path)
            else:
                path.unlink()
        shutil.rmtree(BACKUP)
        print('Input mapping removed: %d file(s) restored.' % len(manifest))
        return
    if any(MARK in norm((base / 'src/hle/ios.cpp').read_bytes()) for base in TREES.values()):
        print('Input mapping already installed.')
        return
    plan = {}  # (tree, rel) -> new bytes
    for tree, base in TREES.items():
        for rel, pairs in EDITS.items():
            raw = (base / rel).read_bytes()
            crlf = b'\r\n' in raw
            t = norm(raw)
            for old, new in pairs:
                if t.count(old) != 1:
                    raise Skip('%s/%s: expected one %r, found %d' % (tree, rel, old.strip()[:60], t.count(old)))
                t = t.replace(old, new, 1)
            plan[(tree, rel)] = (t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape')
        for name, rel in FILES.items():
            plan[(tree, rel)] = (SRC / name).read_bytes()
    manifest = {}
    for (tree, rel), data in plan.items():
        path = TREES[tree] / rel
        existed = path.exists()
        if existed:
            dst = BACKUP / tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest[tree + '/' + rel] = {'sha': hashlib.sha256(data).hexdigest(), 'existed': existed}
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print('Input mapping installed: %d file(s) written.' % len(manifest))


if __name__ == '__main__':
    try:
        main()
    except Skip as reason:
        print('fix_input_mapping_mom: ' + str(reason))
        sys.exit(1)
