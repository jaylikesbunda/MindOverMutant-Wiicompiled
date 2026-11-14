"""Capture the frame when the whole picture is tinted green.

A full-screen green flash lasts exactly one frame, so a probe that samples
every Nth frame misses it. Reading the framebuffer three times per frame is
far too expensive, so this probe *blits the whole EFB down* to a 32x24 texture
once (one cached FBO, one blit) and reads that back: one tiny readback per
frame, covering the entire picture.

A colour *wash* tints every band including the sky; a normal scene has a
blue/grey sky and a green floor. The probe therefore requires the top, middle
and bottom bands to be green-dominant before it writes anything, and logs each
band's mean so a borderline case is readable.

Opt-in: ``NWII_GREEN_PROBE=1``. At most eight frames are written, each as a
PPM plus the TEV colour/konst registers, the texture address/format and the
TLUT offset/format. ``NWII_GREEN_PROBE_EVERY`` still throttles it if wanted.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
path = port / 'vendor/NWiiRecomp/nWiiRuntime/src/core/main.cpp'
text = path.read_text()
MARK = 'NATIVE_GREEN_WASH_PROBE_V2'

if MARK in text:
    print('Green-wash probe already present.')
    raise SystemExit(0)

# Drop the earlier version of the probe if it is installed.
for old_mark in ('NATIVE_GREEN_WASH_PROBE_V1', 'NATIVE_GREEN_FRAME_PROBE_V1'):
    start_marker = '        { // ' + old_mark
    if start_marker in text:
        start = text.index(start_marker)
        end = text.index('        if (const char *shot_pfx = std::getenv("NWII_SCREENSHOT")) {', start)
        text = text[:start] + text[end:]

anchor = """        if (const char *shot_pfx = std::getenv("NWII_SCREENSHOT")) {"""
assert text.count(anchor) == 1, 'screenshot block anchor'

probe = r'''        { // ''' + MARK + r'''
          // One downsample blit of the whole EFB plus one 32x24 readback: cheap
          // enough to run every frame, and it sees the entire picture, so a
          // one-frame wash cannot slip between samples.
          static const bool green_probe = std::getenv("NWII_GREEN_PROBE") != nullptr;
          static const unsigned every = []{
            const char* e = std::getenv("NWII_GREEN_PROBE_EVERY");
            const long v = e ? std::strtol(e, nullptr, 10) : 1;
            return unsigned(v < 1 ? 1 : (v > 600 ? 600 : v));
          }();
          static unsigned tick = 0, written = 0;
          if (green_probe && written < 8 && (++tick % every) == 0) {
            static GLuint down_fbo = 0, down_tex = 0;
            const int dw = 32, dh = 24;
            if (!down_fbo) {
              glGenFramebuffers(1, &down_fbo);
              glGenTextures(1, &down_tex);
              glBindTexture(GL_TEXTURE_2D, down_tex);
              glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, dw, dh, 0, GL_RGBA,
                           GL_UNSIGNED_BYTE, nullptr);
              glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
              glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
              glBindFramebuffer(GL_DRAW_FRAMEBUFFER, down_fbo);
              glFramebufferTexture2D(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                     GL_TEXTURE_2D, down_tex, 0);
            }
            GLint prev_read = 0, prev_draw = 0;
            glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &prev_read);
            glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &prev_draw);
            const GLboolean scissor = glIsEnabled(GL_SCISSOR_TEST);
            glDisable(GL_SCISSOR_TEST);
            glBindFramebuffer(GL_READ_FRAMEBUFFER, efb_fbo);
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, down_fbo);
            glBlitFramebuffer(0, 0, NativeEfbWidth(), NativeEfbHeight(),
                              0, 0, dw, dh, GL_COLOR_BUFFER_BIT, GL_LINEAR);
            unsigned char small[dw * dh * 4];
            glBindFramebuffer(GL_READ_FRAMEBUFFER, down_fbo);
            glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, small);
            glBindFramebuffer(GL_READ_FRAMEBUFFER, GLuint(prev_read));
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, GLuint(prev_draw));
            if (scissor) glEnable(GL_SCISSOR_TEST);
            // Row 0 of the readback is the bottom of the picture.
            double band[3][3] = {{0}};
            bool green_band[3] = {false, false, false};
            for (int b = 0; b < 3; ++b) {
              const int y0 = dh - 1 - int(dh * (b * 0.30 + 0.12));
              const int y1 = y0 - 5;
              int n = 0;
              for (int y = y1; y <= y0; ++y) {
                if (y < 0 || y >= dh) continue;
                for (int x = 0; x < dw; ++x) {
                  const unsigned char* p = &small[(y * dw + x) * 4];
                  band[b][0] += p[0]; band[b][1] += p[1]; band[b][2] += p[2];
                  ++n;
                }
              }
              if (n) { for (int c = 0; c < 3; ++c) band[b][c] /= n; }
              green_band[b] = band[b][1] > 30.0 &&
                              band[b][1] > band[b][0] * 1.25 &&
                              band[b][1] > band[b][2] * 1.25;
            }
            if (green_band[0] && green_band[1] && green_band[2]) {
              ++written;
              std::cout << "[GreenWash] #" << written;
              for (int b = 0; b < 3; ++b)
                std::cout << " band" << b << "=" << int(band[b][0]) << ","
                          << int(band[b][1]) << "," << int(band[b][2]);
              std::cout << " gen=" << std::hex
                        << nwii::runtime::gx::g_state.bp[0]
                        << " C0=" << nwii::runtime::gx::g_state.bp[0xc0]
                        << " C1=" << nwii::runtime::gx::g_state.bp[0xc1]
                        << " C2=" << nwii::runtime::gx::g_state.bp[0xc2]
                        << " C3=" << nwii::runtime::gx::g_state.bp[0xc3]
                        << " K0=" << nwii::runtime::gx::g_state.bp[0xf6]
                        << " K1=" << nwii::runtime::gx::g_state.bp[0xf7]
                        << std::dec << " tex0=0x" << std::hex
                        << nwii::runtime::gx::g_state.texStages[0].base_addr
                        << " fmt=" << int(nwii::runtime::gx::g_state.texStages[0].format)
                        << " tlut=0x" << nwii::runtime::gx::g_state.texStages[0].tlut_offset
                        << std::dec << " tlutfmt="
                        << int(nwii::runtime::gx::g_state.texStages[0].tlut_format)
                        << " tev=" << int(nwii::runtime::gx::g_state.numTevStages)
                        << std::endl;
              std::vector<unsigned char> full(NativeEfbWidth() * NativeEfbHeight() * 4);
              glBindFramebuffer(GL_READ_FRAMEBUFFER, efb_fbo);
              glReadPixels(0, 0, NativeEfbWidth(), NativeEfbHeight(), GL_RGBA,
                           GL_UNSIGNED_BYTE, full.data());
              glBindFramebuffer(GL_READ_FRAMEBUFFER, GLuint(prev_read));
              const std::string name = std::string("logs/greenwash_") +
                                       std::to_string(written) + ".ppm";
              if (FILE *f = std::fopen(name.c_str(), "wb")) {
                std::fprintf(f, "P6\n%d %d\n255\n", NativeEfbWidth(), NativeEfbHeight());
                for (int yy = NativeEfbHeight() - 1; yy >= 0; --yy)
                  for (int xx = 0; xx < NativeEfbWidth(); ++xx) {
                    const size_t o = (size_t(yy) * NativeEfbWidth() + xx) * 4;
                    std::fwrite(&full[o], 1, 3, f);
                  }
                std::fclose(f);
                std::cout << "[GreenWash] wrote " << name << std::endl;
              }
            }
          }
        }

'''
text = text.replace(anchor, probe + anchor, 1)
path.write_text(text)
print('Green-wash probe v2 installed (whole-frame downsample, every frame).')
