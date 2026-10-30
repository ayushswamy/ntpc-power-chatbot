# Deploying the NTPC Power Data Chatbot

Audience: the administrator installing and running the chatbot on the production Windows Server.
Commands are for PowerShell **run as Administrator** unless noted.

## 1. How the pieces fit

```
 Users' browsers ──► ASP.NET application ──(/chatbot/chat + API key)──► Chatbot API (Windows Server :8000)
                                                                           │
                         SQL Server 10.4.8.44 / DB_PlantLive  ◄──read-only─┤
                         Qdrant (same server, 127.0.0.1:6333)  ◄───────────┤
                         LLM server (Ollama or vLLM, see 6)   ◄───────────┘
```

| Component | Runs as | Port | Notes |
|---|---|---|---|
| Chatbot API | Windows service `NTPC-Chatbot-API` | 8000 | Firewall open only for the ASP.NET server |
| Qdrant | Windows service `NTPC-Qdrant` | 6333 (local only) | Example questions, glossary; can be rebuilt any time |
| Ollama (optional) | Windows service `NTPC-Ollama` | 11434 (local only) | LLM + embeddings when they run on this server |
| vLLM (recommended for 20+ users) | Linux GPU server | 8001 | See section 8 |

**Expected load: more than 20 people at the same time.** The LLM is the bottleneck. Recommended:
a **Linux GPU server with vLLM** (one 48 GB GPU such as an L40S, or an 80 GB A100/H100) for the
LLM, with the chatbot API and Qdrant on the Windows Server. Ollama on a Windows GPU server also
works, but it handles fewer simultaneous questions (about 4 at a time on a 24 GB GPU).
Until the GPU arrives, the chatbot runs with the small CPU model, one question at a time.

## 2. Server requirements

| | Minimum | Notes |
|---|---|---|
| OS | Windows Server 2019 or later, 64-bit | |
| CPU / RAM | 8 cores / 16 GB (32 GB if Ollama runs on this server) | |
| Disk | 20 GB free (+ 10 GB per Ollama model) | |
| Software | Python 3.12 (64-bit, all users), ODBC Driver 17 for SQL Server | Installer handles Python if included |
| Network | To SQL Server 10.4.8.44:1433; from the ASP.NET server to port 8000; to the LLM server | |

ODBC Driver 17 for SQL Server: download `msodbcsql.msi` (x64) from Microsoft's
"Download ODBC Driver for SQL Server" page and put it in the package's `installers\` folder, or
install it beforehand.

## 3. Build the package (on the development PC)

```powershell
cd D:\ntpc-power-chatbot
powershell -ExecutionPolicy Bypass -File scripts\build_offline_package.ps1 -IncludeInstallers -IncludeModels
```
This creates `offline_packages\NTPC-Chatbot-<date>\`. It is about 130 MB without models, about 7 GB
with them. Copy the whole folder to the server (network share or USB), then compare file sizes or
checksums with `PACKAGE_CONTENTS.txt`.

- `-IncludeInstallers` adds the Python 3.12 and Ollama installers.
- `-IncludeModels` adds the Ollama models used today (qwen3-8b-ntpc, bge-m3).
- If the server has internet, you can skip both and use `ollama pull` on the server instead.

## 4. Install

1. In the copied package folder, right-click **`install_offline.bat`** and choose **Run as administrator**.
   If Ollama should run on this server, run this instead:
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1 -WithOllama
   ```
   The installer:
   - checks Python and the ODBC driver,
   - copies the program to `C:\NTPC-Chatbot`,
   - installs the Python packages **without internet**,
   - creates `.env` from the production template,
   - runs the offline self-test. It must end with **"Installation complete"**.

2. **Edit `C:\NTPC-Chatbot\.env`** (Notepad as Administrator) and replace every `<...>`:

   | Setting | What to put |
   |---|---|
   | `DB_USER`, `DB_PASSWORD` | The **read-only** login. Ask the DBA for a **new, strong password** first: the development password was shared in a chat. |
   | `API_KEY` | A new random key: `C:\NTPC-Chatbot\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `LLM_*` | Where the LLM runs (section 8) |
   | `MAX_CONCURRENT_CHATS` | 1 on CPU, 4 with Ollama on a 24 GB GPU, 16 with vLLM on 48 GB |

3. **Check the database login is still read-only** (the same check as in Phase 0):
   ```powershell
   cd C:\NTPC-Chatbot
   .\.venv\Scripts\python.exe scripts\check_db_connection.py
   ```
   It must end with **VERDICT: PASS**.

4. **Register and start the services.** Replace the address with the ASP.NET web server's IP;
   several can be given separated by commas:
   ```powershell
   powershell -ExecutionPolicy Bypass -File install_services.ps1 -AllowFrom 10.1.2.3
   # with Ollama on this server, answering 4 questions at a time:
   powershell -ExecutionPolicy Bypass -File install_services.ps1 -AllowFrom 10.1.2.3 -WithOllama -OllamaParallel 4
   ```

5. **Fill the example-question database** (after the services are running):
   ```powershell
   cd C:\NTPC-Chatbot
   .\.venv\Scripts\python.exe scripts\index_qdrant.py
   ```

6. **Connect the ASP.NET application:** follow [docs/EMBEDDING.md](docs/EMBEDDING.md). Put the same
   `API_KEY` in its `web.config`.

7. **Verify end to end:**
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8000/health
   .\.venv\Scripts\python.exe scripts\api_smoke_test.py
   ```

## 5. Daily operation

| Task | Command (Administrator PowerShell) |
|---|---|
| Status | `Get-Service NTPC-*` |
| Stop all | `Stop-Service NTPC-Chatbot-API, NTPC-Ollama, NTPC-Qdrant -ErrorAction SilentlyContinue` |
| Start all | `Start-Service NTPC-Qdrant; Start-Service NTPC-Ollama -ErrorAction SilentlyContinue; Start-Service NTPC-Chatbot-API` |
| Restart the API (after editing `.env` or YAML files) | `Restart-Service NTPC-Chatbot-API` |
| Health | `Invoke-RestMethod http://127.0.0.1:8000/health` |
| Remove services (keeps files) | `powershell -ExecutionPolicy Bypass -File C:\NTPC-Chatbot\deploy\uninstall_services.ps1` |

The API loads the LLM when it starts (`WARMUP_ON_START=true`), and the Ollama service keeps the
model in memory (`OLLAMA_KEEP_ALIVE=-1`). The first user therefore does not wait for the model to load.

### Logs (`C:\NTPC-Chatbot\logs\`)

| File | Contents |
|---|---|
| `audit.jsonl` | One line per question: time, user (from the ASP.NET handler), session, question, tool, SQL, row counts, response time, errors |
| `queries.jsonl` | Every database query: purpose, SQL, parameters, rows, duration. No result data and no passwords. |
| `health.log` | Health check every 5 minutes, plus a full check every hour |
| `NTPC-*.log` | Service console output (rotated every 10 MB) |

`audit.jsonl` and `queries.jsonl` are renamed with a date when they reach `LOG_MAX_MB` (50 MB).
If the API stops answering twice in a row, the health-check task restarts it and logs that.

## 6. Backup and restore

```powershell
powershell -ExecutionPolicy Bypass -File C:\NTPC-Chatbot\scripts\backup.ps1
# daily at 02:00:
schtasks /Create /TN NTPC-Chatbot-Backup /SC DAILY /ST 02:00 /RU SYSTEM /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\NTPC-Chatbot\scripts\backup.ps1"
```

**What's saved** (in `backups\<date>\`):
- the conversation database (a consistent copy, safe while running),
- the semantic/example YAML files,
- old logs (zipped).

Backups older than 30 days are deleted. `.env` is **not** backed up because it holds passwords;
keep its values in the password safe.

**Restore:**
1. Stop `NTPC-Chatbot-API`.
2. Copy `chatbot.sqlite` back to `data\`.
3. Copy the YAML files back.
4. Start the service.
5. Run `index_qdrant.py`.

Qdrant itself never needs restoring, because it is rebuilt from the YAML files.

## 7. Changing what the chatbot knows

| Change | Edit | Then |
|---|---|---|
| New example question | `examples\examples.yaml` (copy an existing block) | `.\.venv\Scripts\python.exe scripts\index_qdrant.py` |
| Plant nickname / alias | `semantic\aliases.yaml` | `Restart-Service NTPC-Chatbot-API` |
| Glossary term | `semantic\glossary.yaml` | `index_qdrant.py` |
| Metric, unit or switching a metric on/off | `semantic\metrics.yaml` | `Restart-Service NTPC-Chatbot-API` |
| Data-owner decisions | `semantic\decisions.yaml` (record of decisions) | nothing; it is documentation |

After any change, run the evaluation to make sure nothing broke. It reads the production database
read-only and uses the LLM:
```powershell
.\.venv\Scripts\python.exe eval\run_eval.py --quick
```

## 8. Switching the model (when the GPU arrives)

The chatbot talks to any OpenAI-compatible server, so only `.env` changes.

**Option A: Ollama on a Windows GPU server (fewer simultaneous users)**
```powershell
ollama pull qwen3:32b
ollama create qwen3-32b-ntpc -f C:\NTPC-Chatbot\ollama\Modelfile.qwen3-32b-ntpc
```
In `.env`, set:
```
LLM_PROVIDER=ollama
LLM_MODEL=qwen3-32b-ntpc
MAX_CONCURRENT_CHATS=4
```
Use `-OllamaParallel 4` for the service.

**Option B: vLLM on a Linux GPU server (recommended for 20+ simultaneous users)**
```bash
pip install vllm
vllm serve Qwen/Qwen3-32B-AWQ --host 0.0.0.0 --port 8001 --api-key <long-random-key> \
     --max-model-len 8192 --enable-auto-tool-choice --tool-call-parser hermes --gpu-memory-utilization 0.90
```
Check these flags against the vLLM version you install: tool calling must be switched on for
Qwen3. In `.env` on the Windows Server, set:
```
LLM_PROVIDER=vllm
LLM_BASE_URL=http://<gpu-server>:8001/v1
LLM_MODEL=Qwen/Qwen3-32B-AWQ
LLM_API_KEY=<the same key>
MAX_CONCURRENT_CHATS=16
```
The embedding model (bge-m3) can stay on Ollama (`EMBED_BASE_URL`).

**After switching:**
1. `Restart-Service NTPC-Chatbot-API`.
2. Run the full evaluation, `.\.venv\Scripts\python.exe eval\run_eval.py`, and compare it with the
   last report in `outputs\eval\`. It should stay at or near 91/91, and answers should take seconds
   instead of a minute.

## 9. Security checklist before go-live

- [ ] New strong database password; `check_db_connection.py` shows **PASS** (read-only).
- [ ] New `API_KEY`, used only in `.env` and the ASP.NET `web.config`.
- [ ] `DEBUG=false` (hides the API documentation page and SQL in answers).
- [ ] Firewall: port 8000 open only for the ASP.NET server (`install_services.ps1 -AllowFrom`).
- [ ] `.env` readable only by Administrators and SYSTEM (the installer sets this).
- [ ] The ASP.NET handler requires a logged-in user and sends `X-User` (audit trail).
- [ ] The IT security team has reviewed the API and widget.

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| Widget says "Missing or invalid API key" | `web.config` `ChatbotApiKey` = `.env` `API_KEY`; restart the API after changing `.env` |
| "The chatbot API key is not configured" | `API_KEY` in `.env` is empty or `change-me` |
| Answers very slow (> 1 min) | CPU-only model; set `MAX_CONCURRENT_CHATS` to match the hardware; see section 8 |
| "The language model is not reachable" | `Get-Service NTPC-Ollama`, or the vLLM server; `LLM_BASE_URL` in `.env` |
| "The database took too long to answer" | SQL Server load or network; `logs\queries.jsonl` shows which query |
| Live values missing for a plant | Normal when its PI feed is down; the answer says so and gives yesterday's figure |
| Answer appears only at the end (no typing) | Compression or buffering on the ASP.NET side (docs/EMBEDDING.md, step 5) |
| Service will not start | `logs\NTPC-<service>.log`; run the same command by hand in PowerShell |
| Example questions seem ignored | Qdrant down or empty: `Get-Service NTPC-Qdrant`, re-run `index_qdrant.py` |
