"""Compile the generated dispatcher at -O2 instead of -O0 (CotT's fix_dispatcher_optimization).

``main_output.cpp`` carries

    #if defined(__GNUC__) || defined(__clang__)
    #pragma GCC optimize ("O0")
    #endif
    #if defined(__clang__)
    #pragma clang optimize off
    #endif

at file scope, so everything defined after it - ``find_func_bound``,
``find_dispatch_target`` and ``run_game`` itself - was compiled without
optimisation, even though ``export/CMakeLists.txt`` asks for -O2 on this
translation unit.  Those three functions are the guest's per-call path: every
guest ``bl``/``blr`` that is not a straight-line goto runs through them.

The pragma only affects code generation, not semantics: the block is ordinary
C++ with no undefined behaviour (uint32_t arithmetic, no aliasing, no signed
overflow), and the rest of the port - the runtime and all 70 guest chunks - is
already built with optimisation.  This stage therefore changes no guest-visible
behaviour; it only lets the compiler do to the dispatcher what it already does
to everything else.

CotT retired the same override (``tools/fix_dispatcher_optimization.py``) after
qualifying the dispatcher's return fixtures at -O0 and -O2: identical exit code
and identical dispatch state hash.  MoM has no such fixture, so the evidence
here is the build plus the instrumented run (dispatch behaviour is covered by
the [Sample]/[INTERP] counters and by the game reaching the same scenes).

Idempotent: the marker replaces the block in one edit and is a no-op after.
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKER = 'NATIVE_MOM_DISPATCHER_O2_V1'
OLD = ('#if defined(__GNUC__) || defined(__clang__)\n'
       '#pragma GCC optimize ("O0")\n'
       '#endif\n'
       '#if defined(__clang__)\n'
       '#pragma clang optimize off\n'
       '#endif\n')
NEW = ('// ' + MARKER + ': the dispatcher is the guest\'s per-call hot path; it was the\n'
       '// only generated translation unit compiled at -O0 (see the stage docstring).\n'
       '#if defined(__GNUC__) || defined(__clang__)\n'
       '#pragma GCC optimize ("O2")\n'
       '#endif\n'
       '#if defined(__clang__)\n'
       '#pragma clang optimize on\n'
       '#endif\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=None)
    args = parser.parse_args()

    root = args.root
    if root is None:
        root = Path(__file__).resolve().parents[1]  # REPO_ROOT_FALLBACK
        if not (root / 'vendor').is_dir():
            root = root.parent
    path = root / 'export' / 'main_output.cpp'
    if not path.is_file():
        print('Export not generated; dispatcher -O2 deferred.')
        return 0
    text = path.read_text(errors='surrogateescape')
    if MARKER in text:
        print('Dispatcher already compiled at -O2.')
        return 0
    if text.count(OLD) != 1:
        print('Dispatcher -O0 pragma block not found exactly once (%d); left unchanged.'
              % text.count(OLD))
        return 1
    path.write_text(text.replace(OLD, NEW, 1), errors='surrogateescape')
    print('Generated dispatcher compiled at -O2 instead of -O0.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
