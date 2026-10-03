# Registers Strategy Bot in Windows Task Scheduler.
#
# Tasks registered:
#   1. "StrategyBot_DailyStart" -> runs scripts/start_day.ps1 daily at 09:00 AM.
#      Starts the uvicorn server and trading session (skips weekends safely).
#
#   2. "StrategyBot_Supervisor" (optional) -> starts scripts/start_supervisor.ps1
#      at user logon so Telegram control is available 24/7.
param(
    [string]$Time = "09:00",
    [switch]$WithSupervisor,
    [switch]$Unregister
)

$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend

$taskNameStart = "StrategyBot_DailyStart"
$taskNameSup   = "StrategyBot_Supervisor"

if ($Unregister) {
    Write-Host "Unregistering Strategy Bot scheduled tasks..."
    schtasks.exe /delete /tn $taskNameStart /f 2>$null
    schtasks.exe /delete /tn $taskNameSup /f 2>$null
    Write-Host "Scheduled tasks removed."
    exit 0
}

# 1. Register Daily 9:00 AM start task
$scriptPath = Join-Path $backend "scripts\start_day.ps1"
$psExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

Write-Host "Registering '$taskNameStart' to run daily at $Time AM..."
try {
    $action = New-ScheduledTaskAction -Execute $psExe -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskNameStart -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "SUCCESS: '$taskNameStart' registered for $Time AM daily (with battery support enabled)."
} catch {
    $actionCmd = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""
    schtasks.exe /create /tn $taskNameStart /tr $actionCmd /sc DAILY /st $Time /f | Out-Null
    Write-Host "SUCCESS: '$taskNameStart' registered for $Time AM daily."
}

# 2. Register Supervisor to start on user logon if requested or default
if ($WithSupervisor) {
    $supScript = Join-Path $backend "scripts\start_supervisor.ps1"
    $supCmd = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supScript`""
    
    Write-Host "Registering '$taskNameSup' on user logon..."
    schtasks.exe /create /tn $taskNameSup `
        /tr $supCmd `
        /sc ONLOGON `
        /f

    if ($LASTEXITCODE -eq 0) {
        Write-Host "SUCCESS: '$taskNameSup' registered to run at logon."
    }
}

Write-Host "`nTask Scheduler configuration complete!"
Write-Host "View details anytime with: schtasks.exe /query /tn $taskNameStart /v /fo LIST"
