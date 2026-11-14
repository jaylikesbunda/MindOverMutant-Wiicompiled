"""Move program-binary file writes to a bounded background worker (MoM).

Marker: NATIVE_MOM_SHADER_CACHE_IO_V1.

MoM's NativeProgramCache defers each ~25 KB binary write to a per-Render
``flush_pending()`` call - bounded, but still on the render thread.  This
installs CotT's fix_shader_cache_io mechanism on MoM's cache:

* the GL queries (link status, binary length, glGetProgramBinary) stay on the
  GL thread exactly where they were;
* the owned header+bytes are handed to ``native_shader_cache::Writer``: a
  worker thread, a bounded queue (128 jobs / 8 MiB total / 8 MiB per record),
  atomic temp+rename writes that never touch an existing destination, and
  atomic counters (stores on success, skipped, failed).  A full queue skips
  the save - it never blocks a frame.  Worker start failure skips caching,
  never the game.
* ``NWII_NO_SHADER_ASYNC_STORE=1`` restores the synchronous write_blob path.
* The writer drains and joins only at destruction (shutdown) or an explicit
  ``flush()`` - never at the end of Render.
* load() gains the missing trailing-payload check: the file must be exactly
  24-byte header + declared length (magic/key/length checks unchanged;
  "NWPBC001" format unchanged - no migration).

Idempotent and reversible via exact inverse pairs (--restore).
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_SHADER_CACHE_IO_V1'

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'vendor').is_dir():
    ROOT = ROOT.parent

RUNTIME = 'vendor/NWiiRecomp/nWiiRuntime'
GL = RUNTIME + '/src/hle/gx/renderer_gl.cpp'
OVERLAY = 'overlays/native_shader_cache_io.h'
HEADER = RUNTIME + '/include/runtime/native_shader_cache_io.h'

GL_PAIRS = [
    ('''#include "runtime/gx/tev_shader_gen.h"
''',
     '''#include "runtime/gx/tev_shader_gen.h"
#include "runtime/native_shader_cache_io.h" // ''' + MARKER + '''
'''),
    ('''  std::filesystem::path directory;std::string salt;uint64_t hits=0,stores=0,rejected=0;
''',
     '''  std::filesystem::path directory;std::string salt;uint64_t hits=0,rejected=0;
  std::atomic<uint64_t> stores{0};native_shader_cache::Writer writer; // ''' + MARKER + '''
'''),
    ('''  struct PendingWrite {std::filesystem::path target;std::vector<char> blob;};
  std::vector<PendingWrite> pending;
  size_t pending_bytes=0;
  static constexpr size_t kPendingCap=32u<<20; // ~1,300 binaries, then drain early
''',
     '''  // ''' + MARKER + ''': bounded writer-thread queue replaces the
  // per-Render pending vector.  flush() remains for tests and shutdown joins.
'''),
    ('''  // End of the Render pass and teardown.  No GL, so it is safe with no
  // context current; every caller already has the program's bytes.
  void flush_pending() {
    if(pending.empty())return;
    for(const auto &write : pending) if(write_blob(write.target,write.blob))++stores;
    pending.clear();pending_bytes=0;
  }
  ~NativeProgramCache(){
    flush_pending();
''',
     '''  // ''' + MARKER + ''': explicit flush for tests; the writer also drains
  // and joins in its own destructor at shutdown.  Never called per Render.
  void flush_pending() { writer.flush(); }
  ~NativeProgramCache(){
    writer.flush();
'''),
    ('''    Header header{};in.read(reinterpret_cast<char*>(&header),sizeof header);
    if(!in || std::memcmp(header.magic,"NWPBC001",8) || header.key!=value || !header.length || header.length>(64u<<20)){++rejected;return 0;}
    std::vector<char> data(header.length);in.read(data.data(),std::streamsize(data.size()));if(!in){++rejected;return 0;}
''',
     '''    Header header{};in.read(reinterpret_cast<char*>(&header),sizeof header);
    if(!in || std::memcmp(header.magic,"NWPBC001",8) || header.key!=value || !header.length || header.length>(64u<<20)){++rejected;return 0;}
    {std::error_code se;const auto fs=std::filesystem::file_size(path(value),se); // ''' + MARKER + ''': reject trailing payload too
     if(se || fs!=sizeof(Header)+header.length){++rejected;return 0;}}
    std::vector<char> data(header.length);in.read(data.data(),std::streamsize(data.size()));if(!in){++rejected;return 0;}
'''),
    ('''    Header header{};std::memcpy(header.magic,"NWPBC001",8);header.key=value;header.format=uint32_t(format);header.length=uint32_t(written);
    // NATIVE_MOM_SHADER_LANE_V1: hand the finished record to the frame-end writer.
    std::vector<char> blob(sizeof header+size_t(written));
    std::memcpy(blob.data(),&header,sizeof header);
    std::memcpy(blob.data()+sizeof header,data.data(),size_t(written));
    static const bool async_store=std::getenv("NWII_NO_SHADER_ASYNC_STORE")==nullptr;
    if(async_store){
      if(pending_bytes+blob.size()>kPendingCap)flush_pending();
      pending_bytes+=blob.size();
      pending.resize(pending.size()+1);
      pending.back().target=path(value);
      pending.back().blob.swap(blob);
      return;
    }
    if(write_blob(path(value),blob))++stores;
''',
     '''    Header header{};std::memcpy(header.magic,"NWPBC001",8);header.key=value;header.format=uint32_t(format);header.length=uint32_t(written);
    // ''' + MARKER + ''': GL extraction above stays on this thread; only the
    // owned bytes go to the worker.  A full queue skips the save (counted).
    static const bool async_store=std::getenv("NWII_NO_SHADER_ASYNC_STORE")==nullptr;
    if(async_store){
      data.resize(size_t(written));
      writer.enqueue(path(value),&header,std::move(data),stores);
      return;
    }
    std::vector<char> blob(sizeof header+size_t(written));
    std::memcpy(blob.data(),&header,sizeof header);
    std::memcpy(blob.data()+sizeof header,data.data(),size_t(written));
    if(write_blob(path(value),blob))++stores;
'''),
    ('''    // NATIVE_MOM_SHADER_LANE_V1: the batch is closed, so this is outside every draw.
    NativeProgramBinaryCache().flush_pending();
''',
     '''    // ''' + MARKER + ''': the writer thread owns the disk work now; nothing
    // to drain here (it flushes on test request and joins at shutdown).
'''),
]

GL_PAIRS += [
    ('''  bool write_blob(const std::filesystem::path &target,const std::vector<char> &blob) {
    auto temporary=target;temporary+=".tmp";
    {std::ofstream out(temporary,std::ios::binary|std::ios::trunc);if(!out)return false;
     out.write(blob.data(),std::streamsize(blob.size()));if(!out)return false;}
    std::error_code error;std::filesystem::rename(temporary,target,error);
    if(error){std::filesystem::remove(temporary,error);return false;}
    return true;
  }
''',
     '''  bool write_blob(const std::filesystem::path &target,const std::vector<char> &blob) {
    return native_shader_cache::Writer::write_blob(target, blob);
  }
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
    changed = []
    for rel, pairs in ((GL, GL_PAIRS),):
        path = root / rel
        if not path.is_file():
            raise SystemExit('%s: %s is missing' % (MARKER, rel))
        text = read(path)
        if MARKER in text and '~NativeProgramCache(){\n    writer.flush();' not in text:
            text = text.replace('~NativeProgramCache(){\n', '~NativeProgramCache(){\n    writer.flush();\n', 1)
        text = text.replace('#include "runtime/native_shader_cache_io.h" // ' + MARKER + chr(10), '')
        updated = apply_pairs(restore_pairs(text, pairs, rel), pairs, rel)
        if updated != text:
            write(path, updated)
            changed.append(rel)
    print('%s staged: %d file(s), header %s; NWII_NO_SHADER_ASYNC_STORE=1 '
          'restores synchronous writes' % (MARKER, len(changed),
                                           'installed' if wrote else 'present'))


def restore(root):
    path = root / GL
    changed = []
    if path.is_file():
        orig = read(path)
        text = orig.replace('#include "runtime/native_shader_cache_io.h" // ' + MARKER + chr(10), '')
        if MARKER in text and '~NativeProgramCache(){\n    writer.flush();' not in text:
            text = text.replace('~NativeProgramCache(){\n', '~NativeProgramCache(){\n    writer.flush();\n', 1)
        updated = restore_pairs(text, GL_PAIRS, GL)
        if updated != orig:
            write(path, updated)
            changed.append(GL)
    print('%s restored: %s (the header stays; it is inert once nothing uses it)'
          % (MARKER, ', '.join(changed) or 'already pristine'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    (restore if args.restore else apply)(args.root)


if __name__ == '__main__':
    main()
