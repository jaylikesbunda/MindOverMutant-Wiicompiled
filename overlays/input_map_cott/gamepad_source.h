#pragma once
#include "input/input_source.h"
#include <SDL.h>

namespace nwii::runtime::input {

class GamepadSource : public IInputSource {
public:
    GamepadSource() = default;
    ~GamepadSource() override;

    void update(GameCubePadState pads[4], WiimoteState motes[4]) override;
private:
    SDL_GameController* controllers[4]{};
};

} 
