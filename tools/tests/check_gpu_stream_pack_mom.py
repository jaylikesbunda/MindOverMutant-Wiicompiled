"""Compare the actual fused packer with the actual GPU vertex writer prefix.

Builds a small GL-free fixture outside the repository and exercises every
stride, optional attribute combination, and cross-lane texgen source.
"""
from pathlib import Path
import subprocess, tempfile

ROOT=Path(__file__).resolve().parents[2]
s=(ROOT/'export/nWiiRuntime/src/hle/gx/renderer_gl.cpp').read_text()
def function(signature):
    a=s.index(signature); start=s.index('{',a); depth=1; end=start+1
    while depth:
        depth += (s[end]=='{')-(s[end]=='}'); end+=1
    return s[a:end]
packer=function('static void NativeFillGpuSpans(void* destination, size_t stride) {')
lanes=function('static unsigned NativeGpuInputLanes() {')
writer=s[s.index('    bv.x = vtx.pos[0];'):s.index('    } else {',s.index('    bv.x = vtx.pos[0];'))]
# The cut is after the active-texgen loop, before the CPU-texgen fallback.
writer=writer.replace('    if (s_gpu_texgen_lanes) {','')
fixture='''#include "runtime/gx_command.h"
#include <algorithm>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <vector>
using nwii::runtime::gx::VertexData;
struct BatchVertex {float x,y,z,r,g,b,a,u,v,nx,ny,nz,color1[4],texgen[8][3];};
struct NativeGpuSpan {const VertexData* vertices;size_t count;};
static std::vector<NativeGpuSpan> s_gpu_spans;
static unsigned s_gpu_texgen_lanes=1;
static bool s_gpu_vertex_mode=true;
struct State { unsigned xf[256]{}; } state;
static State& RenderState(){return state;}
static void reference(const VertexData& vtx,BatchVertex& bv){
'''+writer+'\n}\n'+packer+'\n'+lanes+'''
int main(){
  for(unsigned active=1;active<=8;++active) for(unsigned source=5;source<=12;++source){
    state={};s_gpu_texgen_lanes=active;state.xf[0x40]=(source<<7);
    if(NativeGpuInputLanes()!=std::max(active,source-4))return 1;
    state.xf[0x40]|=(2<<4);
    if(NativeGpuInputLanes()!=active)return 2;
  }
  VertexData v[3];
  for(unsigned flags=0;flags<4;++flags) for(unsigned n=0;n<100;++n){
    for(unsigned i=0;i<3;++i){
      auto& q=v[i];q.has_norm=flags&1;q.has_color=flags&2;q.posMtxIdx=uint8_t(n+i);
      for(unsigned j=0;j<3;++j){q.pos[j]=float(int(n)-50+i+j)/7;q.norm[j]=float(j)-.5f;}
      for(unsigned j=0;j<4;++j){q.color[j]=float(j+n)/255;q.color1[j]=float(j+i)/7;}
      for(unsigned t=0;t<8;++t){q.tex[t][0]=float(n+t);q.tex[t][1]=-float(i+t);q.texMtxIdx[t]=uint8_t(3*t+n);}
    }
    s_gpu_spans={{v,1},{v+1,2}};
    for(unsigned lanes=1;lanes<=8;++lanes){
      const size_t stride=64+12*lanes;
      std::vector<unsigned char> bytes(3*stride+16,0xcd);
      NativeFillGpuSpans(bytes.data(),stride);
      for(unsigned i=0;i<3;++i){BatchVertex expected;reference(v[i],expected);
        if(std::memcmp(bytes.data()+i*stride,&expected,stride))return 3;}
      for(size_t i=3*stride;i<bytes.size();++i)if(bytes[i]!=0xcd)return 4;
    }
  }
  puts("PASS: 9600 packed vertices; all lane dependencies; output bounds");
}
'''
with tempfile.TemporaryDirectory(prefix='mom-gpu-pack-') as folder:
    cpp=Path(folder)/'test.cpp'; exe=Path(folder)/'test.exe';cpp.write_text(fixture)
    subprocess.run(['g++','-std=c++17','-O2','-I',str(ROOT/'export/nWiiRuntime/include'),str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
