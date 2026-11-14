"""CotT XF uniform streaming, GPU input packing, and buffer-bind shadows.

Runtime-only; preserves generated guest code and its PGO profile. Backups and
hashes allow --restore only if no subsequent edits would be overwritten.
"""
from pathlib import Path
import hashlib, json, sys

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT/'logs/stage-backups/fix_gpu_stream_pack_mom'
MARK = 'NATIVE_MOM_GPU_STREAM_PACK_V1'
FILES = [ROOT/t/'src/hle/gx/renderer_gl.cpp' for t in
         ('vendor/NWiiRecomp/nWiiRuntime', 'export/nWiiRuntime')]

def transform(s):
    def part(a,b):
        nonlocal s
        assert s.count(a)==1, (a[:90],s.count(a))
        s=s.replace(a,b,1)
    part('static void NativeFillGpuSpans(void* destination);',
         'static void NativeFillGpuSpans(void* destination, size_t stride);')
    part('static GLuint s_native_bound_vao = 0;', '''static GLuint s_native_bound_vao = 0;
// NATIVE_MOM_GPU_STREAM_PACK_V1: reset at each renderer entry; all buffer binds
// in this renderer, including persistent-ring callbacks, pass through here.
static GLuint s_native_array_buffer = ~GLuint(0);
static GLuint s_native_element_buffer = ~GLuint(0), s_native_element_vao = ~GLuint(0);
static void NativeBindBuffer(GLenum target, GLuint buffer) {
  static const bool enabled = std::getenv("NWII_NO_GL_STATE_REDUNDANCY") == nullptr;
  if (target == GL_ARRAY_BUFFER) {
    if (enabled && s_native_array_buffer == buffer) return;
    s_native_array_buffer = buffer;
  } else if (target == GL_ELEMENT_ARRAY_BUFFER) {
    if (enabled && s_native_element_buffer == buffer && s_native_element_vao == s_native_bound_vao) return;
    s_native_element_buffer = buffer; s_native_element_vao = s_native_bound_vao;
  }
  glBindBuffer(target, buffer);
}''')
    # Route literal and function-table binds; leave the helper's GL call alone.
    s=s.replace('glBindBuffer(GL_ARRAY_BUFFER,', 'NativeBindBuffer(GL_ARRAY_BUFFER,')
    s=s.replace('glBindBuffer(GL_ELEMENT_ARRAY_BUFFER,', 'NativeBindBuffer(GL_ELEMENT_ARRAY_BUFFER,')
    s=s.replace('glBindBuffer((GLenum)t,', 'NativeBindBuffer((GLenum)t,')
    # No assumptions about bindings left by presentation/HUD between Render calls.
    a=s.index('void Render(const std::vector<GXCommand> &commands) override'); b=s.index('{',a)+1
    s=s[:b]+'''\n  s_native_array_buffer = s_native_element_buffer = s_native_element_vao = ~GLuint(0);
'''+s[b:]
    part('static void NativeConfigureBatchLayout(GLuint buffer, bool packed) {\n  const int layout = packed ? 1 : 0;',
         '''static unsigned NativeGpuInputLanes() {
  if (!s_gpu_vertex_mode || !s_gpu_texgen_lanes) return 8;
  static const bool enabled = std::getenv("NWII_NO_GPU_INPUT_PACKING") == nullptr;
  if (!enabled) return 8;
  unsigned lanes = s_gpu_texgen_lanes;
  for (unsigned t = 0; t < s_gpu_texgen_lanes; ++t) {
    const unsigned info = RenderState().xf[0x40+t], type = (info >> 4) & 7;
    if (type == 2 || type == 3) continue;
    const unsigned source = (info >> 7) & 31;
    if (source >= 5 && source <= 12) lanes = std::max(lanes, source - 4);
  }
  return std::min(lanes, 8u);
}
static std::vector<uint8_t> s_compact_gpu_batch;
static const void* NativeCompactGpuBatch(size_t stride) {
  s_compact_gpu_batch.resize(s_batch.size()*stride);
  for (size_t i=0; i<s_batch.size(); ++i)
    std::memcpy(s_compact_gpu_batch.data()+i*stride, &s_batch[i], stride);
  return s_compact_gpu_batch.data();
}
static void NativeConfigureBatchLayout(GLuint buffer, bool packed, unsigned lanes = 8) {
  const int layout = packed ? 1 : int(2 + lanes);''')
    part('''  const GLsizei stride = GLsizei(packed ? sizeof(NativePackedBatchVertex)
                                        : sizeof(BatchVertex));''', '''  const GLsizei stride = GLsizei(packed ? sizeof(NativePackedBatchVertex)
                                        : 64 + lanes*12);''')
    part('''  for (unsigned t = 0; t < 8; ++t)
    glVertexAttribPointer(5 + t, 3, GL_FLOAT, GL_FALSE, stride,
                          (void *)(tgoff + t * 12));''', '''  for (unsigned t = 0; t < 8; ++t) {
    if (t < lanes) {
      glEnableVertexAttribArray(5+t);
      glVertexAttribPointer(5+t, 3, GL_FLOAT, GL_FALSE, stride, (void*)(tgoff+t*12));
    } else {
      glDisableVertexAttribArray(5+t);
      glVertexAttrib3f(5+t, 0, 0, 0);
    }
  }''')
    part('''  const size_t native_vertex_stride = native_packed_layout
      ? sizeof(NativePackedBatchVertex) : sizeof(BatchVertex);
  NativeConfigureBatchLayout(s_vbo, native_packed_layout);''', '''  const unsigned native_input_lanes = native_packed_layout ? 8 : NativeGpuInputLanes();
  const size_t native_vertex_stride = native_packed_layout
      ? sizeof(NativePackedBatchVertex) : 64 + native_input_lanes*12;
  if (native_input_lanes < 8 && s_gpu_spans.empty())
    native_vertex_data = NativeCompactGpuBatch(native_vertex_stride);
  NativeConfigureBatchLayout(s_vbo, native_packed_layout, native_input_lanes);''')
    part('if (poff != SIZE_MAX) NativeFillGpuSpans(destination);',
         'if (poff != SIZE_MAX) NativeFillGpuSpans(destination, native_vertex_stride);')
    part('''                             native_packed_layout);''', '''                             native_packed_layout, native_input_lanes);''')
    part('''      native_vertex_data = s_batch.data();''', '''      native_vertex_data = native_input_lanes < 8
          ? NativeCompactGpuBatch(native_vertex_stride) : s_batch.data();''')
    part('''    NativeConfigureBatchLayout(s_vbo, native_packed_layout);''',
         '''    NativeConfigureBatchLayout(s_vbo, native_packed_layout, native_input_lanes);''')
    a=s.index('static void NativeFillGpuSpans(void* destination) {');b=s.index('\nstatic void NativeMaterializeGpuSpans()',a)
    s=s[:a]+'''static void NativeFillGpuSpans(void* destination, size_t stride) {
  // Same float fields as NativeWriteConvertedVertex's GPU-texgen branch,
  // truncated only after the last required raw ST/matrix-index input.
  auto* cursor = static_cast<uint8_t*>(destination);
  const unsigned lanes = unsigned((stride-64)/12);
  for (const auto& span : s_gpu_spans) for (size_t i=0;i<span.count;++i,cursor+=stride) {
    const auto& v = span.vertices[i];
    float* out = reinterpret_cast<float*>(cursor);
    for(unsigned j=0;j<3;++j) out[j]=v.pos[j];
    for(unsigned j=0;j<4;++j) out[3+j]=v.has_color?v.color[j]:1.f;
    out[7]=float(v.posMtxIdx); out[8]=v.has_norm?1.f:0.f;
    for(unsigned j=0;j<3;++j) out[9+j]=v.has_norm?v.norm[j]:0.f;
    for(unsigned j=0;j<4;++j) out[12+j]=v.color1[j];
    for(unsigned t=0;t<lanes;++t) {
      out[16+t*3]=v.tex[t][0];out[17+t*3]=v.tex[t][1];out[18+t*3]=float(v.texMtxIdx[t]);
    }
  }
}
'''+s[b:]
    part('''  static bool previous_gpu = false;
  if (!buffer) {''', '''  static bool previous_gpu = false;
  static bool have = false;
  static NativeUploadRing xf_ring;
  static GLint alignment = 0;
  static const bool stream = std::getenv("NWII_NO_GPU_UNIFORM_STREAM") == nullptr;
  if (!buffer) {''')
    part('''    glBufferData(GL_UNIFORM_BUFFER, sizeof(NativeGpuXfBlock), &previous, GL_STREAM_DRAW);
  }''', '''    glGetIntegerv(GL_UNIFORM_BUFFER_OFFSET_ALIGNMENT, &alignment);
    glBufferData(GL_UNIFORM_BUFFER, sizeof(NativeGpuXfBlock), &previous, GL_STREAM_DRAW);
    if (!stream) have = true;
  }''')
    part('if (!s_gpu_vertex_mode && !previous_gpu) return;',
         'if (have && !s_gpu_vertex_mode && !previous_gpu) return;')
    part('''  if (!previous_gpu || std::memcmp(&previous, &block, sizeof(block)) != 0) {
    glBindBuffer(GL_UNIFORM_BUFFER, buffer);
    glBufferData(GL_UNIFORM_BUFFER, sizeof(block), &block, GL_STREAM_DRAW);
    previous = block;
  }''', '''  if (!have || std::memcmp(&previous, &block, sizeof(block)) != 0) {
    glBindBuffer(GL_UNIFORM_BUFFER, buffer);
    const size_t offset = stream ? NativeRingUpload(GL_UNIFORM_BUFFER, xf_ring,
        &block, sizeof(block), size_t(std::max(1, alignment))) : SIZE_MAX;
    if (offset != SIZE_MAX)
      glBindBufferRange(GL_UNIFORM_BUFFER, 1, buffer, GLintptr(offset), sizeof(block));
    else {
      glBufferData(GL_UNIFORM_BUFFER, sizeof(block), &block, GL_STREAM_DRAW);
      NativeRingReset(xf_ring);
      glBindBufferBase(GL_UNIFORM_BUFFER, 1, buffer);
    }
    previous = block; have = true;
  }''')
    return s

manifest=BACKUP/'manifest.json'
if '--restore' in sys.argv:
    records=json.loads(manifest.read_text())
    for record in records:
        path=ROOT/record['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==record['after'], str(path)
    for record in records:
        (ROOT/record['path']).write_bytes((BACKUP/record['backup']).read_bytes())
    print('GPU stream/pack restored')
else:
    pending=[]
    for i,path in enumerate(FILES):
        raw=path.read_bytes();s=raw.decode('utf-8')
        if MARK in s: continue
        crlf='\r\n' in s
        n=transform(s.replace('\r\n','\n'))
        data=(n.replace('\n','\r\n') if crlf else n).encode('utf-8')
        pending.append((path,raw,data,str(i)+'.cpp'))
    if pending:
        assert len(pending)==len(FILES), 'partially installed stage'
        BACKUP.mkdir(parents=True,exist_ok=True)
        records=[]
        for path,raw,data,backup in pending:
            (BACKUP/backup).write_bytes(raw)
            records.append(dict(path=str(path.relative_to(ROOT)),backup=backup,
                after=hashlib.sha256(data).hexdigest()))
        manifest.write_text(json.dumps(records,indent=2))
        for path,raw,data,backup in pending: path.write_bytes(data)
    print('GPU stream/pack installed')
