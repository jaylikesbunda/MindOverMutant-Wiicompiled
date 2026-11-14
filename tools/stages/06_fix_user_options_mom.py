"""Launcher-facing options: borderless fullscreen, F11, anisotropic filtering, pointer by window size.

Ported from CotT's ``tools/fix_user_options.py``, re-derived against this tree's
window setup (which has no developer HUD, so the F11 hook is placed in the
event loop directly).

  * ``NWII_WINDOW_MODE=borderless``  a desktop-resolution fullscreen window;
    F11 toggles it at any time;
  * ``NWII_ANISO=2|4|8|16``  anisotropic filtering on every texture sampler
    (clamped to what the GPU reports);
  * the mouse pointer is normalised by the actual window size, so resizing and
    fullscreen no longer offset the pointer.

Each edit is anchored on one stable line and tagged, so the stage is idempotent
on a pristine or an already patched tree.

The anisotropic block is inserted right after the sampler object is created,
i.e. between the sampler helper's signature and its body, so it does not
conflict with the mipmap stage, which edits the same helper further down.

Verified by reading ``src/core/main.cpp`` (window creation, the event loop) and
``src/input/sources/mouse_keyboard_source.cpp`` (the pointer normalisation,
which used the configured guest window size rather than the real one).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_USER_OPTIONS_V1'

# ---- 1. anisotropic filtering on every sampler ------------------------------
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if 'NATIVE_MOM_USER_ANISO_V1' not in text:
    old = '  if(!samplers[unit])glGenSamplers(1,&samplers[unit]);\n'
    assert text.count(old) == 1, 'sampler creation anchor'
    new = ('  if(!samplers[unit]) { // NATIVE_MOM_USER_ANISO_V1\n'
           '    glGenSamplers(1,&samplers[unit]);\n'
           '    static const float aniso=[](){\n'
           '      const char*e=std::getenv("NWII_ANISO");float v=e?float(std::atof(e)):1.f;if(v<=1.f)return 1.f;\n'
           '      GLfloat hw=1.f;glGetFloatv(0x84FF,&hw);while(glGetError()!=GL_NO_ERROR){} // GL_MAX_TEXTURE_MAX_ANISOTROPY\n'
           '      return hw<1.f?1.f:(v>hw?hw:v);}();\n'
           '    if(aniso>1.f)glSamplerParameterf(samplers[unit],0x84FE,aniso); // GL_TEXTURE_MAX_ANISOTROPY\n'
           '  }\n')
    text = text.replace(old, new, 1)
    renderer.write_text(text)

# ---- 2. window mode and the F11 toggle -------------------------------------
main_cpp = runtime / 'src/core/main.cpp'
text = main_cpp.read_text()
if 'NATIVE_MOM_USER_OPTIONS_V1' not in text:
    old = '    window = SDL_CreateWindow("NWiiRecomp", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,\n'
    assert text.count(old) == 1, 'window creation anchor'
    new = ('    { // ' + MARK + '\n'
           '      const char* wm = std::getenv("NWII_WINDOW_MODE");\n'
           '      if (wm && !offscreen && std::string(wm) == "borderless") window_flags |= SDL_WINDOW_FULLSCREEN_DESKTOP;\n'
           '    }\n')
    text = text.replace(old, new + old, 1)
    main_cpp.write_text(text)

if 'NATIVE_MOM_USER_F11_V1' not in text:
    old = '          if (e.type == SDL_QUIT) quit = true;\n'
    assert text.count(old) == 1, 'event loop anchor'
    new = (old +
           '          else if (e.type == SDL_KEYDOWN && !e.key.repeat && e.key.keysym.sym == SDLK_F11) { // NATIVE_MOM_USER_F11_V1\n'
           '            const bool full = (SDL_GetWindowFlags(window) & SDL_WINDOW_FULLSCREEN_DESKTOP) == SDL_WINDOW_FULLSCREEN_DESKTOP;\n'
           '            SDL_SetWindowFullscreen(window, full ? 0 : SDL_WINDOW_FULLSCREEN_DESKTOP);\n'
           '          }\n')
    text = text.replace(old, new, 1)
    main_cpp.write_text(text)

# ---- 3. pointer normalised by the real window size -------------------------
mouse = runtime / 'src/input/sources/mouse_keyboard_source.cpp'
text = mouse.read_text()
if 'NATIVE_MOM_USER_POINTER_V1' not in text:
    old = '    if (h == 0) h = 480;\n'
    assert text.count(old) == 1, 'window size anchor'
    new = (old +
           '    if (SDL_Window* win = SDL_GetMouseFocus()) { // NATIVE_MOM_USER_POINTER_V1\n'
           '        int ww = 0, hh = 0; SDL_GetWindowSize(win, &ww, &hh);\n'
           '        if (ww > 0 && hh > 0) { w = ww; h = hh; }\n'
           '    }\n')
    text = text.replace(old, new, 1)
    mouse.write_text(text)

print('User options installed: borderless window mode, F11 toggle, anisotropic filtering, pointer by window size.')
