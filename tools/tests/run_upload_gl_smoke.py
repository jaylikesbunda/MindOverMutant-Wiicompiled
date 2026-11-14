"""Build/run a hidden-context draw regression using the actual renderer bindings.

Uses existing local SDL/GLAD sources and the configured MinGW toolchain only.
This does not start the game or qualify guest lighting/texgen transforms.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def mip_method(text):
    first = text.index('void TextureCache::ensure_mips(')
    opening = text.index('{', first)
    depth = 1
    end = opening + 1
    while depth:
        if text[end] == '{':
            depth += 1
        elif text[end] == '}':
            depth -= 1
        end += 1
    return text[first:end] + '\n'


def main():
    build = ROOT / 'export/build'
    runtime = ROOT / 'export/nWiiRuntime'
    cache = (build / 'CMakeCache.txt').read_text()
    compilers = [line.split('=', 1)[1] for line in cache.splitlines()
                 if line.startswith('CMAKE_CXX_COMPILER:FILEPATH=')]
    if len(compilers) != 1:
        raise RuntimeError('expected one configured C++ compiler')
    compiler = Path(compilers[0])
    out = Path(tempfile.mkdtemp(prefix='mom-upload-gl-'))
    renderer = runtime / 'src/hle/gx/renderer_gl.cpp'
    text = renderer.read_text()
    first = text.index('static bool s_persist_gl_done = false;')
    last = text.index('static void FlushBatch(native_batch_census::Reason reason =', first)
    bindings = text[first:last]
    if 'static void NativeConfigureBatchLayout(' not in bindings:
        raise RuntimeError('renderer layout helper was not captured')
    (out / 'upload_gl_bindings.inc').write_text(bindings, newline='\n')
    texture = runtime / 'src/hle/texture_cache_gl.cpp'
    texture_text = texture.read_text()
    (out / 'mipmap_gl_body.inc').write_text(mip_method(texture_text), newline='\n')
    stage_path = ROOT / 'tools/stages/fix_mipmap_state_trace_mom.py'
    spec = importlib.util.spec_from_file_location('mip_stage', stage_path)
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    legacy = out / 'legacy'
    legacy.mkdir()
    (legacy / 'upload_gl_bindings.inc').write_text(bindings, newline='\n')
    old_text = stage.restore_pairs(texture_text, stage.TEX_PAIRS, str(texture))
    (legacy / 'mipmap_gl_body.inc').write_text(mip_method(old_text), newline='\n')
    exe = out / 'upload_gl_smoke.exe'
    source = ROOT / 'tools/tests/upload_gl_smoke.cpp'
    # glad lives in the configured deps build, not vendored under src/platform:
    # prefer the runtime tree if it is ever installed there, else use deps.
    glad = runtime / 'src/platform/glad'
    if not (glad / 'include/glad/glad.h').is_file():
        glad = build / '_deps/glad-build'
    command = [
        str(compiler), '-std=c++20', '-O2', '-ffp-contract=off', '-static',
        '-I' + str(runtime / 'include'), '-I' + str(glad / 'include'),
        '-I' + str(build / '_deps/sdl2-build/include-config-release'),
        '-I' + str(build / '_deps/sdl2-src/include'), '-I' + str(out),
        str(source), str(runtime / 'src/hle/gx/tev_shader_gen.cpp'), str(glad / 'src/glad.c'),
        str(build / '_deps/sdl2-build/libSDL2.a'),
        '-ldinput8', '-ldxguid', '-luser32', '-lgdi32', '-lwinmm', '-limm32',
        '-lole32', '-loleaut32', '-lshell32', '-lsetupapi', '-lversion',
        '-luuid', '-ladvapi32', '-o', str(exe),
    ]
    env = os.environ.copy()
    env = {k: v for k, v in env.items() if not k.upper().startswith('NWII_')}
    env['PATH'] = str(compiler.parent) + os.pathsep + env.get('PATH', '')
    files = [renderer, texture, source, stage_path,
             ROOT / 'tools/tests/mipmap_gl_fixture.h',
             out / 'mipmap_gl_body.inc', legacy / 'mipmap_gl_body.inc',
             runtime / 'src/hle/gx/tev_shader_gen.cpp',
             runtime / 'include/runtime/native_persistent_upload_mom.h',
             runtime / 'include/runtime/native_packed_vertices_mom.h']
    record = {'command': command,
              'source_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (out / 'command.json').write_text(json.dumps(record, indent=2))
    for mode in ('uniform', 'literal', 'disabled'):
        (out / mode).mkdir()
    legacy_command = [('-I' + str(legacy) if item == '-I' + str(out)
                       else str(legacy / exe.name) if item == str(exe)
                       else item) for item in command]
    jobs = [('compile-legacy', legacy_command, 180, 0),
            ('legacy', [str(legacy / exe.name)], 60, 1),
            ('compile', command, 180, 0),
            ('uniform', [str(exe), str(out / 'uniform')], 60, 0),
            ('literal', [str(exe), str(out / 'literal')], 60, 0),
            ('disabled', [str(exe), str(out / 'disabled')], 60, 0)]
    (out / 'jobs.json').write_text(json.dumps(jobs, indent=2))
    print('artifacts:', out, flush=True)
    for label, cmd, timeout, expected in jobs:
        child = dict(env)
        if label == 'literal':
            child['NWII_LITERAL_SHADER_DATA'] = '1'
        if label == 'disabled':
            child['NWII_NO_MIPMAPS'] = '1'
        result = subprocess.run(cmd, cwd=ROOT, env=child, capture_output=True, timeout=timeout)
        (out / (label + '.stdout')).write_bytes(result.stdout)
        (out / (label + '.stderr')).write_bytes(result.stderr)
        (out / (label + '.exit')).write_text(str(result.returncode))
        print(label, 'exit', result.returncode, 'artifacts:', out)
        sys.stdout.flush()
        if result.returncode != expected:
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
            return result.returncode or 1
        if label == 'legacy' and b'FAIL mipmap: a unit binding changed' not in result.stderr:
            raise RuntimeError('legacy did not fail at the expected binding assertion')
        if not label.startswith('compile'):
            sys.stdout.buffer.write(result.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
