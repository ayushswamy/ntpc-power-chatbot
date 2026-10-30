# NTPC Power Data Chatbot — rules for Claude

Chatbot answering questions on NTPC live + historical power data from SQL Server
10.4.8.44 / DB_PlantLive. Full spec: phases 0–6 (setup, semantic layer, tools, agent,
eval, API+widget, deployment). Stop after each phase, summarise, list open
questions, wait for "continue".

## The user
Beginner coder. For every script: explain briefly what it does and give the exact
command to run it. Keep code simple and well commented. Windows + PowerShell;
run Python as `.\.venv\Scripts\python.exe`.

## Production database — hard rules
- Before running ANY script/query against the DB, show what it does and wait for approval.
- SELECT only. TOP/row limit. 30 s timeout. Date filter on large tables
  (Data_Outage, Data_Partial, Data_Generation, Performance_DayCum_*, XML_RealView).
- Never hardcode, print or log credentials (they live in .env only).
- Connect only via `app.db.connection.get_connection()`.
- Save query outputs (samples, test results, eval reports) to outputs/.

## Data rules
- 1900-01-01 = missing. Ignore future dates (Data_Generation has 2030 rows).
- Daily tables end at yesterday; "now/current/live/today" use real-time sources
  (Data_RealTime_PIData, XML_RealView). Label live values "as of HH:MM".
- Only PI tags updated within LIVE_FRESHNESS_MINUTES count as current.
- Out-of-range requests: state the available range, answer for it. Never estimate.
- Ignore Temp_*, *_bkup_*, import_log, empty tables, tbl_Pithead_NonPithead_DayReport,
  Temp_RealViewData_Computed, Report_RealViewData.

## Production (Phase 6)
Windows Server, installed by the user as admin; internet unknown -> offline package
(scripts/build_offline_package.ps1, deploy/*.ps1, DEPLOYMENT.md). >20 concurrent users ->
LLM on a Linux GPU server with vLLM (LLM_PROVIDER=vllm) when the GPU arrives; until then CPU model.
Data-owner decisions: semantic/decisions.yaml.

## Environment (dev PC)
No GPU, 16 GB RAM → LLM_MODEL=qwen3:8b on CPU (prod target qwen3:32b on GPU).
Ollama at localhost:11434 (OpenAI-compatible /v1). Docker engine not usable (no
admin) → run Qdrant as native Windows binary. ODBC Driver 17 for SQL Server.
