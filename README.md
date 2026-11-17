<div align="center">
  <h1>Crash: Mind over Mutant</h1>
  <p>A static recompilation of the Wii version into a native Windows game.</p>
  <p>
    <a href="../../releases/latest"><img src="https://img.shields.io/badge/Download-Launcher-8053ba?style=for-the-badge&logo=windows11&logoColor=white" alt="Download the launcher"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/Licence-Non--commercial-555?style=for-the-badge" alt="Licence: non-commercial"></a>
  </p>
</div>

> [!IMPORTANT]
> The game is not included. You need your own Wii disc image of **Crash: Mind over Mutant**, PAL (`RC8P7D`).
> The launcher builds the game on your PC from your disc; no game code or disc assets are distributed.

The game's PowerPC code is translated to C++ with [NWiiRecomp](https://github.com/BlackLineInteractive/NWiiRecomp)
and compiled with a high-level Wii runtime. Launcher `0.1.2`. See [CHANGELOG.md](CHANGELOG.md).

| Release | Disc ID | Status |
| --- | --- | --- |
| PAL | `RC8P7D` | Supported |
| Other regions and revisions | — | Not supported |

The launcher checks the executable's SHA-256 against [the supported disc hash](data/main.dol.sha256).

## RecompLauncher

Use the Windows x64 ZIP with [RecompLauncher](https://recomplauncher.com), or unzip it and run `mom-launcher.exe` yourself. The installer remains available. Pick your PAL Wii image (RVZ, ISO, GCZ or WBFS); setup verifies and extracts it, then builds the native game with the bundled offline toolchain. The first build can take 15 minutes or longer. No game files are included.

RecompLauncher can change display, language and input settings, then start the game directly. Big Picture uses borderless fullscreen and the Classic gamepad layout for that session. Use MoMLauncher's Controls page for per-player remapping, and its Episodes and Save states pages for those features.

The same profile is shared with the normal launcher: `%APPDATA%/net.ghostesp.mom-launcher`. Settings are in `settings.json`, game saves in `profile/nand`, and windowless setup/play logs in `profile/logs` (current and previous run). Save states stay in `profile/savestates`; they are not included in RecompLauncher's automatic save backups. The ZIP uses this per-user profile too.

For scripts, `mom-launcher.exe --prepare --game "path/to/disc.rvz"` performs windowless setup; `--play --game "path/to/disc.rvz"` plays the prepared game and waits until it exits. Both accept `--data-dir "path/to/profile"` to use a separate profile. Run without these arguments to open the normal launcher.

## Features

- **Controls.** Per-player layouts, keyboard and mouse, gamepad pointer, motion and supported-controller gyro. Changes apply during play.
- **Button prompts.** Xbox, PlayStation and keyboard/mouse glyphs follow player 1's bindings. Automatic mode follows the last input device; original Wii prompts are also available.
- **Save states.** F1 snapshots the game; the Save states page resumes or deletes snapshots.
- **Area selection.** The Episodes page selects MoM's areas, L0–L9. Normal Play restores the original startup. These are area entry points, not story-mission checkpoints.
- **Soundtrack.** Play music and ambience from your disc and export tracks as WAV.
- **Display.** 1x–4x internal resolution, anisotropic filtering, borderless fullscreen, 16:9, six languages, custom frame limits and a persistent shader cache.
- **Discs.** ISO, WBFS, RVZ, WIA, CISO and GCZ. Disc images remain unchanged.
- **Local builds.** The installer bundles the source builder and toolchain. Saves, settings and shader caches survive rebuilds.

## Performance

The source builder applies the guest and runtime performance fixes, including entry-switch pruning, batched instruction counting,
GPU vertices, persistent uploads and PGO. Builder packs include [trained execution counters](reference/pgo/README.md); game code and assets still come from your disc.
The launcher requires a rebuild when those inputs change, so an older unprofiled build cannot be reused as the current release.

A comparison on a Ryzen 5 3600 and RTX 3060 used the same busy save, GPU vertices, persistent uploads and a 120 FPS cap:

| Build | Average FPS |
| --- | --- |
| Existing PGO development build | 87.7 |
| Packaged source builder with PGO | 89.1 |

Each 40-second run used two-second up/down strokes and discarded the first 12 FPS samples. These are single-run results;
performance varies by hardware and scene. Earlier source-builder runs measured roughly 75–79 FPS before the pipeline fixes, in separate tests.

## Requirements

| Component | Requirement |
| --- | --- |
| OS | Windows 10 or 11, 64-bit |
| CPU | x86-64-v3, including AVX2 and FMA |
| GPU | OpenGL 3.3 |
| Memory | 16 GB for building locally |
| Disk | 15 GB free for the build, plus your disc image and extracted files; SSD recommended |

## Quick start

1. Install `MoMLauncher-<version>-windows-x64-setup.exe` from [Releases](../../releases).
2. Choose your disc image and prepare the game files.
3. Select **Start build**. The first build takes tens of minutes.
4. Select **Play**.

Development installer candidates are available from manual runs of the [Release workflow](../../actions/workflows/release.yml).

Keys: **F1** save state, **F11** fullscreen. Saves, states, settings and shader caches are stored under
`%APPDATA%/net.ghostesp.mom-launcher/profile/`.

## Known limitations

Only the PAL disc above is qualified. CotT's camera smoothing, precise frame-pacing switch, compact-command switch and adaptive-batching
controls are not implemented in MoM. GPU vertex processing, batching and threaded rendering are already present.

Action prompts are replaced on the host GPU; motion illustrations and directional icons keep their original artwork.
Audio playback has been checked, but saving during an active mix and loading that new snapshot still needs validation.

<details>
<summary><b>Building from source</b></summary>

Needs Python 3.12+, Git, WinLibs GCC 16.1.0 POSIX/UCRT with CMake and Ninja, and Rust/Tauri 2 for the launcher.
Put `mingw64/bin` on PATH. See [vendor/README.md](vendor/README.md) for pinned dependencies.

```powershell
git clone https://github.com/BlackLineInteractive/NWiiRecomp.git vendor/NWiiRecomp
git -C vendor/NWiiRecomp checkout --detach 595f176f1d24cc54ff2e8389feed12d7fb553cc2
```

Extract your disc separately, with `<game>/sys/main.dol` present. Build the game in a new directory:

```powershell
python tools/clean_room_build.py --dest build/local --dol '<game>/sys/main.dol' --jobs 5
$env:NWII_GPU_VERTEX = '1'
$env:NWII_PERSISTENT_UPLOAD = '1'
build/local/storage/toolchain/CrashNative.exe '<game>' config.toml
```

The builder copies the required runtime DLLs alongside the executable. `--generate-only` stops after guest-source staging;
`--deps <directory>` uses local SDL2, toml++ and glad sources. No CotT or decompilation checkout is required.

Build a launcher installer, including the offline source builder:

```powershell
python tools/builder_revision.py
python tools/make_builder_pack.py --mingw '<winlibs>/mingw64'
python tools/check_builder_pack.py
cd launcher/src-tauri
cargo test --locked --release --lib
cargo tauri build --config tauri.bundle.conf.json
```

The [release workflow](.github/workflows/release.yml) follows the same path and verifies the installed pack.
Manual runs upload candidates. A tag matching the Cargo and Tauri version, such as `v0.1.0`, publishes a release;
a tag containing a hyphen is a prerelease. See [CONTRIBUTING.md](CONTRIBUTING.md) for source and builder updates.

</details>

## Credits and legal

Built on [NWiiRecomp](https://github.com/BlackLineInteractive/NWiiRecomp) by Volodymyr Vovchok / BlackLine Interactive,
with runtime and launcher work adapted from Crash of the Titans WiiComp and audio code adapted from
[Dolphin](https://github.com/dolphin-emu/dolphin). Third-party licences and provenance are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Unofficial fan project, not affiliated with or endorsed by any rights holder. Crash Bandicoot, Mind over Mutant and related names
and artwork belong to their owners. Non-commercial use only under [LICENSE](LICENSE), which does not cover game code, data,
generated guest code, disc images, saves or game artwork. Game title art is composed locally from the player's disc.
