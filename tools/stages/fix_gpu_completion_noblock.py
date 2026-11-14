"""Do not block the GX thread on glFinish at every PE finish and GPU token (ported from the CotT port).

NativeGpuCompletion called glFinish() for each BP finish (0x45) and each token
(0x47/0x48). Those stalls serialise the CPU thread and the GPU, and a driver
stall there can freeze the host loop while the guest waits on GXDrawDone.
glFlush submits the work without waiting. NWII_GPU_FINISH=1 restores the old
blocking behaviour.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
path = port / "vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/renderer_gl.cpp"
text = path.read_text()
marker = "NATIVE_GPU_COMPLETION_NOBLOCK_V1"
if marker not in text:
    old = "    if(have_gl)glFinish();"
    assert text.count(old) == 2
    new = ('    if(have_gl){ static const bool block = std::getenv("NWII_GPU_FINISH") != nullptr; '
           'if(block) glFinish(); else glFlush(); } // NATIVE_GPU_COMPLETION_NOBLOCK_V1')
    text = text.replace(old, new)
    path.write_text(text)
print("PE finish/token no longer block on glFinish (NWII_GPU_FINISH=1 restores).")
