"""Keep EFB alpha out of anything the player sees (ported from the CotT port).

The EFB is RGBA8 here, so TEV alpha from alpha-tested or blended draws reaches
  * EFB-to-texture copies, which are later sampled with that alpha, and
  * the presented XFB blit, where a surface with alpha bits can be composited
    translucently by the desktop.
A menu drawn with alpha 0 therefore disappears. Present the XFB with alpha writes
masked (the back-buffer alpha stays 1 from the clear), and force alpha to 1 in
EFB texture copies when the guest pixel format (BP 0x43 low bits) is not RGBA6.
Same fix as native-port/tools/fix_alpha_isolation.py in the CotT port.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
src = port / "vendor/NWiiRecomp/nWiiRuntime/src"
marker = "NATIVE_ALPHA_ISOLATION_V1"

p = src / "core/main.cpp"
t = p.read_text()
if marker not in t:
    old = "          if(have_present) {\n"
    assert t.count(old) == 1, "present block"
    t = t.replace(old, "          glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_FALSE); // NATIVE_ALPHA_ISOLATION_V1\n" + old, 1)
    p.write_text(t)

p = src / "hle/gx/renderer_gl.cpp"
t = p.read_text()
if marker not in t:
    a = t.index("static void NativeCopyEfbTexture")
    b = t.index("NATIVE_EFB_COPY_GEOMETRY_END")
    seg = t[a:b]
    old = "  glBindFramebuffer(GL_DRAW_FRAMEBUFFER,GLuint(draw));\n  glBindFramebuffer(GL_READ_FRAMEBUFFER,GLuint(read));"
    assert seg.count(old) == 1, "copy restore"
    new = (
        "  if ((g_state.bp[0x43] & 7) != 1) { // NATIVE_ALPHA_ISOLATION_V1: no EFB alpha -> copies read alpha 1\n"
        "    GLboolean saved_mask[4];\n"
        "    glGetBooleanv(GL_COLOR_WRITEMASK, saved_mask);\n"
        "    glColorMask(GL_FALSE, GL_FALSE, GL_FALSE, GL_TRUE);\n"
        "    glClearColor(0.f, 0.f, 0.f, 1.f);\n"
        "    glClear(GL_COLOR_BUFFER_BIT);\n"
        "    glColorMask(saved_mask[0], saved_mask[1], saved_mask[2], saved_mask[3]);\n"
        "  }\n"
    ) + old
    seg = seg.replace(old, new, 1)
    t = t[:a] + seg + t[b:]
    p.write_text(t)
print("EFB alpha isolated from presentation and texture copies.")
