#Requires -Version 5.1
<#
  Replace ONLY the Kite API key and secret (e.g. after creating a new Kite Connect app).
  Proxy settings are left as they are. Input is hidden; values go to your Windows user
  environment variables, never to a file.

      powershell -ExecutionPolicy Bypass -File .\set_kite_keys.ps1
#>
function Read-Hidden([string]$Prompt) {
    $s = Read-Host -Prompt $Prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b).Trim() }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

Write-Host ""
Write-Host "Paste the NEW app's values from https://developers.kite.trade/apps (right-click pastes; nothing is shown)." -ForegroundColor Cyan
$key = Read-Hidden "Kite API key"
$secret = Read-Hidden "Kite API secret"
if (-not $key -or -not $secret) { Write-Host "[x] Both values are needed - nothing changed." -ForegroundColor Red; exit 1 }
if ($key.Length -lt 8 -or $key -match '\s') { Write-Host "[x] That doesn't look like an API key - nothing changed." -ForegroundColor Red; exit 1 }

[Environment]::SetEnvironmentVariable("KITE_API_KEY", $key, "User")
[Environment]::SetEnvironmentVariable("KITE_API_SECRET", $secret, "User")
$old = Join-Path $PSScriptRoot ".kite_token.json"
if (Test-Path $old) { Remove-Item $old -Force -Confirm:$false }   # the old app's login token is useless now
Write-Host "[ok] New Kite API key and secret saved (key ends in ...$($key.Substring($key.Length - 4)))." -ForegroundColor Green
Write-Host "     Old login token removed - log in again from the dashboard."
