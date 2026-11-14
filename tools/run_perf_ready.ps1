# run_perf_ready.ps1 - bounded, measured MoM run for the perf comparison.
#
#   powershell -File tools/run_perf_ready.ps1 -Label base-a -Variant baseline -Seconds 180 -Fps 50
#   powershell -File tools/run_perf_ready.ps1 -Label cand-a -Variant candidate -CpuProfile
#   powershell -File tools/run_perf_ready.ps1 -Label cand-a -DryRun
#
# The game takes its dump directory as argv[1] and config.toml as argv[2]
# (main.cpp usage).  -Variant baseline runs the snapshot at
# logs/perf-ready/baseline-MindOverMutant.exe; its child PATH is prepended
# with export/build (the dir it was built in, holding the side-by-side DLLs)
# and the MinGW runtime dir - the child's PATH only, never the caller's.
#
# One run = one unique label: the manifest file is created with CreateNew
# BEFORE the process starts, so a reused label fails fast and nothing is
# ever overwritten.  Only the run's own process is terminated at -Seconds.
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$Label,
    [ValidateRange(20, 86400)]
    [int]$Seconds = 180,
    [ValidateRange(10, 1000)]
    [int]$Fps = 50,
    [ValidateSet('candidate', 'baseline')]
    [string]$Variant = 'candidate',
    [switch]$CpuProfile,
    [switch]$FrameTrace,
    [switch]$ColdCache,
    [switch]$GuestFrameGate,
    [switch]$DryRun,
    [string]$ExtraEnv = ''    # e.g. "NWII_POLL_CREDIT=1;NWII_NO_GPU_VERTEX=1" - recorded in the manifest
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$outdir = Join-Path $root 'logs/perf-ready'
New-Item -ItemType Directory -Force -Path $outdir | Out-Null

$buildDir = Join-Path $root 'export/build'
$mingwBin = 'C:/Users/deki/AppData/Local/Microsoft/WinGet/Packages/BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/mingw64/bin'

$exe = if ($Variant -eq 'baseline') {
    Join-Path $outdir 'baseline-MindOverMutant.exe'
} else {
    Join-Path $buildDir 'MindOverMutant.exe'
}
if (-not (Test-Path $exe)) { Write-Error "exe not found: $exe"; exit 2 }
if ($GuestFrameGate -and $Variant -eq 'baseline') {
    Write-Error 'the saved baseline does not implement the guest frame-gate override'; exit 2
}
if ($FrameTrace -and $Variant -eq 'baseline') {
    Write-Error 'the detailed renderer frame trace requires the candidate'; exit 2
}
if ($Variant -eq 'baseline') {
    Write-Host 'NOTE: baseline exe predates the new stages; do not pass flags it does not implement.'
}

# Unique outputs - the manifest reservation below is the atomic check.
$phaseCsv = Join-Path $outdir "$Label-phase.csv"
$manifest = Join-Path $outdir "$Label-manifest.json"
$results  = Join-Path $outdir "$Label-results.json"
$runlog   = Join-Path $outdir "$Label-run.log"
$cpuCsv   = Join-Path $outdir "$Label-cpuprof.csv"
$configSnapshot = Join-Path $outdir "$Label-config.toml"
$tracePrefix = Join-Path $outdir "$Label-frame-trace"
$labelFiles = @($phaseCsv, $manifest, $results, $runlog, "$runlog.err", $cpuCsv, $configSnapshot)
if ($FrameTrace) {
    $labelFiles += @("$tracePrefix.json", "$tracePrefix.done", "$tracePrefix.arm", "$tracePrefix.stop")
}
foreach ($f in $labelFiles) {
    if (Test-Path $f) { Write-Error "label '$Label' already used ($f exists)"; exit 2 }
}
if ($Variant -eq 'candidate') {
    $builtAt = (Get-Item $exe).LastWriteTimeUtc
    $inputs = @(Get-ChildItem (Join-Path $root 'export/nWiiRuntime') -Recurse -File |
        Where-Object { $_.Extension -in @('.h', '.hpp', '.c', '.cpp', '.inc') })
    $inputs += @(Get-ChildItem (Join-Path $root 'export') -File |
        Where-Object { $_.Extension -in @('.h', '.cpp') -or $_.Name -eq 'CMakeLists.txt' })
    $newer = $inputs | Where-Object { $_.LastWriteTimeUtc -gt $builtAt } | Select-Object -First 1
    if ($newer) { Write-Error "candidate is stale; rebuild before testing ($($newer.FullName))"; exit 2 }
}
try {
    $configBytes = [System.IO.File]::ReadAllBytes((Join-Path $root 'config.toml'))
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    $configHash = ([BitConverter]::ToString($hasher.ComputeHash($configBytes))).Replace('-', '')
    $hasher.Dispose()
} catch { Write-Error "cannot snapshot config.toml: $_"; exit 2 }

$cacheDir = Join-Path $outdir "cache-$Variant"
if ($ColdCache) {
    $cacheDir = Join-Path $outdir ("cache-cold-{0}-{1}" -f $Label, [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
}
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null

# Child-only environment via ProcessStartInfo: nothing in the caller's
# PowerShell session is mutated.  Every inherited NWII_* is removed from the
# child's environment block; the measurement recipe is then set explicitly.
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $exe
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.Arguments = '"dump" "{0}"' -f $configSnapshot

foreach ($key in @($psi.EnvironmentVariables.Keys)) {
    if ($key -like 'NWII_*') { $psi.EnvironmentVariables.Remove($key) }
}
$psi.EnvironmentVariables['NWII_WALLTB'] = '1'
$psi.EnvironmentVariables['NWII_SINGLE_VI'] = '1'
$psi.EnvironmentVariables['NWII_FPS_LIMIT'] = "$Fps"
# Keep the original guest update threshold for the first CPU/render A/B.
# Changing that threshold is a separate experiment, not a throughput gain.
if (-not $GuestFrameGate) { $psi.EnvironmentVariables['NWII_NO_GUEST_FRAME_LIMIT'] = '1' }
$psi.EnvironmentVariables['NWII_FPS_LOG'] = '1'
$psi.EnvironmentVariables['NWII_NO_DIAG'] = '1'
$psi.EnvironmentVariables['NWII_PERF_PHASE'] = '1'
$psi.EnvironmentVariables['NWII_PERF_PHASE_PERIOD'] = '3'
$psi.EnvironmentVariables['NWII_PERF_PHASE_FILE'] = $phaseCsv
$psi.EnvironmentVariables['NWII_PROGRAM_CACHE_DIR'] = $cacheDir
if ($FrameTrace) {
    $psi.EnvironmentVariables['NWII_FRAME_TRACE'] = $tracePrefix
    Write-Host "DIAGNOSTIC frame trace: arm $tracePrefix.arm at the affected scene."
    Write-Host 'Do not treat the armed capture interval as an uninstrumented FPS comparison.'
}
if ($CpuProfile) {
    $psi.EnvironmentVariables['NWII_CPU_PROFILE'] = '1'
    $psi.EnvironmentVariables['NWII_CPU_PROFILE_DELAY'] = '15'
    $psi.EnvironmentVariables['NWII_CPU_PROFILE_SECONDS'] = "$([Math]::Max(1, $Seconds - 20))"
    $psi.EnvironmentVariables['NWII_CPU_PROFILE_HZ'] = '200'
    $psi.EnvironmentVariables['NWII_CPU_PROFILE_FILE'] = $cpuCsv
}
foreach ($pair in ($ExtraEnv -split ';')) {
    if (-not $pair.Trim()) { continue }
    if ($pair -notmatch '^NWII_[A-Z0-9_]+=') { Write-Error "invalid ExtraEnv entry: $pair"; exit 2 }
    $k, $v = $pair -split '=', 2
    if ($k -match '^NWII_(FPS_LIMIT|WALLTB|SINGLE_VI|NO_DIAG|FPS_LOG|PERF_PHASE.*|PROGRAM_CACHE_DIR|CPU_PROFILE.*)$') {
        Write-Error "$k is controlled by the test preset"; exit 2
    }
    if ($k -match '^NWII_(.*TRACE.*|.*PROBE.*|DRAW_ARCHIVE|DRAWLOG|SKINDBG|SAMPLE|PERF_CENSUS.*|PERF_PC_HISTOGRAM|STARTUP_PROFILE)$') {
        Write-Error "$k distorts this performance preset; use a diagnostic launcher instead"; exit 2
    }
    if ($k -eq 'NWII_MOM_TRIM') { Write-Error 'NWII_MOM_TRIM is a build-time stage selector, not a runtime fallback'; exit 2 }
    $psi.EnvironmentVariables[$k] = $v
}
# Side-by-side DLLs for the relocated exe (baseline) and the MinGW runtime.
$childPath = $psi.EnvironmentVariables['PATH']
$psi.EnvironmentVariables['PATH'] = "$buildDir;$mingwBin;$childPath"

$childEnv = @{}
foreach ($k in $psi.EnvironmentVariables.Keys) {
    if ($k -like 'NWII_*' -or $k -eq 'PATH') { $childEnv[$k] = $psi.EnvironmentVariables[$k] }
}
$exeHash = (Get-FileHash $exe -Algorithm SHA256).Hash
$started = Get-Date

# Manifest BEFORE start: CreateNew is the atomic label reservation; it also
# records a startup failure instead of leaving a false-success result.
$manifestData = @{
    label       = $Label
    variant     = $Variant
    exe         = $exe
    exe_sha256  = $exeHash
    exe_written = (Get-Item $exe).LastWriteTimeUtc.ToString('o')
    config_path = $configSnapshot
    config_sha256 = $configHash
    seconds_requested = $Seconds
    fps         = $Fps
    guest_frame_gate = [bool]$GuestFrameGate
    cpu_profile = [bool]$CpuProfile
    diagnostic_frame_trace = [bool]$FrameTrace
    frame_trace_prefix = $psi.EnvironmentVariables['NWII_FRAME_TRACE']
    cold_cache  = [bool]$ColdCache
    cache_dir   = $cacheDir
    child_env   = $childEnv
    command     = "$exe $($psi.Arguments)"
    started_utc = $started.ToUniversalTime().ToString('o')
    dry_run     = [bool]$DryRun
}
try {
    $fs = [System.IO.File]::Open($manifest, 'CreateNew')
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($manifestData | ConvertTo-Json -Depth 4))
    $fs.Write($bytes, 0, $bytes.Length); $fs.Close()
    $fs = [System.IO.File]::Open($configSnapshot, 'CreateNew')
    $fs.Write($configBytes, 0, $configBytes.Length); $fs.Close()
} catch {
    if ($fs) { $fs.Dispose() }
    Write-Error "cannot reserve label '$Label' and its config snapshot: $_"; exit 2
}

if ($DryRun) {
    Write-Host "DRY RUN - would launch:"
    Write-Host "  exe:      $exe"
    Write-Host "  args:     $($psi.Arguments)"
    Write-Host "  workdir:  $root"
    Write-Host "  manifest: $manifest"
    $psi.EnvironmentVariables.GetEnumerator() | Where-Object { $_.Key -like 'NWII_*' } |
        ForEach-Object { Write-Host ("  env {0}={1}" -f $_.Key, $_.Value) }
    Write-Host "  child PATH head: $buildDir;$mingwBin"
    exit 0
}

$forced = $false
$runError = $null
$proc = $null
$outFs = $null
$errFs = $null
$outTask = $null
$errTask = $null
try {
    $outFs = [System.IO.File]::Open($runlog, 'CreateNew')
    $errFs = [System.IO.File]::Open("$runlog.err", 'CreateNew')
    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc) { throw 'process failed to start' }
    $outTask = $proc.StandardOutput.BaseStream.CopyToAsync($outFs)
    $errTask = $proc.StandardError.BaseStream.CopyToAsync($errFs)
    if (-not $proc.WaitForExit($Seconds * 1000)) {
        $forced = $true
        # Only terminate the process created by this invocation.
        $proc.Kill()
        $proc.WaitForExit(5000) | Out-Null
    }
} catch {
    $runError = $_.Exception.Message
} finally {
    if ($proc -and -not $proc.HasExited) {
        try { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null } catch {}
        $forced = $true
    }
    foreach ($task in @($outTask, $errTask)) {
        if ($task) {
            try {
                if (-not $task.Wait(5000)) { throw 'log drain timed out' }
            } catch {
                if (-not $runError) { $runError = $_.Exception.Message }
            }
        }
    }
    if ($outFs) { $outFs.Dispose() }
    if ($errFs) { $errFs.Dispose() }
}
$ended = Get-Date
$exitCode = if ($proc -and $proc.HasExited) { $proc.ExitCode } else { $null }

@{
    label       = $Label
    variant     = $Variant
    ended_utc   = $ended.ToUniversalTime().ToString('o')
    exit_code   = $exitCode
    error       = $runError
    forced_kill = $forced
    note        = $(if ($forced) { 'terminated at the deadline; cache writer flush after a forced kill is not promised' } else { 'natural exit' })
    phase_csv   = $phaseCsv
    run_log     = $runlog
} | ConvertTo-Json -Depth 4 | Set-Content $results -Encoding UTF8

Write-Host "manifest: $manifest"
Write-Host "results:  $results"
Write-Host "run log:  $runlog"
Write-Host "phase:    $phaseCsv"
if ($runError -or (-not $forced -and $exitCode -ne 0)) { exit 1 }
exit 0
