"""NATIVE_GATHER_REDIRECT_CACHE_V1: classify the write-gather destination once per configuration.

native_gather_redirect_span runs on every WGPIPE write that is not on the CPU-private
ordinary-FIFO fast path (1.25% of the guest thread, 0.22 ms/frame in capture
cpu-running-20261014-112801-308000). Every call re-derived the same three-way decision
from the PI FIFO base/end registers, the CP FIFO base register and the launch-fixed
flags, through two out-of-line calls (pi_fifo_get, cp_fifo_base_reg) plus several
branches, and only the write pointer actually changes between calls.

This stage caches the decision as three states -- Ordinary (the byte path is not this
function's business), Redirect (scalar store into guest RAM) and ByteFallback (the
original per-byte path) -- together with the mirrored configuration the decision
depends on. The write pointer itself is still read fresh on every call, and the
"would a scalar write straddle `end`" test is still evaluated per call, so every byte
boundary keeps its original path.

Invalidation is explicit at the only writers of that configuration:
  * native_wpar_write                (WPAR redirect state)
  * hw_pi.cpp 0xCC00300C/0xCC003010  (PI FIFO base/end)
  * hw_cp.cpp 0xCC000020/0xCC000024  (CP FIFO base/end)
plus g_watch_addr, which is read at classification time; MoM has no per-span watch
helper, so an armed watch is re-checked on every call and forces the byte path, the
same rule the pre-existing native_gather_redirect_span applied.

MoM adaptation of the CotT stage:
  * CotT's `nwii::runtime::native_watch_span_may_hit(phys, bytes)` (introduced by its
    fix_watch_span_guards stage, absent here) becomes a g_watch_addr test on the call.
  * CotT's `native_gather_redirect_probe` flag is a function-local getenv here, matching
    the pre-existing MoM body.
  * MoM has no NATIVE_FIFO_OK_FLAG_V1, so only the raw writer forms are accepted.

Build rollback: NWII_BUILD_GATHER_CACHE=0. Runtime: NWII_NO_GATHER_CACHE=1.
Idempotent.
"""
from pathlib import Path
import argparse
import os

MARK = 'NATIVE_GATHER_REDIRECT_CACHE_V1'

SIG = 'static inline int native_gather_redirect_span(uint64_t value, unsigned bytes) {'

HELPER = '''// NATIVE_GATHER_REDIRECT_CACHE_V1: the redirect decision as three states plus the
// mirrored configuration it depends on. Rebuilt by native_gather_classify() whenever a
// writer of that configuration invalidates it; see tools/stages/fix_gather_redirect_cache.py.
enum : int { kGatherUnknown = 0, kGatherOrdinary = 1, kGatherRedirect = 2, kGatherFallback = 3 };
struct NativeGatherState {
    int mode = kGatherUnknown;
    bool wpar = false;      // destination is the WPAR pointer: no base/end, 30-bit mask
    bool wrap = false;      // base==0 && end>=0x04000000: the byte path never wraps
    uint32_t base = 0, end = 0;
    uint32_t mask = 0x1fffffffu, end_mask = 0x03ffffffu;
};
static NativeGatherState g_gather_state;
void native_gather_invalidate() { g_gather_state.mode = kGatherUnknown; } // declared in runtime/hw/hw.h
// Exactly the original decision, evaluated once per configuration change.
static void native_gather_classify() {
    NativeGatherState& st = g_gather_state;
    st.wpar = g_wpar_redirect;
    st.wrap = false;
    st.mask = 0x1fffffffu;
    st.end_mask = 0x03ffffffu;
    st.base = nwii::runtime::hw::pi_fifo_base;
    st.end = nwii::runtime::hw::pi_fifo_end;
    if (st.wpar) { st.mask = 0x3fffffffu; st.mode = kGatherRedirect; return; }
    if (st.base == 0 && st.end < 0x04000000u) {
        st.mode = nwii::runtime::g_watch_addr ? kGatherFallback : kGatherOrdinary;
        return;
    }
    if (st.base != 0 && (st.base & 0x03ffffffu) == (nwii::runtime::hw::g_cp_fifo_base & 0x03ffffffu)) {
        st.mode = nwii::runtime::g_watch_addr ? kGatherFallback : kGatherOrdinary;
        return;
    }
    st.wrap = (st.base == 0 && st.end >= 0x04000000u);
    st.mode = kGatherRedirect;
}
'''

BODY = '''static inline int native_gather_redirect_span(uint64_t value, unsigned bytes) {
    static const bool probe=std::getenv("NWII_GX_REDIRECT_PROBE")!=nullptr;
    if(probe)return -1;
    auto* mmu=nwii::runtime::g_mmu;
    if(!mmu)return nwii::runtime::g_watch_addr?-1:0;
    // NATIVE_GATHER_REDIRECT_CACHE_V1: an armed watch keeps the byte path.
    if(nwii::runtime::g_watch_addr)return -1;
    static const bool no_cache=std::getenv("NWII_NO_GATHER_CACHE")!=nullptr;
    if(__builtin_expect(no_cache || g_gather_state.mode==kGatherUnknown,0))native_gather_classify();
    const NativeGatherState& st=g_gather_state;
    if(st.mode==kGatherOrdinary)return 0;
    if(st.mode==kGatherFallback)return -1;
    const uint32_t ptr=st.wpar?g_wpar_cur:nwii::runtime::hw::pi_fifo_wptr;
    // The byte path can wrap inside a scalar write; keep that path intact.
    if(!st.wpar && !st.wrap &&
       uint64_t(ptr&st.end_mask)+bytes-1>uint64_t(st.end&st.end_mask))return -1;
    const uint32_t phys=ptr&st.mask;
    uint8_t* dst=nullptr;
    if(phys<0x01800000u && uint64_t(phys)+bytes<=0x01800000u)dst=mmu->mem1.data()+phys;
    else if(phys>=0x10000000u && uint64_t(phys)+bytes<=0x14000000u)dst=mmu->mem2.data()+(phys-0x10000000u);
    if(!dst)return -1; // includes RAM-end crossing and invalid bus addresses
    const uint64_t be=__builtin_bswap64(value<<(8*(8-bytes)));
    std::memcpy(dst,&be,bytes);
    uint32_t next=ptr+bytes;
    if(st.wpar){g_wpar_cur=next;g_wpar_fill=(g_wpar_fill+bytes)&31u;}
    else {
        if(!st.wrap && (next&st.end_mask)>(st.end&st.end_mask))next=st.base;
        nwii::runtime::hw::pi_fifo_wptr=next;
    }
    return 1;
}
'''

HW_DECL_OLD = """namespace nwii::runtime {
uint64_t get_os_time();
"""
HW_DECL_NEW = """namespace nwii::runtime {
uint64_t get_os_time();
void native_gather_invalidate(); // NATIVE_GATHER_REDIRECT_CACHE_V1
"""
HW_FIFO_OLD = """void pi_fifo_get(uint32_t &base, uint32_t &end, uint32_t &wptr);
void pi_fifo_set_wptr(uint32_t wptr);"""
HW_FIFO_NEW = """void pi_fifo_get(uint32_t &base, uint32_t &end, uint32_t &wptr);
void pi_fifo_set_wptr(uint32_t wptr);
// NATIVE_GATHER_REDIRECT_CACHE_V1: read directly on the gather path.
extern uint32_t pi_fifo_base, pi_fifo_end, pi_fifo_wptr;
extern uint32_t g_cp_fifo_base;"""

PI_BASE_RAW = 'if (addr == 0xCC00300C) pi_fifo_base = val;'
PI_END_RAW = 'if (addr == 0xCC003010) pi_fifo_end = val;'
PI_INVALIDATE = ' nwii::runtime::native_gather_invalidate();'  # NATIVE_GATHER_REDIRECT_CACHE_V1

PI_WPTR_OLD = 'static uint32_t pi_fifo_wptr = 0;'
PI_WPTR_NEW = ('uint32_t pi_fifo_wptr = 0; // NATIVE_GATHER_REDIRECT_CACHE_V1: read by gx.cpp')

CP_BASE_RAW = 'write_reg32(addr, val, g_cp_fifo_base); }'
CP_END_RAW = 'write_reg32(addr, val, g_cp_fifo_end); }'
CP_INVALIDATE = ' nwii::runtime::native_gather_invalidate();'  # NATIVE_GATHER_REDIRECT_CACHE_V1

WPAR_RAW = '    g_wpar_redirect = (a & 0x3FFFFFFFu) != 0x0C008000u && (a & 0x3FFFFFFFu) != 0u;\n}'
WPAR_INVALIDATE = '    native_gather_invalidate(); // NATIVE_GATHER_REDIRECT_CACHE_V1\n'


def _insert_before_close(text, raw, invalidate, tag):
    """Insert the invalidate statement into the writer's own block.

    `raw` anchors either a brace-less single statement (PI base/end) or a block that
    already ends in the writer's closing brace (CP base/end, WPAR writer). Appending a
    statement after a `}` would close the enclosing function.
    """
    assert text.count(raw) == 1, tag
    if raw.endswith('}'):
        return text.replace(raw, raw[:-1] + invalidate + '}', 1)
    return text.replace(raw, raw + invalidate, 1)


def _patch(path, edits, tag):
    text = path.read_text(errors='surrogateescape')
    if MARK in text:
        return False
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r' % (MARK, tag, n, old[:70]))
        text = text.replace(old, new, 1)
    path.write_text(text, errors='surrogateescape')
    return True


def apply(port):
    rt = port / 'build/nwii-full-source/nWiiRuntime'
    changed = []
    if _patch(rt / 'include/runtime/hw/hw.h', [(HW_DECL_OLD, HW_DECL_NEW), (HW_FIFO_OLD, HW_FIFO_NEW)], 'hw.h'):
        changed.append('hw.h')
    hw_pi = rt / 'src/hw/hw_pi.cpp'
    text = hw_pi.read_text(errors='surrogateescape')
    if MARK not in text and 'nwii::runtime::native_gather_invalidate' not in text:
        assert text.count(PI_WPTR_OLD) == 1, 'pi_fifo_wptr definition anchor'
        text = text.replace(PI_WPTR_OLD, PI_WPTR_NEW, 1)
        text = _insert_before_close(text, PI_BASE_RAW, PI_INVALIDATE, 'pi_fifo_base writer')
        text = _insert_before_close(text, PI_END_RAW, PI_INVALIDATE, 'pi_fifo_end writer')
        hw_pi.write_text(text, errors='surrogateescape')
        changed.append('hw_pi.cpp')

    hw_cp = rt / 'src/hw/hw_cp.cpp'
    text = hw_cp.read_text(errors='surrogateescape')
    # hw_cp.cpp carries no marker text of this stage, so the emitted call itself is the
    # applied-probe: without it a second pass re-runs the anchors against the already
    # rewritten writers and asserts.
    if MARK not in text and 'nwii::runtime::native_gather_invalidate' not in text:
        text = _insert_before_close(text, CP_BASE_RAW, CP_INVALIDATE, 'g_cp_fifo_base writer')
        text = _insert_before_close(text, CP_END_RAW, CP_INVALIDATE, 'g_cp_fifo_end writer')
        hw_cp.write_text(text, errors='surrogateescape')
        changed.append('hw_cp.cpp')

    gx = rt / 'src/hle/gx.cpp'
    text = gx.read_text(errors='surrogateescape')
    if MARK not in text:
        text = _insert_before_close(text, WPAR_RAW, WPAR_INVALIDATE, 'native_wpar_write')
        a = text.index(SIG)
        b = text.index('\n}\n', a) + 3
        text = text[:a] + HELPER + BODY + text[b:]
        gx.write_text(text, errors='surrogateescape')
        changed.append('gx.cpp')
    print('%s staged (%s)' % (MARK, ','.join(changed) or 'no change'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args()
    if os.environ.get('NWII_BUILD_GATHER_CACHE', '1') == '0':
        print('gather redirect cache disabled')
        return
    apply(args.port)


if __name__ == '__main__':
    main()
