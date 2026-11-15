#Requires -Version 5.1
<#
  One-time setup for PAPER trading (no Zerodha account, API key or proxy needed).
  Double-click 1-SETUP.bat instead of running this directly.

  1. Finds Python 3.12+ (tells you how to install it if missing)
  2. Creates a private Python environment in .venv
  3. Installs the packages from requirements.txt
  4. Runs the self-tests so you know everything works
#>
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Info($m) { Write-Host "[i] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ok] $m" -ForegroundColor Green }
function Fail($m) {
    Write-Host ""
    Write-Host "[x] $m" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "=== NSE Intraday ORB Bot - setup (paper trading) ===" -ForegroundColor Yellow
Write-Host ""

# 1. Python ------------------------------------------------------------------
function Find-Python {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            foreach ($v in "3.13", "3.12", "3.14", "3") {
                & py "-$v" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" *> $null
                if ($LASTEXITCODE -eq 0) { return ,@("py", "-$v") }
            }
        }
        foreach ($c in Get-Command python -All -ErrorAction SilentlyContinue) {
            if ($c.Source -like "*WindowsApps*") { continue }   # the Microsoft Store stub, not real Python
            & $c.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) { return ,@($c.Source) }
        }
        return $null
    } finally { $ErrorActionPreference = $old }
}

$py = Find-Python
if (-not $py) {
    Fail ("Python 3.12 or newer is not installed.`n" +
          "    1. Go to https://www.python.org/downloads/ and click the yellow 'Download Python' button.`n" +
          "    2. Run the installer and TICK 'Add python.exe to PATH' at the bottom of the first screen.`n" +
          "    3. Click 'Install Now', wait, then double-click 1-SETUP.bat again.")
}
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
$ver = & $pyExe @pyArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Ok "Found Python $ver"

# 2. Private environment ------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Info "Creating a private Python environment in .venv (one time, ~20 seconds)"
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv. Try running 1-SETUP.bat again." }
}
Ok ".venv ready"

# 3. Packages -------------------------------------------------------------------
Info "Installing packages (kiteconnect, requests, numpy, pytest) - needs internet, 1-3 minutes"
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPy -m pip install -r requirements-dev.txt --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Package install failed. Check your internet connection and run 1-SETUP.bat again." }
Ok "Packages installed"

# 4. Self-test ------------------------------------------------------------------
Info "Running the self-tests (offline, ~10 seconds)"
& $venvPy -m pytest -q
if ($LASTEXITCODE -ne 0) { Fail "Some self-tests failed - copy the red text above into a GitHub issue." }
Ok "All self-tests passed"

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Next:"
Write-Host "  * 3-DASHBOARD.bat    look around any time (weekends too)"
Write-Host "  * 2-START-BOT.bat    run the bot in PAPER mode on a market day (start before 9:15 AM IST)"
Write-Host "  * 4-SCHEDULE.bat     optional: start the bot automatically at 9:00 AM every weekday"
Write-Host ""
