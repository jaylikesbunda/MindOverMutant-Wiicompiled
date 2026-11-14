r"""Bulk vertex/index emission and deeper render queue (MoM; CotT fix_renderer_batch_ring / fix_draw_submission_reuse line).

Marker: NATIVE_MOM_RENDER_EMIT_BULK_V1.

GL-thread profile (glprof, busy scene): EmitVertex 8.5%, std::vector<unsigned>::emplace_back 2.8% (index generation one
push_back at a time, each with a capacity test), memmove from vector growth.
  * EmitPrimitive reserves the batch's vertices once per primitive, then emits them as before.
  * The index lists for quads (0x80), triangle lists in an indexed batch (0x90), strips (0x98) and fans (0xA0), and the
    mixed-batch back-fill, are written through one resize per primitive and a pointer, in exactly the same order and
    values as the push_back loops produced.
  * The render queue's default depth goes from 8 to 16 batches (the existing clamp maximum), so a GL-thread spike does not
    block the front end - and with it the PE signals the guest waits on - as soon. NWII_RENDER_QUEUE_DEPTH still overrides.

Edits src/hle/gx/renderer_gl.cpp and src/hle/render_queue_glue_mom.inc in vendor and export. Idempotent; --restore is exact.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_RENDER_EMIT_BULK_V1'
ROOT = Path(__file__).resolve().parents[2]
TREES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime', ROOT / 'export/nWiiRuntime']

OLD_EMIT = '''  if(!count)return;
  for(size_t i=0;i<count;++i) EmitVertex(v[i]);
  // NATIVE_MIXED_INDEX_PREFIX_V1: a GL draw is either indexed or not, so a batch that
  // joins both kinds back-fills the vertices the array path drew.
  if(cmd.prim_type!=0x90 && s_indices.empty())
    for(uint32_t i=0;i<base;++i)s_indices.push_back(i); // NATIVE_MIXED_INDEX_PREFIX_V1_BACKFILL
  switch(cmd.prim_type) {
  case 0x80:
    for(uint32_t i=0;i<count;i+=4) {
      s_indices.push_back(base+i);s_indices.push_back(base+i+1);s_indices.push_back(base+i+2);
      s_indices.push_back(base+i+2);s_indices.push_back(base+i+3);s_indices.push_back(base+i);
    }
    break;
  case 0x90:
    // NATIVE_MIXED_INDEX_PREFIX_V1: an indexed batch needs indices for this list too, or
    // its triangles are never submitted.
    if(!s_indices.empty())
      for(uint32_t i=0;i<count;++i)s_indices.push_back(base+i); // NATIVE_MIXED_INDEX_PREFIX_V1_LIST90
    break;
  case 0x98:
    for(uint32_t i=2;i<count;++i) {
      s_indices.push_back(base+i-((i&1)?1:2));
      s_indices.push_back(base+i-((i&1)?2:1));
      s_indices.push_back(base+i);
    }
    break;
  case 0xA0:
    for(uint32_t i=2;i<count;++i) {
      s_indices.push_back(base);s_indices.push_back(base+i-1);s_indices.push_back(base+i);
    }
    break;
  }
}
'''
NEW_EMIT = '''  if(!count)return;
  s_batch.reserve(base+count); // NATIVE_MOM_RENDER_EMIT_BULK_V1: one growth per primitive
  for(size_t i=0;i<count;++i) EmitVertex(v[i]);
  // NATIVE_MIXED_INDEX_PREFIX_V1: a GL draw is either indexed or not, so a batch that
  // joins both kinds back-fills the vertices the array path drew.
  // NATIVE_MOM_RENDER_EMIT_BULK_V1: each list is sized once and written through a pointer, same values and order.
  auto grow=[](size_t add)->uint32_t*{const size_t at=s_indices.size();s_indices.resize(at+add);return s_indices.data()+at;};
  if(cmd.prim_type!=0x90 && s_indices.empty() && base) {
    uint32_t* o=grow(base);
    for(uint32_t i=0;i<base;++i)o[i]=i; // NATIVE_MIXED_INDEX_PREFIX_V1_BACKFILL
  }
  const uint32_t c=uint32_t(count);
  switch(cmd.prim_type) {
  case 0x80: {
    uint32_t* o=grow(size_t(c/4)*6);
    for(uint32_t i=0;i<c;i+=4) {
      *o++=base+i;*o++=base+i+1;*o++=base+i+2;
      *o++=base+i+2;*o++=base+i+3;*o++=base+i;
    }
    break;
  }
  case 0x90:
    // NATIVE_MIXED_INDEX_PREFIX_V1: an indexed batch needs indices for this list too, or
    // its triangles are never submitted.
    if(!s_indices.empty()) {
      uint32_t* o=grow(c);
      for(uint32_t i=0;i<c;++i)o[i]=base+i; // NATIVE_MIXED_INDEX_PREFIX_V1_LIST90
    }
    break;
  case 0x98: {
    uint32_t* o=grow(size_t(c-2)*3);
    for(uint32_t i=2;i<c;++i) {
      *o++=base+i-((i&1)?1:2);
      *o++=base+i-((i&1)?2:1);
      *o++=base+i;
    }
    break;
  }
  case 0xA0: {
    uint32_t* o=grow(size_t(c-2)*3);
    for(uint32_t i=2;i<c;++i) {
      *o++=base;*o++=base+i-1;*o++=base+i;
    }
    break;
  }
  }
}
'''
OLD_DEPTH = 'return size_t(std::clamp(e ? std::atoi(e) : 8, 1, 16));'
NEW_DEPTH = 'return size_t(std::clamp(e ? std::atoi(e) : 16, 1, 16)); // NATIVE_MOM_RENDER_EMIT_BULK_V1: default 16 (was 8)'
EDITS = {'src/hle/gx/renderer_gl.cpp': [(OLD_EMIT, NEW_EMIT)], 'src/hle/render_queue_glue_mom.inc': [(OLD_DEPTH, NEW_DEPTH)]}


def main():
    restore = '--restore' in sys.argv
    for base in TREES:
        for rel, pairs in EDITS.items():
            p = base / rel
            raw = p.read_bytes().decode('utf-8', errors='surrogateescape')
            crlf = '\r\n' in raw
            t = raw.replace('\r\n', '\n')
            for old, new in pairs:
                a, b = (new, old) if restore else (old, new)
                if b in t:
                    continue
                if t.count(a) != 1:
                    print('fix_render_emit_bulk_mom: %s: anchor found %d times' % (p, t.count(a))); sys.exit(1)
                t = t.replace(a, b, 1)
            p.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
    print('render emit bulk %s.' % ('removed' if restore else 'installed'))


if __name__ == '__main__':
    main()
