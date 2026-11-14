"""Keep EFB alpha out of anything the player sees.

Ported from CotT's ``tools/fix_alpha_isolation.py``, re-derived against this
tree's raster path.

The console EFB is normally RGB8: alpha written by draws is not stored, copies
to RGB formats read alpha as 1, and the XFB has no alpha at all.  Here the EFB
is RGBA8, so TEV alpha from alpha-tested / blended objects (leaves, fruit,
characters) leaked into

  * EFB-to-texture copies, which are later sampled with that alpha, and
  * the window back buffer via the XFB blit, where a surface with alpha bits
    can be composited translucently by the desktop.

Two changes:

  * the present blit into the window back buffer runs with the alpha write
    disabled, so the back buffer keeps the alpha 1 the clear gave it;
  * an EFB copy whose guest pixel format (BP 0x43 low bits) is not RGBA6 forces
    the copied texture's alpha to 1 after the blit, exactly as hardware reads it.

The colour mask is changed with plain ``glColorMask`` and restored inside the
same block, so the renderer's own mask shadow (which the surrounding code
keeps) is unaffected: the shadow still holds the guest's mask when the
temporary change is undone.

Verified by reading both call sites (``src/core/main.cpp`` present block and
``src/hle/gx/renderer_gl.cpp`` ``NativeCopyEfbTexture``); the clear is issued
while the scissor test is disabled in the copy path, so it covers the whole
copied rectangle, and the clear colour is saved and restored so the guest's
EFB clear colour is unchanged.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
MARK = 'NATIVE_MOM_ALPHA_ISOLATION_V1'

# ---- presentation: do not write alpha into the window back buffer ----------
main_cpp = port / 'vendor/NWiiRecomp/nWiiRuntime/src/core/main.cpp'
text = main_cpp.read_text()
if MARK not in text:
    old = ('            glBindFramebuffer(GL_READ_FRAMEBUFFER,present_fbo);\n'
           '            glBindFramebuffer(GL_DRAW_FRAMEBUFFER,0);\n'
           '            glBlitFramebuffer(0,0,present_w,present_h,0,0,draw_w,draw_h,GL_COLOR_BUFFER_BIT,GL_LINEAR);\n')
    assert text.count(old) == 1, 'present blit anchor'
    new = ('            glBindFramebuffer(GL_READ_FRAMEBUFFER,present_fbo);\n'
           '            glBindFramebuffer(GL_DRAW_FRAMEBUFFER,0);\n'
           '            // ' + MARK + ': the back buffer keeps the alpha 1 the clear gave it.\n'
           '            glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_FALSE);\n'
           '            glBlitFramebuffer(0,0,present_w,present_h,0,0,draw_w,draw_h,GL_COLOR_BUFFER_BIT,GL_LINEAR);\n'
           '            glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_TRUE);\n')
    text = text.replace(old, new, 1)
    main_cpp.write_text(text)

# ---- EFB to texture copy: a format without alpha reads alpha 1 --------------
renderer = port / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
if MARK not in text:
    old = ('  glBlitFramebuffer(x*scale,(kNativeEfbHeight-y)*scale,(x+width)*scale,(kNativeEfbHeight-y-height)*scale,\n'
           '                    0,0,width,height,GL_COLOR_BUFFER_BIT,GL_NEAREST);\n'
           '  NativeRasterColorMask(copy_mask[0],copy_mask[1],copy_mask[2],copy_mask[3]);\n')
    assert text.count(old) == 1, 'EFB copy blit anchor'
    new = ('  glBlitFramebuffer(x*scale,(kNativeEfbHeight-y)*scale,(x+width)*scale,(kNativeEfbHeight-y-height)*scale,\n'
           '                    0,0,width,height,GL_COLOR_BUFFER_BIT,GL_NEAREST);\n'
           '  if ((g_state.bp[0x43] & 7) != 1) { // ' + MARK + ': no EFB alpha -> copies read alpha 1\n'
           '    GLboolean saved_mask[4];glGetBooleanv(GL_COLOR_WRITEMASK,saved_mask);\n'
           '    GLfloat saved_clear[4];glGetFloatv(GL_COLOR_CLEAR_VALUE,saved_clear);\n'
           '    glColorMask(GL_FALSE,GL_FALSE,GL_FALSE,GL_TRUE);\n'
           '    glClearColor(0.f,0.f,0.f,1.f);\n'
           '    glClear(GL_COLOR_BUFFER_BIT);\n'
           '    glClearColor(saved_clear[0],saved_clear[1],saved_clear[2],saved_clear[3]);\n'
           '    glColorMask(saved_mask[0],saved_mask[1],saved_mask[2],saved_mask[3]);\n'
           '  }\n'
           '  NativeRasterColorMask(copy_mask[0],copy_mask[1],copy_mask[2],copy_mask[3]);\n')
    text = text.replace(old, new, 1)
    renderer.write_text(text)

print('EFB alpha stays out of the presented frame and out of EFB texture copies.')
