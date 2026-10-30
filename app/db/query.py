"""
Small helpers for running READ-ONLY queries and saving results as CSV.

Used by the Phase 1 export scripts. (Phase 2 adds a stricter runner with
sqlglot validation for SQL written by the LLM.)

Safety built in:
- The SQL must start with SELECT or WITH and must not contain any
  data-changing keyword (INSERT, UPDATE, DELETE, INTO, EXEC, ...).
- At most `max_rows` rows are fetched, even if the query returns more.
- The query timeout (30 s) is set on the connection in app/db/connection.py.
"""
import csv
import re
import time
from datetime import date, datetime
from pathlib import Path

import pyodbc

from app.config import settings

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|DENY"
    r"|INTO|BACKUP|RESTORE|DBCC|SHUTDOWN|KILL|OPENROWSET|OPENQUERY|BULK)\b",
    re.IGNORECASE,
)


class UnsafeQueryError(Exception):
    pass


def check_read_only(sql: str) -> None:
    """Raise UnsafeQueryError unless `sql` is a single plain SELECT."""
    text = sql.strip().rstrip(";")
    if not re.match(r"^(SELECT|WITH)\b", text, re.IGNORECASE):
        raise UnsafeQueryError("Query must start with SELECT or WITH")
    if ";" in text:
        raise UnsafeQueryError("Only one statement is allowed")
    match = _FORBIDDEN.search(text)
    if match:
        raise UnsafeQueryError(f"Forbidden keyword in query: {match.group(0)}")


def run_select(conn: pyodbc.Connection, sql: str, params: tuple = (), max_rows: int | None = None):
    """
    Run a read-only query. Returns (columns, rows, seconds, truncated).
    `truncated` is True when the query had more rows than max_rows.
    """
    check_read_only(sql)
    limit = max_rows or settings.max_rows
    start = time.perf_counter()
    cur = conn.cursor()
    cur.execute(sql, params)
    columns = [d[0] for d in cur.description]
    rows = cur.fetchmany(limit + 1)
    cur.close()
    truncated = len(rows) > limit
    return columns, [tuple(r) for r in rows[:limit]], time.perf_counter() - start, truncated


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, str):
        return value.rstrip()  # char(n) columns come back padded with spaces
    return value


def save_csv(path: Path, columns: list[str], rows: list[tuple]) -> None:
    """Save rows to CSV (utf-8 with BOM so Excel opens it correctly)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(v) for v in row])
