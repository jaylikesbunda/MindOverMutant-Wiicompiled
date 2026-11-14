#define SDL_MAIN_HANDLED
#include <glad/glad.h>
#include <SDL.h>
#include "runtime/native_packed_vertices_mom.h"
#include "runtime/native_persistent_upload_mom.h"
#include "runtime/gx/tev_shader_gen.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <memory>
#include <string>
#include <vector>

struct BatchVertex {
  float x, y, z, r, g, b, a, u, v, nx, ny, nz;
  float color1[4], texgen[8][3];
};
static_assert(sizeof(BatchVertex) == 160);
namespace nwii::runtime { NativePersistentGL g_persistent_gl; }
static GLuint s_attrib_buffer = 0;
static int s_attrib_layout = -1;
// Extracted verbatim from the staged renderer by run_upload_gl_smoke.py.
#include "upload_gl_bindings.inc"
#include "mipmap_gl_fixture.h"

static void require(bool ok, const char* what) {
  if (!ok) {
    std::fprintf(stderr, "FAIL %s; SDL=%s GL=0x%x\n", what, SDL_GetError(), glGetError ? glGetError() : 0);
    std::exit(1);
  }
}
static GLuint shader(GLenum type, const char* source) {
  GLuint s = glCreateShader(type);
  glShaderSource(s, 1, &source, nullptr);
  glCompileShader(s);
  GLint ok = 0; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
  if (!ok) { char log[4096] = {}; glGetShaderInfoLog(s, sizeof log, nullptr, log); std::fputs(log, stderr); }
  require(ok, "compile smoke shader"); return s;
}
static int comparisons = 0;
static void compare(const std::vector<unsigned char>& reference,
                    const std::vector<unsigned char>& pixels) {
  int maximum = 0;
  for (size_t i = 0; i < reference.size(); ++i)
    maximum = std::max(maximum, std::abs(int(reference[i]) - int(pixels[i])));
  require(maximum <= 1, "RGBA8 differs by more than one channel unit");
  ++comparisons;
}

int main(int argc, char** argv) {
  SDL_SetMainReady();
  require(SDL_Init(SDL_INIT_VIDEO) == 0, "SDL video init");
  SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
  SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 3);
  SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);
  SDL_Window* window = SDL_CreateWindow("Upload Regression", 0, 0, 64, 64,
                                        SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN);
  require(window != nullptr, "create hidden window");
  SDL_GLContext context = SDL_GL_CreateContext(window);
  require(context != nullptr, "create GL context");
  require(gladLoadGLLoader((GLADloadproc)SDL_GL_GetProcAddress), "load GL");
  std::printf("GL vendor=%s renderer=%s version=%s\n",
              glGetString(GL_VENDOR), glGetString(GL_RENDERER), glGetString(GL_VERSION));
  test_mipmap_state();
  // GPU helper definitions exist in generated shaders even on CPU-fallback
  // draws: they must compile/link before a default candidate can be qualified.
  auto state = std::make_unique<nwii::runtime::gx::GXState>();
  for (unsigned variant = 0; variant < 3; ++variant) {
    state->numTevStages = 1;
    state->numTexGens = variant == 2 ? 8 : 1;
    state->alphaTest.comp0 = 4; state->alphaTest.ref0 = 128;
    state->alphaTest.comp1 = 7;
    state->bp[0xee] = 0x3f800;
    state->bp[0xf1] = (variant << 21) | 0x3f000;
    state->bp[0xef] = 0x123456;
    state->bp[0xf0] = 8;
    const auto generated = nwii::runtime::gx::GenerateTEVShader(*state, 0x90);
    if (argc > 1) {
      const std::string prefix = std::string(argv[1]) + "/generated-" + std::to_string(variant);
      std::ofstream(prefix + ".vert") << generated.vertex_source;
      std::ofstream(prefix + ".frag") << generated.fragment_source;
    }
    GLuint gv = shader(GL_VERTEX_SHADER, generated.vertex_source.c_str());
    GLuint gf = shader(GL_FRAGMENT_SHADER, generated.fragment_source.c_str());
    GLuint gp = glCreateProgram(); glAttachShader(gp, gv); glAttachShader(gp, gf);
    glLinkProgram(gp); GLint valid = 0; glGetProgramiv(gp, GL_LINK_STATUS, &valid);
    if (!valid) { char log[4096] = {}; glGetProgramInfoLog(gp, sizeof log, nullptr, log); std::fputs(log, stderr); }
    require(valid, "link actual generated TEV/GPU-helper shader");
    glDeleteProgram(gp); glDeleteShader(gv); glDeleteShader(gf);
  }
  std::puts("PASS actual generated shader compile/link: three TEV/fog/texgen variants");
  NativePersistentResolve();
  nwii::runtime::NativePersistentRing vertices(4096), indices(256);
  require(vertices.Init() && indices.Init(), "persistent storage support");

  GLuint vao = 0, mutable_v = 0, mutable_i = 0, fbo = 0, image = 0;
  glGenVertexArrays(1, &vao); glBindVertexArray(vao);
  for (unsigned a = 0; a < 13; ++a) glEnableVertexAttribArray(a);
  glGenBuffers(1, &mutable_v); glGenBuffers(1, &mutable_i);
  glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
  glGenTextures(1, &image); glBindTexture(GL_TEXTURE_2D, image);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
  glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, image, 0);
  require(glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE, "RGBA8 FBO");
  GLuint vs = shader(GL_VERTEX_SHADER,
      "#version 330 core\n"
      "layout(location=0) in vec3 p; layout(location=1) in vec4 c;\n"
      "layout(location=2) in vec2 uv; layout(location=3) in vec3 n;\n"
      "layout(location=4) in vec4 c1; layout(location=5) in vec3 t;\n"
      "out vec4 color; void main(){gl_Position=vec4(p,1);"
      "color=(c+c1)*0.4+vec4(uv*0.05,n.x*0.05,0)+vec4(t*0.03,0);}\n");
  GLuint fs = shader(GL_FRAGMENT_SHADER,
      "#version 330 core\nin vec4 color; out vec4 result; void main(){result=color;}\n");
  GLuint program = glCreateProgram(); glAttachShader(program, vs); glAttachShader(program, fs);
  glLinkProgram(program); GLint linked = 0; glGetProgramiv(program, GL_LINK_STATUS, &linked);
  require(linked, "link smoke program"); glUseProgram(program);
  glViewport(0, 0, 64, 64); glDisable(GL_BLEND); glDisable(GL_DEPTH_TEST);
  glDisable(GL_CULL_FACE); glDisable(GL_DITHER);
  std::vector<BatchVertex> source(3);
  source[0].x = -.75f; source[0].y = -.75f;
  source[1].x = .75f; source[1].y = -.75f;
  source[2].x = 0.f; source[2].y = .75f;
  for (auto& v : source) {
    v.r = .2f; v.g = .4f; v.b = .6f; v.a = 1;
    v.u = .25f; v.v = -.25f; v.nx = .2f;
    v.color1[0] = v.color1[2] = v.color1[3] = 1;
    v.texgen[0][0] = .5f; v.texgen[0][1] = .25f; v.texgen[0][2] = 1;
  }
  std::vector<NativePackedBatchVertex> compact;
  const uint32_t index_data[3] = {0, 1, 2};
  auto draw = [&](bool packed, bool persistent, bool indexed, bool force_fallback) {
    require(!packed || NativePackBatch(source, compact), "pack eligible triangle");
    const void* data = packed ? (const void*)compact.data() : (const void*)source.data();
    size_t stride = packed ? 136 : 160, bytes = source.size() * stride;
    size_t voff = persistent ? vertices.upload(GL_ARRAY_BUFFER, data,
                           force_fallback ? 4097 : bytes, stride) : SIZE_MAX;
    if (voff == SIZE_MAX) {
      glBindBuffer(GL_ARRAY_BUFFER, mutable_v);
      glBufferData(GL_ARRAY_BUFFER, bytes, data, GL_STREAM_DRAW);
      NativeConfigureBatchLayout(mutable_v, packed); voff = 0;
    } else {
      require(voff % stride == 0, "vertex offset alignment");
      NativeConfigureBatchLayout(vertices.buffer(), packed);
    }
    glClearColor(0, 0, 0, 0); glClear(GL_COLOR_BUFFER_BIT);
    if (indexed) {
      size_t ioff = persistent ? indices.upload(GL_ELEMENT_ARRAY_BUFFER, index_data,
                           force_fallback ? 257 : sizeof index_data, 4) : SIZE_MAX;
      if (ioff == SIZE_MAX) {
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, mutable_i);
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, sizeof index_data, index_data, GL_STREAM_DRAW); ioff = 0;
      } else glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, indices.buffer());
      glDrawElementsBaseVertex(GL_TRIANGLES, 3, GL_UNSIGNED_INT,
                               (void*)(uintptr_t)ioff, GLint(voff / stride));
    } else glDrawArrays(GL_TRIANGLES, GLint(voff / stride), 3);
    std::vector<unsigned char> pixels(64 * 64 * 4);
    glReadPixels(0, 0, 64, 64, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
    require(glGetError() == GL_NO_ERROR, "draw/readback GL error");
    return pixels;
  };
  const auto reference = draw(false, false, false, false);
  require(std::any_of(reference.begin(), reference.end(), [](unsigned char c){return c != 0;}),
          "reference triangle must render, not compare two empty images");
  compare(reference, draw(true, false, true, false));
  for (unsigned i = 0; i < 40; ++i)
    compare(reference, draw((i & 1) != 0, true, (i & 2) != 0, i % 7 == 0));
  source[0].r = .5f; // Unrepresentable byte colour: select the real float fallback.
  require(!NativePackBatch(source, compact), "non-byte colour rejects whole batch");
  const auto fallback_reference = draw(false, false, false, false);
  compare(fallback_reference, draw(false, true, true, true));
  vertices.Release(); indices.Release();
  require(glGetError() == GL_NO_ERROR, "context-owner upload cleanup");
  glDeleteProgram(program); glDeleteShader(vs); glDeleteShader(fs);
  glDeleteBuffers(1, &mutable_v); glDeleteBuffers(1, &mutable_i);
  glDeleteVertexArrays(1, &vao); glDeleteFramebuffers(1, &fbo); glDeleteTextures(1, &image);
  SDL_GL_DeleteContext(context); SDL_DestroyWindow(window); SDL_Quit();
  std::printf("PASS upload GL smoke: %d RGBA8 comparisons, float/packed, indexed/nonindexed, segment reuse and mutable fallback\n",
              comparisons);
}
