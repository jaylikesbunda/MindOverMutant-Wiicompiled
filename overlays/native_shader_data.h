#pragma once
// NATIVE_MOM_SHADER_DATA_UNIFORMS_V1: fog BP data and alpha-test reference
// values ride three uniforms, so a value-only state change no longer costs a
// new shader program.  Modes, comparators and the 0/255 endpoints stay
// source-specialised; NWII_LITERAL_SHADER_DATA=1 restores the literal GLSL.
#include <cstdlib>
namespace nwii::runtime::gx {
inline bool NativeShaderDataUniformsEnabled(){
 static const bool enabled=std::getenv("NWII_LITERAL_SHADER_DATA")==nullptr;
 return enabled;
}
}
