#Requires -Version 5.1
<#
  Starts the ORB bot (paper mode unless DRY_RUN=false and CONFIRM_LIVE=YES are set)
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

Start-Dashboard
Start-Job -ScriptBlock { Start-Sleep 4; Start-Process "http://127.0.0.1:5000" } | Out-Null
& $py bot.py
Write-Host ""
Write-Host "Ayodhya ORB stopped (it rests by itself after 3:35 PM, on weekends and on NSE holidays)." -ForegroundColor Yellow
Write-Host "The dashboard keeps running in its own window; to open it again, double-click 3-DASHBOARD.bat"
