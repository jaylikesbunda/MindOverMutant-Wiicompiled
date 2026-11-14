"""Apply guest-only halves of development stages to a frozen runtime snapshot."""
from pathlib import Path
import argparse, runpy
ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('stage');args=ap.parse_args()
    module=runpy.run_path(str(ROOT/'tools/stages'/args.stage))
    files=sorted((ROOT/'export').glob('output_*.cpp'))
    if args.stage=='fix_poll_due_mom.py':
        for path in files:module['apply_generated'](path)
    elif args.stage=='fix_guest_memory_lane_mom.py':
        for path in files:
            text=module['read'](path);updated,*_=module['patch_generated'](text)
            if updated!=text:module['write'](path,updated)
    elif args.stage=='fix_savestate_mom.py':
        path=ROOT/'export/main_output.cpp';raw=path.read_bytes().decode('utf-8');crlf='\r\n' in raw
        text=module['generated_apply'](raw.replace('\r\n','\n'))
        path.write_bytes((text.replace('\n','\r\n') if crlf else text).encode())
    elif args.stage=='fix_guest_runtime_trim_mom.py':
        path=ROOT/'export/main_output.cpp';text=module['read'](path)
        pairs=module['MAIN_EDITS']
        # Call helpers may already sit between this declaration and the entry
        # wrapper. The report counter belongs immediately before the wrapper.
        declaration='namespace nwii::runtime { bool native_wpad_dispatch(CPUContext&, uint32_t); }\n'
        pairs=[(a.replace(declaration,''),b.replace(declaration,'')) if '// --- Entry Point Wrapper ---' in a else (a,b) for a,b in pairs]
        text=module['apply_pairs'](text,pairs,'release-main')
        module['write'](path,text)
    else:raise SystemExit('unsupported guest-only stage')
    print(args.stage+': guest-only release transform complete')

if __name__=='__main__':main()
