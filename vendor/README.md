# Upstream sources

Upstream checkouts are local and ignored. The source builder requires this revision:

| Dependency | Path | Pin |
| --- | --- | --- |
| [NWiiRecomp](https://github.com/BlackLineInteractive/NWiiRecomp) | `vendor/NWiiRecomp` | `595f176f1d24cc54ff2e8389feed12d7fb553cc2` |

```powershell
git clone https://github.com/BlackLineInteractive/NWiiRecomp.git vendor/NWiiRecomp
git -C vendor/NWiiRecomp checkout --detach 595f176f1d24cc54ff2e8389feed12d7fb553cc2
```

`tools/clean_room_build.py` copies the pinned source into an isolated workspace and applies
`patches/mom-release-runtime.patch`. Developer edits in the vendor checkout are never copied into a release pack:
the pack starts from a Git archive of the pin and carries `PINNED_COMMIT` in place of Git metadata.

| Offline dependency | Version |
| --- | --- |
| SDL2 | `release-2.28.5` |
| toml++ | `v3.4.0` |
| glad | `v0.1.36` |

The source builder fetches these when needed. Builder packs include them with Python, CMake, Ninja and WinLibs GCC 16.1.0
POSIX/UCRT. The release workflow checks the WinLibs archive's SHA-256 before packaging it.

Dolphin audio reference sources and their notices are kept in `reference/dolphin-ax/`.
