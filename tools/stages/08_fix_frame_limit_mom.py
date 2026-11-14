"""A custom frame-rate cap that the host pacing and the guest VI agree on.

Ported from CotT's ``tools/fix_frame_limit.py``; only the runtime half is
ported here (the guest-side half of the CotT stage patches CotT's generated
guest code and is game specific - the recipe is in
logs/cott-quality-plan.md, area 2).

``NWII_FPS_LIMIT=<fps>`` (10 - 1000) makes the host present at that rate
instead of the regional field rate, and makes the guest's VI interrupt arrive
at the same rate, so the game runs at the requested speed instead of being
paced by the host while the guest believes it is running at 50/60 Hz.

Without the option the behaviour is unchanged: the regional field period
(20 ms PAL, 16.683 ms NTSC, divided by the turbo factor) and the guest's
regional VI period are used exactly as before.

Verified by reading ``src/core/main.cpp`` (the field period and the video
pacing) and ``src/hle/ios.cpp`` (the guest VI period and the single-VI
source), the same two places the CotT stage edits.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_CUSTOM_FRAME_LIMIT_V1'

# ---- the helper header ------------------------------------------------------
header = runtime / 'include/runtime/native_frame_limit.h'
if not header.exists() or header.stat().st_size == 0:
    header.write_text('''#pragma once
// ' + MARK + ': a custom frame-rate cap shared by the host pacing and the
// guest VI period. No dependency on the developer HUD.
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>

namespace native_frame_limit {

// The regional field period: 20 ms PAL, 16.683 ms NTSC, scaled by turbo.
inline std::chrono::nanoseconds Period(bool pal, double turbo, const char* custom) {
    const int64_t regional = int64_t((pal ? 20000000 : 16683333) / turbo);
    if (!custom || !*custom) return std::chrono::nanoseconds(regional);
    char* end = nullptr;
    const double fps = std::strtod(custom, &end);
    if (end == custom || *end || !std::isfinite(fps) || fps < 10 || fps > 1000)
        return std::chrono::nanoseconds(regional);
    return std::chrono::nanoseconds(int64_t(std::llround(1000000000.0 / fps)));
}

// The same cap expressed in guest timebase ticks.
inline uint64_t GuestPeriodTicks(bool pal, uint64_t frequency, const char* custom) {
    return uint64_t(std::llround(frequency * (Period(pal, 1.0, custom).count() / 1000000000.0)));
}

} // namespace native_frame_limit
''')

# ---- host pacing ------------------------------------------------------------
main_cpp = runtime / 'src/core/main.cpp'
text = main_cpp.read_text()
if MARK not in text:
    old = '  const auto field_period = std::chrono::nanoseconds(int64_t((field_pal ? 20000000 : 16683333) / turbo_factor));\n'
    assert text.count(old) == 1, 'field period anchor'
    new = '  const auto field_period = native_frame_limit::Period(field_pal,turbo_factor,std::getenv("NWII_FPS_LIMIT")); // ' + MARK + '\n'
    text = text.replace(old, new, 1)
    if '#include "runtime/native_frame_limit.h"' not in text:
        text = '#include "runtime/native_frame_limit.h"\n' + text
    main_cpp.write_text(text)

# ---- guest VI period --------------------------------------------------------
ios_cpp = runtime / 'src/hle/ios.cpp'
text = ios_cpp.read_text()
if MARK not in text:
    old = '              vi_period = vi_pal ? ctx.tb_freq / 50 : ctx.tb_freq * 1001 / 60000;\n'
    assert text.count(old) == 1, 'guest VI period anchor'
    new = (old +
           '              if(std::getenv("NWII_FPS_LIMIT"))vi_period=native_frame_limit::GuestPeriodTicks(vi_pal,ctx.tb_freq,std::getenv("NWII_FPS_LIMIT")); // ' + MARK + '\n')
    text = text.replace(old, new, 1)

    # The cap must apply even when the guest VI source is not the single-VI
    # one: the override above sits inside that branch, so without this the
    # guest would keep its hard 60 Hz while the host followed the cap.
    old = '          if (vi_period == 0) vi_period = 500000;\n'
    assert text.count(old) == 1, 'vi period zero anchor'
    new = ('          { // ' + MARK + ': the cap applies regardless of the VI source.\n'
           '            if(std::getenv("NWII_FPS_LIMIT")){\n'
           '              const auto &vi_cap_id=nwii::runtime::Config::get().game_id;\n'
           "              const char vi_cap_region=vi_cap_id.size()>=4?vi_cap_id[3]:'E';\n"
           "              const bool vi_cap_pal=vi_cap_region=='P'||vi_cap_region=='D'||vi_cap_region=='F'||vi_cap_region=='I'||vi_cap_region=='S'||vi_cap_region=='H'||vi_cap_region=='U'||vi_cap_region=='X'||vi_cap_region=='Y'||vi_cap_region=='Z';\n"
           '              vi_period=native_frame_limit::GuestPeriodTicks(vi_cap_pal,ctx.tb_freq,std::getenv("NWII_FPS_LIMIT"));\n'
           '            }\n'
           '          }\n')
    text = text.replace(old, new + old, 1)
    if '#include "runtime/native_frame_limit.h"' not in text:
        text = '#include "runtime/native_frame_limit.h"\n' + text
    ios_cpp.write_text(text)

print('NWII_FPS_LIMIT caps the host presentation rate and the guest VI period together.')
