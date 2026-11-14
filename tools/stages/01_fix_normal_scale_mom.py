"""Decode integer vertex normals with the hardware fixed-point scale.

Ported from CotT's ``tools/fix_normal_scale.py``.  The CotT stage anchors on
its own ``NativeVectorPlan`` decode plan, which this tree does not have, so the
fix is re-derived against the ``ReadVectorAttr`` path here.

GX normals carry a fixed number of fraction bits that depends only on the
component type (the VAT has no normal shift): U8 7, S8 6, U16 15, S16 14 (float
normals are unscaled).  Dolphin's ``VertexLoader_Normal`` uses the same table
(1/128, 1/64, 1/32768, 1/16384).  The decoder passed shift 0 for every normal,
so an S8 normal of length 1.0 (64) came out as 64.0 and an S16 one as 16384.0.

Lighting never showed it because the generated lighting code normalises the
normal first.  Texture-coordinate generation from normals does not (hardware
does not either), so every environment / sphere map driven by integer normals
got coordinates 64x-16384x too large: with repeat wrap a 32x32 environment map
tiled dozens of times across the surface (speckle) and swam as the object
turned (shimmer).  A flat surface uses a constant normal and therefore a
constant lookup, so it looked right.

Verified by reading the decoder: ``ReadVectorAttr`` scales integer attributes
by ``1 / (1 << shift)`` (``include``-side ``inv``) and ``DecodeScalar`` does the
same for direct attributes; the only normal call site is the one below, and it
passed the literal 0.  The table matches Dolphin's ``VertexLoader_Normal``.
Re-derivation notes: logs/cott-quality-plan.md, area 1, item 01.
"""
from pathlib import Path

port = Path(__file__).resolve().parents[1]
if not (port / 'vendor').is_dir():  # REPO_ROOT_FALLBACK
    port = port.parent
path = port / 'vendor/NWiiRecomp/nWiiRuntime/src/hle/gx/fifo_parser.cpp'
text = path.read_text()

MARK = 'NATIVE_MOM_NORMAL_SCALE_V1'

if MARK not in text:
    anchor = '    bool ReadVectorAttr(const std::vector<uint8_t>& fifo, size_t& fifo_offset, size_t fifo_size,'
    assert text.count(anchor) == 1, 'ReadVectorAttr anchor'
    helper = (
        '    // ' + MARK + ': fraction bits of integer normals (the VAT has no normal shift):\n'
        '    // U8 7, S8 6, U16 15, S16 14, float 0 (Dolphin VertexLoader_Normal).\n'
        '    static inline uint8_t NativeNormalFracBits(VtxAttrType type) {\n'
        '        switch (type) {\n'
        '        case VtxAttrType::U8:  return 7;\n'
        '        case VtxAttrType::S8:  return 6;\n'
        '        case VtxAttrType::U16: return 15;\n'
        '        case VtxAttrType::S16: return 14;\n'
        '        default: return 0;\n'
        '        }\n'
        '    }\n'
        '\n'
    )
    text = text.replace(anchor, helper + anchor, 1)

    # The one normal decode site: give it the hardware scale.
    call = ('            if (!ReadVectorAttr(buf, off, buf_size, r.vat.nrmMask, r.vat.nrmType,\n'
            '                                0, r.arrayBase[1], r.arrayStride[1],')
    assert text.count(call) == 1, 'normal decode site'
    text = text.replace(
        call,
        '            if (!ReadVectorAttr(buf, off, buf_size, r.vat.nrmMask, r.vat.nrmType,\n'
        '                                NativeNormalFracBits(r.vat.nrmType), // ' + MARK + '\n'
        '                                r.arrayBase[1], r.arrayStride[1],', 1)
    path.write_text(text)

print('Integer vertex normals are decoded with the hardware fixed-point scale.')
