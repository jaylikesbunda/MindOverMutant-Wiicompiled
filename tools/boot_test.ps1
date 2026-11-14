# boot_test.ps1 - run the port headless for a bounded time and show the log.
#
#   powershell -File tools\boot_test.ps1
#   powershell -File tools\boot_test.ps1 -Seconds 180 -TraceCalls
#
# The runtime writes its HLE/MMIO/PC trace to stdout, which is the only useful
# signal before the port has a window. A run still alive at the deadline is killed
# and reported as alive. The NWII_* switches mirror the probe variables the Crash
# of the Titans launch harness (native-port/tools/run_startup.py) exposes.
param(
    [int]$Seconds = 60,
    [string]$Config = "config.toml",
    [string]$Log = "logs\boot.log",
    [switch]$TraceCalls,          # NWII_TRACE_CALLS: log guest function calls
    [switch]$WallClock,           # NWII_WALLTB: drive the timebase from the host clock
    [switch]$SingleVi,            # NWII_SINGLE_VI: one VI interrupt source (needs -WallClock)
    [int]$Sample = 0,             # NWII_SAMPLE: periodic perf sample interval (ms)
    [string]$StartupProfile = "", # NWII_STARTUP_PROFILE: profile the boot sequence
    [switch]$FrameTrace           # NWII_FRAME_TRACE: per-frame timing trace
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$exe = "export\build\MindOverMutant.exe"
if (-not (Test-Path $exe)) { throw "$exe not built yet - see README.md" }
$built = (Get-Item $exe).LastWriteTime
$age = [int]((Get-Date) - $built).TotalMinutes
Write-Host ("exe: {0} (built {1:N0} min ago; stale if a build failed quietly)" -f $built, $age) -ForegroundColor DarkGray
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null

$env:NWII_HEADLESS = "1"
if ($TraceCalls) { $env:NWII_TRACE_CALLS = "1" }
if ($WallClock) { $env:NWII_WALLTB = "1" }
if ($SingleVi) { $env:NWII_SINGLE_VI = "1" }
if ($Sample -gt 0) { $env:NWII_SAMPLE = "$Sample" }
if ($StartupProfile) { $env:NWII_STARTUP_PROFILE = $StartupProfile }
if ($FrameTrace) { $env:NWII_FRAME_TRACE = "1" }

Write-Host "probes: " + ((Get-ChildItem env: | Where-Object { $_.Name -like 'NWII_*' } |
    ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' ') -ForegroundColor DarkGray

$errLog = $Log -replace '\.log$', '.err'
$watch = [Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath $exe -ArgumentList "dump", $Config `
    -RedirectStandardOutput $Log -RedirectStandardError $errLog `
    -PassThru -NoNewWindow

if (-not $proc.WaitForExit($Seconds * 1000)) {
    $proc.Kill()
    Write-Host ("still running after {0}s - killed (alive is a good sign)" -f $Seconds) -ForegroundColor Green
} else {
    Write-Host ("exited after {0:N1}s with code {1} (0x{1:X8})" -f `
        $watch.Elapsed.TotalSeconds, $proc.ExitCode) -ForegroundColor Yellow
}

Write-Host "`n--- $Log (tail) ---"
Get-Content $Log -Tail 40 -ErrorAction SilentlyContinue
$err = Get-Content $errLog -Tail 10 -ErrorAction SilentlyContinue
if ($err) { Write-Host "`n--- $errLog (tail) ---"; $err }
