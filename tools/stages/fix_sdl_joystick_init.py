"""Initialise SDL's joystick subsystem so every pad control is visible.

The runtime called SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMECONTROLLER)
without SDL_INIT_JOYSTICK. SDL's HIDAPI/raw-joystick backend is only brought
up by SDL_INIT_JOYSTICK, so a DualSense was enumerated through the degraded
path: 11 buttons, 6 axes and no hat. The d-pad (raw buttons 11..14 in the
community mapping) was simply not there, and neither were the stick-click and
touchpad buttons.

With SDL_INIT_JOYSTICK the same pad reports 17 buttons, 6 axes and the full
community mapping (a:b0 b:b1 dpup:b11 dpdown:b12 dpleft:b13 dpright:b14 ...).
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
p = port / 'vendor/NWiiRecomp/nWiiRuntime/src/core/main.cpp'
t = p.read_text()
if 'NATIVE_SDL_JOYSTICK_INIT_V1' not in t:
    old = 'if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMECONTROLLER) < 0) {'
    new = ('// NATIVE_SDL_JOYSTICK_INIT_V1: the joystick backend must be initialised or\n'
           '    // pads lose their d-pad, stick clicks and touchpad buttons.\n'
           '    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMECONTROLLER | SDL_INIT_JOYSTICK) < 0) {')
    assert t.count(old) == 1
    t = t.replace(old, new)
    p.write_text(t)
print('SDL joystick subsystem initialised.')
