"""Report the texture the last draw sampled when a green frame is captured.

The wash is flat, so either the upload was uniform or the sampler picked a mip
level that is uniform (the 1x1 average of the picture - mid grey, which the
TEV then maps to a flat colour). Level 0 tells them apart:

  * level 0 uniform  -> the content itself was flat, so the guest wrote a flat
    frame or the upload read a cleared buffer;
  * level 0 detailed -> the sampler was reading a higher mip, i.e. the LOD was
    wrong for that draw.

The probe reads back level 0 of whatever texture unit 0 has bound when the
green probe captures a frame, plus the mip level count, and prints the mean and
spread per channel. One readback per capture, at most eight captures.

Opt-in: it runs only inside the capture path that ``NWII_GREEN_PROBE`` enables.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_TEX_CAPTURE_PROBE_V1'
main = runtime / 'src/core/main.cpp'
text = main.read_text()
if MARK in text:
    print('Texture capture probe already present.')
    raise SystemExit(0)

anchor = """              NativeDumpDrawRing();"""
assert text.count(anchor) == 1, 'dump anchor'
probe = anchor + """
              { // """ + MARK + """: was level 0 flat, or did the sampler pick a mip?
                GLint prev_active = 0;
                glGetIntegerv(GL_ACTIVE_TEXTURE, &prev_active);
                glActiveTexture(GL_TEXTURE0);
                GLint bound = 0;
                glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
                GLint w = 0, h = 0;
                if (bound) glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH, &w);
                if (bound) glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT, &h);
                int levels = 0;
                for (int l = 0; l < 12; ++l) {
                  GLint lw = 0;
                  glGetTexLevelParameteriv(GL_TEXTURE_2D, l, GL_TEXTURE_WIDTH, &lw);
                  if (lw <= 0) break;
                  ++levels;
                }
                std::cout << "[TexCapture] gl=" << bound << " " << w << "x" << h
                          << " levels=" << levels;
                if (bound && w > 0 && h > 0 && w <= 2048 && h <= 2048) {
                  std::vector<unsigned char> px(size_t(w) * h * 4);
                  glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                  double mean[3] = {0, 0, 0};
                  unsigned char lo[3] = {255, 255, 255}, hi[3] = {0, 0, 0};
                  for (int y = 0; y < h; ++y)
                    for (int x = 0; x < w; ++x) {
                      const unsigned char* p = &px[(size_t(y) * w + x) * 4];
                      for (int c = 0; c < 3; ++c) {
                        mean[c] += p[c];
                        if (p[c] < lo[c]) lo[c] = p[c];
                        if (p[c] > hi[c]) hi[c] = p[c];
                      }
                    }
                  for (int c = 0; c < 3; ++c) mean[c] /= double(w) * h;
                  std::cout << " mean=" << int(mean[0]) << "," << int(mean[1]) << ","
                            << int(mean[2]) << " range="
                            << int(lo[0]) << "-" << int(hi[0]) << "/"
                            << int(lo[1]) << "-" << int(hi[1]) << "/"
                            << int(lo[2]) << "-" << int(hi[2]);
                }
                std::cout << std::endl;
                glActiveTexture(GLuint(prev_active));
              }"""
text = text.replace(anchor, probe, 1)
main.write_text(text)
print('Texture capture probe installed.')
