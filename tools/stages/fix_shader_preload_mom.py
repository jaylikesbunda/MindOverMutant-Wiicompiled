"""NATIVE_MOM_SHADER_PRELOAD_V1 - read cached program binaries off the render
thread and link them a few at a time (MoM port of CotT's fix_shader_preload.py).

* native_shader_preload::Loader (runtime-overlays/native_shader_preload.h)
  scans the program cache newest-first on an I/O worker, validating magic,
  file-name==key and exact length; it holds owned bytes only and never calls
  GL.  Bounds: 256 files / 64 MiB queued; a failed scan means on-demand loads.
* NativeShaderPreloadPump() runs once per Render pass on the render thread -
  the only GL work there is one glProgramBinary + link query per entry, and
  the budget is checked BETWEEN calls (2 ms default via
  NWII_SHADER_PRELOAD_BUDGET_US; a >=20 ms idle gap allows
  NWII_SHADER_PRELOAD_SLACK_US, default 6000, 0 disables slack).  At most 128
  linked programs wait in the ready pool.
* CompileShader claims a ready program by the same salted key; an absent or
  driver-rejected binary falls back to today's load/compile path unchanged.
* NWII_NO_SHADER_PRELOAD=1 disables the feature; NWII_NO_PROGRAM_CACHE=1
  disables the cache it reads.

Idempotent and reversible via exact inverse pairs (--restore).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_SHADER_PRELOAD_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'
OVERLAY = 'overlays/native_shader_preload.h'
HEADER = RUNTIME + '/include/runtime/native_shader_preload.h'

PUMP = '''
// ''' + MARKER + ''': see tools/stages/fix_shader_preload_mom.py.  Only the
// render thread calls GL here; the loader thread owns bytes only.
namespace {
struct NativeShaderPreloadState {
  native_shader_preload::Loader loader;
  std::unordered_map<uint64_t,GLuint> ready; // linked ahead of first use; CompileShader claims them by key
  bool started=false;
  std::chrono::steady_clock::time_point last_pump{};
  uint64_t created=0,claimed=0,rejected=0,slack_pumps=0;
};
NativeShaderPreloadState& NativeShaderPreload(){static NativeShaderPreloadState state;return state;}
}
static constexpr size_t kNativeShaderPreloadReadyMax=128;
static void NativeShaderPreloadPump(){
  static const bool enabled=std::getenv("NWII_NO_SHADER_PRELOAD")==nullptr;
  if(!enabled)return;
  auto& cache=NativeProgramBinaryCache();
  if(!cache.enabled)return;
  auto& state=NativeShaderPreload();
  if(!state.started){state.started=true;state.loader.start(cache.directory);}
  static const long budget_us=[]{if(const char* e=std::getenv("NWII_SHADER_PRELOAD_BUDGET_US")){const long v=std::strtol(e,nullptr,10);if(v>0)return v;}return 2000L;}();
  // Slack: 20 ms or more between submissions means the render thread is idle,
  // so this pump may take a larger budget while binaries are still queued.
  static const long slack_us=[]{if(const char* e=std::getenv("NWII_SHADER_PRELOAD_SLACK_US")){const long v=std::strtol(e,nullptr,10);if(v>=0)return v;}return 6000L;}();
  const auto began=std::chrono::steady_clock::now();
  const bool slack=state.last_pump.time_since_epoch().count()!=0 && began-state.last_pump>=std::chrono::milliseconds(20) && !state.loader.drained();
  state.last_pump=began;
  const auto limit=std::chrono::microseconds(slack && slack_us>budget_us ? slack_us : budget_us);
  if(slack && slack_us>budget_us)++state.slack_pumps;
  native_shader_preload::Entry entry;
  while(state.ready.size()<kNativeShaderPreloadReadyMax && state.loader.take(entry)){
    native_trace::Scope trace_bind("gx.shader_preload_bind"); // one glProgramBinary + link query per entry
    while(glGetError()!=GL_NO_ERROR){}
    const GLuint program=glCreateProgram();
    cache.set_binary(program,GLenum(entry.format),entry.data.data(),GLsizei(entry.data.size()));
    GLint linked=0;glGetProgramiv(program,GL_LINK_STATUS,&linked);
    if(!linked || glGetError()!=GL_NO_ERROR){glDeleteProgram(program);++state.rejected;}
    else if(!state.ready.emplace(entry.key,program).second){glDeleteProgram(program);}
    else ++state.created;
    if(std::chrono::steady_clock::now()-began>=limit)break; // budget checked BETWEEN calls
  }
}
static GLuint NativeShaderPreloadTake(uint64_t key){
  auto& state=NativeShaderPreload();
  if(state.ready.empty())return 0;
  const auto it=state.ready.find(key);
  if(it==state.ready.end())return 0;
  const GLuint program=it->second;
  state.ready.erase(it);++state.claimed;
  return program;
}
'''

GL_PAIRS = [
    ('''#include "runtime/native_shader_cache_io.h" // NATIVE_MOM_SHADER_CACHE_IO_V1
''',
     '''#include "runtime/native_shader_cache_io.h" // NATIVE_MOM_SHADER_CACHE_IO_V1
#include "runtime/native_shader_preload.h" // ''' + MARKER + '''
'''),
    ('''NativeProgramCache& NativeProgramBinaryCache() {static NativeProgramCache cache;return cache;}
}
''',
     '''NativeProgramCache& NativeProgramBinaryCache() {static NativeProgramCache cache;return cache;}
}
''' + PUMP),
    ('''    cache_key=cache.key(vs,fs);
''',
     '''    cache_key=cache.key(vs,fs);
    if(const GLuint preloaded=NativeShaderPreloadTake(cache_key)){native_trace::mark("shader.preload_hit");if(dedupe_exact){s_program_by_source[source_key]=preloaded;s_program_src_text[source_key]=std::string(vs)+char(31)+fs;}++g_stat_programs;return preloaded;} // ''' + MARKER + ''': same key as on-demand
'''),
    ('''    s_compiles_this_frame = 0; // reset the per-frame shader compile budget
''',
     '''    s_compiles_this_frame = 0; // reset the per-frame shader compile budget
    NativeShaderPreloadPump(); // ''' + MARKER + ''': bounded binary loads between submissions
'''),
]


def read(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline='') as h:
        return h.read().replace('\r\n', '\n')


def write(path, text):
    with open(path, 'rb') as h:
        crlf = b'\r\n' in h.read(4096)
    data = text.replace('\n', '\r\n') if crlf else text
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as h:
        h.write(data)


def apply_pairs(text, pairs, tag):
    for old, new in pairs:
        if new in text:
            continue
        found = text.count(old)
        if found != 1:
            raise SystemExit('%s: %s anchor matched %d times (expected 1): %r'
                             % (MARKER, tag, found, old.splitlines()[0][:70]))
        text = text.replace(old, new, 1)
    return text


def restore_pairs(text, pairs, tag):
    for old, new in reversed(pairs):
        found = text.count(new)
        if found == 0:
            continue
        if found != 1:
            raise SystemExit('%s: %s restore matched %d times (expected 1)'
                             % (MARKER, tag, found))
        text = text.replace(new, old, 1)
    return text


def apply(root):
    data = (root / OVERLAY).read_bytes()
    target = root / HEADER
    wrote = False
    if not target.exists() or target.read_bytes() != data:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        wrote = True
    path = root / GL
    if not path.is_file():
        raise SystemExit('%s: %s is missing' % (MARKER, GL))
    text = read(path)
    # Remove this stage's old and current take site before re-inserting it.
    # Earlier revisions emitted different spellings of the source identity.
    text = '\n'.join(line for line in text.split('\n') if not
                    line.lstrip().startswith('if(const GLuint preloaded=NativeShaderPreloadTake(cache_key))'))
    text = text.replace('#include "runtime/native_shader_preload.h" // ' + MARKER + chr(10), '')
    updated = apply_pairs(restore_pairs(text, GL_PAIRS, GL), GL_PAIRS, GL)
    if updated != text:
        write(path, updated)
    print('%s staged: renderer %s, header %s; NWII_NO_SHADER_PRELOAD=1 disables'
          % (MARKER, 'rewritten' if updated != text else 'unchanged',
             'installed' if wrote else 'present'))


def restore(root):
    path = root / GL
    changed = False
    if path.is_file():
        orig = read(path)
        text = orig.replace('#include "runtime/native_shader_preload.h" // ' + MARKER + chr(10), '')
        text = '\n'.join(line for line in text.split('\n') if not
                        line.lstrip().startswith('if(const GLuint preloaded=NativeShaderPreloadTake(cache_key))'))
        updated = restore_pairs(text, GL_PAIRS, GL)
        if updated != orig:
            write(path, updated)
            changed = True
    print('%s restored: %s (the header stays; it is inert once nothing uses it)'
          % (MARKER, 'renderer_gl.cpp' if changed else 'already pristine'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
