"""
Text-to-SQL fallback - used only when no predefined tool fits the question.

1. The LLM gets: rules + a compact description of the allowed tables (from
   semantic/tables.yaml and the real column list) + up to 3 similar example queries.
2. The SQL it writes must pass:
     - app/db/sql_validator.py (single SELECT, allowed tables, date filter on large
       tables, TOP limit - added if missing)
     - a column check: every column must exist in the tables used
   If not, the LLM gets ONE retry with the error message.
3. The query runs through the safe runner (read-only login, timeout, row limit, logged).
"""
import csv
import re

import sqlglot
import yaml
from sqlglot import exp

from app import clock
from app.agent.llm import LLMError, chat
from app.config import PROJECT_ROOT
from app.db.runner import TEXT2SQL_TABLES, QueryError, QueryRunner
from app.db.sql_validator import validate_sql
from app.rag.store import search
from app.tools.common import ToolResult

MAX_ROWS = 200
_schema_text: str | None = None
_columns: dict[str, set[str]] | None = None


_original: dict[str, set[str]] | None = None


def table_columns_original() -> dict[str, set[str]]:
    """table (lower-case) -> column names as spelled in the database."""
    global _original
    if _original is None:
        _original = {}
        with open(PROJECT_ROOT / "discovery" / "plantlive_columns.csv", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                _original.setdefault(row["TABLE_NAME"].lower(), set()).add(row["COLUMN_NAME"])
    return _original


def table_columns() -> dict[str, set[str]]:
    """table (lower-case) -> column names (lower-case), for checking generated SQL."""
    global _columns
    if _columns is None:
        _columns = {t: {c.lower() for c in cols} for t, cols in table_columns_original().items()}
    return _columns


def _collapse(columns: set[str]) -> list[str]:
    """['u1', 'u2', ..., 'u13', 'ag'] -> ['ag', 'u1..u13'] - keeps the prompt short."""
    groups: dict[str, list[int]] = {}
    plain = []
    for c in columns:
        m = re.fullmatch(r"(.*?[A-Za-z_])(\d+)", c)
        if m:
            groups.setdefault(m.group(1), []).append(int(m.group(2)))
        else:
            plain.append(c)
    out = sorted(plain)
    for prefix, nums in sorted(groups.items()):
        out.append(f"{prefix}{min(nums)}..{prefix}{max(nums)}" if len(nums) > 2 else ", ".join(f"{prefix}{n}" for n in sorted(nums)))
    return out


def schema_text() -> str:
    """Compact description of the tables the LLM may use (built once, part of the cached prompt prefix)."""
    global _schema_text
    if _schema_text is None:
        doc = yaml.safe_load(open(PROJECT_ROOT / "semantic" / "tables.yaml", encoding="utf-8"))
        cols = table_columns()
        lines = []
        for name, t in doc["tables"].items():
            if name.lower() not in TEXT2SQL_TABLES:
                continue
            line = f"dbo.{name}: {t.get('purpose', '')}"
            if t.get("date_column"):
                avail = t.get("available") or {}
                line += f" Date column {t['date_column']}" + (f" ({avail.get('from')} to {avail.get('to')})" if avail else "") + "."
            if t.get("large"):
                line += " LARGE: must filter on the date column."
            if t.get("sql_note"):
                line += f" NOTE: {t['sql_note']}"
            real = {c for c in table_columns_original().get(name.lower(), set())}
            line += f"\n  columns: {', '.join(_collapse(real))}"
            lines.append(line)
        _schema_text = "\n".join(lines)
    return _schema_text


PROMPT = """You write ONE Microsoft SQL Server (T-SQL) SELECT query for NTPC's read-only database DB_PlantLive.
Rules:
- Only SELECT. Use only the tables and columns listed below, with the dbo. prefix.
- Always use TOP n (n <= 200). Always filter large tables on their date column.
- Dates 1900-01-01 mean missing; ignore dates after today.
- PlantCode is text like '1004'. Join Master_Plants on PlantCode to show StationName.
- Performance_DayCum_* tables are long format: one row per ParamCode; DayCY = value for the day, MonthCY = month-to-date, YearCY = FY-to-date.
- Answer with the SQL only, inside ```sql ... ```.

Tables:
"""


def _extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else text).strip().rstrip(";")


def check_columns(sql: str) -> str | None:
    """Return an error message if the SQL uses a column that does not exist in its tables."""
    try:
        tree = sqlglot.parse_one(sql, read="tsql")
    except sqlglot.errors.ParseError:
        return None   # the validator reports parse errors
    cols = table_columns()
    tables = {t.name.lower() for t in tree.find_all(exp.Table)}
    known = set().union(*(cols.get(t, set()) for t in tables)) if tables else set()
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)} | {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    unknown = sorted({c.name for c in tree.find_all(exp.Column)
                      if c.name and c.name.lower() not in known and c.name.lower() not in aliases})
    return f"Unknown column(s): {', '.join(unknown)}" if unknown else None


def sql_query(question: str) -> tuple[ToolResult, list[str]]:
    """Returns (ToolResult, list of SQL attempts)."""
    today = clock.today()
    examples = [e for e in search(question, "example", k=6) if e.get("sql")][:3]
    shots = "\n".join(f"Q: {e['question']}\n```sql\n{e['sql']}\n```" for e in examples)
    messages = [{"role": "system", "content": PROMPT + schema_text()},
                {"role": "user", "content": (f"Examples:\n{shots}\n\n" if shots else "")
                 + f"Today is {today.isoformat()} (financial year starts 1 April).\nQuestion: {question}"}]
    attempts: list[str] = []
    runner = QueryRunner(TEXT2SQL_TABLES)
    for attempt in (1, 2):
        try:
            out = chat(messages, max_tokens=400)
        except LLMError as exc:
            return ToolResult("sql_query", ok=False, error=str(exc)), attempts
        sql = _extract_sql(out["content"])
        attempts.append(sql)
        check = validate_sql(sql, TEXT2SQL_TABLES, max_rows=MAX_ROWS, add_top=True)
        problem = None if check.ok else check.reason
        problem = problem or check_columns(check.sql)
        if problem is None:
            try:
                result = runner.run(check.sql, purpose="text2sql", max_rows=MAX_ROWS)
            except QueryError as exc:
                problem = str(exc)
            else:
                tables = sorted({t.name for t in sqlglot.parse_one(check.sql, read="tsql").find_all(exp.Table)})
                rows = result.rows
                # the result values are in the table; answer only says how many result rows there are
                return ToolResult("sql_query", answer={"number_of_result_rows": len(rows), "result_columns": result.columns},
                                  table=rows[:50] or None, sources=tables,
                                  notes=["This answer comes from a query generated for this question (not a standard report); "
                                         "please verify important figures."]
                                  + (["More rows exist; only the first 200 were read."] if result.truncated else [])), attempts
        hint = ""
        if problem.startswith("Unknown column"):
            try:   # tell the model which columns really exist in the tables it used
                used = {t.name.lower() for t in sqlglot.parse_one(check.sql, read="tsql").find_all(exp.Table)}
                cols = table_columns_original()
                hint = " Valid columns: " + "; ".join(f"{t}: {', '.join(_collapse(cols.get(t, set())))}" for t in sorted(used))
            except sqlglot.errors.ParseError:
                pass
        messages += [{"role": "assistant", "content": out["content"]},
                     {"role": "user", "content": f"That query was rejected: {problem}.{hint} Write a corrected query."}]
    return ToolResult("sql_query", ok=False,
                      error="I could not build a safe query for this question. Please rephrase it or ask about "
                            "generation, PLF, availability, outages, trips, coal or capacity."), attempts
