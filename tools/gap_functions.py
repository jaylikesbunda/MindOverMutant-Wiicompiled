#!/usr/bin/env python3
"""Recompile the small code ranges between recompiler function extents instead of
letting the runtime's fallback interpreter execute them.

Mind over Mutant port of the Crash of the Titans stage
(`native-port/tools/fix_gap_functions.py`). g_func_bounds only covers the extents
the exporter admitted; the small 4-aligned ranges between consecutive extents are
leaf/stub functions it missed, and the runtime interprets them one instruction at
a time (the boot log's 200 `[Interp] Interpreting ...` lines). This stage turns
every gap of at most MAX_GAP (0x400) bytes into a real function using the same
standalone emitter the full port uses, appends the results as output_gap_*.cpp,
declares them in functions.h and merges their extents into g_func_bounds in
address order. Interior entries use the standard pc switch; data inside a gap is
never executed. Original extents, polls and every existing chunk are untouched.

Target-tree adaptations relative to the CotT original (same conventions as
tools/apply_stages.py):
  * the generated dir is ``export/`` (not ``build/crash-generated``)
  * the staged runtime copy is ``export/nWiiRuntime`` (not
    ``build/nwii-full-source/nWiiRuntime``)
  * the emitter is built with the existing recompiler build tree
    ``build/nwiirecomp`` (target ``game_emit`` ->
    ``build/nwiirecomp/nWiiRecomp/game_emit.exe``, same layout as nwiirecomp.exe)
  * the input DOL is ``dump/sys/main.dol`` (this tree has no ``orig/main.dol``)
  * the executable target in the generated CMakeLists is ``MindOverMutant``
  * the CotT-only ``runtime-overlays/large_block_cache.cpp`` compile-options entry
    and the ``port_paths.default_storage`` import are dropped
  * g_func_bounds rows here may carry SDK names (``__fill_mem_800061D0``), so the
    bound-row regex accepts any C identifier instead of only ``func_``

The stage is marker-guarded with NATIVE_GAP_FUNCTIONS_V1 and is idempotent: a
second run repairs fall-throughs and re-applies the CMake edits if needed, but
does not re-emit. Run ``python tools/gap_functions.py --dry-run`` to print the gap
inventory (count, byte total, largest gaps) without building the emitter or
modifying any file.
"""
from pathlib import Path
import argparse,re,subprocess,shutil,os,sys
from stages.fix_fallthrough import repair_fallthrough

# Repo root: this file lives in <root>/tools/, so parents[1] is <root>.
PORT=Path(__file__).resolve().parents[1]
GEN=PORT/'export';MAIN=GEN/'main_output.cpp'
RUNTIME=GEN/'nWiiRuntime'
TOOL=PORT/'build/nwiirecomp';EMITTER_TARGET='game_emit'
EMITTER_EXE=TOOL/'nWiiRecomp/game_emit.exe'
DOL=PORT/'dump/sys/main.dol'
CMAKELISTS=GEN/'CMakeLists.txt';GAME_TARGET='MindOverMutant'
WORK=PORT/'build/gap-emit'
MARKER='NATIVE_GAP_FUNCTIONS_V1'
MAX_GAP=0x400
# This tree names some extents after SDK symbols (__fill_mem_...), so accept any
# C identifier where the CotT table only ever held func_<addr>.
BOUND_ROW=re.compile(r'\s*\{0x([0-9A-Fa-f]+), 0x([0-9A-Fa-f]+), ([A-Za-z_]\w*)\},')


def validate_gap_emission(text):
    """Reject stale instruction lowering before any later optimization can hide it.

    A successful process exit, or an executable left by an earlier build, does
    not establish that the emitter contains the staged semantic repairs.
    This check applies to fresh, unoptimized emitter output only.
    """
    instructions=list(re.finditer(r'^    // 0x([0-9A-F]{8}): ([0-9A-F]{8})\n',text,re.M))
    for i,match in enumerate(instructions):
        pc,encoded=match.groups();word=int(encoded,16)
        op=word>>26;xo=(word>>1)&1023;fd=(word>>21)&31
        body=text[match.end():instructions[i+1].start() if i+1<len(instructions) else len(text)]
        uses_fpu=(op==4 and xo!=1014) or 48<=op<=57 or op in (59,60,61,63) or (
            op==31 and xo in (535,567,599,631,663,695,727,759,983))
        if uses_fpu and f'ctx.require_fpu(0x{pc})' not in body:
            raise ValueError(f'Stale gap emitter at 0x{pc}: missing MSR.FP exception check')
        if re.search(r'// (?:fadds|fsubs|fmuls|fdivs|fsqrts|fres|fmadds|fmsubs|fnmadds|fnmsubs|frsp)\b',body):
            if not re.search(r'ctx\.fpr\['+str(fd)+r'\]\s*=\s*ctx\.ps1\['+str(fd)+r'\]\s*=',body):
                raise ValueError(f'Stale gap emitter at 0x{pc}: scalar-single result must update both lanes')
        if (op in (8,12,13) or (op==31 and xo in (792,824))) and 'ctx.xer' not in body:
            raise ValueError(f'Stale gap emitter at 0x{pc}: missing carry update')
        if op in (12,13) and '(uint64_t)(uint32_t)' not in body:
            raise ValueError(f'Stale gap emitter at 0x{pc}: addic carry requires a 32-bit immediate')
    return len(instructions)


def text_ranges():
    """Text section extents from the DOL header.

    Gaps are only code if they sit inside one text section. This game's DOL has
    two text sections with the data sections between them, so the range spanning
    that boundary looks like a gap but is data; the emitter rejects it
    ("Unmapped function extent"), so it is filtered here.
    """
    import struct
    path = Path(os.environ.get('NWII_INPUT_DOL', str(DOL)))
    data = path.read_bytes()
    ranges = []
    for i in range(7):
        size = struct.unpack_from('>I', data, 0x90 + i * 4)[0]
        if not size:
            continue
        addr = struct.unpack_from('>I', data, 0x48 + i * 4)[0]
        ranges.append((addr, addr + size))
    return ranges


def read_bounds(text):
    """Parse g_func_bounds and return the table slice and its <=MAX_GAP gaps."""
    a=text.index('g_func_bounds[] = {');b=text.index('    {0, 0, nullptr}\n};',a)
    table=text[a:b];head=table[:table.index('\n')+1]
    lines=[l for l in table.splitlines()[1:] if l.strip()]
    rows=[(int(m[1],16),int(m[2],16),l) for l in lines for m in [BOUND_ROW.match(l)] if m]
    assert len(rows)==len(lines),(len(rows),len(lines))
    assert all(rows[i][0]<rows[i+1][0] for i in range(len(rows)-1))
    gaps=[(rows[i][1],rows[i+1][0]) for i in range(len(rows)-1) if 0<rows[i+1][0]-rows[i][1]<=MAX_GAP and (rows[i+1][0]-rows[i][1])%4==0 and rows[i][1]%4==0]
    # Two filters, both learned from failed runs:
    #  * g_func_bounds is not the complete emitted set: functions found by the
    #    signature matcher (this game's four OSReport aliases) are emitted but
    #    absent from the table, so they look like gaps and collide at link time
    #    ("multiple definition of func_..."). functions.h is the truth.
    #  * a gap must sit inside one text section; this DOL has two with data
    #    between them, and that range is not code (the emitter rejects it).
    emitted=set()
    declarations=GEN/'functions.h'
    if declarations.exists():
        emitted={int(m,16) for m in re.findall(r'func_([0-9A-Fa-f]{8})\b',declarations.read_text(errors='replace'))}
    if emitted:
        kept=[g for g in gaps if g[0] not in emitted]
        if len(kept)!=len(gaps):
            print(f'skipped {len(gaps)-len(kept)} gap(s) already emitted as real functions in functions.h')
        gaps=kept
    if DOL.exists():
        ranges=text_ranges()
        kept=[g for g in gaps if any(lo<=g[0] and g[1]<=hi for lo,hi in ranges)]
        if len(kept)!=len(gaps):
            print(f'skipped {len(gaps)-len(kept)} gap(s) that lie outside a single text section')
        gaps=kept
    return a,b,head,rows,gaps


def report_gaps(gaps):
    total=sum(y-x for x,y in gaps)
    print(f'function extents parsed; gap inventory: {len(gaps)} gaps, {total} bytes (0x{total:X}), each <= 0x{MAX_GAP:X} and 4-aligned')
    largest=sorted(gaps,key=lambda g:g[1]-g[0],reverse=True)[:20]
    print(f'largest gaps ({len(largest)} of {len(gaps)}):')
    for x,y in largest:
        print(f'  0x{x:08X}-0x{y:08X}  {y-x:4d} bytes (0x{y-x:X})')


def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run',action='store_true',help='print the gap inventory without building the emitter or editing files')
    args=ap.parse_args()

    s=MAIN.read_text();applied=MARKER in s
    if not applied:a,b,head,rows,gaps=read_bounds(s)
    if args.dry_run:
        if applied:print(f'{MARKER} already present in {MAIN}; nothing to do.');return
        report_gaps(gaps);return

    if applied:
        print(f'{MARKER} already present in {MAIN}; skipping emission and merge.')
    else:
        if not DOL.exists():raise SystemExit(f'Input DOL not found: {DOL}')
        if not RUNTIME.is_dir():raise SystemExit(f'Runtime source not found: {RUNTIME}')
        shutil.rmtree(WORK,ignore_errors=True);(WORK/'out').mkdir(parents=True)
        (WORK/'gaps.tsv').write_text('address\tsize\tname\tfile\n'+''.join(f'{x:08x}\t0x{y-x:x}\tGAP_{x:08x}\tgap_{x:08x}\n' for x,y in gaps))
        r=subprocess.run(['cmake','--build',str(TOOL),'--target',EMITTER_TARGET,'-j','8'],capture_output=True,text=True)
        print(r.stdout[-300:])
        if r.returncode:
            raise SystemExit('Gap emitter rebuild failed; refusing to use a stale executable.\n'+r.stdout[-4000:]+r.stderr[-4000:])
        r=subprocess.run([str(EMITTER_EXE),os.environ.get('NWII_INPUT_DOL',str(DOL)),str(WORK/'gaps.tsv'),str(RUNTIME),str(WORK/'out')],capture_output=True,text=True)
        print(r.stdout[-400:]);assert r.returncode==0,r.stderr[-500:]
        outputs=sorted((WORK/'out').glob('output_*.cpp'));assert outputs
        for i,path in enumerate(outputs):
            text=path.read_text();assert text.count('#include "functions.h"')==1
            validate_gap_emission(text)
            text,_=repair_fallthrough(text)
            (GEN/f'output_gap_{i}.cpp').write_text(text)
        decl=re.findall(r'^void func_[0-9A-F]+\(nwii::runtime::CPUContext& ctx\);$',(WORK/'out/functions.h').read_text(),re.M)
        assert len(decl)==len(gaps),(len(decl),len(gaps))
        fh=GEN/'functions.h';t=fh.read_text()
        missing=[d for d in decl if d not in t];fh.write_text(t.rstrip('\n')+'\n// '+MARKER+'\n'+'\n'.join(missing)+'\n')
        extra=[(x,y,f'    {{0x{x:08X}, 0x{y:08X}, func_{x:08X}}},') for x,y in gaps]
        merged=sorted(list(rows)+extra,key=lambda r_:r_[0])
        s=s[:a]+head+'    // '+MARKER+f': {len(gaps)} gap functions merged\n'+'\n'.join(l for _,_,l in merged)+'\n'+s[b:]
        MAIN.write_text(s)

    # Also repair already-generated gaps when resuming an older staged tree.
    files=sorted(GEN.glob('output_gap_*.cpp'));repaired=0
    for path in files:
        text=path.read_text();updated,count=repair_fallthrough(text)
        if updated!=text:path.write_text(updated)
        repaired+=count
    t=CMAKELISTS.read_text()
    if files and MARKER not in t:
        start=t.index('add_executable('+GAME_TARGET);end=t.index(')',start)
        add=''.join(f'    "{f.name}"\n' for f in files)
        t=t[:end]+add+t[end:]
        t+='\n# '+MARKER+'\nset_source_files_properties('+' '.join(f'"{f.name}"' for f in files)+'\n    PROPERTIES COMPILE_OPTIONS "-O2;-march=x86-64-v3;-mfma;-fwrapv;-fno-strict-aliasing;-ffp-contract=off")\n'
        CMAKELISTS.write_text(t)
    print(f'Gap functions merged; {repaired} existing boundary fall-throughs repaired.')


if __name__=='__main__':main()
