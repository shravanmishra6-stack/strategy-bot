param(
    [int]$PidToKill = 0
)

$Port = 8000
$BackendDir = Split-Path -Parent $PSScriptRoot
Set-Location $BackendDir

if ($PidToKill -gt 0) {
    # Brief pause to allow the HTTP response to be flushed to the browser
    Start-Sleep -Milliseconds 500
    Stop-Process -Id $PidToKill -Force -ErrorAction SilentlyContinue
}

# Wait for port 8000 to be completely free
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    $busy = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Listen' }
    if (-not $busy) { break }
    Start-Sleep -Milliseconds 400
}

# Socket cooldown
Start-Sleep -Milliseconds 800

$pythonBin = "python"
$venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonBin = $venvPython
}

Start-Process -FilePath $pythonBin `
    -ArgumentList "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", "$Port" `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput (Join-Path $BackendDir "runtime\server-restart.log") `
    -RedirectStandardError  (Join-Path $BackendDir "runtime\server-restart.err") `
    -WindowStyle Hidden
