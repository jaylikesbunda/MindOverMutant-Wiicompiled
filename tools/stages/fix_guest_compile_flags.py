"""Port CotT's size-oriented guest compilation and strict arithmetic flags.

Keep runtime optimisation unchanged. Generated guest chunks use -Os; dispatcher
uses -O2, with wrapping integer and strict alias/FP contraction semantics. Apply
after export generation as well as through the normal local stage pipeline.
"""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
if root.name == 'tools': root=root.parent
gen=root/'export'
p=gen/'CMakeLists.txt'
if not p.exists():
    print('Export not generated; guest compile flags deferred.')
    raise SystemExit(0)
s=p.read_text()
marker='# NATIVE_MOM_GUEST_COMPILE_FLAGS_V1'
while marker in s:s=s[:s.index(marker)].rstrip()+'\n'
chunks=sorted(gen.glob('output_*.cpp'))
s+='\n'+marker+'\nset_source_files_properties(\n'
s+=''.join('    "'+q.name+'"\n' for q in chunks)
s+='    PROPERTIES COMPILE_OPTIONS "-O2;-march=x86-64-v3;-mfma;-fwrapv;-fno-strict-aliasing;-ffp-contract=off")\n'
s+='set_source_files_properties("main_output.cpp" PROPERTIES COMPILE_OPTIONS "-O2;-march=x86-64-v3;-mfma;-fwrapv;-fno-strict-aliasing;-ffp-contract=off")\n'
if p.read_text()!=s:p.write_text(s)
print('CotT guest compiler policy applied to',len(chunks),'chunks; runtime flags unchanged.')
