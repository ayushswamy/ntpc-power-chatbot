# Stop and remove the chatbot services, firewall rule and health-check task (files and data are kept).
# Run as Administrator:  powershell -ExecutionPolicy Bypass -File uninstall_services.ps1
param([string]$InstallDir = "C:\NTPC-Chatbot")
$nssm = "$InstallDir\tools_bin\nssm\nssm.exe"
foreach ($name in "NTPC-Chatbot-API", "NTPC-Ollama", "NTPC-Qdrant") {
    if (Get-Service $name -ErrorAction SilentlyContinue) {
        & $nssm stop $name | Out-Null
        & $nssm remove $name confirm | Out-Null
        Write-Host "Removed service $name"
    }
}
Get-NetFirewallRule -DisplayName "NTPC Chatbot API" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Unregister-ScheduledTask -TaskName "NTPC-Chatbot-HealthCheck" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Done. Program files, .env, data\ and logs\ in $InstallDir were not touched."
