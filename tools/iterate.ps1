# iterate.ps1 - one guarded port iteration: stages -> regenerate -> build -> boot -> compare.
#
#   powershell -File tools/iterate.ps1 -Label gap-round -WallClock -SingleVi
#   powershell -File tools/iterate.ps1 -Label mirrors -Stages -WallClock -SingleVi
#
# Everything that made earlier iterations waste time is handled here:
#   * the stale export\nWiiRuntime copy is removed before translating
#   * the build's exit code is checked, so a stale exe is never booted
#   * the boot log is saved under a label and compared with the previous label
#   * the exe age is printed, so "did my change actually get built" is visible
param(
    [Parameter(Mandatory = $true)][string]$Label,
    [switch]$Stages,          # re-apply tools/stages before regenerating
    [switch]$Gaps,            # recompile the ranges between function extents
    [switch]$SkipBuild,
    [switch]$Boot,
    [int]$Seconds = 60,
    [switch]$WallClock,
    [switch]$SingleVi,
    [int]$Sample = 0,         # NWII_SAMPLE: enables the runtime's sampled diagnostics
    [string]$ExtraEnv = "",   # e.g. "NWII_WATCH=0x80003000" (semicolon-separated)
    [string]$Compare = ""     # label to diff against; default = newest other log
)

# Deliberately not "Stop": native tools (cmake, ninja) write warnings to stderr,
# and with Stop PowerShell turns that into a terminating error before the exit
# code can be checked. Every command below is checked explicitly instead.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$logDir = Join-Path $root "logs"

function Assert-Ok([string]$what, [int]$code) {
    if ($code -ne 0) { throw "$what failed (exit $code)" }
}

if ($Stages) {
    Write-Host "applying stages" -ForegroundColor Cyan
    python tools\apply_stages.py
    if ($LASTEXITCODE -ne 0) { throw "apply_stages failed" }
}

if (-not $SkipBuild) {
    Write-Host "building recompiler" -ForegroundColor Cyan
    # Stage scripts edit the analyzer and the emitter; without this the old
    # wiirecomp.exe would regenerate the project and the change would silently
    # not apply (the gap step only rebuilds game_emit).
    cmake --build build/nwiirecomp --target nwiirecomp 2>&1 | Select-Object -Last 2
    if ($LASTEXITCODE -ne 0) { throw "recompiler build failed ($LASTEXITCODE)" }

    Write-Host "regenerating" -ForegroundColor Cyan
    if (Test-Path "export\nWiiRuntime") { Remove-Item "export\nWiiRuntime" -Recurse -Force }
    # The recompiler rewrites output_*.cpp but leaves gap files from an earlier
    # run behind; they would be stale and are no longer in the regenerated
    # CMakeLists, so clear them too.
    Get-ChildItem "export\output_gap_*.cpp" -ErrorAction SilentlyContinue | Remove-Item -Force
    & "build\nwiirecomp\nWiiRecomp\nwiirecomp.exe" recomp_config.toml
    if ($LASTEXITCODE -ne 0) { throw "recompiler failed ($LASTEXITCODE)" }

    if ($Gaps) {
        Write-Host "recompiling gap ranges" -ForegroundColor Cyan
        cmake --build build/nwiirecomp --target game_emit 2>&1 | Select-Object -Last 2
        if ($LASTEXITCODE -ne 0) { throw "game_emit build failed ($LASTEXITCODE)" }
        python tools\gap_functions.py
        if ($LASTEXITCODE -ne 0) { throw "gap stage failed ($LASTEXITCODE)" }
    }

    python tools/stages/fix_guest_compile_flags.py
    if ($LASTEXITCODE -ne 0) { throw "guest compile flags failed" }

    Write-Host "building" -ForegroundColor Cyan
    ninja -C export/build MindOverMutant
    if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE) - not booting" }
}

$exe = Get-Item "export\build\MindOverMutant.exe"
Write-Host ("exe built {0} ({1:N0} min ago)" -f $exe.LastWriteTime, ((Get-Date) - $exe.LastWriteTime).TotalMinutes) -ForegroundColor DarkGray

if (-not $Boot) { Write-Host "built only (-Boot to run)" -ForegroundColor Green; return }

$bootArgs = @("-File", "tools\boot_test.ps1", "-Seconds", "$Seconds")
if ($WallClock) { $bootArgs += "-WallClock" }
if ($SingleVi) { $bootArgs += "-SingleVi" }
if ($Sample -gt 0) { $bootArgs += @("-Sample", "$Sample") }
if ($ExtraEnv) {
    foreach ($pair in $ExtraEnv.Split(";")) {
        $kv = $pair.Split("=", 2)
        if ($kv.Count -eq 2) { Set-Item -Path ("env:" + $kv[0].Trim()) -Value $kv[1].Trim() }
    }
}
powershell -ExecutionPolicy Bypass @bootArgs

$saved = Join-Path $logDir "boot-$Label.log"
Copy-Item (Join-Path $logDir "boot.log") $saved -Force
Copy-Item (Join-Path $logDir "boot.err") (Join-Path $logDir "boot-$Label.err") -Force
Write-Host "saved $saved" -ForegroundColor Green

if (-not $Compare) {
    $previous = Get-ChildItem $logDir -Filter "boot-*.log" |
        Where-Object { $_.Name -ne "boot-$Label.log" -and $_.Name -ne "boot.log" } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($previous) { $Compare = $previous.FullName }
}
if ($Compare) {
    Write-Host "`n=== diff vs $Compare ===" -ForegroundColor Cyan
    python tools\boot_diff.py $Compare $saved --frontier 8
}
