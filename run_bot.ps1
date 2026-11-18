#Requires -Version 5.1
<#
  Starts the ORB bot: PAPER unless DRY_RUN=false and CONFIRM_LIVE=YES are set AND real money was armed today
  on the dashboard (livemode.py); the 9:00 scheduled task therefore trades paper on any day you didn't arm it
  and the dashboard, which is its own program (web.py): it keeps running when the bot stops,
  and it can be restarted for a dashboard update without touching the bot.
  Used by Task Scheduler (register_task.ps1) or by hand:
      powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
#>
Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "[x] .venv missing - double-click 1-SETUP.bat first" -ForegroundColor Red; exit 1 }

function Start-Dashboard {
    # Start the dashboard in its own minimized window unless something already answers on port 5000.
    $listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    if ($listening) { return }
    Start-Process -FilePath $py -ArgumentList @("web.py", "--no-browser") -WorkingDirectory $PSScriptRoot `
        -WindowStyle Minimized | Out-Null
}

# Don't start twice. A running bot holds this named mutex (see claim_single_instance in bot.py);
# checking it is instant, unlike the process (WMI) query used before, which took 1-3 minutes.
$m = $null
$running = [System.Threading.Mutex]::TryOpenExisting("Local\AyodhyaORB_bot", [ref]$m)
if ($m) { $m.Dispose() }
if ($running) {
    Write-Host "[i] Ayodhya ORB already running - opening the Ayodhya Darbar dashboard"
    Start-Dashboard
    Start-Process "http://127.0.0.1:5000"
    exit 0
}

function Write-LauncherLog([string]$text) {
    # One line per bot start and per bot exit (with its exit code) in data\logs\launcher.log, so "why did it
    # stop?" has an answer even when this window is gone. A logging problem never stops the launcher.
    try {
        $dir = if ($env:DATA_DIR) { $env:DATA_DIR } else { "data" }
        if (-not [System.IO.Path]::IsPathRooted($dir)) { $dir = Join-Path $PSScriptRoot $dir }
        $logs = Join-Path $dir "logs"
        New-Item -ItemType Directory -Force -Path $logs | Out-Null
        $line = "{0} launcher {1}: {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $PID, $text
        Add-Content -Path (Join-Path $logs "launcher.log") -Value $line -Encoding UTF8
    } catch { }
}

Start-Dashboard
Start-Job -ScriptBlock { Start-Sleep 4; Start-Process "http://127.0.0.1:5000" } | Out-Null
# Exit code 3 = the bot restarted itself to load an update, after "Restart when safe" on the dashboard
# (PAPER only, before the open or with no open position; see restart.py). Start it again, at most 3 times.
$restarts = 0
while ($true) {
    # --launcher-pid: if this window is ended (e.g. Stop on the "Kite ORB Bot" task), the bot stops by itself
    # the same way as with the STOP file, instead of trading on with nobody watching (as v1.8.0 did).
    Write-LauncherLog "starting the bot"
    & $py bot.py --launcher-pid $PID
    $code = $LASTEXITCODE
    Write-LauncherLog "the bot ended with exit code $code (0 = normal end, 3 = restart to load an update, anything else = error)"
    if ($LASTEXITCODE -eq 3 -and $restarts -lt 3) {
        $restarts++
        Write-Host "[i] Restarting Ayodhya ORB to load the update ($restarts)..."
        Start-Sleep -Seconds 2
        continue
    }
    break
}
Write-Host ""
Write-Host "Ayodhya ORB stopped (it rests by itself after 3:35 PM, on weekends and on NSE holidays)." -ForegroundColor Yellow
Write-Host "The dashboard keeps running in its own window; to open it again, double-click 3-DASHBOARD.bat"
