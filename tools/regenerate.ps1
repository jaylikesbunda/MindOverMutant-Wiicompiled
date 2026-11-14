# regenerate.ps1 - deterministic regeneration of the recompiled project.
#
#   powershell -File tools/regenerate.ps1
#   powershell -File tools/regenerate.ps1 -Boot -WallClock -SingleVi
#
# The recompiler copies the runtime into export/nWiiRuntime but never prunes it,
# so a header deleted from the vendored tree survives regeneration and breaks the
# build in a confusing way. This always removes that copy first, then translates,
# configures if needed, and builds only the game target (the static-SDL2 link
# path; see README.md for why plain `ninja` fails).
param(
    [switch]$Boot,
    [int]$Seconds = 60,
    [switch]$WallClock,
    [switch]$SingleVi
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$recompiler = "build\nwiirecomp\nWiiRecomp\nwiirecomp.exe"
if (-not (Test-Path $recompiler)) { throw "$recompiler missing - build it first (README.md)" }
if (-not (Test-Path "orig\main.dol") -and -not (Test-Path "dump\sys\main.dol")) {
    throw "no executable found: need dump/sys/main.dol"
}

Write-Host "clearing export\nWiiRuntime (the recompiler does not prune)" -ForegroundColor DarkGray
if (Test-Path "export\nWiiRuntime") { Remove-Item "export\nWiiRuntime" -Recurse -Force }

Write-Host "translating" -ForegroundColor Cyan
& $recompiler recomp_config.toml
if ($LASTEXITCODE -ne 0) { throw "recompiler failed ($LASTEXITCODE)" }

if (-not (Test-Path "export\build\CMakeCache.txt")) {
    Write-Host "configuring (first run fetches SDL2, toml++ and glad)" -ForegroundColor Cyan
    cmake -S export -B export/build -G Ninja -DCMAKE_BUILD_TYPE=Release '-DCMAKE_POLICY_VERSION_MINIMUM=3.5'
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed ($LASTEXITCODE)" }
}

Write-Host "building MindOverMutant" -ForegroundColor Cyan
ninja -C export/build MindOverMutant
if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE) - not booting a stale exe" }

Write-Host "built export\build\MindOverMutant.exe" -ForegroundColor Green

if ($Boot) {
    $bootArgs = @("-File", "tools\boot_test.ps1", "-Seconds", "$Seconds")
    if ($WallClock) { $bootArgs += "-WallClock" }
    if ($SingleVi) { $bootArgs += "-SingleVi" }
    powershell -ExecutionPolicy Bypass @bootArgs
}
