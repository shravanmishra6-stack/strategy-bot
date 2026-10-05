# Starts the Strategy Bot Telegram Supervisor in the background.
# This supervisor stays alive 24/7 even when the trading server is killed,
# allowing /start_server, /kill, /restart, /status, etc. via Telegram.

$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend

$pythonBin = "python"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonBin = $venvPython
}

$log = Join-Path $backend "runtime"
New-Item -ItemType Directory -Force -Path $log | Out-Null
$stdout = Join-Path $log "supervisor.log"
$stderr = Join-Path $log "supervisor.err"

# Check if supervisor is already running
$existing = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*bot_supervisor*"
}
if ($existing) {
    Write-Host "Telegram Supervisor is already running (PID: $($existing.ProcessId))"
    exit 0
}

Start-Process -FilePath $pythonBin `
    -ArgumentList "-m", "scripts.bot_supervisor" `
    -WorkingDirectory $backend `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden

Write-Host "Telegram Supervisor started in background."
Write-Host "Listening for Telegram commands: /start_server, /kill, /restart, /status, etc."
