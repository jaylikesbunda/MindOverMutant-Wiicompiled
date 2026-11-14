r"""Install a training-only gcov dump hook in the runtime main loop, compiled only when NWII_PGO_HOOK is defined.

Marker: NATIVE_MOM_PGO_DUMP_HOOK_V1. Used by tools/pgo_train_mom.py on an instrumented (pgo_mom.py gen) build.

Why a macro and not a weak reference: a normal build does not link libgcov, so __gcov_dump does not exist. A weak
undefined reference would avoid the link error only if MinGW-w64 on PE/COFF resolves it to null; that was not verified
here (no build was run), so the hook is never compiled unless the build defines NWII_PGO_HOOK. Normal builds therefore
contain no hook code at all, not even the getenv/stat poll.

With NWII_PGO_HOOK defined, every 32 main-loop iterations the runtime checks env NWII_PGO_DUMP_TRIGGER. If that file
exists it is removed, __gcov_dump() writes the profile now, and <trigger>.done is created as a completion marker.
The define is added to the MindOverMutant target by pgo_mom.py gen (see logs/agent-reports/pgo_training.md).

Edits both vendor/NWiiRecomp/nWiiRuntime and export/nWiiRuntime, src/core/main.cpp. Idempotent; --restore is the
exact inverse. Re-run after regenerating export/.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = 'NATIVE_MOM_PGO_DUMP_HOOK_V1'
ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / 'vendor/NWiiRecomp/nWiiRuntime/src/core/main.cpp',
         ROOT / 'export/nWiiRuntime/src/core/main.cpp']

DECL_OLD = '#include <glad/glad.h>\n'
DECL_NEW = DECL_OLD + (
    '#ifdef NWII_PGO_HOOK // ' + MARK + ': training-only gcov dump, see tools/pgo_train_mom.py\n'
    '#include <cstdlib>\n'
    '#include <filesystem>\n'
    '#include <fstream>\n'
    '#include <iostream>\n'
    '#include <system_error>\n'
    'extern "C" void __gcov_dump(void);\n'
    '#endif\n')

UPD_OLD = ('      nwii::runtime::input::InputManager::get().update();\n'
           '      { std::string title;')
UPD_NEW = ('      nwii::runtime::input::InputManager::get().update();\n'
           '#ifdef NWII_PGO_HOOK // ' + MARK + '\n'
           '      {\n'
           '        // Every 32 frames: if NWII_PGO_DUMP_TRIGGER exists, consume it, write the gcov profile, then mark done.\n'
           '        static unsigned pgo_tick = 0;\n'
           '        if ((++pgo_tick & 31u) == 0) {\n'
           '          const char* trigger = std::getenv("NWII_PGO_DUMP_TRIGGER");\n'
           '          std::error_code ec;\n'
           '          if (trigger && *trigger && std::filesystem::exists(trigger, ec) && std::filesystem::remove(trigger, ec)) {\n'
           '            __gcov_dump();\n'
           '            { std::ofstream done(std::string(trigger) + ".done"); done << "dumped\\n"; }\n'
           '            std::cout << "[PGO] dumped" << std::endl;\n'
           '          }\n'
           '        }\n'
           '      }\n'
           '#endif\n'
           '      { std::string title;')

PAIRS = [(DECL_OLD, DECL_NEW), (UPD_OLD, UPD_NEW)]


def convert(path: Path, restore: bool) -> bool:
    raw = path.read_bytes().decode('utf-8', errors='surrogateescape')
    crlf = '\r\n' in raw
    t = raw.replace('\r\n', '\n')
    if (MARK in t) != restore:
        return False
    for old, new in PAIRS:
        src, dst = (new, old) if restore else (old, new)
        if t.count(src) != 1:
            print('fix_pgo_dump_hook_mom: anchor not found exactly once in ' + str(path))
            sys.exit(1)
        t = t.replace(src, dst, 1)
    path.write_bytes((t.replace('\n', '\r\n') if crlf else t).encode('utf-8', errors='surrogateescape'))
    return True


def main():
    restore = '--restore' in sys.argv
    changed = sum(1 for path in FILES if convert(path, restore))
    print('PGO dump hook %s: %d file(s) changed.' % ('removed' if restore else 'installed', changed))


if __name__ == '__main__':
    main()
