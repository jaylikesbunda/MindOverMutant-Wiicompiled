"""Keep original GX SDK setters so FIFO writes and __GXData stay coherent.

CotT executes these guest functions. Native partial-register setters omit guest
cached state, allowing subsequent guest setters/dirty flushes to overwrite them.
"""
from pathlib import Path
port = Path(__file__).resolve().parents[1]
vendor = port / 'vendor/NWiiRecomp'
analyzer = vendor / 'nWiiAnalyzer/src/analyzer.cpp'
recompiler = vendor / 'nWiiRecomp/src/recompiler.cpp'
if not analyzer.exists():
    print('Analyzer absent in runtime-only preflight; nothing to do.')
    raise SystemExit(0)
names = ('GXLoadNrmMtxImm', 'GXLoadPosMtxImm', 'GXSetAlphaCompare',
         'GXSetBlendMode', 'GXSetCullMode', 'GXSetCurrentMtx', 'GXSetDstAlpha',
         'GXSetNumTevStages', 'GXSetScissor', 'GXSetZCompLoc', 'GXSetZMode')
marker = 'NATIVE_MOM_GX_SDK_STATE_V2'
text = analyzer.read_text()
if marker not in text:
    anchor = '    func.hle_hook_name = scanner.match(opcodes);'
    assert text.count(anchor) == 1
    condition = ' ||\n        '.join('func.hle_hook_name == "' + name + '"' for name in names)
    text = text.replace(anchor, anchor + '\n    // ' + marker + ': keep guest SDK cached registers and dirty flags coherent.\n    if (' + condition + ') func.hle_hook_name.clear();', 1)
    analyzer.write_text(text)
text = recompiler.read_text()
if marker not in text:
    for name in names:
        anchor = '      "' + name + '",'
        if name == 'GXSetNumTevStages' and anchor not in text:
            continue  # V1 already removed this entry.
        assert text.count(anchor) == 1, name
        text = text.replace(anchor, '      // ' + marker + ': ' + name + ' retains guest SDK state.', 1)
    recompiler.write_text(text)
print('GX SDK setters retain original translated guest implementations.')

# Extend the existing opt-in trace with the packed GX constant colour.
stubs = vendor / 'nWiiRuntime/src/core/os_hle_stubs.cpp'
text = stubs.read_text()
probe = 'NATIVE_MOM_GX_SDK_COLOR_PROBE_V1'
if probe not in text:
    anchor = 'void trace_call(uint32_t func_addr, CPUContext &ctx) {'
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + r'''
    // NATIVE_MOM_GX_SDK_COLOR_PROBE_V1: reached with NWII_TRACE_CALLS=1.
    if (func_addr == 0x8047C0A4u &&
        (g_trace_only.empty() || g_trace_only.count(func_addr))) {
      std::cout << "[GXKColor] index=" << ctx.gpr[3] << " packed=0x" << std::hex
                << ctx.mmu.read32(ctx.gpr[4]) << " r2=0x" << ctx.gpr[2]
                << " source=0x" << ctx.mmu.read32(ctx.gpr[2]-16960)
                << ",0x" << ctx.mmu.read32(ctx.gpr[2]-16956)
                << ",0x" << ctx.mmu.read32(ctx.gpr[2]-16952) << std::dec << "\n";
    }
''', 1)
    stubs.write_text(text)

renderer = vendor / 'nWiiRuntime/src/hle/gx/renderer_gl.cpp'
text = renderer.read_text()
marker = 'NATIVE_MOM_GX_TEV_WRITE_PROBE_V1'
if marker not in text:
    anchor = 'static void NativeTevRegisterWrite(uint8_t reg, uint32_t val) {'
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + r'''
  // NATIVE_MOM_GX_TEV_WRITE_PROBE_V1: opt-in, bounded stream-state trace.
  if (reg >= 0xe0 && reg <= 0xe7 && std::getenv("NWII_TEVTRACE")) {
    static unsigned n = 0;
    if (n++ < 2048) std::printf("[TEVWrite] reg=%02x value=%06x\n", reg, val);
  }
''', 1)
    renderer.write_text(text)
