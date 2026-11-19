#Requires -Version 5.1
<#
  Creates two Windows Task Scheduler tasks (only while you're logged in, so the dashboard/login page can open).
  No admin needed.
    "Kite ORB Bot"       starts the bot at 09:00 every weekday.
    "Kite ORB Watchdog"  every 5 minutes from 09:05 to 15:45 on weekdays, runs `watchdog.py check` without a
                         window: it starts the bot or the dashboard if either is missing on a trading day, and
                         ends a bot that stopped responding so that it starts again. It never starts a bot that
                         was stopped on purpose that day (see watchdog.py).

      powershell -ExecutionPolicy Bypass -File .\register_task.ps1              # create / update both
      powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -NoWatchdog  # only the 09:00 start
      powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Unregister  # remove both
#>
param([string]$At = "09:00", [switch]$Unregister, [switch]$NoWatchdog,
      [string]$Name = "Kite ORB Bot", [string]$WatchName = "Kite ORB Watchdog")

$name = $Name
if ($Unregister) {
    foreach ($n in @($name, $WatchName)) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[ok] Task '$n' removed"
    }
    exit 0
}

$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$script = Join-Path $PSScriptRoot "run_bot.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew `
    -Priority 4  # normal priority; Task Scheduler's default (7) is "below normal", which made start-up slow

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Starts Ayodhya ORB (NSE intraday ORB bot, Ramayana edition) and its dashboard (http://127.0.0.1:5000)" -Force | Out-Null
Write-Host "[ok] Task '$name' will start Ayodhya ORB at $At, Monday-Friday."

if ($NoWatchdog) {
    Unregister-ScheduledTask -TaskName $WatchName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[i] No watchdog task (asked with -NoWatchdog)."
} else {
    # pythonw.exe has no window, so nothing flashes on screen every 5 minutes. The check takes a second or two;
    # anything it starts runs on its own (through Windows/WMI), never inside this task.
    $pyw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
    $first = ([datetime]::ParseExact($At, "HH:mm", $null)).AddMinutes(5).ToString("HH:mm")
    $wAction = New-ScheduledTaskAction -Execute $pyw -Argument "watchdog.py check" -WorkingDirectory $PSScriptRoot
    $wTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $first
    $wTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At $first -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 40)).Repetition
    $wSettings = New-ScheduledTaskSettingsSet -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew -Priority 4
    Register-ScheduledTask -TaskName $WatchName -Action $wAction -Trigger $wTrigger -Settings $wSettings `
        -Principal $principal -Description "Ayodhya ORB watchdog: every 5 minutes in market hours, starts the bot or dashboard if missing (watchdog.py check)" -Force | Out-Null
    Write-Host "[ok] Task '$WatchName' checks every 5 minutes from $first, Monday-Friday."
}
Write-Host "     Keep the PC on and logged in during market hours (9:15-15:30 IST); disable sleep in Power settings."
