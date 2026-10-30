# =============================================================================
# Install (or upgrade) the NTPC Power Data Chatbot on a Windows Server - works WITHOUT internet.
# Run as Administrator from the unpacked offline package folder:
#     install_offline.bat              (or)
#     powershell -ExecutionPolicy Bypass -File install.ps1 [-InstallDir C:\NTPC-Chatbot] [-WithOllama]
#
# What it does:
#   1. checks Python 3.12 and the "ODBC Driver 17 for SQL Server" (installs Python from installers\ if present)
#   2. copies the program to InstallDir (an upgrade keeps .env, data\ and logs\)
#   3. creates the Python environment and installs packages from wheels\ (no internet)
#   4. puts qdrant.exe and nssm.exe in InstallDir\tools_bin
#   5. creates .env from the production template (first install only) and limits it to administrators
#   6. (-WithOllama) installs Ollama from installers\ and copies the models from ollama_models\
#   7. runs the offline tests (no database needed) to prove the installation works
# =============================================================================
param(
    [string]$InstallDir = "C:\NTPC-Chatbot",
    [switch]$WithOllama
)
$ErrorActionPreference = "Stop"
$pkg = $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Please run this script as Administrator (right-click PowerShell > Run as administrator)."
}

Step "1. Python 3.12 and ODBC driver"
$python = $null
try { $python = (& py -3.12 -c "import sys; print(sys.executable)") 2>$null } catch {}
if (-not $python) {
    $installer = Get-ChildItem "$pkg\installers\python-3.12*-amd64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $installer) { throw "Python 3.12 is not installed and no installer is in installers\. Install Python 3.12 (64-bit) for all users first." }
    Write-Host "Installing $($installer.Name) for all users..."
    Start-Process $installer.FullName -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    $python = (& py -3.12 -c "import sys; print(sys.executable)")
}
Write-Host "Python: $python"
if (-not (Get-OdbcDriver -Name "ODBC Driver 17 for SQL Server" -Platform 64-bit -ErrorAction SilentlyContinue)) {
    $msi = Get-ChildItem "$pkg\installers\msodbcsql*.msi" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $msi) { throw "ODBC Driver 17 for SQL Server is missing. Install it (see DEPLOYMENT.md) or put msodbcsql.msi in installers\." }
    Start-Process msiexec.exe -ArgumentList "/i `"$($msi.FullName)`" IACCEPTMSODBCSQLLICENSETERMS=YES /qn" -Wait
}
Write-Host "ODBC Driver 17 for SQL Server: OK"

Step "2. Copy program files to $InstallDir"
New-Item -ItemType Directory -Force $InstallDir | Out-Null
robocopy "$pkg\app_files" $InstallDir /E /NFL /NDL /NJH /NJS /NP /XD data logs backups /XF .env | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copy failed (robocopy $LASTEXITCODE)" }

Step "3. Python environment and packages (offline)"
if (-not (Test-Path "$InstallDir\.venv\Scripts\python.exe")) { & $python -m venv "$InstallDir\.venv" }
& "$InstallDir\.venv\Scripts\python.exe" -m pip install --no-index --find-links "$pkg\wheels" --upgrade pip --quiet
& "$InstallDir\.venv\Scripts\python.exe" -m pip install --no-index --find-links "$pkg\wheels" -r "$InstallDir\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { throw "Package installation failed" }

Step "4. Tools (Qdrant, NSSM)"
New-Item -ItemType Directory -Force "$InstallDir\tools_bin\qdrant", "$InstallDir\tools_bin\nssm" | Out-Null
Copy-Item "$pkg\tools\qdrant.exe" "$InstallDir\tools_bin\qdrant\qdrant.exe" -Force
Copy-Item "$pkg\tools\nssm.exe" "$InstallDir\tools_bin\nssm\nssm.exe" -Force

Step "5. Settings file (.env)"
if (-not (Test-Path "$InstallDir\.env")) {
    Copy-Item "$pkg\.env.production.example" "$InstallDir\.env"
    Write-Host "Created $InstallDir\.env from the template - EDIT IT before starting the services." -ForegroundColor Yellow
} else { Write-Host ".env already exists - kept unchanged." }
# only Administrators and SYSTEM (the services) may read the secrets
icacls "$InstallDir\.env" /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" | Out-Null

if ($WithOllama) {
    Step "6. Ollama"
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        $setup = "$pkg\installers\OllamaSetup.exe"
        if (-not (Test-Path $setup)) { throw "Ollama is not installed and installers\OllamaSetup.exe is missing." }
        Start-Process $setup -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" -Wait
    }
    if (Test-Path "$pkg\ollama_models") {
        robocopy "$pkg\ollama_models" "$InstallDir\ollama_models" /E /NFL /NDL /NJH /NJS /NP | Out-Null
        Write-Host "Models copied to $InstallDir\ollama_models (used by the NTPC-Ollama service)."
    } else { Write-Host "No ollama_models\ in the package: run 'ollama pull' on the server if it has internet." }
} else { Write-Host "`n(Ollama not installed: -WithOllama not given; the LLM runs on another server - set LLM_BASE_URL in .env)" }

Step "7. Offline self-test (no database needed)"
Push-Location $InstallDir
& "$InstallDir\.venv\Scripts\python.exe" -m pytest tests -q
$ok = $LASTEXITCODE -eq 0
Pop-Location
if (-not $ok) { throw "Self-test failed - see the output above." }

Write-Host "`nInstallation complete." -ForegroundColor Green
Write-Host "Next: 1) edit $InstallDir\.env   2) run install_services.ps1 as Administrator   (see DEPLOYMENT.md)"
