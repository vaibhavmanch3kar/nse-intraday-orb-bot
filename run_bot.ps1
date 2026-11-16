#Requires -Version 5.1
<#
  Starts the ORB bot (paper mode unless DRY_RUN=false and CONFIRM_LIVE=YES are set)
  and opens the dashboard. Used by Task Scheduler (register_task.ps1) or by hand:
      powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
#>
Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "[x] .venv missing - double-click 1-SETUP.bat first" -ForegroundColor Red; exit 1 }

# Don't start twice. A running bot holds this named mutex (see claim_single_instance in bot.py);
# checking it is instant, unlike the process (WMI) query used before, which took 1-3 minutes.
$m = $null
$running = [System.Threading.Mutex]::TryOpenExisting("Local\AyodhyaORB_bot", [ref]$m)
if ($m) { $m.Dispose() }
if ($running) { Write-Host "[i] Ayodhya ORB already running - opening the Ayodhya Darbar dashboard"; Start-Process "http://127.0.0.1:5000"; exit 0 }

Start-Job -ScriptBlock { Start-Sleep 4; Start-Process "http://127.0.0.1:5000" } | Out-Null
& $py bot.py
Write-Host ""
Write-Host "Ayodhya ORB stopped (it rests by itself after 3:35 PM, on weekends and on NSE holidays)." -ForegroundColor Yellow
Write-Host "To look at the dashboard any time, double-click 3-DASHBOARD.bat"
