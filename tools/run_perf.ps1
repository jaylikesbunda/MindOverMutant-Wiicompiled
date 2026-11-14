param(
  [string]$Prefix = 'perf',
  [int]$Seconds = 120,
  [string]$Presses = '20:0800,45:0C00,60:0C00,80:0C00,100:0800,115:0004,130:0800,145:0004',
  [switch]$NoCensus,
  [switch]$NoPhases
)
$ErrorActionPreference='Stop'
# A bounded, visible run with the performance instrumentation switched on.
#
# Derived from logs/run-sdk6.ps1: same environment, same synthetic press
# timeline (so the movie is skipped without a human), same bounded stop. The
# only additions are the measurement knobs, all of them new and opt-in:
#
#   NWII_PERF_CENSUS=1        the guest poll census (exact poll and
#                             instruction counts, and the per-poll-site table)
#   NWII_PERF_PC_HISTOGRAM=1  ... with the per-site histogram
#   NWII_PERF_CENSUS_FILE     the per-site table as CSV
#   NWII_PERF_PHASE=1         the frame/phase timeline
#   NWII_PERF_PHASE_FILE      the per-frame timeline as CSV
#
# The logs land in logs/boot-<prefix>.log; the tools that read them are
# tools/analyze_mom_perf.py and friends.
$env:NWII_WALLTB='1'
$env:NWII_SINGLE_VI='1'
$env:NWII_SAMPLE='1'
$env:NWII_TEXT_GUARD='1'
Remove-Item Env:NWII_WPAD_SCRIPT -ErrorAction SilentlyContinue
# The synthetic press timeline is delivered through the live command file, so
# the file has to be the one the runtime watches.
$env:NWII_WPAD_LIVE="logs/$Prefix-wpad.txt"
Set-Content "logs/$Prefix-wpad.txt" ''
$env:NWII_SCREENSHOT="logs/$Prefix-shot"
$env:NWII_EFB_PROBE="logs/$Prefix-probe"
$env:NWII_DRAW_ARCHIVE='1'
$env:NWII_EFB_PROBE_MAX='50'
$env:NWII_EFB_PROBE_ARM_FILE="logs/$Prefix-probe.arm"
$env:NWII_TEVTRACE='1'
$env:NWII_TRACE_CALLS='1'
$env:NWII_TRACE_ONLY='8047C0A4,8047941C'
$env:NWII_STARTUP_PROFILE='1'
$env:NWII_FONT_PROBE_AFTER='0'
if (-not $NoCensus) {
  $env:NWII_PERF_CENSUS='1'
  $env:NWII_PERF_PC_HISTOGRAM='1'
  $env:NWII_PERF_CENSUS_FILE="logs/$Prefix.pcs.csv"
  $env:NWII_PERF_CENSUS_PERIOD='5'
}
if (-not $NoPhases) {
  $env:NWII_PERF_PHASE='1'
  $env:NWII_PERF_PHASE_FILE="logs/$Prefix.frames.csv"
  $env:NWII_PERF_PHASE_PERIOD='5'
}
Set-Content "logs/$Prefix-probe.arm" 'start 0'
$momProcess=Start-Process -FilePath 'export/build/MindOverMutant.exe' -ArgumentList 'dump','config.toml' -WindowStyle Normal -PassThru -RedirectStandardOutput "logs/boot-$Prefix.log" -RedirectStandardError "logs/boot-$Prefix.err"
Set-Content "logs/$Prefix.pid" $momProcess.Id
Write-Output "MoM PID $($momProcess.Id) prefix=$Prefix bounded $Seconds-second run (census=$(-not $NoCensus) phases=$(-not $NoPhases))."
$plan = @()
foreach ($p in $Presses.Split(',')) {
  if (-not $p.Trim()) { continue }
  $kv = $p.Split(':')
  $plan += [pscustomobject]@{ At = [double]$kv[0]; Mask = $kv[1] }
}
$momTimer=[Diagnostics.Stopwatch]::StartNew()
$sent = 0
while (!$momProcess.HasExited -and $momTimer.Elapsed.TotalSeconds -lt $Seconds) {
    while ($sent -lt $plan.Count -and $plan[$sent].At -le $momTimer.Elapsed.TotalSeconds) {
      Add-Content "logs/$Prefix-wpad.txt" ("$($plan[$sent].Mask) 250")
      Write-Output ("t={0:N0}s press {1}" -f $plan[$sent].At, $plan[$sent].Mask)
      $sent++
    }
    Start-Sleep -Milliseconds 500
    $momProcess.Refresh()
}
if (!$momProcess.HasExited) { Stop-Process -Id $momProcess.Id; Write-Output 'Stopped owned MoM at test limit.' }
Write-Output "done $Prefix"
Write-Output "log: logs/boot-$Prefix.log"
