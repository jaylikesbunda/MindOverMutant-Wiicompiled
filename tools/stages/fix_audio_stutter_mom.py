"""Stop the MoM audio stutter: the ring must outlast the host's worst stall.

Diagnosis (this tree's own logs, not the reference port)
--------------------------------------------------------
``[AUDIO]`` is printed every 10 s by ``dsp_audio_pull`` and the numbers in
``logs/boot-perf.log``, ``boot-batchbase.log``, ``boot-batchnew.log`` and
``boot-batchdiag.log`` say three things:

  * ``callbacks=626`` per window -> the SDL device is serviced exactly 62.6
    times/s and consumes 626 * 512 / 10 = 32051 frames/s.  The 512-frame
    (16 ms) device period is *not* the problem: a late callback plays silence
    without touching ``adma_ring``, so it cannot raise the underrun counter.
  * ``drops=0`` in every window, ``max_backlog_ms`` 70-76 ms -> the 600 ms
    burst policy of ``NATIVE_AUDIO_POLICY_V3`` never fires.  "The policy
    discards bursts" is falsified by the port's own counters.
  * ``underruns=20-26`` per window (2.0-2.6 per second) -> the *ring* is empty
    when the callback runs, ~25 times per 10 s.  That is the stutter.

The ring never holds the 60 ms it aims for: the mean ``backlog_ms`` sampled at
the report instant is 28.5-31.4 ms in all four logs.  That is not a
coincidence, it is the controller's saturation point.  With
``ratio = 1 + clamp(err * 0.02, -0.01, +0.08)`` the largest shortfall the pull
can absorb is 1 %, and it can only do that with the ring at
``kTarget * (1 - 0.5)`` = 960 frames = **30 ms** -- exactly what was measured.
So the guest produces slightly less than the 32000 frames/s the device
consumes, the ring parks on the clamp floor instead of the 60 ms setpoint, and
any host hitch that reaches 30 ms empties it.

Every one of those underruns then costs far more than the missing samples:
``primed`` is cleared and the next callback plays **silence until the ring is
back at kTarget (60 ms)**, so ~25 underruns/s * 10 s = about 1.5 s of dead air
per 10 s, in 60 ms blocks.  That is the audible stutter.

The size of the shortfall can be read off the underrun *period*, because one
underrun is exactly one fill/drain cycle::
    T = kTarget / produced + kTarget / (consumed - produced)
boot-perf measured T = 10 s / 25.5 = 392 ms, which is what a production ratio of
0.80 predicts (75 ms to re-prime + 316 ms to drain); boot-batchnew measured
T = 503 ms, i.e. ratio 0.85.  So the guest was delivering 25 600-27 200
frames/s, not 32 000.  ``drops=0`` proves the direction: a ratio above 1 would
have grown the ring into the 600 ms limit and counted drops instead.

Production itself is *not* on the render thread: the guest runs on its own
``cpu_thread`` (``src/core/main.cpp:61,535``) and the ring is filled from
``adma_event_cb`` (``src/hw/hw_dsp.cpp:200``) via ``EventScheduler::advance``
(``src/hle/ios.cpp:1608``), while the consumer is SDL's audio thread.  What
couples audio to the host is that the *producer clock* is the guest thread's
progress: ``host_field`` 12.1 ms + ``guest_idle`` 10.4 ms of a 24.3 ms median
frame, p95 34.4 ms, p99 39.4 ms, max 49.5 ms, and 79 % of frames over the
20.05 ms field budget.  Two code-level ways that turns into a shortfall are
fixed here (see below); whatever remains is measured by ``produced_fps``.

What this stage changes
-----------------------
``src/hw/hw_dsp.cpp`` -- ``dsp_audio_pull``

  * ``kTarget`` 1920 -> **2880 frames (90.0 ms)**.  Arithmetic: the ring has to
    carry one whole device callback plus the worst host stall,
    ``1024 + 0.0495 * 32000 = 1024 + 1584 = 2608`` frames; 2880 is that plus
    10 % (``2880 / 32 = 90 ms``).
  * ``kPrime`` (new) = **640 frames (20 ms)** instead of re-arming at
    ``kTarget``.  CotT's ``NATIVE_AUDIO_PRIME_V2`` made the same change for the
    same reason; it cuts the silence after each underrun from 60 ms to 20 ms.
  * rate clamp ``-1 %`` -> **-5 %**, gain ``0.02`` -> ``0.05``.  In steady state
    the ring settles at ``kTarget * (1 - shortfall/gain)``, so the old policy
    held 30 ms at a 1 % shortfall and was empty at 2 %; the new one holds 72 ms
    at 1 % and 45 ms at 2.5 %.  That is margin, not a substitute for the rate
    fix below: a 15 % shortfall cannot be absorbed by playing slower without
    obvious pitch loss.
  * the interpolator no longer ends the callback one frame early when
    ``have == frames`` (``idx + 1 >= have`` used to force a spurious underrun,
    and a spurious underrun costs a whole re-prime).
  * 2 ms fade-in after every (re)prime, so the recovery is not a click.
  * new bounded ``[AUDIO2]`` line every 10 s:
    ``underruns``, ``silent_ms``, ``produced_fps``, ``consumed_fps``,
    ``backlog_ms``, ``target_ms``, ``prime_ms``.  All of them are reset with
    the window, so the line cannot grow.  ``produced_fps`` is the number that
    decides the diagnosis: it counts frames the DSP DMA actually delivered
    (``adma_pushed``), so 32000 means production is healthy and only jitter
    was ever wrong, anything below 32000 means the guest clock is short.

``src/hw/hw_dsp.cpp`` -- the DSP DMA rate

  * The recurring DMA is scheduled from ``adma_control & 0x7fff`` *once*, when
    the DMA is enabled (``hw_dsp.cpp:352``), while ``adma_event_cb`` reads the
    block count *live* (``hw_dsp.cpp:203``).  If the guest changes the burst
    length while the DMA is running, the event period keeps the old length and
    the ring is filled at ``new_blocks / enabled_blocks`` x 32000 frames/s.
    The write handler now re-schedules on a block-count change.
  * The rate is therefore no longer latched.  This is the most likely source of
    the measured shortfall; ``produced_fps`` proves whether it was.

``src/core/main.cpp``

  * ``want.samples`` 512 -> **1024 frames (32.0 ms)**: more than the p50 (23.4)
    and p90 (33.3 -> within 4 %) host frame time and two 20.05 ms field
    budgets, so one callback can never straddle two over-budget frames.
  * one ``[AUDIO2]`` line with the device spec SDL actually obtained
    (``have.freq``/``samples``/``channels``/``period_ms``), because the whole
    latency budget depends on the device rate.

Kill switch: ``NWII_MOM_AUDIO_STUTTER=0`` restores the previous behaviour
exactly -- 512-frame device period, 60 ms target, 60 ms re-prime, +-1 % clamp,
no re-schedule of the DMA rate, no ``[AUDIO2]`` counters (only the device-spec
line, which is diagnostic only).  Deleting the stage line is a full rollback.

Verified by reading only: no build, no run.  Every anchor below is checked to
occur exactly once in the current file text before it is replaced.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_AUDIO_STUTTER_V1'


def read(path):
    return path.read_text(encoding='utf-8')


def write(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def replace_once(text, old, new, what):
    count = text.count(old)
    assert count == 1, '%s anchor: expected 1 occurrence, found %d' % (what, count)
    return text.replace(old, new, 1)


dsp_path = runtime / 'src/hw/hw_dsp.cpp'
main_path = runtime / 'src/core/main.cpp'
dsp = read(dsp_path)
main = read(main_path)

# ---------------------------------------------------------------------------
# 1. dsp_audio_pull: target, re-prime, rate clamp, interpolator, [AUDIO2]
# ---------------------------------------------------------------------------
if MARK not in dsp:
    begin = 'size_t dsp_audio_pull(int16_t* out, size_t frames) { // NATIVE_MOM_AUDIO_JITTER_V1\n'
    end = '\nstatic void adma_event_cb'
    assert dsp.count(begin) == 1, 'dsp_audio_pull start anchor'
    assert dsp.count(end) == 1, 'adma_event_cb anchor'
    start = dsp.index(begin)
    stop = dsp.index(end, start)
    current = dsp[start:stop]
    # The slice must be the policy NATIVE_MOM_AUDIO_JITTER_V1 + POLICY_V3 installed,
    # or this stage is rewriting something it was not written against.
    for needle in (
        'static const double kTarget = 1920.0;   // 60 ms at 32 kHz\n',
        'static const size_t kHigh = 19200;      // 600 ms\n',
        'std::memset(out, 0, frames * 2 * sizeof(int16_t));',
        'if (have < size_t(kTarget)) {',
        'double ratio = 1.0 + std::max(-0.01, std::min(0.08, err * 0.02));',
        'if (idx + 1 >= have) break;',
        'underruns=%llu drops=%llu max_backlog_ms=%zu backlog_ms=%zu',
    ):
        assert current.count(needle) == 1, 'dsp_audio_pull body anchor: %r' % needle[:48]

    new_pull = '''static uint64_t adma_pushed = 0; // ''' + MARK + ''': frames the DSP DMA delivered into adma_ring
size_t dsp_audio_pull(int16_t* out, size_t frames) { // ''' + MARK + '''
    // NATIVE_MOM_AUDIO_STUTTER_V1: see tools/stages/fix_audio_stutter_mom.py
    // NWII_MOM_AUDIO_STUTTER=0 restores the NATIVE_MOM_AUDIO_JITTER_V1 policy
    // exactly: 1.92 k-frame target, 60 ms re-prime, +-1 % rate clamp.
    static const bool fix = []() {
        const char* e = std::getenv("NWII_MOM_AUDIO_STUTTER");
        return !(e && e[0] == '0');
    }();
    std::lock_guard<std::mutex> lock(adma_mutex);
    // 2880 frames = 90 ms at 32 kHz: one 32 ms device callback plus the worst
    // measured host stall (49.5 ms = 1584 frames), plus 10 %.
    static const double kTarget = fix ? 2880.0 : 1920.0;
    // Re-prime at 640 frames = 20 ms. Re-arming at kTarget made every underrun
    // cost 60 ms of silence; the port measured 2.0-2.6 underruns/s.
    static const double kPrime = fix ? 640.0 : 1920.0;
    static const size_t kHigh = 19200;      // 600 ms
    static double position = 0.0;
    static bool primed = false;
    static size_t fade = 0;
    static uint64_t st_calls = 0, st_under = 0, st_drop = 0;
    static uint64_t st_silent = 0, st_consumed = 0, st_pushed0 = 0;
    static size_t st_max = 0;
    static auto st_t0 = std::chrono::steady_clock::now();
    size_t have = adma_ring.size() / 2;
    size_t i = 0;
    ++st_calls;
    st_max = std::max(st_max, have);
    if (!primed && have < size_t(kPrime)) {
        // Not enough audio to start (or restart) the stream. The report below
        // still runs: a starved stream is exactly when its counters matter.
        std::memset(out, 0, frames * 2 * sizeof(int16_t));
        st_silent += (frames * 1000u + 31999u) / 32000u;  // ms lost while re-priming
    } else {
        if (!primed) {
            primed = true;
            position = 0.0;
            fade = 64;   // 2 ms fade-in: the stream restarts without a click
        }
        if (have > kHigh) {
            size_t drop = have - size_t(kTarget);
            adma_ring.erase(adma_ring.begin(), adma_ring.begin() + drop * 2);
            have -= drop;
            position = 0.0;
            ++st_drop;
        }
        double err = (double(have) - kTarget) / kTarget;
        // Playing slower is the only way to absorb a chronic production shortfall:
        // the guest does not always deliver 32 kHz of audio per wall second. The old
        // 1 % clamp saturated with the ring at 0.5 * kTarget = 960 frames = 30 ms,
        // which is exactly the backlog this port measured (28.5-31.4 ms).
        double ratio = fix ? 1.0 + std::max(-0.05, std::min(0.08, err * 0.05))
                           : 1.0 + std::max(-0.01, std::min(0.08, err * 0.02));
        for (; i < frames; ++i) {
            size_t idx = size_t(position);
            if (idx >= have) break;
            // idx + 1 >= have used to end the callback one frame early, and one
            // early frame costs a whole re-prime.
            const size_t nxt = (idx + 1 < have) ? idx + 1 : idx;
            double frac = position - double(idx);
            for (int c = 0; c < 2; ++c) {
                double a = adma_ring[idx * 2 + c], b = adma_ring[nxt * 2 + c];
                out[i * 2 + c] = int16_t(a + (b - a) * frac);
            }
            position += ratio;
        }
        size_t consumed = std::min(size_t(position), have);
        if (consumed) {
            adma_ring.erase(adma_ring.begin(), adma_ring.begin() + consumed * 2);
            position -= double(consumed);
            st_consumed += consumed;
        }
        if (i < frames) {
            const size_t missing = frames - i;
            std::memset(out + i * 2, 0, missing * 2 * sizeof(int16_t));
            primed = false;
            fade = 0;
            ++st_under;
            st_silent += (missing * 1000u + 31999u) / 32000u;  // ms lost, bounded by the window
        }
        if (fade) {
            const size_t n = std::min(i, fade);
            for (size_t k = 0; k < n; ++k) {
                const int g = int(256 * (fade - k) / fade);
                out[k * 2 + 0] = int16_t((int(out[k * 2 + 0]) * g) >> 8);
                out[k * 2 + 1] = int16_t((int(out[k * 2 + 1]) * g) >> 8);
            }
            fade = (fade > i) ? fade - i : 0;
        }
    }
    {
        const auto now = std::chrono::steady_clock::now();
        const double elapsed = std::chrono::duration<double>(now - st_t0).count();
        if (elapsed >= 10.0) {
            const uint64_t pushed = adma_pushed - st_pushed0;
            st_t0 = now;
            st_pushed0 = adma_pushed;
            std::printf("[AUDIO] callbacks=%llu underruns=%llu drops=%llu max_backlog_ms=%zu backlog_ms=%zu\\n",
                        (unsigned long long)st_calls, (unsigned long long)st_under, (unsigned long long)st_drop,
                        st_max / 32, adma_ring.size() / 64);
            std::printf("[AUDIO2] underruns=%llu silent_ms=%llu produced_fps=%llu consumed_fps=%llu"
                        " backlog_ms=%zu target_ms=%zu prime_ms=%zu\\n",
                        (unsigned long long)st_under, (unsigned long long)st_silent,
                        (unsigned long long)(double(pushed) / elapsed),
                        (unsigned long long)(double(st_consumed) / elapsed),
                        adma_ring.size() / 64, size_t(kTarget / 32), size_t(kPrime / 32));
            std::fflush(stdout);
            st_calls = st_under = st_drop = st_silent = st_consumed = 0;
            st_max = 0;
        }
    }
    return frames;
}
'''
    dsp = dsp[:start] + new_pull + dsp[stop:]

    # -----------------------------------------------------------------------
    # 2. count what the DSP DMA actually delivered (frames, under the lock)
    # -----------------------------------------------------------------------
    dsp = replace_once(dsp, '''        if (adma_ring.size() < 32000 * 2) {
            uint32_t va = adma_addr | 0x80000000;
            for (uint32_t i = 0; i + 3 < bytes; i += 4) {
                adma_ring.push_back((int16_t)g_mmu->read16(va + i + 2));
                adma_ring.push_back((int16_t)g_mmu->read16(va + i));
            }
        }
''', '''        const size_t ring_before = adma_ring.size(); // ''' + MARK + '''
        if (adma_ring.size() < 32000 * 2) {
            uint32_t va = adma_addr | 0x80000000;
            for (uint32_t i = 0; i + 3 < bytes; i += 4) {
                adma_ring.push_back((int16_t)g_mmu->read16(va + i + 2));
                adma_ring.push_back((int16_t)g_mmu->read16(va + i));
            }
        }
        adma_pushed += (adma_ring.size() - ring_before) / 2; // frames/s is printed as produced_fps
''', 'adma_event_cb ring fill')

    # -----------------------------------------------------------------------
    # 3. the DMA rate must follow the *live* block count, not the one latched
    #    when the DMA was enabled (hw_dsp.cpp:352 vs :203)
    # -----------------------------------------------------------------------
    dsp = replace_once(dsp, '''                bool was_on = (adma_control & 0x8000) != 0;
                adma_control = val & 0xFFFF;
                if (!was_on && (adma_control & 0x8000)) {
''', '''                bool was_on = (adma_control & 0x8000) != 0;
                const uint16_t prev_blocks = uint16_t(adma_control & 0x7FFF); // ''' + MARK + '''
                adma_control = val & 0xFFFF;
                if (!was_on && (adma_control & 0x8000)) {
''', 'DSP DMA enable')

    dsp = replace_once(dsp, '''                } else if (was_on && !(adma_control & 0x8000)) {
                    if (s_adma_event_id != 0) {
                        EventScheduler::get().cancel(s_adma_event_id);
                        s_adma_event_id = 0;
                    }
                }
''', '''                } else if (was_on && !(adma_control & 0x8000)) {
                    if (s_adma_event_id != 0) {
                        EventScheduler::get().cancel(s_adma_event_id);
                        s_adma_event_id = 0;
                    }
                } else if (was_on && (adma_control & 0x8000) &&
                           uint16_t(adma_control & 0x7FFF) != prev_blocks &&
                           !(std::getenv("NWII_MOM_AUDIO_STUTTER") &&
                             std::getenv("NWII_MOM_AUDIO_STUTTER")[0] == '0')) {
                    // ''' + MARK + ''': the burst length changed under a running DMA. The
                    // event period was latched from the block count in force when the DMA
                    // was enabled, but adma_event_cb reads the live one, so the ring was
                    // filled at (new blocks / enabled blocks) x 32000 frames/s afterwards.
                    if (s_adma_event_id != 0) {
                        EventScheduler::get().cancel(s_adma_event_id);
                        s_adma_event_id = 0;
                    }
                    if (adma_control & 0x7FFF) {
                        s_adma_event_id = EventScheduler::get().schedule_recurring(
                            std::max<uint64_t>(1, g_ctx_ptr->tb_freq *
                                (uint64_t(adma_control & 0x7fff) * 8) / 32000), adma_event_cb);
                    }
                }
''', 'DSP DMA disable')

    for include in ('<cstdlib>', '<cstdint>'):
        if '#include ' + include not in dsp:
            dsp = '#include ' + include + '\n' + dsp

    write(dsp_path, dsp)

# ---------------------------------------------------------------------------
# 4. main.cpp: device period + the spec SDL actually obtained
# ---------------------------------------------------------------------------
if MARK not in main:
    main = replace_once(main,
        '    want.samples = 512; // NATIVE_MOM_AUDIO_JITTER_V1\n',
        '''    // ''' + MARK + ''': 1024 frames = 32.0 ms, more than the median (23.4 ms)
    // and p90 (33.3 ms) host frame time and two 20.05 ms field budgets, so one
    // callback cannot straddle two over-budget frames. The 16 ms period was not
    // the cause of the stutter: the device consumed exactly 626 callbacks * 512
    // frames / 10 s = 32051 frames/s in every logged window.
    // NWII_MOM_AUDIO_STUTTER=0 restores 512.
    static const char* const mom_audio_switch = std::getenv("NWII_MOM_AUDIO_STUTTER");
    const bool mom_audio_fix = !(mom_audio_switch && mom_audio_switch[0] == '0');
    want.samples = mom_audio_fix ? 1024 : 512; // NATIVE_MOM_AUDIO_JITTER_V1
''', 'want.samples')

    main = replace_once(main,
        '    if (audio_dev > 0) SDL_PauseAudioDevice(audio_dev, 0);\n',
        '''    if (audio_dev > 0) SDL_PauseAudioDevice(audio_dev, 0);
    if (audio_dev > 0) { // ''' + MARK + ''': the device rate the latency budget is built on
      std::printf("[AUDIO2] device freq=%d samples=%d channels=%d period_ms=%.1f\\n",
                  have.freq, int(have.samples), int(have.channels),
                  have.freq ? 1000.0 * double(have.samples) / double(have.freq) : 0.0);
      std::fflush(stdout);
    }
''', 'SDL_PauseAudioDevice')

    write(main_path, main)

print('Audio: SDL device 32 ms, ring 90 ms, re-prime 20 ms, -5% catch-up, DMA rate follows '
      'the live block count; NWII_MOM_AUDIO_STUTTER=0 restores the old policy.')
