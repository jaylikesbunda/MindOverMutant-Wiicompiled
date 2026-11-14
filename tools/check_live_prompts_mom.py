"""Check matching, palette invalidation and family selection without game data or GL."""
from pathlib import Path
import argparse,os,shutil,subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--compiler',default='g++');args=ap.parse_args()
    compiler=shutil.which(args.compiler)
    if not compiler:raise SystemExit('C++ compiler not found: '+args.compiler)
    with tempfile.TemporaryDirectory(prefix='mom-glyph-tests-') as folder:
        work=Path(folder);include=work/'runtime';include.mkdir()
        shutil.copy2(ROOT/'overlays/input_map_cott/native_prompt_device.h',include/'native_prompt_device.h')
        exe=work/'glyph-tests.exe';pack=work/'pack.bin'
        subprocess.run([compiler,'-std=c++17','-O2','-UNDEBUG','-I'+str(work),'-I'+str(ROOT/'overlays'),
                        str(ROOT/'tools/tests/prompt_textures_mom.cpp'),'-o',str(exe)],check=True)
        data=bytearray(b'MMPRMT01')
        for i in range(8):
            data.extend(bytes(v for y in range(32) for x in range(32) for v in (60+i*16+y,101+x,180-y,145)))
            for family in range(3):data.extend(bytes(v for y in range(32) for x in range(32) for v in (80+family*20+i+y,90,100,255)))
        pack.write_bytes(data)
        env=os.environ.copy();env['NWII_PROMPT_PACK']=str(pack)
        env['PATH']=str(Path(compiler).parent)+os.pathsep+env.get('PATH','')
        subprocess.run([str(exe),str(pack)],env=env,check=True)
    print('MoM glyph tests passed: all 8 actions, 3 families, Wii fallback, RGB5A3/flipped rows, exact rejection and malformed updates')
if __name__=='__main__':main()
