# Starts the dashboard + trading session on a weekday morning.
#
# Registered as a Windows Scheduled Task (Mon-Fri 09:10 IST) by
# scripts/register_schedule.ps1. Safe by design:
#   * never touches weekends (session.py also refuses them)
#   * starts the server and OPENS the session, but does NOT arm live trading -
#     arming stays a deliberate dashboard click (POST /api/arm)
#   * idempotent: reuses an already-running server instead of stacking copies
param(
    [string]$Port = "8000",
    [switch]$Arm   # off by default; pass -Arm only if you intentionally want
                   # an unattended armed session
)
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot   # .../backend
Set-Location $backend

$log = Join-Path $backend "runtime"
New-Item -ItemType Directory -Force -Path $log | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd"

# Weekend guard - do not even start the process.
if ((Get-Date).DayOfWeek -in @("Saturday", "Sunday")) {
    Add-Content (Join-Path $log "scheduler.log") "$stamp skipped: weekend"
    exit 0
}

function Test-Port([int]$p) {
    # Get-NetTCPConnection, NOT New-NetTCPConnection. The latter cmdlet does not
    # exist, so under $ErrorActionPreference = "Stop" this function threw and
    # was caught, i.e. it ALWAYS reported "not listening". Every run then
    # spawned a second uvicorn that failed to bind the port, and the wait loop
    # below always burned its full 30s before POSTing /api/start.
    try {
        if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) {
            return $true
        }
    } catch { }
    # Fallback for hosts without the NetTCPIP module.
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect("127.0.0.1", $p)
        $client.Close()
        return $true
    } catch { return $false }
}

if (-not (Test-Port $Port)) {
    Start-Process -FilePath python -ArgumentList "-m","uvicorn","app.api:app",
        "--host","127.0.0.1","--port",$Port `
        -RedirectStandardOutput (Join-Path $log "server-$stamp.log") `
        -RedirectStandardError  (Join-Path $log "server-$stamp.err") `
        -WindowStyle Hidden
    # wait for the socket so the POST below cannot race startup
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and -not (Test-Port $Port)) { Start-Sleep -Milliseconds 500 }
}

$base = "http://127.0.0.1:$Port"
$schedLog = Join-Path $log "scheduler.log"
try {
    try {
        Invoke-RestMethod -Method Post -Uri "$base/api/start" -TimeoutSec 15 | Out-Null
        Add-Content $schedLog "$stamp session started (arm=$Arm)"
    } catch {
        # A 409 means a session is ALREADY running: that is the desired end
        # state, not a failure. It must not skip the -Arm call below (previously
        # $ErrorActionPreference = "Stop" aborted the script right here).
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -eq 409) {
            Add-Content $schedLog "$stamp session already running (arm=$Arm)"
        } else {
            throw
        }
    }
    if ($Arm) {
        Invoke-RestMethod -Method Post -Uri "$base/api/arm" -TimeoutSec 15 | Out-Null
        Add-Content $schedLog "$stamp ARMED live trading"
    }
    exit 0
} catch {
    Add-Content $schedLog "$stamp FAILED: $($_.Exception.Message)"
    exit 1
}
