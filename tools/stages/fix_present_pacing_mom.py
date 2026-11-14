"""Swap the window only when the picture it would show is new.

Symptom (developer HUD, live session)::

    new 8.8/s   present 35.2/s   xfb 8.8/s
    frame ms  avg 25.69  p50 21.00  p95 54.79
    xfb_copy 0.01 ms   present 0.15 ms   swap 0.73 ms

``SDL_GL_SwapWindow`` ran once per host loop iteration, unconditionally, so
the window was swapped 35.2 times a second while the guest completed a display
copy only 8.8 times a second: three of every four swaps re-presented an image
that was already on screen.

Why the loop is faster than the content
---------------------------------------
The host loop is paced by ``pace_video_field()`` (PAL 20 ms nominal), not by
content, and every iteration reaches the present block.  Present pacing is the
only thing missing: the loop cannot "wait for a frame", because the guest's
field timer, its GX drain requests and its input all ride on the loop turning.

Why ``xfb``/``new`` are 8.8/s and not ~35/s
-------------------------------------------
Every accepted ``BP 0x52`` write with bit 14 (the EFB-to-XFB display copy)
calls ``NativeLatchXfb``, which increments ``s_native_xfb_copies`` and stores
that value in the destination address's ``frame.generation``.  The title's
``xfb`` rate is that counter, and its ``new`` rate is the same generation
compared against the last one shown: two independent readings of the same
latch, and both say 8.8/s.  A latch is never skipped for a repeated address
(every bit 14 copy re-blits and re-stamps), and a generation is only ever
raised, so the counter cannot miss an update.  The one thing it can do is
*under*count: if two copies complete between two presents the older one is
never selected by ``g_vi_top_field_base`` and is not "new", and a copy the
geometry guard in ``NativeLatchXfb`` rejects is not counted at all - but such
a copy also never reaches the screen.  Neither can turn 35 latches into 8.8,
because 8.8 < 35: presents outnumber copies, so copies are not being coalesced
away.

So the content rate is real.  What the loop does about it is not: it swaps a
stale image four times, paying 0.73 ms of swap per repeat (~2.2 ms of every
~114 ms content frame) and, worse, presenting the same buffer again while the
next guest frame is still being built, which is what reads as jitter at an
unchanged average frame time.  This stage removes the repeats; it does not,
and cannot, raise the 8.8/s.  Raising that is a guest/render throughput job,
and the numbers here say where to look: ``p95 54.79 ms`` against a 20 ms field
budget (``avg 25.69`` already over it) is the one iteration per content frame
that does that frame's GX work.

What CotT does
--------------
CotT's ``native-port/tools/fix_xfb_presentation.py`` is copy-then-clear
ordering, VI-selected presentation, the XFB screenshot and offscreen capture -
no presentation timing at all; CotT's cadence comes from
``fix_field_pacing.py`` plus ``fix_swap_interval.py`` (vsync off).  Its
present-only-on-change gate lives in ``fix_audit_speedups.py``
(``native_do_present``), which counts a present only when it swaps.  This stage
is that gate for MoM, on top of the ordering that MoM already has
(``NATIVE_XFB_COPY_ORDER``, ``NATIVE_COPY_CLEAR_ORDER``,
``NATIVE_XFB_MAIN_PRESENT``), so MoM's equivalent was missing the rate, not the
ordering.

The stage
---------
* presents when the VI-selected XFB's generation is newer than the one on
  screen (the title's ``new`` rule, so the two cannot disagree), when there is
  no XFB yet, when the drawable changed size, or when 200 ms passed with
  nothing to show (a window that has to repaint);
* never waits: the skipped path is the same loop - input, ``ProcessGXFifo``,
  ``native_idle::notify()``, ``pace_video_field()`` - so the guest, the field
  timer and the GX drain are untouched;
* counts a present only when it swaps, so the title's ``presented`` becomes the
  real swap rate (``new`` and ``xfb`` are unchanged, and the screenshot path is
  untouched: it reads the EFB and the presented FBO, not the back buffer);
* ``NWII_NO_PRESENT_PACING=1`` restores present-every-iteration for A/B.

Ordering: register *after* ``07_fix_present_metrics_mom.py`` (it uses
``NativeXfbGeneration`` and the swap anchor) and, because it wraps the line
``fix_dev_hud_mom.py`` anchors on, after ``fix_dev_hud_mom.py`` as well.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_PRESENT_PACING_V1'

main_cpp = runtime / 'src/core/main.cpp'
text = main_cpp.read_text()

if MARK not in text:
    # 1. Decide once per iteration, right where the presented image is chosen.
    anchor = ('        const bool have_present=nwii::runtime::gx::NativeGetXfbFrame('
              'xfb_addr,&present_fbo,&present_w,&present_h);\n')
    assert text.count(anchor) == 1, 'NativeGetXfbFrame anchor'

    gate = (
        '        // ' + MARK + ': swap only when the screen would change.\n'
        '        // The loop is paced by the field timer, not by content, so a swap\n'
        '        // here ran once per host field (~35/s) while the guest completes a\n'
        '        // display copy only ~9 times a second. Nothing below waits: with no\n'
        '        // new image the loop still services input, the GX drain and the\n'
        '        // field timer exactly as before.\n'
        '        static const bool pp_repeat=std::getenv("NWII_NO_PRESENT_PACING")!=nullptr;\n'
        '        static uint64_t pp_shown_gen=0;   // generation of the image on screen\n'
        '        static uint64_t pp_last_ns=0;     // when that image last reached the screen\n'
        '        static int pp_shown_w=0,pp_shown_h=0;\n'
        '        const uint64_t pp_gen=have_present?nwii::runtime::gx::NativeXfbGeneration(xfb_addr):0;\n'
        '        const uint64_t pp_now_ns=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(\n'
        '            std::chrono::steady_clock::now().time_since_epoch()).count());\n'
        '        // Content: a latch newer than anything shown (the title\'s `new` rule,\n'
        '        // so swap rate and `new` rate can never disagree), the first image, a\n'
        '        // resize, or 200 ms with nothing to show (a window that has to repaint).\n'
        '        const bool pp_present_now=pp_repeat || !have_present || pp_gen>pp_shown_gen ||\n'
        '            draw_w!=pp_shown_w || draw_h!=pp_shown_h ||\n'
        '            pp_now_ns-pp_last_ns>200000000ull;\n')
    text = text.replace(anchor, anchor + gate, 1)

    # 2. Guard the swap and remember what the screen now holds.
    anchor = ('        if(std::getenv("NWII_OFFSCREEN")==nullptr){\n'
              '          native_perf_phase::Scope native_perf_phase_swap(native_perf_phase::Phase_Swap); // NATIVE_PERF_PHASE_V1_SWAP\n'
              '          SDL_GL_SwapWindow(window);\n'
              '        }\n')
    assert text.count(anchor) == 1, 'swap anchor'
    new = ('        if(pp_present_now && std::getenv("NWII_OFFSCREEN")==nullptr){\n'
           '          native_perf_phase::Scope native_perf_phase_swap(native_perf_phase::Phase_Swap); // NATIVE_PERF_PHASE_V1_SWAP\n'
           '          SDL_GL_SwapWindow(window);\n'
           '          pp_shown_gen=pp_gen;pp_shown_w=draw_w;pp_shown_h=draw_h;pp_last_ns=pp_now_ns; // ' + MARK + '\n'
           '        }\n')
    text = text.replace(anchor, new, 1)

    # 3. A "present" is a swap: keep the title's rate honest. `new` still counts
    #    each fresh generation exactly once, so it does not move.
    anchor = '          ++pm_presents; pm_new+=fresh?1:0;\n'
    assert text.count(anchor) == 1, 'present counter anchor'
    text = text.replace(anchor, '          if(pp_present_now){++pm_presents; pm_new+=fresh?1:0;}\n', 1)

    # The developer HUD's own present counter means the same thing; gate it too
    # when fix_dev_hud_mom.py has installed it (it runs before this stage).
    hud = '          native_dev_hud::present(fresh); // NATIVE_MOM_DEV_HUD_V1\n'
    assert text.count(hud) <= 1, 'dev HUD present anchor duplicated'
    if text.count(hud) == 1:
        text = text.replace(hud, '          if(pp_present_now)native_dev_hud::present(fresh); // NATIVE_MOM_DEV_HUD_V1\n', 1)

    main_cpp.write_text(text, newline='\n')

print('Presentation is paced to new XFB content (NWII_NO_PRESENT_PACING=1 repeats it).')
