#Requires -Version 5.1
<#
  Creates a Windows Task Scheduler task that starts the bot at 09:00 every weekday
  (only while you're logged in, so the dashboard/login page can open). No admin needed.

      powershell -ExecutionPolicy Bypass -File .\register_task.ps1              # create / update
      powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Unregister  # remove
#>
param([string]$At = "09:00", [switch]$Unregister)

$name = "Kite ORB Bot"
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[ok] Task '$name' removed"
    exit 0
}

$script = Join-Path $PSScriptRoot "run_bot.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Starts Ayodhya ORB (NSE intraday ORB bot, Ramayana edition) and its dashboard (http://127.0.0.1:5000)" -Force | Out-Null
Write-Host "[ok] Task '$name' will start Ayodhya ORB at $At, Monday-Friday."
Write-Host "     Keep the PC on and logged in during market hours (9:15-15:30 IST); disable sleep in Power settings."
