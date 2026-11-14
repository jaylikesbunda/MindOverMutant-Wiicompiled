"""Qualify copy snapshots against MoM's raw parse-time BP register seam."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path('vendor/NWiiRecomp/nWiiRuntime')
MARK = 'NATIVE_MOM_COPY_REGISTERS_V1'
HEADER = 'include/runtime/native_copy_snapshot_mom.h'
PAIRS = {
    'include/runtime/gx_command.h': [
        ('    uint32_t native_copy_dest = 0, native_copy_stride = 0;\n',
         '''    uint32_t native_copy_dest = 0, native_copy_stride = 0;
    uint32_t native_copy_zmode = 0, native_copy_blend = 0;
    uint32_t native_copy_clear_ar = 0, native_copy_clear_gb = 0;
    uint32_t native_copy_clear_depth = 0; // ''' + MARK + '\n'),
    ],
    'src/hle/gx/fifo_parser.cpp': [
        ('#include "runtime/gx_state.h"\n',
         '#include "runtime/gx_state.h"\n#include "runtime/native_copy_snapshot_mom.h"\n'),
        ('''        c.native_copy_valid = true;
        c.native_copy_src_x = g_state.efbSrcX;
        c.native_copy_src_y = g_state.efbSrcY;
        c.native_copy_w = g_state.efbW;
        c.native_copy_h = g_state.efbH;
        c.native_copy_dest = g_state.efbCopyDest;
        c.native_copy_stride = g_state.efbCopyStride;
''',
         '''        // ''' + MARK + ''': derived fields update only at render time.
        // NOSEED diagnostics bypass the raw parse shadow, so use the existing
        // ordered render-state fallback rather than freeze incomplete data.
        static const bool seeded = std::getenv("NWII_NOSEED") == nullptr;
        c.native_copy_valid = false;
        if (seeded) NativeCaptureCopyRegisters(c, g_state.bp);
'''),
    ],
    'src/hle/gx/renderer_gl.cpp': [
        ('''static void NativeClearEfb() {
  ApplyColorMask();
  NativeRasterDepthMask((g_state.bp[0x40] & 16) ? GL_TRUE : GL_FALSE);
  glClearDepth(double(g_state.bp[0x51] & 0xffffff) / 16777215.0);
  glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
}''',
         '''static void NativeClearEfb(uint32_t zmode, uint32_t blend, uint32_t depth) {
  const GLboolean rgb = (blend & 8) ? GL_TRUE : GL_FALSE;
  NativeRasterColorMask(rgb, rgb, rgb, (blend & 16) ? GL_TRUE : GL_FALSE);
  NativeRasterDepthMask((zmode & 16) ? GL_TRUE : GL_FALSE);
  glClearDepth(double(depth & 0xffffff) / 16777215.0);
  glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
}'''),
        ('            unsigned char cc[4];::GX_GetClearColor(cc);\n',
         '''            unsigned char cc[4];
            if (copy_snap) {
              cc[0] = cmd.native_copy_clear_ar & 255;
              cc[1] = (cmd.native_copy_clear_gb >> 8) & 255;
              cc[2] = cmd.native_copy_clear_gb & 255;
              cc[3] = (cmd.native_copy_clear_ar >> 8) & 255;
            } else ::GX_GetClearColor(cc);
'''),
        ('            NativeEfbProbe("pre_clear");NativeClearEfb();\n',
         '''            NativeEfbProbe("pre_clear");
            NativeClearEfb(copy_snap ? cmd.native_copy_zmode : g_state.bp[0x40],
                           copy_snap ? cmd.native_copy_blend : g_state.bp[0x41],
                           copy_snap ? cmd.native_copy_clear_depth : g_state.bp[0x51]);
'''),
    ],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if args.restore and not any(
            new in (args.root / RUNTIME / relative).read_text(encoding='utf-8')
            for relative, pairs in PAIRS.items() for old, new in pairs):
        print(MARK + ' already pristine')
        return
    prepared = []
    for relative, pairs in PAIRS.items():
        path = args.root / RUNTIME / relative
        original = path.read_text(encoding='utf-8')
        text = original
        for old, new in (reversed(pairs) if args.restore else pairs):
            if not args.restore and new in text:
                continue
            if args.restore and new not in text and old in text:
                continue
            source, target = (new, old) if args.restore else (old, new)
            if text.count(source) != 1:
                raise SystemExit('%s: ambiguous anchor in %s: %r' %
                                 (MARK, relative, source.splitlines()[0]))
            text = text.replace(source, target, 1)
        prepared.append((path, original, text))
    if not args.restore:
        data = (args.root / 'overlays/native_copy_snapshot_mom.h').read_bytes()
        header = args.root / RUNTIME / HEADER
        if not header.is_file() or header.read_bytes() != data:
            header.write_bytes(data)
    for path, original, text in prepared:
        if original != text:
            path.write_text(text, encoding='utf-8', newline='\n')
    print(MARK + (' restored' if args.restore else ' staged'))


if __name__ == '__main__':
    main()
