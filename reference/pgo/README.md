# Release optimisation profile

`mom-pal.json` contains GCC 16.1 execution counters from seven player-supplied
MoM PAL save states. Training alternates up/down in two-second strokes. It
contains numeric compiler metadata and execution counts, with source hashes;
it contains no game instructions, translated source, executable code, disc
assets or save states.

The source builder recreates GCC's local counter files from this JSON and uses
them for every generated guest translation unit. Stable `#line` filenames make
the profiles usable from different install folders, including the inline
runtime headers. Instrumentation is limited to those filenames, so compiler
header locations do not enter the profile. Source hashes, the pinned compiler version and GCC's control-flow
checks reject stale or incompatible profiles instead of silently dropping PGO.

To update the profile, generate and build an isolated instrumented workspace
with `tools/clean_room_build.py --pgo gen`, then run `tools/pgo_train_mom.py`
with its executable and build directory. Package the resulting counters with
`tools/release_pgo.py package --export <generated-source> --build <build-dir>`.
Regenerate `builder-revision.json`, refresh the builder pack and verify a
profile-use build in a different workspace before publishing.
