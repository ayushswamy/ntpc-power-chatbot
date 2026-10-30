# =============================================================================
# Build the OFFLINE installation package for the production Windows Server.
# Runs on THIS PC (which has internet). Does not touch the database.
#
# What it puts in offline_packages\NTPC-Chatbot-<date>\ :
#   app_files\     the project (code, semantic layer, examples, widget, docs, tests) - no .env, no data, no logs
#   wheels\        every Python package for Windows / Python 3.12 (pip install works without internet)
#   tools\         qdrant.exe (vector DB) and nssm.exe (runs programs as Windows services)
#   installers\    (-IncludeInstallers) Python 3.12 and Ollama installers
#   ollama_models\ (-IncludeModels) the Ollama models used now: qwen3-8b-ntpc (+ its base qwen3:8b) and bge-m3
#   install_offline.bat, install.ps1, install_services.ps1, ...   (copied from deploy\)
#   PACKAGE_CONTENTS.txt   list of files with sizes and SHA-256 checksums
#
# Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\build_offline_package.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_offline_package.ps1 -IncludeInstallers -IncludeModels
# =============================================================================
param(
    [switch]$IncludeInstallers,
    [switch]$IncludeModels,
    [string]$PythonVersion = "3.12.2"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$pkg = Join-Path $root ("offline_packages\NTPC-Chatbot-" + (Get-Date -Format "yyyyMMdd"))
Write-Host "Building $pkg"
New-Item -ItemType Directory -Force $pkg, "$pkg\app_files", "$pkg\wheels", "$pkg\tools" | Out-Null

# 1. Project files (robocopy: /XD = skip folders, /XF = skip files)
Write-Host "1/5 Copying project files..."
robocopy $root "$pkg\app_files" /E /NFL /NDL /NJH /NJS /NP `
    /XD .venv data logs outputs offline_packages tools_bin __pycache__ .pytest_cache .git `
    /XF .env "*.pyc" "Solution_to_Query.xlsx" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
Copy-Item "$root\deploy\*" $pkg -Force          # install scripts at the top of the package

# 2. Python packages as wheels for the server (Windows 64-bit, Python 3.12)
Write-Host "2/5 Downloading Python packages (wheels)..."
& "$root\.venv\Scripts\python.exe" -m pip download -r "$root\requirements.txt" -d "$pkg\wheels" `
    --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --quiet
if ($LASTEXITCODE -ne 0) { throw "pip download failed" }
& "$root\.venv\Scripts\python.exe" -m pip download pip setuptools -d "$pkg\wheels" --only-binary=:all: --quiet

# 3. Tools: Qdrant (from this PC) and NSSM (service manager)
Write-Host "3/5 Adding Qdrant and NSSM..."
Copy-Item "$root\tools_bin\qdrant\qdrant.exe" "$pkg\tools\qdrant.exe" -Force
if (-not (Test-Path "$pkg\tools\nssm.exe")) {
    $zip = "$env:TEMP\nssm-2.24.zip"
    Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath "$env:TEMP\nssm" -Force
    Copy-Item "$env:TEMP\nssm\nssm-2.24\win64\nssm.exe" "$pkg\tools\nssm.exe" -Force
}

# 4a. Optional installers (Python, Ollama). The ODBC driver: see DEPLOYMENT.md.
if ($IncludeInstallers) {
    Write-Host "4/5 Downloading installers..."
    New-Item -ItemType Directory -Force "$pkg\installers" | Out-Null
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile "$pkg\installers\python-$PythonVersion-amd64.exe" -UseBasicParsing
    Invoke-WebRequest "https://ollama.com/download/OllamaSetup.exe" -OutFile "$pkg\installers\OllamaSetup.exe" -UseBasicParsing
} else { Write-Host "4/5 Installers skipped (use -IncludeInstallers)" }

# 4b. Optional Ollama models: copy only the blobs that the needed models use
if ($IncludeModels) {
    Write-Host "4/5 Copying Ollama models..."
    $src = Join-Path $env:USERPROFILE ".ollama\models"
    $dst = "$pkg\ollama_models"
    foreach ($m in @("qwen3-8b-ntpc\latest", "qwen3\8b", "bge-m3\latest")) {
        $manifest = Join-Path $src "manifests\registry.ollama.ai\library\$m"
        if (-not (Test-Path $manifest)) { Write-Warning "model $m not found - skipped"; continue }
        $target = Join-Path $dst "manifests\registry.ollama.ai\library\$m"
        New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
        Copy-Item $manifest $target -Force
        $json = Get-Content $manifest -Raw | ConvertFrom-Json
        $digests = @($json.config.digest) + @($json.layers | ForEach-Object { $_.digest })
        New-Item -ItemType Directory -Force "$dst\blobs" | Out-Null
        foreach ($d in $digests) {
            $blob = "sha256-" + ($d -replace "^sha256:", "")
            if (-not (Test-Path "$dst\blobs\$blob")) { Copy-Item "$src\blobs\$blob" "$dst\blobs\$blob" }
        }
    }
} else { Write-Host "4/5 Models skipped (use -IncludeModels, or 'ollama pull' on the server)" }

# 5. Contents list with checksums (to check the copy on the server)
Write-Host "5/5 Writing PACKAGE_CONTENTS.txt..."
$lines = @("NTPC Power Data Chatbot - offline package, built $(Get-Date -Format 'yyyy-MM-dd HH:mm')", "")
Get-ChildItem $pkg -Recurse -File | Where-Object { $_.FullName -notlike "*\app_files\*" } | ForEach-Object {
    $hash = if ($_.Length -lt 3GB) { (Get-FileHash $_.FullName -Algorithm SHA256).Hash } else { "(large file)" }
    $lines += "{0,12:N0}  {1}  {2}" -f $_.Length, $hash, $_.FullName.Substring($pkg.Length + 1)
}
$lines | Set-Content "$pkg\PACKAGE_CONTENTS.txt" -Encoding UTF8
$size = (Get-ChildItem $pkg -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("Done: {0:N0} MB in {1}" -f ($size / 1MB), $pkg)
