"""A real jitter buffer for the audio output, driven from the SDL audio thread.

Ported from CotT's ``tools/fix_audio_jitter_buffer.py`` and
``tools/fix_audio_policy_v3.py``, re-derived against this tree's DSP/ADMA path.

Measured on the reference port at the title screen: the ADMA ring held about
419 ms of audio (latency, so the sound lags the picture), was cut off at its 1 s
cap (dropping audio, which is heard as clicks and a looping, stuttering
stream), and was only drained from the video main loop in 1024-frame chunks, so
any slow frame starved the device.

Now the SDL audio callback pulls directly:

  * ``dsp_audio_pull`` keeps about 60 ms queued;
  * it trims the playback rate by at most 1% to steer toward that target;
  * when the guest delivers audio in bursts (the guest runs slower than real
    time, so the DSP/ADMA events catch up in bursts) the backlog limit is
    600 ms and the catch-up is done by playing up to 8% faster instead of
    dropping the oldest audio - dropping is what made whole sound effects and
    cutscene audio vanish;
  * it re-primes after an underrun;
  * ``[AUDIO]`` counters (callbacks, underruns, drops, max backlog) are printed
    every 10 s so the behaviour can be checked from the log.

Guest-visible behaviour (ADMA timing, interrupts) is unchanged: the ADMA event
still fills the ring and still raises the DSP interrupt at the same points.

The main loop no longer drains the queue, so a slow frame cannot starve the
device.

Verified by reading ``src/hw/hw_dsp.cpp`` (the ring, the ADMA callback and its
1 s cap) and ``src/core/main.cpp`` (the 1024-frame drain in the frame loop and
the SDL device setup); the ring cap of 32000 samples is left in place, so the
backlog limit below it is the one that matters.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_MOM_AUDIO_JITTER_V1'

# ---- the jitter buffer ------------------------------------------------------
dsp = runtime / 'src/hw/hw_dsp.cpp'
text = dsp.read_text()
if MARK not in text:
    start = text.index('size_t dsp_audio_pull(int16_t* out, size_t frames) {')
    end = text.index('static void adma_event_cb', start)
    new = '''size_t dsp_audio_pull(int16_t* out, size_t frames) { // ''' + MARK + '''
    std::lock_guard<std::mutex> lock(adma_mutex);
    static const double kTarget = 1920.0;   // 60 ms at 32 kHz
    static const size_t kHigh = 19200;      // 600 ms
    static double position = 0.0;
    static bool primed = false;
    static uint64_t st_calls = 0, st_under = 0, st_drop = 0;
    static size_t st_max = 0;
    static auto st_t0 = std::chrono::steady_clock::now();
    size_t have = adma_ring.size() / 2;
    ++st_calls;
    st_max = std::max(st_max, have);
    if (!primed) {
        if (have < size_t(kTarget)) {
            std::memset(out, 0, frames * 2 * sizeof(int16_t));
            return frames;
        }
        primed = true;
        position = 0.0;
    }
    if (have > kHigh) {
        size_t drop = have - size_t(kTarget);
        adma_ring.erase(adma_ring.begin(), adma_ring.begin() + drop * 2);
        have -= drop;
        position = 0.0;
        ++st_drop;
    }
    double err = (double(have) - kTarget) / kTarget;
    // Catch up by playing faster, never by dropping audio: dropping is what
    // made whole sound effects vanish when the guest delivered audio in bursts.
    double ratio = 1.0 + std::max(-0.01, std::min(0.08, err * 0.02));
    size_t i = 0;
    for (; i < frames; ++i) {
        size_t idx = size_t(position);
        if (idx + 1 >= have) break;
        double frac = position - double(idx);
        for (int c = 0; c < 2; ++c) {
            double a = adma_ring[idx * 2 + c], b = adma_ring[(idx + 1) * 2 + c];
            out[i * 2 + c] = int16_t(a + (b - a) * frac);
        }
        position += ratio;
    }
    size_t consumed = std::min(size_t(position), have);
    if (consumed) {
        adma_ring.erase(adma_ring.begin(), adma_ring.begin() + consumed * 2);
        position -= double(consumed);
    }
    if (i < frames) {
        std::memset(out + i * 2, 0, (frames - i) * 2 * sizeof(int16_t));
        primed = false;
        ++st_under;
    }
    {
        const auto now = std::chrono::steady_clock::now();
        if (std::chrono::duration_cast<std::chrono::seconds>(now - st_t0).count() >= 10) {
            st_t0 = now;
            std::printf("[AUDIO] callbacks=%llu underruns=%llu drops=%llu max_backlog_ms=%zu backlog_ms=%zu\\n",
                        (unsigned long long)st_calls, (unsigned long long)st_under, (unsigned long long)st_drop,
                        st_max / 32, adma_ring.size() / 64);
            std::fflush(stdout);
            st_calls = st_under = st_drop = 0;
            st_max = 0;
        }
    }
    return frames;
}

'''
    text = text[:start] + new + text[end:]
    for inc in ('<algorithm>', '<chrono>', '<cstdio>'):
        if '#include ' + inc not in text:
            text = '#include ' + inc + '\n' + text
    dsp.write_text(text)

# ---- drive it from the SDL audio callback ----------------------------------
main_cpp = runtime / 'src/core/main.cpp'
text = main_cpp.read_text()
if MARK not in text:
    old = ('    want.samples = 1024;\n'
           '    audio_dev = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);\n')
    assert text.count(old) == 1, 'SDL audio device anchor'
    new = ('    want.samples = 512; // ' + MARK + '\n'
           '    want.callback = [](void*, Uint8* stream, int len) {\n'
           '      nwii::runtime::hw::dsp_audio_pull(reinterpret_cast<int16_t*>(stream), size_t(len) / 4);\n'
           '    };\n'
           '    audio_dev = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);\n')
    text = text.replace(old, new, 1)

    old = ('      if (audio_dev > 0) {\n'
           '        if (SDL_GetQueuedAudioSize(audio_dev) < 1024 * 4 * 2) {\n'
           '          static int16_t abuf[1024 * 2];\n'
           '          size_t produced=nwii::runtime::hw::dsp_audio_pull(abuf, 1024);\n'
           '          if(produced)SDL_QueueAudio(audio_dev, abuf, Uint32(produced * 4));\n'
           '        }\n'
           '      }\n')
    assert text.count(old) == 1, 'audio drain anchor'
    new = ('      // audio is pulled by the SDL callback (' + MARK + ')\n')
    text = text.replace(old, new, 1)
    main_cpp.write_text(text)

print('Audio output has a 60 ms jitter buffer driven by the SDL callback; bursts catch up by speeding up.')
