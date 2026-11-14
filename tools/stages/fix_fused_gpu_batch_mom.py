"""Fuse qualified GPU vertex batches into the persistent upload ring.

The path is active only with the existing GPU vertex and persistent upload
opt-ins, and only when GPU texgen is active and packed colours are disabled.
Unsupported or busy rings materialise the same vertices into the old vector.
Run with --restore to remove the stage.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
TREES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', ROOT / 'export/nWiiRuntime']
RESTORE = '--restore' in sys.argv

old_upload = '''  size_t upload(unsigned int target, const void* data, size_t bytes,
                size_t align) {
    if (!ok_ || !data || bytes == 0 || align == 0 || bytes > seg_bytes_)
      return SIZE_MAX;'''
new_upload = '''  // NATIVE_MOM_FUSED_GPU_BATCH_V1: reserve a writable coherent range. The
  // caller fills it before issuing the draw; failed reservations consume none.
  size_t reserve(size_t bytes, size_t align, void** destination) {
    *destination = nullptr;
    if (!ok_ || bytes == 0 || align == 0 || bytes > seg_bytes_)
      return SIZE_MAX;'''
old_tail = '''      return upload(target, data, bytes, align); // fresh segment
    }
    std::memcpy((char*)map_ + aligned, data, bytes);
    cursor_ = rel + bytes;
    (void)target;
    return aligned;
  }'''
new_tail = '''      return reserve(bytes, align, destination); // fresh segment
    }
    *destination = (char*)map_ + aligned;
    cursor_ = rel + bytes;
    return aligned;
  }

  size_t upload(unsigned int target, const void* data, size_t bytes,
                size_t align) {
    if (!data) return SIZE_MAX;
    void* destination = nullptr;
    const size_t offset = reserve(bytes, align, &destination);
    if (offset != SIZE_MAX) std::memcpy(destination, data, bytes);
    (void)target;
    return offset;
  }'''

decl_old = 'static std::vector<BatchVertex> s_batch;'
decl_new = decl_old + '''
// NATIVE_MOM_FUSED_GPU_BATCH_V1: commands retain decoded vertices until Render
// returns, and every open batch flushes before that return.
struct NativeGpuSpan { const VertexData* vertices; size_t count; };
static std::vector<NativeGpuSpan> s_gpu_spans;
static size_t s_gpu_span_count = 0;
static size_t NativeBatchSize() { return s_gpu_span_count ? s_gpu_span_count : s_batch.size(); }
static void NativeFillGpuSpans(void* destination);
static void NativeMaterializeGpuSpans();'''

emit_old = '''  if(!count)return;
  s_batch.reserve(base+count); // NATIVE_MOM_RENDER_EMIT_BULK_V1: one growth per primitive
  for(size_t i=0;i<count;++i) EmitVertex(v[i]);'''
emit_new = '''  if(!count)return;
  static const bool fused = std::getenv("NWII_NO_FUSED_BATCH") == nullptr;
  if (fused && s_gpu_vertex_mode && s_gpu_texgen_lanes && s_persist_wanted &&
      !s_packed_wanted && s_batch.empty()) {
    s_gpu_spans.push_back({v.data(), count});
    s_gpu_span_count += count;
  } else {
    NativeMaterializeGpuSpans();
    s_batch.reserve(base+count); // NATIVE_MOM_RENDER_EMIT_BULK_V1
    for(size_t i=0;i<count;++i) EmitVertex(v[i]);
  }'''
line_old = '''  const uint32_t base=uint32_t(s_batch.size());
  if (cmd.prim_type>=0xA8 && cmd.prim_type<=0xB8) {
    NativeEmitLinesPoints(cmd);'''
line_new = '''  const uint32_t base=uint32_t(NativeBatchSize());
  if (cmd.prim_type>=0xA8 && cmd.prim_type<=0xB8) {
    NativeMaterializeGpuSpans();
    NativeEmitLinesPoints(cmd);'''

fill_old = 'static bool s_batch_open = false;'
fill_new = '''// NATIVE_MOM_FUSED_GPU_BATCH_V1: identical writer and layout on both paths.
static void NativeFillGpuSpans(void* destination) {
  auto* out = static_cast<BatchVertex*>(destination);
  for (const auto& span : s_gpu_spans)
    for (size_t i = 0; i < span.count; ++i, ++out) {
      new (out) BatchVertex(BatchVertex::NoInitTag());
      NativeWriteConvertedVertex(span.vertices[i], *out);
    }
}
static void NativeMaterializeGpuSpans() {
  if (s_gpu_spans.empty()) return;
  s_batch.reserve(s_gpu_span_count);
  for (const auto& span : s_gpu_spans)
    for (size_t i = 0; i < span.count; ++i) EmitVertex(span.vertices[i]);
  s_gpu_spans.clear(); s_gpu_span_count = 0;
}
''' + fill_old

persist_old = '''      const size_t poff = nwii::runtime::g_persist_vertex.upload(
          GL_ARRAY_BUFFER, native_vertex_data, native_vertex_bytes,
          native_vertex_stride);'''
persist_new = '''      size_t poff;
      if (!s_gpu_spans.empty()) {
        void* destination = nullptr;
        poff = nwii::runtime::g_persist_vertex.reserve(
            native_vertex_bytes, native_vertex_stride, &destination);
        if (poff != SIZE_MAX) NativeFillGpuSpans(destination);
      } else {
        poff = nwii::runtime::g_persist_vertex.upload(
            GL_ARRAY_BUFFER, native_vertex_data, native_vertex_bytes,
            native_vertex_stride);
      }'''
fallback_old = '''  } else {
    glBindBuffer(GL_ARRAY_BUFFER, s_vbo); // explicit mutable rebind'''
fallback_new = '''  } else {
    if (!s_gpu_spans.empty()) {
      NativeMaterializeGpuSpans();
      native_vertex_data = s_batch.data();
    }
    glBindBuffer(GL_ARRAY_BUFFER, s_vbo); // explicit mutable rebind'''

def replace(s, old, new, label):
    if RESTORE: old, new = new, old
    if RESTORE and s.count(old) == 1: return s.replace(old, new, 1)
    if new in s: return s
    if s.count(old) != 1: raise ValueError((label, s.count(old)))
    return s.replace(old, new, 1)

def edit(path, fn):
    raw = path.read_bytes().decode('utf-8', 'surrogateescape')
    crlf = '\r\n' in raw
    s = fn(raw.replace('\r\n', '\n'))
    path.write_bytes((s.replace('\n', '\r\n') if crlf else s).encode('utf-8', 'surrogateescape'))

def header(s):
    s = replace(s, old_upload, new_upload, 'reserve')
    return replace(s, old_tail, new_tail, 'reserve tail')

def renderer(s):
    s = replace(s, '#include <vector>\n#include <cmath>', '#include <vector>\n#include <new> // NATIVE_MOM_FUSED_GPU_BATCH_V1\n#include <cmath>', 'new')
    s = replace(s, decl_old, decl_new, 'declarations')
    s = replace(s, fill_old, fill_new, 'span writer')
    s = replace(s, line_old, line_new, 'line fallback')
    s = replace(s, emit_old, emit_new, 'emit')
    s = replace(s, persist_old, persist_new, 'upload')
    s = replace(s, fallback_old, fallback_new, 'fallback')
    start = s.index('static void FlushBatch(')
    end = s.index('static ', start + len('static void FlushBatch('))
    # Find the actual function end using the next top-level function following it.
    end = s.index('\nstatic ', s.index('s_batch.clear();', start))
    body = s[start:end]
    if not RESTORE and 'NATIVE_MOM_FUSED_GPU_BATCH_FLUSH' not in body:
        body = body.replace('s_batch.empty()', '(NativeBatchSize() == 0)').replace('s_batch.size()', 'NativeBatchSize()')
        body = body.replace('  s_batch.clear();', '  s_batch.clear();\n  s_gpu_spans.clear(); s_gpu_span_count = 0; // NATIVE_MOM_FUSED_GPU_BATCH_FLUSH')
    elif RESTORE and 'NATIVE_MOM_FUSED_GPU_BATCH_FLUSH' in body:
        body = body.replace('  s_gpu_spans.clear(); s_gpu_span_count = 0; // NATIVE_MOM_FUSED_GPU_BATCH_FLUSH\n', '')
        body = body.replace('(NativeBatchSize() == 0)', 's_batch.empty()').replace('NativeBatchSize()', 's_batch.size()')
    return s[:start] + body + s[end:]

for path in [ROOT / 'overlays/native_persistent_upload_mom.h'] + [t / 'include/runtime/native_persistent_upload_mom.h' for t in TREES]:
    edit(path, header)
for tree in TREES:
    edit(tree / 'src/hle/gx/renderer_gl.cpp', renderer)
print('fused GPU batch ' + ('removed' if RESTORE else 'installed'))
