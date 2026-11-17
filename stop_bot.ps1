#Requires -Version 5.1
<#
  Stops the ORB bot the safe way and checks that it has really stopped:
    1. writes the STOP file (the same as the dashboard's Stop button); the bot finishes its current step,
       saves today's journal and exits (in live mode open positions keep their exchange stop-loss);
    2. waits up to 30 seconds for the bot's single-instance lock to be released.
  It never kills the bot. The dashboard keeps running. Also works after Stop on the "Kite ORB Bot" task.
      powershell -ExecutionPolicy Bypass -File .\stop_bot.ps1
#>
Set-Location -Path $PSScriptRoot

function Test-BotRunning {
    $m = $null
    $running = [System.Threading.Mutex]::TryOpenExisting("Local\AyodhyaORB_bot", [ref]$m)
    if ($m) { $m.Dispose() }
    return $running
}

if (-not (Test-BotRunning)) { Write-Host "[i] Ayodhya ORB is not running."; exit 0 }

$data = if ($env:DATA_DIR) { $env:DATA_DIR } else { Join-Path $PSScriptRoot "data" }
New-Item -ItemType Directory -Force -Path $data | Out-Null
New-Item -ItemType File -Force -Path (Join-Path $data "STOP") | Out-Null
Write-Host "[i] Asked Ayodhya ORB to stop (STOP file). Waiting for it to finish..."

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (-not (Test-BotRunning)) {
        Write-Host "[ok] Ayodhya ORB has stopped. The dashboard keeps running." -ForegroundColor Green
        exit 0
    }
}
Write-Host "[!] Ayodhya ORB is still running after 30 seconds. It may be in the middle of a step (a slow" -ForegroundColor Yellow
Write-Host "    data download). The STOP file stays, so it stops at its next check. Look at its window." -ForegroundColor Yellow
exit 1
