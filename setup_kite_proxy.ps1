#Requires -Version 5.1
<#
.SYNOPSIS
  LIVE-TRADING setup only (paper trading needs just setup.bat):
  Python venv + kiteconnect + your static-IP proxy (e.g. TrueIP) + Kite API keys, then an end-to-end test.
  Everything you type is stored in your Windows user environment variables, never in a file.

.USAGE  (normal PowerShell window, NOT "Run as administrator")
  cd <the folder containing this file>
  powershell -ExecutionPolicy Bypass -File .\setup_kite_proxy.ps1

  Re-enter everything:       ... -File .\setup_kite_proxy.ps1 -ResetSecrets
  Force a Python version:    ... -File .\setup_kite_proxy.ps1 -PythonVersion 3.13
#>
[CmdletBinding()]
param(
    [string]$PythonVersion  = "3.12",
    [string]$ProxyHost      = "",      # asked for if not given (your proxy's IP or host name)
    [string]$ProxyPort      = "",
    [string]$ProxyUser      = "",
    [string]$ExpectedEgress = "",      # the static IP whitelisted on your Kite app (default: ProxyHost)
    [switch]$ResetSecrets
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Info($m) { Write-Host "[i] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 0. sanity
if (-not (Test-Path ".\kite_proxy.py")) { Fail "kite_proxy.py not found next to this script ($PSScriptRoot)." }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "This is an elevated (admin) PowerShell. Close it and run from a normal PowerShell window."
}

foreach ($v in "HTTP_PROXY", "HTTPS_PROXY") {
    if ([Environment]::GetEnvironmentVariable($v, "User") -or [Environment]::GetEnvironmentVariable($v, "Machine")) {
        Warn "$v is set globally. It will route ALL your apps via a proxy. Consider removing it."
    }
}

# ---------------------------------------------------------------- 1. find a working Python
function Find-Python {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            foreach ($v in @($PythonVersion, "3.12", "3.13", "3.14", "3")) {
                & py "-$v" -c "import sys" *> $null
                if ($LASTEXITCODE -eq 0) { return ,@("py", "-$v") }
            }
        }
        $p = Get-Command python -All -ErrorAction SilentlyContinue |
             Where-Object { $_.Source -notlike "*WindowsApps*" } | Select-Object -First 1
        if ($p) {
            & $p.Source -c "import sys" *> $null
            if ($LASTEXITCODE -eq 0) { return ,@($p.Source) }
        }
        return $null
    } finally { $ErrorActionPreference = $old }
}

$py = Find-Python
if (-not $py) { Fail "No working Python found. Install Python 3.12 from python.org (tick 'Add to PATH'), then re-run." }
$pyExe  = $py[0]
$pyArgs = @($py | Select-Object -Skip 1)
$ver = & $pyExe @pyArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Info "Using Python $ver  ($($py -join ' '))"

# ---------------------------------------------------------------- 2. venv + packages
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Info "Creating virtual environment .venv"
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed." }
}
Info "Installing / updating pip and kiteconnect (inside .venv)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --upgrade kiteconnect requests --quiet
if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed. If it mentions a C compiler / Visual C++, re-run with -PythonVersion 3.12."
}
$kcv = & $venvPy -c "from kiteconnect.__version__ import __version__ as v; print(v)"
Ok "kiteconnect $kcv installed"

# ---------------------------------------------------------------- 3. credentials (User env vars, never in history)
function Set-UserVar([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, "User")
    Set-Item -Path "Env:$Name" -Value $Value
}
function Read-Secret([string]$Prompt) {
    $s = Read-Host -Prompt $Prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Ensure-Secret([string]$Name, [string]$Prompt) {
    $cur = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($cur -and -not $ResetSecrets) {
        Set-Item -Path "Env:$Name" -Value $cur
        Info "$Name already stored (use -ResetSecrets to change)"
        return
    }
    $val = ""
    for ($i = 1; $i -le 3 -and -not $val; $i++) {
        $val = (Read-Secret "$Prompt [right-click to paste, then Enter]").Trim()
        if (-not $val) { Warn "Empty input - paste with RIGHT-CLICK (Ctrl+V often does nothing here). Try $i/3." }
    }
    if (-not $val) { Fail "$Name cannot be empty." }
    Set-UserVar $Name $val
    Ok "$Name saved"
}

function Ensure-Value([string]$Name, [string]$Given, [string]$Prompt, [string]$Default = "") {
    if ($Given) { Set-UserVar $Name $Given; return }
    $cur = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($cur -and -not $ResetSecrets) { Set-Item -Path "Env:$Name" -Value $cur; Info "$Name already stored ($cur)"; return }
    $p = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
    $val = (Read-Host -Prompt $p).Trim()
    if (-not $val) { $val = $Default }
    if (-not $val) { Fail "$Name cannot be empty." }
    Set-UserVar $Name $val
    Ok "$Name saved"
}

Info "Proxy settings (from your static-IP proxy provider's dashboard)"
Ensure-Value "TRUEIP_HOST" $ProxyHost "Proxy IP or host name"
Ensure-Value "TRUEIP_PORT" $ProxyPort "Proxy port"
Ensure-Value "TRUEIP_USER" $ProxyUser "Proxy username"
Ensure-Value "EXPECTED_EGRESS_IP" $ExpectedEgress "Static IP whitelisted on your Kite app" $env:TRUEIP_HOST
$ExpectedEgress = $env:EXPECTED_EGRESS_IP

Ensure-Secret "TRUEIP_PASS"     "Proxy password (input hidden)"
Ensure-Secret "KITE_API_KEY"    "Kite API key (developers.kite.trade/apps)"
Ensure-Secret "KITE_API_SECRET" "Kite API secret (input hidden)"

# keep the token file out of git if this is a repo
if (-not (Test-Path ".gitignore") -or -not (Select-String -Path ".gitignore" -Pattern "^\.kite_token\.json$" -Quiet)) {
    Add-Content -Path ".gitignore" -Value ".kite_token.json`n.venv/"
}

# ---------------------------------------------------------------- 4. test
Write-Host ""
Info "Step 1/2: proxy + egress check"
& $venvPy .\kite_proxy.py check
if ($LASTEXITCODE -ne 0) { Fail "Proxy check failed - fix the above before continuing." }

Write-Host ""
Warn "Make sure $ExpectedEgress is whitelisted on your Kite app: https://developers.kite.trade/apps"
Read-Host "Press Enter once it is whitelisted (and the redirect URL is set) to log in and verify"

Info "Step 2/2: Kite login + verify through the proxy"
& $venvPy .\kite_proxy.py all
if ($LASTEXITCODE -ne 0) { Fail "Kite verification failed - see message above." }

Write-Host ""
Ok "Setup complete."
Write-Host "Daily use (new terminal):"
Write-Host "  cd $PSScriptRoot"
Write-Host "  .\.venv\Scripts\python.exe kite_proxy.py          # login (once per day) + verify"
Write-Host "  .\.venv\Scripts\python.exe kite_proxy.py ticker   # websocket test (direct)"
Write-Host "In your bot:  from kite_proxy import get_kite;  kite = get_kite()"
