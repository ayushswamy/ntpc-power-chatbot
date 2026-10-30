# Starts the Qdrant vector database (native Windows program, no Docker/admin needed).
#   - listens only on this PC (127.0.0.1:6333)
#   - stores its data in data\qdrant_storage
#   - telemetry (usage reporting to the internet) is switched off
# Run from the project folder and leave the window open:
#   powershell -ExecutionPolicy Bypass -File scripts\start_qdrant.ps1

$root = Split-Path -Parent $PSScriptRoot
$env:QDRANT__SERVICE__HOST = "127.0.0.1"
$env:QDRANT__SERVICE__HTTP_PORT = "6333"
$env:QDRANT__SERVICE__GRPC_PORT = "6334"
$env:QDRANT__STORAGE__STORAGE_PATH = Join-Path $root "data\qdrant_storage"
$env:QDRANT__STORAGE__SNAPSHOTS_PATH = Join-Path $root "data\qdrant_snapshots"
$env:QDRANT__TELEMETRY_DISABLED = "true"
New-Item -ItemType Directory -Force (Join-Path $root "data") | Out-Null
Set-Location $root
& (Join-Path $root "tools_bin\qdrant\qdrant.exe")
