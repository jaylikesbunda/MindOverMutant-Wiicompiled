"""Used-texture-slot bind filter (MoM).

Marker: NATIVE_MOM_USED_TEXTURE_MASK_V1.

GenerateTEVShader emits texture()/textureSize()/textureGrad() only for the
TEV stage's primary texMap and the indirect maps its config reaches.  Binding
all 8 slots every draw does cache lookups + mip checks + sampler params +
protect() for samplers no shader reads.  Install
overlays/native_used_texture_mask_mom.h and filter the bind loop + the XF
boundary check with that exact conservative mask; NWII_ALL_TEXTURE_SLOTS=1
restores the old all-slots path.

Idempotent; --restore removes its own insertions (header left inert).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_USED_TEXTURE_MASK_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'
HDR = RUNTIME + '/include/runtime/native_used_texture_mask_mom.h'

INCLUDE_LINE = '#include "runtime/native_used_texture_mask_mom.h" // ' + MARKER + '\n'

PAIRS = [
    ('#include "runtime/native_persistent_upload_mom.h" // NATIVE_MOM_PERSISTENT_UPLOAD_V1\n',
     '#include "runtime/native_persistent_upload_mom.h" // NATIVE_MOM_PERSISTENT_UPLOAD_V1\n'
     + INCLUDE_LINE),

    # bind loop: compute the used mask once, skip unused slots before any cache
    # lookup / mip work / sampler / protect.
    ('''        if (!s_batch_open && g_ctx_ptr) {
          for (int i = 0; i < 8; ++i) {''',
     '''        if (!s_batch_open && g_ctx_ptr) {
          // ''' + MARKER + ''': only bind samplers the generated shader reads
          // (NWII_ALL_TEXTURE_SLOTS=1 restores the all-slots path).
          static const bool all_slots =
              std::getenv("NWII_ALL_TEXTURE_SLOTS") != nullptr;
          const unsigned used =
              all_slots ? 0xff : NativeUsedTextureMask(g_state);
          for (int i = 0; i < 8; ++i) {
            if (!(used & (1u << i))) continue; // ''' + MARKER + ''''''),

    # XF boundary: only a shader-USED configured texture blocks continuation.
    ('''  // This runtime has no texture-used mask, so XF state that a textured draw
  // would consume stays a boundary.
  for (unsigned i = 0; i < 8; ++i) {
    const auto &stage = g_state.texStages[i];
    if (stage.base_addr != 0 && stage.width > 0 && stage.height > 0)
      return false;
  }''',
     '''  // ''' + MARKER + ''': only a configured texture the shader actually
  // references keeps the XF continuation boundary - an unused slot's state
  // change cannot affect any draw.
  static const bool all_slots_xf =
      std::getenv("NWII_ALL_TEXTURE_SLOTS") != nullptr;
  const unsigned used_xf =
      all_slots_xf ? 0xff : NativeUsedTextureMask(g_state);
  for (unsigned i = 0; i < 8; ++i) {
    const auto &stage = g_state.texStages[i];
    if ((used_xf >> i & 1) && stage.base_addr != 0 &&
        stage.width > 0 && stage.height > 0)
      return false;
  }'''),
]


def read_write(path, text, orig):
    data = text.replace('\n', '\r\n') if '\r\n' in orig else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as h:
        h.write(data)


def apply_pairs(text, tag):
    for old, new in PAIRS:
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:70]))
        text = text.replace(old, new, 1)
    return text


def restore_pairs(text, tag):
    for old, new in reversed(PAIRS):
        found = text.count(new)
        if found == 0:
            continue
        if found != 1:
            raise SystemExit('%s: %s restore matched %d times' % (MARKER, tag, found))
        text = text.replace(new, old, 1)
    return text


def _strip_include(text):
    return '\n'.join(ln for ln in text.split('\n')
                     if ln != INCLUDE_LINE.rstrip('\n'))


def apply(root):
    data = (root / 'overlays/native_used_texture_mask_mom.h').read_bytes()
    gl = root / GL
    orig = gl.read_bytes().decode('utf-8', errors='surrogateescape')
    text = orig.replace('\r\n', '\n')
    if INCLUDE_LINE.strip() in text:
        text = _strip_include(text)
    updated = apply_pairs(restore_pairs(text, GL), GL)
    if updated != orig:
        read_write(gl, updated, orig)
    hdr = root / HDR
    if not hdr.is_file() or hdr.read_bytes() != data:
        hdr.parent.mkdir(parents=True, exist_ok=True)
        hdr.write_bytes(data)
    print('%s staged: used-slot bind filter (NWII_ALL_TEXTURE_SLOTS=1 wins)'
          % MARKER)


def restore(root):
    gl = root / GL
    if gl.is_file():
        orig = gl.read_bytes().decode('utf-8', errors='surrogateescape')
        text = _strip_include(orig.replace('\r\n', '\n'))
        updated = restore_pairs(text, GL)
        if MARKER in updated:
            raise SystemExit('%s: restore left owned markers (not writing)' % MARKER)
        if updated != orig:
            read_write(gl, updated, orig)
    print('%s restored: %s (installed header left inert)' % (MARKER, GL))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
