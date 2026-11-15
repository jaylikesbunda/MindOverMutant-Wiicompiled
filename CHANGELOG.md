# Changelog

## Unreleased

- RecompLauncher setup, settings and direct play through opt-in windowless commands.
- Windows x64 ZIP releases with the offline builder and RecompLauncher manifest.
- Current and previous setup/play logs in the shared profile folder.

## [0.1.1] - 2026-11-14

- Removed the duplicate GPU vertex processing switch from launcher settings.

## [0.1.0] - 2026-11-14

- Native Windows build for the verified PAL Wii release, `RC8P7D`.
- Disc preparation, persistent settings, controller layouts, save-state browser,
  soundtrack playback/export, language and display settings in the Tauri launcher.
- MoM title art composed locally from the player's prepared disc.
- Live keyboard/mouse, Xbox and PlayStation action-button glyphs following player 1's bindings, with automatic device selection and an original Wii option.
- Area selection through MoM's own frontend, with reversible resource edits.
- Corrected launcher text encoding, including internal-resolution labels.
- GPU vertex transforms and texture generation, compact vertex uploads, streamed
  matrix uniforms, asynchronous audio mixing and a separate rendering thread.
- Isolated source builder and content-based builder identity, adapted from CotT.
- Source-only builder packs, manual installer candidates and verified tagged releases.

Camera glide and CotT's
live frame-pacing/graphics-batching controls are not supported in this release.
Only PAL is qualified. Audio playback has been checked; saving during an active
audio mix and then loading that new state still needs validation.
