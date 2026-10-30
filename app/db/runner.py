"""
Safe query runner - the ONLY way the chatbot sends SQL to the database.

Every query:
  - is validated by app/db/sql_validator.py (single SELECT, allowed tables,
    date filter on large tables, TOP limit),
  - uses ? parameters for values (never string-pasted user input),
  - times out after QUERY_TIMEOUT_SECONDS (set on the connection),
  - returns at most max_rows rows,
  - is logged to logs/queries.jsonl: time, purpose, SQL, parameters, row count,
    duration, error. Result data and credentials are NEVER logged.

For tests there are two stand-ins with the same .run() method:
  RecordingRunner - runs real queries and remembers the results (to save as a fixture)
  ReplayRunner    - answers from a saved fixture file, no database needed
"""
import hashlib
import json
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pyodbc
import yaml

from app.config import PROJECT_ROOT, settings
from app.db.connection import get_connection
from app.db.sql_validator import validate_sql


class QueryError(Exception):
    """Raised when a query is rejected or fails. The message is safe to show."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    seconds: float = 0.0
    truncated: bool = False
    sql: str = ""
    params: list = field(default_factory=list)


def _load_allowed_tables() -> tuple[set[str], set[str]]:
    """(tables the tools may use, tables the LLM's text-to-SQL may use) from semantic/tables.yaml."""
    doc = yaml.safe_load(open(PROJECT_ROOT / "semantic" / "tables.yaml", encoding="utf-8"))
    ignore = {t.lower() for t in doc.get("ignore", [])}
    tools = {name.lower() for name in doc["tables"]} - ignore - {"data_partial"}
    text2sql = {name.lower() for name, t in doc["tables"].items() if t.get("text2sql")} - ignore
    return tools, text2sql


TOOL_TABLES, TEXT2SQL_TABLES = _load_allowed_tables()


def _compact(sql: str) -> str:
    return " ".join(sql.split())


# Per-request list of the queries that ran (for the audit log and the DEBUG "sql_debug" field)
_trace: ContextVar[list | None] = ContextVar("query_trace", default=None)


def start_trace() -> list:
    trace: list = []
    _trace.set(trace)
    return trace


def _add_trace(purpose: str, sql: str, rows: int, ms: int, error: str | None = None) -> None:
    trace = _trace.get()
    if trace is not None:
        trace.append({"purpose": purpose, "sql": _compact(sql), "rows": rows, "ms": ms, "error": error})


def _log(entry: dict) -> None:
    from app.logs import append_jsonl
    append_jsonl("queries", entry)


class QueryRunner:
    """Runs validated queries against SQL Server."""

    def __init__(self, allowed_tables: set[str] | None = None):
        self.allowed_tables = allowed_tables or TOOL_TABLES

    def run(self, sql: str, params: tuple | list = (), purpose: str = "", max_rows: int | None = None) -> QueryResult:
        limit = max_rows or settings.max_rows
        check = validate_sql(sql, self.allowed_tables, max_rows=settings.max_rows)
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "purpose": purpose,
                 "sql": _compact(sql)[:1000], "params": [str(p) for p in params]}
        if not check.ok:
            entry["error"] = f"rejected: {check.reason}"
            _log(entry)
            _add_trace(purpose, sql, 0, 0, entry["error"])
            raise QueryError(f"Query rejected by safety check: {check.reason}")

        start = time.perf_counter()
        try:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(check.sql, list(params))
                columns = [d[0] for d in cur.description]
                raw = cur.fetchmany(limit + 1)
            finally:
                conn.close()
        except pyodbc.Error as exc:
            entry.update(error=str(exc)[:300], ms=round((time.perf_counter() - start) * 1000))
            _log(entry)
            _add_trace(purpose, check.sql, 0, entry["ms"], "database error")
            timeout = "HYT00" in str(exc)
            raise QueryError("The database took too long to answer (timeout)." if timeout
                             else "The database query failed.") from None

        seconds = time.perf_counter() - start
        truncated = len(raw) > limit
        rows = [{c: _clean(v) for c, v in zip(columns, r)} for r in raw[:limit]]
        entry.update(rows=len(rows), truncated=truncated, ms=round(seconds * 1000))
        _log(entry)
        _add_trace(purpose, check.sql, len(rows), entry["ms"])
        return QueryResult(columns, rows, seconds, truncated, check.sql, list(params))


def _clean(value):
    """Plain Python values: strip padded char(n) text, Decimal -> float."""
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, Decimal):
        return float(value)
    return value


# ---------------------------------------------------------------- record / replay for tests

def _key(sql: str, params) -> str:
    raw = _compact(sql) + "|" + json.dumps([str(p) for p in params])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _to_json(value):
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__d__": value.isoformat()}
    return value


def _from_json(value):
    if isinstance(value, dict) and "__dt__" in value:
        return datetime.fromisoformat(value["__dt__"])
    if isinstance(value, dict) and "__d__" in value:
        return date.fromisoformat(value["__d__"])
    return value


class RecordingRunner:
    """Wraps a real runner and keeps every result so it can be saved as a test fixture."""

    def __init__(self, inner: QueryRunner):
        self.inner = inner
        self.recorded: dict[str, dict] = {}
        self._lock = threading.Lock()

    def run(self, sql, params=(), purpose="", max_rows=None) -> QueryResult:
        result = self.inner.run(sql, params, purpose, max_rows)
        with self._lock:
            self.recorded[_key(sql, params)] = {
                "purpose": purpose, "columns": result.columns, "truncated": result.truncated,
                "rows": [{k: _to_json(v) for k, v in r.items()} for r in result.rows]}
        return result

    def save(self, path: Path, now: datetime) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"now": now.isoformat(), "queries": self.recorded}, indent=1), encoding="utf-8")


class ReplayRunner:
    """Answers queries from a fixture file saved by RecordingRunner (no database)."""

    def __init__(self, path: Path):
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        self.now = datetime.fromisoformat(doc["now"])
        self.queries = doc["queries"]

    def run(self, sql, params=(), purpose="", max_rows=None) -> QueryResult:
        check = validate_sql(sql, TOOL_TABLES, max_rows=settings.max_rows)  # tests still enforce the rules
        if not check.ok:
            raise QueryError(f"Query rejected by safety check: {check.reason}")
        rec = self.queries.get(_key(sql, params))
        if rec is None:
            raise QueryError(f"No recorded result for this query ({purpose}) - re-record the fixture")
        rows = [{k: _from_json(v) for k, v in r.items()} for r in rec["rows"]]
        _add_trace(purpose, sql, len(rows), 0)
        return QueryResult(rec["columns"], rows, 0.0, rec["truncated"], sql, list(params))


# ---------------------------------------------------------------- the runner the tools use

_runner = None


def get_runner():
    global _runner
    if _runner is None:
        _runner = QueryRunner()
    return _runner


def set_runner(runner) -> None:
    """Swap the runner (tests use ReplayRunner, the tool test script uses RecordingRunner)."""
    global _runner
    _runner = runner
