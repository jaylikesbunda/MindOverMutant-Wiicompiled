#pragma once
#include "runtime/native_prompt_device.h"
#include <array>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unordered_map>
#include <vector>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace native_prompts {
// GL objects and candidate bytes belong exclusively to the render thread.
class MomTextures {
    struct Glyph {
        std::array<unsigned char,4096> original{};
        std::array<std::array<unsigned char,4096>,3> pixels{};
        std::array<std::array<GLuint,3>,2> textures{};
        bool observed=false;
    };
    struct Candidate {
        unsigned format=~0u,palette_format=~0u;
        std::vector<unsigned char> source,palette;
        int glyph=-2;
    };
    std::vector<Glyph> glyphs;
    std::unordered_map<unsigned,Candidate> candidates;
    std::vector<GLuint> retired;
    const std::filesystem::path path=[](){const char* p=std::getenv("NWII_PROMPT_PACK");return p?p:"";}();
    uint64_t pack_time=0;
    std::chrono::steady_clock::time_point next_check{};

    bool modified(uint64_t& stamp) const {
#ifdef _WIN32
        // MinGW's filesystem timestamp can truncate to whole seconds. Rapid
        // same-size remaps need the native 100 ns FILETIME instead.
        WIN32_FILE_ATTRIBUTE_DATA info{};
        if(!GetFileAttributesExW(path.c_str(),GetFileExInfoStandard,&info))return false;
        stamp=(uint64_t(info.ftLastWriteTime.dwHighDateTime)<<32)|info.ftLastWriteTime.dwLowDateTime;
#else
        std::error_code error;const auto value=std::filesystem::last_write_time(path,error);
        if(error)return false;stamp=uint64_t(value.time_since_epoch().count());
#endif
        return true;
    }

    static bool matches(const unsigned char* original,const unsigned char* rgba,bool flip,unsigned threshold) {
        for(unsigned y=0;y<32;++y)for(unsigned x=0;x<32;++x) {
            const unsigned p=(y*32+x)*4,q=((flip?31-y:y)*32+x)*4;
            if(!threshold) { if(std::memcmp(original+q,rgba+p,4))return false;continue; }
            const bool opaque=original[q+3]>=threshold;
            const unsigned a=original[q+3]>>5;
            if(rgba[p+3]!=(opaque?255u:(a<<5)|(a<<2)|(a>>1)))return false;
            for(unsigned c=0;c<3;++c) {
                const unsigned v=original[q+c]>>(opaque?3:4);
                if(rgba[p+c]!=(opaque?((v<<3)|(v>>2)):((v<<4)|v)))return false;
            }
        }
        return true;
    }
    void refresh() {
        if(path.empty())return;
        const auto now=std::chrono::steady_clock::now();if(now<next_check)return;
        next_check=now+std::chrono::milliseconds(250);
        uint64_t stamp=0;
        if(!modified(stamp) || (!glyphs.empty() && stamp==pack_time))return;
        std::ifstream file(path,std::ios::binary);char magic[8]{};file.read(magic,8);
        if(!file || std::memcmp(magic,"MMPRMT01",8))return;
        std::vector<Glyph> loaded(8);
        for(auto& g:loaded) {
            file.read(reinterpret_cast<char*>(g.original.data()),4096);
            for(auto& pixels:g.pixels)file.read(reinterpret_cast<char*>(pixels.data()),4096);
        }
        if(!file || file.peek()!=std::char_traits<char>::eof())return;
        // An open batch may still reference an old name. Delete only at the texture
        // cache's protected-name boundary, never during a live remap.
        for(auto& g:glyphs)for(auto& orientation:g.textures)for(GLuint tex:orientation)if(tex)retired.push_back(tex);
        glyphs=std::move(loaded);candidates.clear();pack_time=stamp;
        std::cout<<"[Prompts] MoM live pack loaded: 8 action glyphs"<<std::endl;
    }
public:
    MomTextures(){refresh();}
    int probe(unsigned address,unsigned format,unsigned palette_format,const unsigned char* source,size_t bytes,
              const unsigned char* palette,size_t palette_bytes) {
        refresh();if(glyphs.empty())return -1;
        if(candidates.size()>1024)candidates.clear();
        auto& c=candidates[address];
        if(c.format==format && c.palette_format==palette_format && c.source.size()==bytes && c.palette.size()==palette_bytes &&
           !std::memcmp(c.source.data(),source,bytes) && (!palette_bytes || !std::memcmp(c.palette.data(),palette,palette_bytes)))return c.glyph;
        c.format=format;c.palette_format=palette_format;c.source.assign(source,source+bytes);
        c.palette.clear();if(palette_bytes)c.palette.assign(palette,palette+palette_bytes);
        c.glyph=-2;return -2;
    }
    int record(unsigned address,const unsigned char* rgba) {
        auto& c=candidates.at(address);c.glyph=-1;if(!rgba)return -1;
        for(unsigned i=0;i<glyphs.size();++i)for(unsigned flip=0;flip<2;++flip) {
            bool match=matches(glyphs[i].original.data(),rgba,flip,0);
            if(!match && (c.format==8 || c.format==9 || c.format==10) && c.palette_format==2)
                match=matches(glyphs[i].original.data(),rgba,flip,224) || matches(glyphs[i].original.data(),rgba,flip,255);
            if(match) {
                c.glyph=int(i+flip*8);
                if(!glyphs[i].observed){glyphs[i].observed=true;std::cout<<"[Prompts] matched MoM action "<<i<<" flip="<<flip<<std::endl;}
                return c.glyph;
            }
        }
        return -1;
    }
    GLuint texture(int glyph) {
        if(glyph<0 || unsigned(glyph)>=glyphs.size()*2)return 0;
        const unsigned selected=device.load(std::memory_order_relaxed);
        if(selected>=3)return 0;
        auto& g=glyphs[unsigned(glyph)%8];const unsigned flip=unsigned(glyph)/8;
        auto& tex=g.textures[flip][selected];if(tex)return tex;
        GLint bound=0;glGetIntegerv(GL_TEXTURE_BINDING_2D,&bound);
        glGenTextures(1,&tex);glBindTexture(GL_TEXTURE_2D,tex);
        std::array<unsigned char,4096> pixels{};
        for(unsigned y=0;y<32;++y)std::memcpy(pixels.data()+128*y,g.pixels[selected].data()+128*(flip?31-y:y),128);
        glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,32,32,0,GL_RGBA,GL_UNSIGNED_BYTE,pixels.data());
        glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);glGenerateMipmap(GL_TEXTURE_2D);
        glBindTexture(GL_TEXTURE_2D,GLuint(bound));return tex;
    }
    void clear() {
        for(auto& g:glyphs)for(auto& orientation:g.textures)for(auto& tex:orientation){if(tex)glDeleteTextures(1,&tex);tex=0;}
        for(GLuint tex:retired)glDeleteTextures(1,&tex);
        retired.clear();candidates.clear();
    }
};
inline MomTextures& mom_textures(){static MomTextures textures;return textures;}
}
