// NATIVE_MOM_PERSISTENT_UPLOAD_V1: persistent, coherently mapped upload ring.
//
// Three independent 8 MiB segments inside ONE immutable buffer created with
// glBufferStorage (MAP_WRITE|MAP_PERSISTENT|MAP_COHERENT).  Uploads append into
// the current segment at an aligned offset; on wrap the segment is sealed with
// a single fence and the next segment's fence is polled NON-blocking.  A busy
// next segment returns SIZE_MAX (the caller falls back to the old orphan/
// glBufferData path) - the ring never overwrites bytes the GPU may still read.
//
// The ring never touches s_vbo/s_ebo: callers bind this buffer for the draw,
// and explicitly re-bind the mutable buffer whenever they fall back.  All GL
// calls run on the render thread only (the context owner); the fixture drives
// the same code through the function table below.
#pragma once

#include <cstdint>
#include <cstring>
#include <cstddef>
#include <vector>

// GL constants used without pulling in glad headers for the fixture.
#ifndef GL_MAP_WRITE_BIT
#define GL_MAP_WRITE_BIT 0x0002
#endif
#ifndef GL_MAP_PERSISTENT_BIT
#define GL_MAP_PERSISTENT_BIT 0x0040
#endif
#ifndef GL_MAP_COHERENT_BIT
#define GL_MAP_COHERENT_BIT 0x0080
#endif
#ifndef GL_SYNC_GPU_COMMANDS_COMPLETE
#define GL_SYNC_GPU_COMMANDS_COMPLETE 0x9117
#endif
#ifndef GL_ALREADY_SIGNALED
#define GL_ALREADY_SIGNALED 0x911A
#endif
#ifndef GL_CONDITION_SATISFIED
#define GL_CONDITION_SATISFIED 0x911C
#endif
#ifndef GL_WAIT_FAILED
#define GL_WAIT_FAILED 0x911D
#endif
#ifndef GL_ARRAY_BUFFER
#define GL_ARRAY_BUFFER 0x8892
#endif
#ifndef GL_ELEMENT_ARRAY_BUFFER
#define GL_ELEMENT_ARRAY_BUFFER 0x8893
#endif
#ifndef GL_NO_ERROR
#define GL_NO_ERROR 0
#endif

namespace nwii::runtime {

// Function table: everything the ring needs from GL.  The runtime fills it from
// glad/SDL_GL_GetProcAddress; the fixture fills it with stubs.  Any null
// required entry means the feature is unsupported -> callers use the old path.
struct NativePersistentGL {
  void  (*GenBuffers)(int, unsigned int*) = nullptr;
  void  (*DeleteBuffers)(int, const unsigned int*) = nullptr;
  void  (*BindBuffer)(unsigned int, unsigned int) = nullptr;
  void  (*BufferStorage)(unsigned int, long long, const void*, unsigned int) = nullptr;
  void* (*MapBufferRange)(unsigned int, long long, long long, unsigned int) = nullptr;
  unsigned long long (*FenceSync)(unsigned int, unsigned int) = nullptr;
  unsigned int (*ClientWaitSync)(unsigned long long, unsigned int, unsigned long long) = nullptr;
  void  (*DeleteSync)(unsigned long long) = nullptr;
  int   (*UnmapBuffer)(unsigned int) = nullptr;
  void  (*Finish)() = nullptr;
  unsigned int (*GetError)() = nullptr;
};

extern NativePersistentGL g_persistent_gl;

class NativePersistentRing {
public:
  static constexpr size_t kDefaultSegment = 8u << 20; // 8 MiB
  static constexpr int kSegments = 3;

  // segment_bytes is a fixture knob; production callers use the default.
  explicit NativePersistentRing(size_t segment_bytes = kDefaultSegment)
      : seg_bytes_(segment_bytes) {}

  // Create the immutable buffer + coherent map once.  Returns false (feature
  // off) when GL is unavailable or creation fails; the caller keeps the old
  // path.  Never creates or maps more than once.
  bool Init() {
    if (tried_) return ok_;
    tried_ = true;
    const NativePersistentGL& gl = g_persistent_gl;
    if (!gl.GenBuffers || !gl.DeleteBuffers || !gl.BufferStorage ||
        !gl.MapBufferRange || !gl.FenceSync || !gl.ClientWaitSync ||
        !gl.DeleteSync || !gl.UnmapBuffer || !gl.BindBuffer ||
        !gl.GetError || !gl.Finish)
      return ok_; // unsupported: no extension / GL < 4.4
    gl.GenBuffers(1, &buffer_);
    if (!buffer_ || gl.GetError() != GL_NO_ERROR) { ReleasePartial(); return ok_; }
    const size_t total = seg_bytes_ * kSegments;
    gl.BindBuffer(GL_ARRAY_BUFFER, buffer_);
    gl.BufferStorage(GL_ARRAY_BUFFER, (long long)total, nullptr,
                     GL_MAP_WRITE_BIT | GL_MAP_PERSISTENT_BIT |
                     GL_MAP_COHERENT_BIT);
    if (gl.GetError() != GL_NO_ERROR) { ReleasePartial(); return ok_; }
    map_ = gl.MapBufferRange(GL_ARRAY_BUFFER, 0, (long long)total,
                             GL_MAP_WRITE_BIT | GL_MAP_PERSISTENT_BIT |
                             GL_MAP_COHERENT_BIT);
    if (!map_ || gl.GetError() != GL_NO_ERROR) { ReleasePartial(); return ok_; }
    return ok_ = true;
  }

  bool ok() const { return ok_; }
  unsigned int buffer() const { return buffer_; }

  // Append `bytes` (>=1) at `align` inside the current segment.  Returns the
  // offset into the buffer, or SIZE_MAX when the caller must use the old path.
  // NATIVE_MOM_FUSED_GPU_BATCH_V1: reserve a writable coherent range. The
  // caller fills it before issuing the draw; failed reservations consume none.
  size_t reserve(size_t bytes, size_t align, void** destination) {
    *destination = nullptr;
    if (!ok_ || bytes == 0 || align == 0 || bytes > seg_bytes_)
      return SIZE_MAX;
    if (!ok_ || closed_) {
      // The current segment was sealed on a previous wrap and its next segment
      // is still busy (or a seal failed and the ring is off): retry only when
      // the ring is still healthy.
      if (!ok_ || !TryRotate()) return SIZE_MAX;
    }
    const size_t base = (size_t)current_ * seg_bytes_;
    // alignUp on the GLOBAL offset: the result stays a multiple of the stride
    // even when the 8 MiB segment boundary itself is not.
    const size_t aligned = (base + cursor_ + align - 1) / align * align;
    const size_t rel = aligned - base;
    if (rel + bytes > seg_bytes_) {
      // Seal THIS segment once (one fence, after all preceding draws) then try
      // the next.  A busy next segment keeps us closed so a later upload
      // retries without overwriting in-flight data; a failed seal disables the
      // ring for good (never reuse an unfenced in-flight segment).
      if (!SealSegment()) return SIZE_MAX;
      if (!TryRotate()) return SIZE_MAX;
      return reserve(bytes, align, destination); // fresh segment
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
  }

  // Context-owner shutdown: ONE glFinish (the caller passes finish=false for
  // the second ring so it fires exactly once across both) then unmap + delete
  // syncs + delete the buffer.  The work happens whenever the buffer or map
  // exists - a ring disabled by WAIT_FAILED still owns live objects.  Called
  // immediately before SDL_GL_DeleteContext; nothing GL runs after this.
  void Release(bool finish = true) {
    const NativePersistentGL& gl = g_persistent_gl;
    if (finish && gl.Finish) gl.Finish();
    ReleasePartial();
    tried_ = false;
    ok_ = false;
  }

private:
  void ReleasePartial() {
    const NativePersistentGL& gl = g_persistent_gl;
    // unmap a live map even when ok_ is false (create-error / disabled paths)
    if (map_) {
      gl.BindBuffer(GL_ARRAY_BUFFER, buffer_);
      gl.UnmapBuffer(GL_ARRAY_BUFFER);
      map_ = nullptr;
    }
    for (int i = 0; i < kSegments; ++i)
      if (fences_[i]) { gl.DeleteSync(fences_[i]); fences_[i] = 0; }
    if (buffer_) { gl.DeleteBuffers(1, &buffer_); buffer_ = 0; }
    current_ = 0; cursor_ = 0; closed_ = false;
  }

  // Insert the seal fence for the current segment exactly once.  A null fence
  // means the driver refused to track completion: the segment can never be
  // proven safe to reuse, so the ring disables permanently (no fallback that
  // could overwrite in-flight bytes later).
  bool SealSegment() {
    const NativePersistentGL& gl = g_persistent_gl;
    if (!fences_[current_]) {
      fences_[current_] = gl.FenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
      if (!fences_[current_]) { ok_ = false; closed_ = true; return false; }
    }
    closed_ = true;
    return true;
  }

  // Poll the NEXT segment's fence without blocking.  True when it is free and
  // became current; false when still busy or the feature died.
  bool TryRotate() {
    const NativePersistentGL& gl = g_persistent_gl;
    const int next = (current_ + 1) % kSegments;
    const unsigned long long f = fences_[next];
    if (f) {
      const unsigned int r = gl.ClientWaitSync(f, 0, 0);
      if (r == GL_WAIT_FAILED) { ok_ = false; return false; }
      if (r != GL_ALREADY_SIGNALED && r != GL_CONDITION_SATISFIED)
        return false; // still in flight - stay closed, never overwrite
      gl.DeleteSync(f);
      fences_[next] = 0;
    }
    current_ = next;
    cursor_ = 0;
    closed_ = false;
    return true;
  }

  size_t seg_bytes_;
  bool tried_ = false, ok_ = false, closed_ = false;
  unsigned int buffer_ = 0;
  void* map_ = nullptr;
  int current_ = 0;
  size_t cursor_ = 0;
  unsigned long long fences_[kSegments] = {0, 0, 0};
};

// One ring per upload domain: vertex and index state never share syncs.
extern NativePersistentRing g_persist_vertex;
extern NativePersistentRing g_persist_index;

// Render-thread shutdown hook, called immediately before SDL_GL_DeleteContext.
void NativePersistentReleaseAll();

} // namespace nwii::runtime
