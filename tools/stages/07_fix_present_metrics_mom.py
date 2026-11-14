"""Frame-rate counters in the window title that cannot disagree with the game.

Ported from CotT's ``tools/fix_present_metrics.py``, re-derived without the
frame-trace instrumentation so it does not depend on the trace tooling.

Three rates, measured on the host thread with no tracing needed:

  * present  - SDL_GL_SwapWindow calls per second (what a frame counter shows;
    the host presents once per VI field: 50/s PAL, 59.94/s NTSC);
  * new      - presents that showed an XFB image not shown before (new game
    frames actually reaching the screen).  A flip back to an older XFB buffer is
    not a new image, so the generation counter has to exceed everything shown
    before;
  * xfb      - XFB copies the game completed per second.

They go to the window title and to stdout once per second (``NWII_FPS_LOG=1``
enables the stdout line).

Verified by reading ``src/hle/gx/renderer_gl.cpp`` (``NativeLatchXfb`` counts
every completed copy) and ``src/core/main.cpp`` (the present block selects the
VI-chosen XFB and swaps the window).
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_PRESENT_METRICS_V1'

# ---- renderer: remember which copy generation each XFB address holds --------
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK not in text:
    old = 'struct NativeXfbFrame { GLuint texture=0,fbo=0; int width=0,height=0; };\n'
    assert text.count(old) == 1, 'NativeXfbFrame anchor'
    new = ('struct NativeXfbFrame { GLuint texture=0,fbo=0; int width=0,height=0; uint64_t generation=0; /* ' + MARK + ' */ };\n')
    text = text.replace(old, new, 1)

    old = '  ++s_native_xfb_copies;\n'
    assert text.count(old) == 1, 'xfb copy counter anchor'
    text = text.replace(old, '  ++s_native_xfb_copies;\n  frame.generation=s_native_xfb_copies; // ' + MARK + '\n', 1)

    old = 'uint64_t NativeXfbCopyCount(){return s_native_xfb_copies;}\n'
    assert text.count(old) == 1, 'NativeXfbCopyCount anchor'
    text = text.replace(
        old,
        old + ('uint64_t NativeXfbGeneration(uint32_t address){auto it=s_native_xfb.find(address&0x3fffffff);'
               'return it==s_native_xfb.end()?0:it->second.generation;} // ' + MARK + '\n'), 1)
    renderer.write_text(text)

# ---- main: the counters ----------------------------------------------------
main_cpp = runtime / 'src/core/main.cpp'
text = main_cpp.read_text()
if MARK not in text:
    old = ('uint64_t NativeXfbCopyCount();\n'
           '} // NATIVE_XFB_MAIN_DECL\n')
    assert text.count(old) == 1, 'declaration anchor'
    new = ('uint64_t NativeXfbCopyCount();\n'
           'uint64_t NativeXfbGeneration(uint32_t address); // ' + MARK + '\n'
           '} // NATIVE_XFB_MAIN_DECL\n')
    text = text.replace(old, new, 1)

    old = ('        if(std::getenv("NWII_OFFSCREEN")==nullptr){\n'
           '          native_perf_phase::Scope native_perf_phase_swap(native_perf_phase::Phase_Swap); // NATIVE_PERF_PHASE_V1_SWAP\n'
           '          SDL_GL_SwapWindow(window);\n'
           '        }\n')
    assert text.count(old) == 1, 'swap anchor'
    new = old + ('        { // ' + MARK + ': present / new-image / XFB-copy rates\n'
                 '          static uint64_t pm_presents=0,pm_new=0,pm_last_gen=0,pm_copies0=nwii::runtime::gx::NativeXfbCopyCount();\n'
                 '          static auto pm_t0=std::chrono::steady_clock::now();\n'
                 '          const uint64_t gen=have_present?nwii::runtime::gx::NativeXfbGeneration(xfb_addr):0;\n'
                 '          const bool fresh=have_present && gen>pm_last_gen; if(fresh)pm_last_gen=gen;\n'
                 '          ++pm_presents; pm_new+=fresh?1:0;\n'
                 '          const auto pm_now=std::chrono::steady_clock::now();const double pm_s=std::chrono::duration<double>(pm_now-pm_t0).count();\n'
                 '          if(pm_s>=1.0){const uint64_t copies=nwii::runtime::gx::NativeXfbCopyCount();\n'
                 '            char title[192];std::snprintf(title,sizeof title,"NWiiRecomp | new frames %.1f fps | presented %.1f | xfb copies %.1f",pm_new/pm_s,pm_presents/pm_s,(copies-pm_copies0)/pm_s);\n'
                 '            if(window)SDL_SetWindowTitle(window,title);\n'
                 '            static const bool pm_print=std::getenv("NWII_FPS_LOG")!=nullptr; if(pm_print){std::printf("[FPS] %s\\n",title+13);std::fflush(stdout);}\n'
                 '            pm_presents=pm_new=0;pm_copies0=copies;pm_t0=pm_now;}\n'
                 '        }\n')
    text = text.replace(old, new, 1)
    main_cpp.write_text(text)

print('Present / new-frame / XFB-copy rates are shown in the window title.')
