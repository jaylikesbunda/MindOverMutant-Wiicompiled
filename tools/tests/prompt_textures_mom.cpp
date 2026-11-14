// Exercise the actual texture matcher without a window or a GL driver.
#include <array>
#include <cassert>
#include <cstring>
#include <fstream>
#include <thread>
using GLuint=unsigned;using GLint=int;
enum {GL_TEXTURE_BINDING_2D,GL_TEXTURE_2D,GL_RGBA,GL_UNSIGNED_BYTE,GL_TEXTURE_MIN_FILTER,GL_TEXTURE_MAG_FILTER,GL_NEAREST};
static unsigned next_texture=1,deleted=0,binding=77;
static std::array<unsigned char,4096> upload{};
void glGetIntegerv(unsigned,int* p){*p=int(binding);}
void glGenTextures(int,unsigned* p){*p=next_texture++;}
void glBindTexture(unsigned,unsigned id){binding=id;}
void glTexImage2D(unsigned,int,int,int,int,int,unsigned,unsigned,const void* p){std::memcpy(upload.data(),p,4096);}
void glTexParameteri(unsigned,unsigned,int){}
void glGenerateMipmap(unsigned){}
void glDeleteTextures(int n,const unsigned*){deleted+=unsigned(n);}
#include "native_prompt_textures_mom.h"

int main(int argc,char** argv) {
    assert(argc==2);
    native_prompts::MomTextures textures;
    std::ifstream pack(argv[1],std::ios::binary);pack.seekg(8);
    std::array<std::array<unsigned char,4096>,8> originals{};
    for(auto& original:originals){pack.read(reinterpret_cast<char*>(original.data()),4096);pack.seekg(3*4096,std::ios::cur);}
    assert(pack);
    pack.seekg(0);std::vector<char> complete((std::istreambuf_iterator<char>(pack)),{});pack.close();
    assert(complete.size()==8+8*16384);
    std::array<unsigned char,1024> source{};std::array<unsigned char,512> palette{};
    for(unsigned i=0;i<8;++i) {
        source[0]=static_cast<unsigned char>(i);
        assert(textures.probe(i,9,2,source.data(),source.size(),palette.data(),palette.size())==-2);
        assert(textures.record(i,originals[i].data())==int(i));
        assert(textures.probe(i,9,2,source.data(),source.size(),palette.data(),palette.size())==int(i));
        for(unsigned family=0;family<3;++family) {
            native_prompts::device=family;assert(textures.texture(int(i)));
            assert(upload[0]==80+family*20+i);assert(binding==77);
        }
        native_prompts::device=3;assert(!textures.texture(int(i)));
        // MoM's TGA upload flips rows and converts its palette to RGB5A3.
        std::array<unsigned char,4096> quantized{};
        for(unsigned y=0;y<32;++y)for(unsigned x=0;x<32;++x) {
            const unsigned p=(y*32+x)*4,q=((31-y)*32+x)*4;
            for(unsigned c=0;c<3;++c){unsigned v=originals[i][q+c]>>4;quantized[p+c]=static_cast<unsigned char>((v<<4)|v);}
            unsigned a=originals[i][q+3]>>5;quantized[p+3]=static_cast<unsigned char>((a<<5)|(a<<2)|(a>>1));
        }
        assert(textures.record(i,quantized.data())==int(i+8));
        native_prompts::device=0;assert(textures.texture(int(i+8)));assert(upload[0]==111+i);
        quantized[4095]^=1;assert(textures.record(i,quantized.data())==-1);
        palette[0]^=1;assert(textures.probe(i,9,2,source.data(),source.size(),palette.data(),palette.size())==-2);
    }
    const auto before=deleted;
    // A bad live update must retain the last complete pack and its GL names.
    {std::ofstream file(argv[1],std::ios::binary|std::ios::trunc);file<<"broken";}
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    assert(textures.probe(0,6,0,source.data(),source.size(),nullptr,0)==-2);
    assert(textures.record(0,originals[0].data())==0);assert(textures.texture(0));assert(deleted==before);
    // A complete remap takes effect, retaining the old texture until a safe clear.
    complete[8+4096]=static_cast<char>(177);
    {std::ofstream file(argv[1],std::ios::binary|std::ios::trunc);file.write(complete.data(),complete.size());}
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    assert(textures.probe(0,6,0,source.data(),source.size(),nullptr,0)==-2);
    assert(textures.record(0,originals[0].data())==0);assert(textures.texture(0));
    assert(upload[0]==177 && deleted==before);
    textures.clear();assert(deleted==33);
}
