"""Raw-joystick fallback so any USB controller can drive the guest.

SDL 2.28 ships only a very small built-in controller database and there is no
``gamecontrollerdb.txt`` next to the executable, so SDL_IsGameController() is
false for a DualShock 4 (VID 054C PID 0CE6) or most other pads. The gamepad
source then saw no device at all and the guest never received a button.

This stage keeps the SDL_GameController path and adds a raw SDL_Joystick path
for every device the database does not know. Known pads get their real layout
(Sony / Microsoft / Nintendo); anything else gets a generic layout. The layout
can be corrected at runtime without a rebuild:

    NWII_RAW_PAD_MAP=A,B,X,Y,Start,Select,Home,Up,Down,Left,Right,L1,R1,L2,R2

Every index is a raw SDL button number; -1 means "not present". The d-pad is
taken from the hat when the layout has no d-pad buttons.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
base = port / 'vendor/NWiiRecomp/nWiiRuntime/src/input/sources'

# ---------------------------------------------------------------- header ----
p = base / 'gamepad_source.h'
t = p.read_text()
if 'NATIVE_RAW_GAMEPAD_V1' not in t:
    old = """private:
    SDL_GameController* controllers[4]{};
};"""
    new = """private:
    SDL_GameController* controllers[4]{};
    // NATIVE_RAW_GAMEPAD_V1
    SDL_Joystick* raw_joysticks[4]{};
    RawPadMap raw_maps[4]{};
};"""
    assert t.count(old) == 1
    t = t.replace(old, new)
    old = """class GamepadSource : public IInputSource {"""
    new = """// NATIVE_RAW_GAMEPAD_V1
// Raw button indices per logical action; -1 means "not present".
struct RawPadMap {
    int a = -1, b = -1, x = -1, y = -1, start = -1, select = -1, home = -1;
    int up = -1, down = -1, left = -1, right = -1;
    int l1 = -1, r1 = -1, l2 = -1, r2 = -1;
    int hat = -1;
    int lx = 0, ly = 1, rx = 2, ry = 3;
    int trigL = -1, trigR = -1;
};

class GamepadSource : public IInputSource {"""
    assert t.count(old) == 1
    t = t.replace(old, new)
    p.write_text(t)

# --------------------------------------------------------------- source ----
p = base / 'gamepad_source.cpp'
t = p.read_text()
if 'NATIVE_RAW_GAMEPAD_V1' not in t:
    old = """#include "gamepad_source.h"
#include "runtime/config.h"
#include <SDL.h>
#include <cmath>
"""
    new = """#include "gamepad_source.h"
#include "runtime/config.h"
#include <SDL.h>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
"""
    assert t.count(old) == 1
    t = t.replace(old, new)

    old = """GamepadSource::~GamepadSource() {
    for (auto* controller : controllers) if (controller) SDL_GameControllerClose(controller);
}"""
    new = """GamepadSource::~GamepadSource() {
    for (auto* controller : controllers) if (controller) SDL_GameControllerClose(controller);
    // NATIVE_RAW_GAMEPAD_V1
    for (auto* js : raw_joysticks) if (js) SDL_JoystickClose(js);
}

// NATIVE_RAW_GAMEPAD_V1
static RawPadMap raw_default_map() {
    RawPadMap m;
    m.a = 0; m.b = 1; m.x = 2; m.y = 3;
    m.start = 7; m.select = 6; m.home = 5;
    m.up = 12; m.down = 13; m.left = 14; m.right = 15;
    m.l1 = 9; m.r1 = 10; m.l2 = 4; m.r2 = 5;
    m.lx = 0; m.ly = 1; m.rx = 2; m.ry = 3;
    m.trigL = 2; m.trigR = 3;
    return m;
}

static RawPadMap raw_map_for(SDL_Joystick* js) {
    RawPadMap m = raw_default_map();
    const Uint16 vendor = SDL_JoystickGetVendor(js);
    const Uint16 product = SDL_JoystickGetProduct(js);
    if (vendor == 0x054c) {                 // Sony DualSense / DualShock 4
        m.a = 1; m.b = 2; m.x = 0; m.y = 3;
        m.start = 9; m.select = 8; m.home = 12;
        m.up = m.down = m.left = m.right = -1; m.hat = 0;
        m.l1 = 4; m.r1 = 5; m.l2 = 6; m.r2 = 7;
        // Windows DirectInput/HIDAPI layout: 0=LX 1=LY 2=RX 3=RY 4=L2 5=R2.
        const int axes = SDL_JoystickNumAxes(js);
        m.lx = 0; m.ly = 1; m.rx = 2; m.ry = 3;
        m.trigL = axes > 4 ? 4 : -1;
        m.trigR = axes > 5 ? 5 : -1;
    } else if (vendor == 0x045e) {          // Microsoft Xbox
        m.a = 0; m.b = 1; m.x = 2; m.y = 3;
        m.start = 6; m.select = 4; m.home = 5;
        m.up = 12; m.down = 13; m.left = 14; m.right = 15; m.hat = -1;
        m.l1 = 9; m.r1 = 10; m.l2 = -1; m.r2 = -1;
        m.lx = 0; m.ly = 1; m.rx = 2; m.ry = 3;
        m.trigL = 2; m.trigR = 3;
    } else if (vendor == 0x057e) {          // Nintendo Switch Pro
        m.a = 1; m.b = 0; m.x = 3; m.y = 2;
        m.start = 9; m.select = 8; m.home = 10;
        m.up = m.down = m.left = m.right = -1; m.hat = 0;
        m.l1 = 4; m.r1 = 5; m.l2 = 6; m.r2 = 7;
        m.lx = 0; m.ly = 1; m.rx = 2; m.ry = 3;
        m.trigL = 4; m.trigR = 5;
    }
    // Runtime override: A,B,X,Y,Start,Select,Home,Up,Down,Left,Right,L1,R1,L2,R2
    //                   then optionally LX,LY,RX,RY,TrigL,TrigR (axis indices).
    if (const char* e = std::getenv("NWII_RAW_PAD_MAP")) {
        int v[22] = {-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
                     -1,-1,-1,-1,-1,-1};
        int n = 0;
        const char* s = e;
        while (n < 22 && *s) {
            char* end = nullptr;
            long value = std::strtol(s, &end, 10);
            if (end == s) break;
            v[n++] = (int)value;
            s = end;
            while (*s == ',' || *s == ' ') ++s;
        }
        if (n >= 4) {
            m.a = v[0]; m.b = v[1]; m.x = v[2]; m.y = v[3];
            if (n > 4) m.start = v[4];
            if (n > 5) m.select = v[5];
            if (n > 6) m.home = v[6];
            if (n > 7) m.up = v[7];
            if (n > 8) m.down = v[8];
            if (n > 9) m.left = v[9];
            if (n > 10) m.right = v[10];
            if (n > 11) m.l1 = v[11];
            if (n > 12) m.r1 = v[12];
            if (n > 13) m.l2 = v[13];
            if (n > 14) m.r2 = v[14];
            m.hat = -1;
            if (n > 18) {
                m.lx = v[16]; m.ly = v[17]; m.rx = v[18];
                if (n > 19) m.ry = v[19];
                if (n > 20) m.trigL = v[20];
                if (n > 21) m.trigR = v[21];
            }
        }
    }
    (void)product;
    return m;
}"""
    assert t.count(old) == 1
    t = t.replace(old, new)

    old = """    SDL_GameControllerUpdate();
    for (int i = 0; i < 4; ++i) {
        auto* controller = controllers[i];
        if (!controller) continue;"""
    new = """    // NATIVE_RAW_GAMEPAD_V1: devices the controller database does not know.
    for (int device = 0; device < SDL_NumJoysticks(); ++device) {
        if (SDL_IsGameController(device)) continue;
        const auto id = SDL_JoystickGetDeviceInstanceID(device);
        bool owned = false;
        for (auto* js : raw_joysticks)
            if (js && SDL_JoystickInstanceID(js) == id) owned = true;
        if (owned) continue;
        for (int slot = 0; slot < 4; ++slot) {
            if (raw_joysticks[slot]) continue;
            SDL_Joystick* js = SDL_JoystickOpen(device);
            if (!js) break;
            raw_joysticks[slot] = js;
            raw_maps[slot] = raw_map_for(js);
            std::cout << "[Input] raw joystick slot " << slot << " '" << SDL_JoystickName(js)
                      << "' vid=" << SDL_JoystickGetVendor(js)
                      << " pid=" << SDL_JoystickGetProduct(js)
                      << " buttons=" << SDL_JoystickNumButtons(js)
                      << " axes=" << SDL_JoystickNumAxes(js)
                      << " hats=" << SDL_JoystickNumHats(js) << std::endl;
            break;
        }
    }
    SDL_GameControllerUpdate();
    for (int i = 0; i < 4; ++i) {
        auto* controller = controllers[i];
        if (!controller) {
            // NATIVE_RAW_GAMEPAD_V1
            SDL_Joystick* js = raw_joysticks[i];
            if (!js) continue;
            const RawPadMap& m = raw_maps[i];
            motes[i].controller_connected = true;
            pads[i].err = 0;
            motes[i].err = 0;
            auto down = [&](int index) {
                return index >= 0 && index < SDL_JoystickNumButtons(js) &&
                       SDL_JoystickGetButton(js, index) != 0;
            };
            bool up = false, dpad_down = false, left = false, right = false;
            if (m.hat >= 0 && m.hat < SDL_JoystickNumHats(js)) {
                const Uint8 hat = SDL_JoystickGetHat(js, m.hat);
                up = (hat & SDL_HAT_UP) != 0;
                dpad_down = (hat & SDL_HAT_DOWN) != 0;
                left = (hat & SDL_HAT_LEFT) != 0;
                right = (hat & SDL_HAT_RIGHT) != 0;
            }
            if (down(m.up)) up = true;
            if (down(m.down)) dpad_down = true;
            if (down(m.left)) left = true;
            if (down(m.right)) right = true;
            if (up) motes[i].buttons |= 0x0008;
            if (dpad_down) motes[i].buttons |= 0x0004;
            if (left) motes[i].buttons |= 0x0001;
            if (right) motes[i].buttons |= 0x0002;
            if (down(m.a)) motes[i].buttons |= 0x0800;
            if (down(m.b)) motes[i].buttons |= 0x0400;
            if (down(m.x)) motes[i].buttons |= 0x0200;
            if (down(m.y)) motes[i].buttons |= 0x0100;
            if (down(m.start)) motes[i].buttons |= 0x0010;
            if (down(m.select)) motes[i].buttons |= 0x1000;
            if (down(m.home)) motes[i].buttons |= 0x8000;
            if (down(m.l1)) { motes[i].buttons |= 0x4000; pads[i].buttons |= 0x0040; }
            if (down(m.r1)) { motes[i].buttons |= 0x2000; pads[i].buttons |= 0x0020; }
            if (down(m.a)) pads[i].buttons |= 0x0100;
            if (down(m.b)) pads[i].buttons |= 0x0200;
            if (down(m.x)) pads[i].buttons |= 0x0400;
            if (down(m.y)) pads[i].buttons |= 0x0800;
            if (down(m.start)) pads[i].buttons |= 0x1000;
            if (up) pads[i].buttons |= 0x0008;
            if (dpad_down) pads[i].buttons |= 0x0004;
            if (left) pads[i].buttons |= 0x0001;
            if (right) pads[i].buttons |= 0x0002;
            if (m.lx >= 0 && m.lx < SDL_JoystickNumAxes(js)) {
                const int lx = SDL_JoystickGetAxis(js, m.lx);
                const int ly = (m.ly >= 0 && m.ly < SDL_JoystickNumAxes(js))
                                   ? SDL_JoystickGetAxis(js, m.ly) : 0;
                if (std::abs(lx) > 8000) pads[i].stick_x = (int8_t)((lx / 32767.0f) * 127.0f);
                if (std::abs(ly) > 8000) pads[i].stick_y = (int8_t)((-ly / 32767.0f) * 127.0f);
            }
            if (!want_pointer) {
                if (m.rx >= 0 && m.rx < SDL_JoystickNumAxes(js)) {
                    const int rx = SDL_JoystickGetAxis(js, m.rx);
                    const int ry = (m.ry >= 0 && m.ry < SDL_JoystickNumAxes(js))
                                       ? SDL_JoystickGetAxis(js, m.ry) : 0;
                    if (std::abs(rx) > 8000) pads[i].substick_x = (int8_t)((rx / 32767.0f) * 127.0f);
                    if (std::abs(ry) > 8000) pads[i].substick_y = (int8_t)((-ry / 32767.0f) * 127.0f);
                }
            } else {
                motes[i].ir_x = 0.5f; motes[i].ir_y = 0.5f;
            }
            int trigL = 0, trigR = 0;
            if (m.trigL >= 0 && m.trigL < SDL_JoystickNumAxes(js))
                trigL = SDL_JoystickGetAxis(js, m.trigL);
            if (m.trigR >= 0 && m.trigR < SDL_JoystickNumAxes(js))
                trigR = SDL_JoystickGetAxis(js, m.trigR);
            if (trigL > 16384) { motes[i].buttons |= 0x0400; }
            if (trigR > 16384) { motes[i].buttons |= 0x2000; }
            if (down(m.l2)) motes[i].buttons |= 0x0400;
            if (down(m.r2)) motes[i].buttons |= 0x2000;
            pads[i].trigger_l = (uint8_t)((trigL / 32767.0f) * 255.0f);
            pads[i].trigger_r = (uint8_t)((trigR / 32767.0f) * 255.0f);
            motes[i].nunchuk_x = pads[i].stick_x;
            motes[i].nunchuk_y = pads[i].stick_y;
            continue;
        }"""
    assert t.count(old) == 1
    t = t.replace(old, new)
    p.write_text(t)

# Name the pads that the SDL controller database does know, so the button
# mapping can be verified from the log without a debugger.
if 'NATIVE_RAW_GAMEPAD_NAME_V1' not in t:
    t = p.read_text()
    old = """    SDL_GameControllerUpdate();
    for (int i = 0; i < 4; ++i) {
        auto* controller = controllers[i];
        if (!controller) {
            // NATIVE_RAW_GAMEPAD_V1"""
    new = """    // NATIVE_RAW_GAMEPAD_NAME_V1
    for (int slot = 0; slot < 4; ++slot) {
        auto* controller = controllers[slot];
        if (!controller) continue;
        static bool announced[4] = {false, false, false, false};
        if (announced[slot]) continue;
        announced[slot] = true;
        SDL_Joystick* js = SDL_GameControllerGetJoystick(controller);
        std::cout << "[Input] game controller slot " << slot << " '"
                  << SDL_GameControllerName(controller) << "'"
                  << " vid=" << (js ? SDL_JoystickGetVendor(js) : 0)
                  << " pid=" << (js ? SDL_JoystickGetProduct(js) : 0)
                  << " buttons=" << (js ? SDL_JoystickNumButtons(js) : 0)
                  << " axes=" << (js ? SDL_JoystickNumAxes(js) : 0)
                  << " hats=" << (js ? SDL_JoystickNumHats(js) : 0) << std::endl;
    }
    SDL_GameControllerUpdate();
    for (int i = 0; i < 4; ++i) {
        auto* controller = controllers[i];
        if (!controller) {
            // NATIVE_RAW_GAMEPAD_V1"""
    assert t.count(old) == 1
    t = t.replace(old, new)
    p.write_text(t)

# Report every button transition so the mapping can be verified from the log
# without a debugger (the periodic diagnostic only samples every few seconds).
if 'NATIVE_RAW_GAMEPAD_ECHO_V1' not in t:
    t = p.read_text()
    old = """        motes[i].nunchuk_x = pads[i].stick_x;
        motes[i].nunchuk_y = pads[i].stick_y;

        if (!want_pointer) {"""
    new = """        motes[i].nunchuk_x = pads[i].stick_x;
        motes[i].nunchuk_y = pads[i].stick_y;

        // NATIVE_RAW_GAMEPAD_ECHO_V1
        {
            static uint16_t last_mask[4] = {0, 0, 0, 0};
            if (motes[i].buttons != last_mask[i]) {
                std::cout << "[Input] slot " << i << " buttons 0x" << std::hex
                          << unsigned(motes[i].buttons) << " (was 0x"
                          << unsigned(last_mask[i]) << ")" << std::dec << std::endl;
                last_mask[i] = motes[i].buttons;
            }
        }

        if (!want_pointer) {"""
    assert t.count(old) == 1
    t = t.replace(old, new)
    p.write_text(t)
print('Raw gamepad fallback applied.')
