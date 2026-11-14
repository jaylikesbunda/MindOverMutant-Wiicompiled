# Contributing

Use Python 3.12+, Git, WinLibs GCC 16 POSIX/UCRT with CMake and Ninja, and Rust/Tauri 2.
Use your own supported PAL disc. Do not commit disc data, translated guest code, executables,
saves, profiles, build output, experiment reports or handoff notes.

## Source changes

Keep changes focused and preserve unrelated work. Stage explicit paths. During development,
runtime changes belong in both `vendor/NWiiRecomp/nWiiRuntime` and `export/nWiiRuntime`.
Release builds use the source patch against the pinned upstream revision, not either working tree.

After changing runtime or recompiler source:

```powershell
python tools/refresh_release_sources.py
python tools/builder_revision.py --write
python tools/check_release.py
```

Changes under `tools/`, `overlays/` and `reference/` also require an updated builder identity.
Bump its date-serial revision for a new distributed builder. Hashes normalize text line endings;
keep source files in UTF-8 with LF endings.

## Checks

```powershell
python tools/builder_revision.py
python tools/check_release.py
python -m compileall -q tools launcher/scripts
python -m unittest discover -s tools/tests -p test_builder_execution.py
python tools/check_live_prompts_mom.py --compiler '<winlibs>/mingw64/bin/g++.exe'
cargo test --manifest-path launcher/src-tauri/Cargo.toml --locked --lib -- --skip packaged_builder_matches_declared_inputs
```

Set `MOM_GAME_DIR` to a prepared game's directory to include real-disc font, logo and area-script checks.
`MOM_EPISODE_SCRATCH` enables archive-edit tests and must point to a throwaway copy of the game.

Validate a changed generation pipeline with `tools/clean_room_build.py` in a fresh destination.
A release must build without a sibling CotT or decompilation checkout.

Batch performance changes and use one gameplay run per batch. Use alternating two-second up/down
strokes; walking left reaches a wall and inflates FPS. Record the scene, build, options and whether
comparisons were paired. Keep captures and reports in ignored local directories.

## Releases

Keep `launcher/src-tauri/Cargo.toml` and `tauri.conf.json` at the same version and update `CHANGELOG.md`.
Refresh an existing local builder pack with `python launcher/scripts/refresh_builder.py`, or assemble a
new one with `tools/make_builder_pack.py`. Run `tools/check_builder_pack.py` before bundling.

The manual Release workflow uploads an installer, source archive and SHA-256 checksums. A matching
`v<version>` tag publishes those files after the installer has been installed and checked on the runner.
Tags containing a hyphen are prereleases. Do not include a translated game runtime in an installer.
