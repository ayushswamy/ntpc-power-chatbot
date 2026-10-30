# =============================================================================
# Backup of the chatbot's own data (NOT the plant database - that is backed up by the DBA).
#
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 [-InstallDir C:\NTPC-Chatbot] [-KeepDays 30]
#
# Saves into backups\<date-time>\ :
#   chatbot.sqlite   conversations and context (a consistent copy, safe while the API is running)
#   config\          semantic\*.yaml, examples\examples.yaml  (everything needed to rebuild Qdrant)
#   logs.zip         rotated log files older than 1 day (they are then removed from logs\)
# .env is NOT copied (it holds passwords) - keep its values in your password safe.
# Qdrant needs no backup: rebuild it any time with  .venv\Scripts\python.exe scripts\index_qdrant.py
# Backups older than -KeepDays are deleted.
# Schedule it daily, e.g.:  schtasks /Create /TN NTPC-Chatbot-Backup /SC DAILY /ST 02:00 /RU SYSTEM
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\NTPC-Chatbot\scripts\backup.ps1"
# =============================================================================
param([string]$InstallDir = "", [int]$KeepDays = 30)
$ErrorActionPreference = "Stop"
if (-not $InstallDir) { $InstallDir = Split-Path -Parent $PSScriptRoot }
$dest = Join-Path $InstallDir ("backups\" + (Get-Date -Format "yyyyMMdd_HHmm"))
New-Item -ItemType Directory -Force "$dest\config" | Out-Null

# 1. SQLite: Python's backup API makes a consistent copy even while the API writes to it
$db = Join-Path $InstallDir "data\chatbot.sqlite"
if (Test-Path $db) {
    & "$InstallDir\.venv\Scripts\python.exe" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $db "$dest\chatbot.sqlite"
}

# 2. Configuration files that define the chatbot's knowledge
Copy-Item "$InstallDir\semantic" "$dest\config\semantic" -Recurse
Copy-Item "$InstallDir\examples" "$dest\config\examples" -Recurse

# 3. Rotated logs older than a day -> zip, then remove (current files stay in place)
$old = Get-ChildItem "$InstallDir\logs" -File | Where-Object {
    $_.Name -match "\.\d{8}_\d{6}\.jsonl$|\-\d{8}T\d+.*\.log$" -and $_.LastWriteTime -lt (Get-Date).AddDays(-1) }
if ($old) {
    Compress-Archive -Path $old.FullName -DestinationPath "$dest\logs.zip"
    $old | Remove-Item
}

# 4. Remove old backups
Get-ChildItem (Join-Path $InstallDir "backups") -Directory | Where-Object { $_.CreationTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Recurse -Force
Write-Host "Backup written to $dest"
