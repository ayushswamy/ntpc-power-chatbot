# =============================================================================
# Health check - run every 5 minutes by the scheduled task "NTPC-Chatbot-HealthCheck"
# (or by hand:  powershell -ExecutionPolicy Bypass -File scripts\health_check.ps1).
#
#   - asks the API  GET /health  (does not touch the database)
#   - once an hour also  GET /health?deep=true  (database, LLM, Qdrant - result cached by the API)
#   - if the API does not answer twice in a row, restarts the NTPC-Chatbot-API service
#   - writes one line per check to logs\health.log
# =============================================================================
param([string]$InstallDir = "C:\NTPC-Chatbot", [int]$Port = 8000)
$log = Join-Path $InstallDir "logs\health.log"
$state = Join-Path $InstallDir "data\health_failures.txt"
$time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Write-Log($text) { Add-Content -Path $log -Value "$time $text" }

try {
    $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 15
    $line = "OK api=$($h.status)"
    if ((Get-Date).Minute -lt 5) {      # first check of each hour: full check
        $key = (Select-String -Path (Join-Path $InstallDir ".env") -Pattern "^API_KEY=(.+)$").Matches[0].Groups[1].Value
        $d = Invoke-RestMethod "http://127.0.0.1:$Port/health?deep=true" -Headers @{ "X-API-Key" = $key } -TimeoutSec 60
        $parts = $d.checks.PSObject.Properties | ForEach-Object { "$($_.Name)=$(if ($_.Value.ok) { 'ok' } else { 'DOWN' })" }
        $line = "OK deep=$($d.status) " + ($parts -join " ")
    }
    Write-Log $line
    Set-Content $state "0"
} catch {
    $failures = 1 + [int](Get-Content $state -ErrorAction SilentlyContinue)
    Set-Content $state "$failures"
    Write-Log "FAIL ($failures in a row): $($_.Exception.Message)"
    if ($failures -ge 2) {
        Restart-Service "NTPC-Chatbot-API" -Force -ErrorAction SilentlyContinue
        Write-Log "Restarted service NTPC-Chatbot-API"
        Set-Content $state "0"
    }
}
