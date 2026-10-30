# NTPC Power Data Chatbot

An AI chatbot for NTPC's operations web app. It answers questions about live and
historical power data (generation, PLF, outages, trips, coal) using only data from
the `DB_PlantLive` SQL Server database.

Status: **Phase 0 (setup)**. See `CLAUDE.md` for the project rules and phase plan.

## Folder layout

| Folder | What's inside |
|---|---|
| `discovery/` | Table/column lists exported from DB_PlantLive |
| `semantic/` | YAML descriptions of tables, metrics, glossary, name aliases, live PI tags |
| `examples/` | Example questions with the expected tool/SQL and answer |
| `app/db/` | DB connection, safe query runner, SQL validator, live-data cache |
| `app/tools/` | Predefined, tested queries (live + historical) |
| `app/agent/` | The chatbot logic (LangGraph) |
| `app/rag/` | Qdrant indexing and search |
| `app/api/` | FastAPI web API |
| `app/memory/` | Conversation history (SQLite) |
| `widget/` | Chat widget (JavaScript/CSS) for the ASP.NET page |
| `scripts/` | One-off scripts (checks, exports, CLI chat) |
| `tests/`, `eval/` | Unit tests and accuracy evaluation |
| `outputs/` | Saved query results and reports (record of what was checked) |

## First-time setup (Windows, PowerShell)

Run all commands from the project folder `D:\ntpc-power-chatbot`.

1. **Create the virtual environment and install packages** (already done on this PC):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. **Create `.env`**: copy `.env.example` to `.env` and fill in `DB_USER` and
   `DB_PASSWORD`. Never share this file.
3. **Check the DB connection and that the login is read-only**:
   ```powershell
   .\.venv\Scripts\python.exe scripts\check_db_connection.py
   ```
   The report is saved in `outputs\phase0\`.
4. **LLM models (Ollama)**:
   ```powershell
   ollama pull qwen3:8b
   ollama pull bge-m3
   ```

## Running the chatbot (development)

1. **Ollama** must be running (tray icon). One-time: `ollama create qwen3-8b-ntpc -f ollama\Modelfile.qwen3-8b-ntpc`
2. **Qdrant** - start it in its own PowerShell window and leave it open:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start_qdrant.ps1
   ```
3. **Index examples, glossary and schema** (once, and after editing the YAML files):
   ```powershell
   .\.venv\Scripts\python.exe scripts\index_qdrant.py
   ```
4. **Chat in the terminal** (queries the production DB read-only):
   ```powershell
   .\.venv\Scripts\python.exe scripts\chat_cli.py
   ```
   Commands: `/new` new conversation, `/debug` show tool/SQL/timings, `/quit`.

On this CPU-only PC an answer takes about 1 minute. `ANSWER_MODE=template` in `.env` skips
the second LLM call (faster, fixed wording).

5. **Web API + chat widget** (Phase 5). Start the API in its own PowerShell window:
   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
   ```
   - Health: http://127.0.0.1:8000/health
   - Widget demo: `http://127.0.0.1:8000/widget/demo.html?key=<API_KEY from .env>` (test only - the key
     is never put in a real page)
   - Adding the widget to the ASP.NET application: [docs/EMBEDDING.md](docs/EMBEDDING.md)
   - End-to-end check of the running API: `.\.venv\Scripts\python.exe scripts\api_smoke_test.py`

## Production deployment

See [DEPLOYMENT.md](DEPLOYMENT.md): offline package, Windows services, backups, switching to the GPU model / vLLM,
security checklist. The ASP.NET side is described in [docs/EMBEDDING.md](docs/EMBEDDING.md).

## Tests

```powershell
# Unit tests - no database needed (replays recorded query results from tests\fixtures\)
.\.venv\Scripts\python.exe -m pytest tests -q

# Test all tools against the REAL database and re-record the fixtures (read-only, ~1 minute)
.\.venv\Scripts\python.exe scripts\test_tools_db.py
```
Every database query is logged (without data or credentials) in `logs\queries.jsonl`.

### Accuracy evaluation (reads the production DB, uses the LLM)
```powershell
.\.venv\Scripts\python.exe eval\run_eval.py --quick          # 25 questions, ~25 min
.\.venv\Scripts\python.exe eval\run_eval.py                  # all 91 questions, ~1 hour on this PC
.\.venv\Scripts\python.exe eval\run_eval.py --ids D01,CV2    # selected examples / conversations
```
Reports are written to `outputs\eval\` (summary + failure list in the `.md`, full details in the `.json`).
After adding examples to `examples\examples.yaml`, re-run `scripts\index_qdrant.py` and the evaluation.

## Hardware note

This development PC has no GPU (i5-12500, 16 GB RAM), so it uses `qwen3:8b` on
the CPU, which is slow but good enough to build and test. Production should run
`qwen3:32b` on a GPU with at least 24 GB VRAM (for example an L4, A10 or RTX 4090),
or 48 GB+ for several users at once. The model is chosen in `.env` (`LLM_MODEL`).
