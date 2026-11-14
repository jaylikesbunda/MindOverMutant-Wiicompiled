"""The right stick drives the Wii-remote pointer, and the mouse stops fighting it.

Ported from CotT's ``tools/fix_gamepad_pointer.py``, re-derived against this
tree's input sources (which also carry the raw-joystick fallback).

Before:

  * only input schemes 3 and 4 moved the pointer from a controller (scheme 3/4
    also let a gyro drive it), so a player on the other schemes had no way to
    aim with a pad;
  * the Classic scheme sent the right stick as a GameCube C-stick, which a Wii
    game never reads;
  * ``MouseKeyboardSource`` rewrote the pointer from the OS mouse position on
    every update, so any controller movement was erased one frame later.

Now:

  * the gamepad source always applies the right stick to the pointer, and the
    gyro only in the full-tilt scheme (mode 4), as documented;
  * the raw-joystick fallback (this tree's ``fix_raw_gamepad.py``) drives the
    pointer from the right stick as well - it used to pin the pointer to the
    centre, which would silently cancel the new behaviour;
  * the mouse source writes the pointer only when the mouse actually moved, so
    both devices can steer the same cursor.

MoM already has a raw-joystick fallback and a working physical-controller
path; this stage keeps them and fixes the pointer handling around them.

Verified by reading ``src/input/sources/gamepad_source.cpp`` (the two pointer
branches and the raw fallback) and ``src/input/sources/mouse_keyboard_source.cpp``
(the unconditional pointer write).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
sources = port / 'vendor/NWiiRecomp/nWiiRuntime/src/input/sources'
MARK = 'NATIVE_MOM_GAMEPAD_POINTER_V1'

# ---- gamepad source ---------------------------------------------------------
path = sources / 'gamepad_source.cpp'
text = path.read_text()
if MARK not in text:
    old = '    bool want_pointer = (mode == 3 || mode == 4);\n'
    assert text.count(old) == 1, 'want_pointer anchor'
    new = ('    bool want_pointer = true; // ' + MARK + ': the right stick drives the Wii-remote\n'
           '    // pointer in every gamepad scheme.\n')
    text = text.replace(old, new, 1)

    old = '            if (SDL_GameControllerHasSensor(controller, SDL_SENSOR_GYRO)) {\n'
    assert text.count(old) == 1, 'gyro anchor'
    new = ('            if (mode == 4 && SDL_GameControllerHasSensor(controller, SDL_SENSOR_GYRO)) {\n'
           '                // ' + MARK + ': the gyro only drives the pointer in the full-tilt scheme.\n')
    text = text.replace(old, new, 1)

    # The raw fallback used to pin the pointer to the centre whenever the pad
    # was in a pointer scheme; it now steers it with the right stick.
    old = ('            } else {\n'
           '                motes[i].ir_x = 0.5f; motes[i].ir_y = 0.5f;\n'
           '            }\n')
    assert text.count(old) == 1, 'raw pointer anchor'
    new = ('            } else {\n'
           '                // ' + MARK + ': the right stick steers the pointer here too.\n'
           '                if (m.rx >= 0 && m.rx < SDL_JoystickNumAxes(js)) {\n'
           '                    const int rx = SDL_JoystickGetAxis(js, m.rx);\n'
           '                    const int ry = (m.ry >= 0 && m.ry < SDL_JoystickNumAxes(js))\n'
           '                                       ? SDL_JoystickGetAxis(js, m.ry) : 0;\n'
           '                    const float speed = 0.05f * cfg.pointer_speed;\n'
           '                    if (std::abs(rx) > 8000) motes[i].ir_x += (rx / 32767.0f) * speed;\n'
           '                    if (std::abs(ry) > 8000) motes[i].ir_y += (ry / 32767.0f) * speed;\n'
           '                    if (motes[i].ir_x < 0.0f) motes[i].ir_x = 0.0f;\n'
           '                    if (motes[i].ir_x > 1.0f) motes[i].ir_x = 1.0f;\n'
           '                    if (motes[i].ir_y < 0.0f) motes[i].ir_y = 0.0f;\n'
           '                    if (motes[i].ir_y > 1.0f) motes[i].ir_y = 1.0f;\n'
           '                }\n'
           '            }\n')
    text = text.replace(old, new, 1)
    path.write_text(text)

# ---- mouse source: only write the pointer when the mouse moved -------------
path = sources / 'mouse_keyboard_source.cpp'
text = path.read_text()
if MARK not in text:
    old = ('    motes[0].ir_x = (float)mx / (float)w;\n'
           '    motes[0].ir_y = (float)my / (float)h;\n')
    assert text.count(old) == 1, 'mouse pointer anchor'
    new = ('    // ' + MARK + ': the OS mouse only steers the pointer when it moves, so a\n'
           '    // controller can steer the same cursor.\n'
           '    static int last_mx = -1, last_my = -1;\n'
           '    if (last_mx < 0 || mx != last_mx || my != last_my) {\n'
           '        motes[0].ir_x = (float)mx / (float)w;\n'
           '        motes[0].ir_y = (float)my / (float)h;\n'
           '        last_mx = mx; last_my = my;\n'
           '    }\n')
    text = text.replace(old, new, 1)
    path.write_text(text)

print('The right stick steers the pointer in every scheme; the mouse only writes on movement.')
