# =============================================================================
# Register the chatbot as Windows services (start automatically with Windows, restart on failure).
# Run as Administrator AFTER install.ps1 and after editing C:\NTPC-Chatbot\.env:
#
#   powershell -ExecutionPolicy Bypass -File install_services.ps1 -AllowFrom 10.x.x.x
#   powershell -ExecutionPolicy Bypass -File install_services.ps1 -AllowFrom 10.x.x.x -WithOllama -OllamaParallel 4
#
#   -AllowFrom       IP address(es) of the ASP.NET web server(s) - the only machines allowed to call the API
#   -WithOllama      also run Ollama (the LLM) on this server as a service
#   -OllamaParallel  questions Ollama answers at the same time (match MAX_CONCURRENT_CHATS in .env)
#
# Services created (startup order):
#   NTPC-Qdrant        vector database        127.0.0.1:6333 (this server only)
#   NTPC-Ollama        LLM + embeddings       127.0.0.1:11434 (this server only)   [-WithOllama]
#   NTPC-Chatbot-API   the chatbot API        0.0.0.0:<Port>, firewall open only for -AllowFrom
# Plus a scheduled task "NTPC-Chatbot-HealthCheck" every 5 minutes (restarts the API if it stops answering).
# =============================================================================
param(
    [Parameter(Mandatory = $true)][string[]]$AllowFrom,
    [string]$InstallDir = "C:\NTPC-Chatbot",
    [int]$Port = 8000,
    [switch]$WithOllama,
    [int]$OllamaParallel = 1,
    [string]$OllamaExe = ""
)
$ErrorActionPreference = "Stop"
$nssm = "$InstallDir\tools_bin\nssm\nssm.exe"
$logs = "$InstallDir\logs"
New-Item -ItemType Directory -Force $logs, "$InstallDir\data" | Out-Null

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run as Administrator." }
if (Select-String -Path "$InstallDir\.env" -Pattern "<" -SimpleMatch -Quiet) {
    throw ".env still contains <...> placeholders - fill in $InstallDir\.env first."
}

function New-NtpcService($name, $exe, $arguments, $envVars, $dependsOn, $description) {
    if (Get-Service $name -ErrorAction SilentlyContinue) {
        & $nssm stop $name | Out-Null
        & $nssm remove $name confirm | Out-Null
    }
    & $nssm install $name $exe $arguments | Out-Null
    & $nssm set $name AppDirectory $InstallDir | Out-Null
    & $nssm set $name Description $description | Out-Null
    & $nssm set $name Start SERVICE_AUTO_START | Out-Null
    & $nssm set $name AppStdout "$logs\$name.log" | Out-Null
    & $nssm set $name AppStderr "$logs\$name.log" | Out-Null
    & $nssm set $name AppRotateFiles 1 | Out-Null
    & $nssm set $name AppRotateOnline 1 | Out-Null
    & $nssm set $name AppRotateBytes 10485760 | Out-Null        # new log file every 10 MB
    & $nssm set $name AppExit Default Restart | Out-Null          # restart if it crashes
    & $nssm set $name AppRestartDelay 10000 | Out-Null
    if ($envVars) { & $nssm set $name AppEnvironmentExtra $envVars | Out-Null }
    if ($dependsOn) { & $nssm set $name DependOnService $dependsOn | Out-Null }
    Write-Host "Service $name registered."
}

# 1. Qdrant - only reachable from this server, telemetry off
New-NtpcService "NTPC-Qdrant" "$InstallDir\tools_bin\qdrant\qdrant.exe" "" @(
    "QDRANT__SERVICE__HOST=127.0.0.1", "QDRANT__SERVICE__HTTP_PORT=6333", "QDRANT__SERVICE__GRPC_PORT=6334",
    "QDRANT__STORAGE__STORAGE_PATH=$InstallDir\data\qdrant_storage",
    "QDRANT__STORAGE__SNAPSHOTS_PATH=$InstallDir\data\qdrant_snapshots", "QDRANT__TELEMETRY_DISABLED=true"
) $null "NTPC chatbot - vector database (examples, glossary)"
$apiDepends = @("NTPC-Qdrant")

# 2. Ollama (optional) - model kept in memory permanently (no slow first answer)
if ($WithOllama) {
    if (-not $OllamaExe) {
        $OllamaExe = @("$env:ProgramFiles\Ollama\ollama.exe", "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $OllamaExe) { throw "ollama.exe not found - pass -OllamaExe <path>." }
    $models = if (Test-Path "$InstallDir\ollama_models") { "$InstallDir\ollama_models" } else { "$env:USERPROFILE\.ollama\models" }
    New-NtpcService "NTPC-Ollama" $OllamaExe "serve" @(
        "OLLAMA_HOST=127.0.0.1:11434", "OLLAMA_MODELS=$models", "OLLAMA_KEEP_ALIVE=-1",
        "OLLAMA_NUM_PARALLEL=$OllamaParallel", "OLLAMA_MAX_LOADED_MODELS=2"
    ) $null "NTPC chatbot - LLM and embedding models (Ollama)"
    $apiDepends += "NTPC-Ollama"
}

# 3. The API - one process (it keeps the rate limits, live-data cache and queue in memory)
New-NtpcService "NTPC-Chatbot-API" "$InstallDir\.venv\Scripts\python.exe" `
    "-m uvicorn app.api.main:app --host 0.0.0.0 --port $Port" $null $apiDepends `
    "NTPC chatbot - web API used by the ASP.NET application"

# 4. Firewall: only the ASP.NET server(s) may call the API
Get-NetFirewallRule -DisplayName "NTPC Chatbot API" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "NTPC Chatbot API" -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress $AllowFrom -Action Allow | Out-Null
Write-Host "Firewall: port $Port open for $($AllowFrom -join ', ') only."

# 5. Start in order
Start-Service NTPC-Qdrant
if ($WithOllama) { Start-Service NTPC-Ollama; Start-Sleep 5 }
Start-Service NTPC-Chatbot-API
Start-Sleep 8

# 6. Health-check task every 5 minutes
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\scripts\health_check.ps1`" -InstallDir `"$InstallDir`" -Port $Port"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "NTPC-Chatbot-HealthCheck" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Scheduled task NTPC-Chatbot-HealthCheck registered (every 5 minutes)."

try {
    $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 10
    Write-Host "API health: $($h.status) (model $($h.model))" -ForegroundColor Green
} catch { Write-Warning "The API did not answer yet - check $logs\NTPC-Chatbot-API.log" }
Write-Host "Next: index the examples into Qdrant:  $InstallDir\.venv\Scripts\python.exe scripts\index_qdrant.py"
