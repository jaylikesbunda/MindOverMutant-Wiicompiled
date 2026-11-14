"""Log a non-black copy clear.

A flat wash that stops exactly at the letterbox bars is what a *cleared
rectangle* looks like: the copy clear runs under a scissor set to the copy
rectangle, so the bars outside it keep whatever they had. The movie's clear
should be black, so any other colour is worth a line.

Opt-in: ``NWII_COPY_CLEAR_PROBE=1``. Prints one line per non-black copy clear
(at most 200), with the rectangle, and the same for a direct EFB clear.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
if not (port / 'vendor').is_dir():
    port = port.parent
runtime = port / 'vendor/NWiiRecomp/nWiiRuntime'
MARK = 'NATIVE_COPY_CLEAR_PROBE_V1'
renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK in text:
    print('Copy-clear probe already present.')
    raise SystemExit(0)

anchor = """            unsigned char cc[4];::GX_GetClearColor(cc);
            glClearColor(cc[0]/255.f,cc[1]/255.f,cc[2]/255.f,cc[3]/255.f);"""
assert text.count(anchor) == 1, 'copy clear anchor'
probe = anchor + """
            { // """ + MARK + """: a clear that is not black tints the copy rect.
              static const bool probe = std::getenv("NWII_COPY_CLEAR_PROBE") != nullptr;
              static unsigned shown = 0;
              if (probe && shown < 200 && (cc[0] | cc[1] | cc[2])) {
                ++shown;
                std::cout << "[CopyClear] rgba=" << int(cc[0]) << "," << int(cc[1])
                          << "," << int(cc[2]) << "," << int(cc[3]) << " rect="
                          << g_state.efbSrcX << "," << g_state.efbSrcY << " "
                          << g_state.efbW << "x" << g_state.efbH << std::endl;
              }
            }"""
text = text.replace(anchor, probe, 1)
renderer.write_text(text)
print('Copy-clear probe installed.')
