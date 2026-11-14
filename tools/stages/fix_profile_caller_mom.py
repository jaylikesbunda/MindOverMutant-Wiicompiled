"""Capture the first executable caller of DLL samples; cache the TEV trace flag.

Idempotent and reversible with --restore. Caller RVAs are stored plus one;
zero means no executable frame was found.
"""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
trees = [root / 'vendor/NWiiRecomp/nWiiRuntime', root / 'export/nWiiRuntime']
marker = 'NATIVE_MOM_PROFILE_CALLER_V1'
edits = [
('  uint8_t used;\n};', '  uint8_t used;\n  uint64_t caller; // ' + marker + '\n};'),
('''      context.ContextFlags = CONTEXT_CONTROL;
      const bool ok = GetThreadContext(handle, &context) != FALSE;
      ResumeThread(handle);''', '''      context.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
      const bool ok = GetThreadContext(handle, &context) != FALSE;
      uint64_t caller_rva = 0; // NATIVE_MOM_PROFILE_CALLER_V1
      if (ok && g_module_count && module_of(uint64_t(context.Rip)) != 0) {
        CONTEXT c = context;
        for (int depth = 0; depth < 24; ++depth) {
          DWORD64 image = 0;
          PRUNTIME_FUNCTION fn = RtlLookupFunctionEntry(c.Rip, &image, nullptr);
          const DWORD64 previous_sp = c.Rsp;
          if (!fn) {
            DWORD64 ret = 0; SIZE_T read = 0;
            if (!ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<void*>(c.Rsp),
                                   &ret, sizeof(ret), &read) || read != sizeof(ret)) break;
            c.Rip = ret; c.Rsp += sizeof(ret);
          } else {
            PVOID handler = nullptr; DWORD64 frame = 0;
            RtlVirtualUnwind(UNW_FLAG_NHANDLER, image, c.Rip, fn, &c, &handler, &frame, nullptr);
          }
          if (!c.Rip || c.Rsp <= previous_sp || c.Rsp - previous_sp > 0x100000) break;
          if (module_of(uint64_t(c.Rip)) == 0) {
            caller_rva = uint64_t(c.Rip) - g_modules[0].base + 1; break;
          }
        }
      }
      ResumeThread(handle);'''),
('      sample.used = 1;\n', '      sample.used = 1;\n      sample.caller = caller_rva;\n'),
('role,module,rva,cpu_us\\n', 'role,module,rva,cpu_us,caller_rva\\n'),
('''std::fprintf(file, "%s,%u,%llu,%u\\n", role_name(sample.role),
                   unsigned(sample.module), (unsigned long long)sample.rva,
                   unsigned(sample.cpu_us));''', '''std::fprintf(file, "%s,%u,%llu,%u,%llu\\n", role_name(sample.role),
                   unsigned(sample.module), (unsigned long long)sample.rva,
                   unsigned(sample.cpu_us), (unsigned long long)sample.caller);'''),
]
tev_old = '  if (reg >= 0xe0 && reg <= 0xe7 && std::getenv("NWII_TEVTRACE")) {'
tev_new = '  static const bool native_tevtrace_cached = std::getenv("NWII_TEVTRACE") != nullptr; // NATIVE_MOM_TEVTRACE_CACHE_V1\n  if (reg >= 0xe0 && reg <= 0xe7 && native_tevtrace_cached) {'

def transform(path, changes):
    raw = path.read_bytes().decode('utf-8', 'surrogateescape')
    crlf = '\r\n' in raw
    content = raw.replace('\r\n', '\n')
    for old, new in changes:
        if '--restore' in sys.argv:
            old, new = new, old
        if new in content:
            continue
        if content.count(old) != 1:
            raise ValueError(f'{path}: expected one anchor: {old[:60]!r}')
        content = content.replace(old, new, 1)
    path.write_bytes((content.replace('\n', '\r\n') if crlf else content).encode('utf-8', 'surrogateescape'))

for tree in trees:
    transform(tree / 'include/runtime/native_cpu_profile.h', edits)
    transform(tree / 'src/hle/gx/renderer_gl.cpp', [(tev_old, tev_new)])
print('profile caller and TEV trace cache ' + ('removed' if '--restore' in sys.argv else 'installed'))
