"""EFB copies ignore the guest's draw colour mask.

GX copy-to-XFB and copy-to-texture read the EFB; they are not draws. The
runtime implemented both with glBlitFramebuffer, which honours glColorMask.
Guest code routinely leaves colour writes disabled (BP 0x41 cmode0 bits 3-4),
so the copy wrote nothing: the XFB keeps its old contents, and screenshots of
the VI-selected surface stayed black while the EFB held a full frame.

Measured on MoM (NWII_XFB_PROBE): 174 of 238 XFB copies ran with mask 0000 and
the destination kept its earlier content; the copy source was fully lit.
Wrap both blits in an all-channels colour mask and restore the guest mask after.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
path = port / "vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/renderer_gl.cpp"
text = path.read_text()

if "NATIVE_EFB_COPY_MASK_V1" not in text:
    blits = (
        ("NativeCopyEfbTexture",
         "  glBlitFramebuffer(x*scale,(kNativeEfbHeight-y)*scale,(x+width)*scale,(kNativeEfbHeight-y-height)*scale,\n"
         "                    0,0,width,height,GL_COLOR_BUFFER_BIT,GL_NEAREST);\n"),
        ("NativeLatchXfb",
         "  glBlitFramebuffer(x*scale,(kNativeEfbHeight-y-height)*scale,(x+width)*scale,(kNativeEfbHeight-y)*scale,\n"
         "                    0,0,output_width,output_height,GL_COLOR_BUFFER_BIT,GL_NEAREST);\n"),
    )
    for name, blit in blits:
        assert text.count(blit) == 1, name
        wrapped = (
            "  // NATIVE_EFB_COPY_MASK_V1: copies read the EFB and ignore the guest colour mask.\n"
            "  GLboolean copy_mask[4];glGetBooleanv(GL_COLOR_WRITEMASK,copy_mask);\n"
            "  glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_TRUE);\n"
            + blit +
            "  glColorMask(copy_mask[0],copy_mask[1],copy_mask[2],copy_mask[3]);\n"
        )
        text = text.replace(blit, wrapped, 1)
    path.write_text(text)
print("EFB copies to texture and XFB run with an all-channel colour mask.")
