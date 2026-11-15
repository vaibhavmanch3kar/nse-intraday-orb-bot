#Requires -Version 5.1
<#
  Opens the dashboard any time - weekends and evenings included - for review.
  If the bot is already running, its dashboard is reused; otherwise a dashboard-only
  server starts (no trading). Close this window or press Ctrl+C to stop it.

      powershell -ExecutionPolicy Bypass -File .\dashboard.ps1
#>
Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "[x] .venv missing - double-click 1-SETUP.bat first" -ForegroundColor Red; exit 1 }

$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[i] Dashboard already running - opening http://127.0.0.1:5000"
    Start-Process "http://127.0.0.1:5000"
    exit 0
}
Write-Host "[i] Starting the Ayodhya Darbar dashboard only (no trading)"; Write-Host "    Pages: Ayodhya Darbar (Live), Chitrakoot (Session review), Hanuman's Watch (Swing scanner), Rajkosh (Trades)"
& $py web.py
