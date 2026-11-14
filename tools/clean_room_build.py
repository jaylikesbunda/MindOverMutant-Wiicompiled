"""Build MoM from a verified user DOL in an isolated, reproducible workspace.

The launcher and CLI use this same entry point. No sibling CotT/decomp checkout
or pre-generated game source is needed. --generate-only validates generation
and guest staging without compiling the final executable.
"""
from pathlib import Path
import argparse, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile

ROOT=Path(__file__).resolve().parents[1]
PIN='595f176f1d24cc54ff2e8389feed12d7fb553cc2'
POSTGEN=[
 'fix_guest_compile_flags.py','fix_cache_range_loops_mom.py',
 'fix_inst_count_batch_mom.py','fix_dispatcher_o2_mom.py',
 'fix_dispatch_hot_cache_mom.py','fix_poll_due_mom.py',
 'fix_guest_memory_lane_mom.py','fix_guest_calls_mom.py',
 'fix_guest_runtime_trim_mom.py','fix_guest_frame_gate_mom.py',
 'fix_savestate_mom.py','fix_indirect_site_cache_mom.py',
 'fix_codegen_slim_mom.py','fix_sqrt_fast_mom.py',
 'fix_dispatch_poll_gate_mom.py','fix_dispatch_inline_mom.py',
 'fix_ctx_restrict_mom.py',
]

def progress(percent, phase):
    print(f'@progress|{int(percent)}|{phase}',flush=True)

def run(args,cwd,env,phase=None,span=None):
    if span:progress(span[0],phase)
    print('+ '+' '.join(map(str,args)),flush=True)
    with subprocess.Popen(list(map(str,args)),cwd=cwd,env=env,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace',bufsize=1,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as child:
        last=span[0] if span else 0
        for line in child.stdout:
            print(line,end='',flush=True)
            match=re.match(r'^\[(\d+)/(\d+)\]',line)
            if span and match and int(match[2]):
                percent=min(span[1],span[0]+int((span[1]-span[0])*int(match[1])/int(match[2])))
                if percent>last:progress(percent,phase);last=percent
        code=child.wait()
    if code:raise subprocess.CalledProcessError(code,args)
    if span:progress(span[1],phase)

def upstream(dest):
    vendor=ROOT/'vendor/NWiiRecomp'
    if (vendor/'.git').exists():
        data=subprocess.check_output(['git','archive',PIN],cwd=vendor)
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(dest,filter='data')
    elif (vendor/'PINNED_COMMIT').read_text().strip()==PIN:
        shutil.copytree(vendor,dest)
    else:raise SystemExit('missing pinned NWiiRecomp source; see README.md')

def copy_runtime_dlls(build,env):
    compiler=shutil.which('g++',path=env.get('PATH'))
    if not compiler:raise SystemExit('g++ is not on the builder PATH')
    for dll in ('libgcc_s_seh-1.dll','libstdc++-6.dll','libwinpthread-1.dll'):
        # WinLibs keeps its runtime DLLs beside g++. GCC's library search does
        # not search bin/, and -print-file-name otherwise returns a bare name.
        source=Path(compiler).resolve().parent/dll
        if not source.is_file():
            source=Path(subprocess.check_output(['g++','-print-file-name='+dll],env=env,text=True).strip())
        if not source.is_file():raise SystemExit('could not locate runtime dependency '+dll)
        shutil.copy2(source,build/dll)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dest',type=Path,required=True)
    ap.add_argument('--dol',type=Path,required=True)
    ap.add_argument('--index',type=Path) # accepted for compatibility with older launcher builds
    ap.add_argument('--jobs',type=int,default=5)
    ap.add_argument('--deps',type=Path)
    ap.add_argument('--resume',action='store_true')
    ap.add_argument('--reuse-deps',action='store_true')
    ap.add_argument('--generate-only',action='store_true')
    ap.add_argument('--expected-builder-revision')
    args=ap.parse_args()
    if args.jobs<1:ap.error('--jobs must be positive')
    progress(1,'Verifying builder and disc')
    import builder_revision
    identity=builder_revision.identity(ROOT)
    if args.expected_builder_revision and args.expected_builder_revision!=identity['revision']:
        raise SystemExit('launcher and builder revision differ')
    builder_revision.validate(ROOT)
    dol=args.dol.resolve();dol_sha=hashlib.sha256(dol.read_bytes()).hexdigest()
    expected=(ROOT/'data/main.dol.sha256').read_text().strip()
    if dol_sha!=expected:raise SystemExit('unsupported DOL: use the verified RC8P7D release')
    dest=args.dest.resolve();work=dest/'source';src=work/'vendor/NWiiRecomp'
    env=os.environ.copy();env['NWII_INPUT_DOL']=str(dol);env['PYTHONUTF8']='1'
    digest=builder_revision.digest(identity)
    marker=dest/'source-identity.json'
    wanted={'dol_sha256':dol_sha,'builder_inputs':digest}
    if marker.exists() and json.loads(marker.read_text())!=wanted:
        raise SystemExit('workspace belongs to different inputs; choose a new --dest')
    if not marker.exists():
        if work.exists():raise SystemExit('unidentified workspace already exists; choose a new --dest')
        work.mkdir(parents=True)
        for folder in ('tools','overlays','reference'):
            shutil.copytree(ROOT/folder,work/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*-before*','*_baseline.*','user'))
        upstream(src)
        from source_patch import apply
        print(f'Applied {apply(src, ROOT/"patches/mom-release-runtime.patch")} source files',flush=True)
        if not (src/'nWiiRuntime/include/runtime/native_gpu_vertex_mom.h').is_file():
            raise SystemExit('runtime patch did not install its required files')
        game=work/'dump/sys';game.mkdir(parents=True);shutil.copy2(dol,game/'main.dol')
        config=(ROOT/'recomp_config.toml').read_text()
        (work/'recomp_config.toml').write_text(config,encoding='utf-8')
        marker.write_text(json.dumps(wanted))
    deps=['-DGLAD_REPRODUCIBLE=ON',f'-DPython_EXECUTABLE={sys.executable}',f'-DPYTHON_EXECUTABLE={sys.executable}']
    if args.deps:
        for name,var in [('tomlplusplus-src','TOMLPLUSPLUS'),('sdl2-src','SDL2'),('glad-src','GLAD')]:
            path=(args.deps/name).resolve()
            if not path.is_dir():raise SystemExit('missing dependency source: '+str(path))
            deps.append(f'-DFETCHCONTENT_SOURCE_DIR_{var}={path}')
    compiler=work/'build/nwiirecomp'
    run(['cmake','-S',src,'-B',compiler,'-G','Ninja','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_POLICY_VERSION_MINIMUM=3.5',*deps],work,env,'Configuring the recompiler',(3,8))
    run(['cmake','--build',compiler,'--target','nwiirecomp','--parallel',args.jobs],work,env,'Building the recompiler',(8,25))
    generated=work/'export'
    if not (generated/'.mom-staged').exists():
        run([compiler/'nWiiRecomp/nwiirecomp.exe',work/'recomp_config.toml'],work,env,'Translating your game',(25,40))
        run([sys.executable,work/'tools/gap_functions.py'],work,env,'Translating remaining functions',(40,48))
        for number,stage in enumerate(POSTGEN):
            progress(48+int(number*12/len(POSTGEN)),'Applying game fixes')
            if stage in ('fix_poll_due_mom.py','fix_guest_memory_lane_mom.py','fix_savestate_mom.py','fix_guest_runtime_trim_mom.py'):
                run([sys.executable,work/'tools/release_postgen.py',stage],work,env)
            else:run([sys.executable,work/'tools/stages'/stage],work,env)
        (generated/'.mom-staged').write_text(digest)
    progress(60,'Guest source ready')
    if args.generate_only:
        print('Generation and guest staging complete: '+str(generated),flush=True);return
    build=dest/'storage/toolchain'
    run(['cmake','-S',generated,'-B',build,'-G','Ninja','-DCMAKE_BUILD_TYPE=Release',*deps],work,env,'Configuring the native game',(60,63))
    run(['cmake','--build',build,'--target','MindOverMutant','--parallel',args.jobs],work,env,'Compiling the native game',(63,98))
    progress(99,'Preparing the game to launch')
    exe=build/'MindOverMutant.exe';target=build/'CrashNative.exe';shutil.copy2(exe,target)
    # Runtime DLLs travel with the locally built game; the launcher needs no toolchain on PATH.
    copy_runtime_dlls(build,env)
    info=dict(builder_revision=identity['revision'],builder_inputs=digest,dol_sha256=dol_sha,
              executable_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    (build/'runtime-build.json').write_text(json.dumps(info,indent=2))
    progress(100,'Ready to play')
    print('Runtime ready: '+str(target),flush=True)

if __name__=='__main__':main()
