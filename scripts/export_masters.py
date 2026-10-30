"""
Phase 1, step 1 - Export master tables and database metadata.

What it does (read-only, all tables involved are small):
  1. Reads database settings: whether "read committed snapshot" is on
     (if yes, our reads can never block the jobs that write data).
  2. Lists every index on every table (which columns can be filtered fast).
  3. Exports these small tables IN FULL (each has fewer than 1000 rows):
     Master_Plants, Master_Stages, Master_Units, Master_Part_Installed_Capacity,
     Master_Blocks, Master_OutageCodes, Master_PITags, Master_GasUnitMapping,
     Master_SKF, Master_UnitSeq, Performance_DayCum_Params,
     Performance_DayCum_Status, Data_SGPercentage.

Output: outputs/phase1/  (database_settings.csv, indexes.csv, masters/*.csv, export_masters_log.txt)

Run from the project folder:
    .\\.venv\\Scripts\\python.exe scripts\\export_masters.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUTS_DIR  # noqa: E402
from app.db.connection import get_connection  # noqa: E402
from app.db.query import run_select, save_csv  # noqa: E402

OUT = OUTPUTS_DIR / "phase1"

MASTER_TABLES = [
    "Master_Plants", "Master_Stages", "Master_Units", "Master_Part_Installed_Capacity",
    "Master_Blocks", "Master_OutageCodes", "Master_PITags", "Master_GasUnitMapping",
    "Master_SKF", "Master_UnitSeq", "Performance_DayCum_Params",
    "Performance_DayCum_Status", "Data_SGPercentage",
]

SQL_DB_SETTINGS = """
SELECT name, compatibility_level, is_read_committed_snapshot_on,
       snapshot_isolation_state_desc, recovery_model_desc
FROM sys.databases WHERE name = DB_NAME()
"""

# One row per (index, column). Heaps (tables without a clustered index) show index_type = HEAP.
SQL_INDEXES = """
SELECT TOP 5000 t.name AS table_name, i.name AS index_name, i.type_desc AS index_type,
       i.is_unique, i.is_primary_key, ic.key_ordinal, ic.is_included_column,
       c.name AS column_name
FROM sys.tables t
JOIN sys.indexes i ON i.object_id = t.object_id
LEFT JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
LEFT JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
ORDER BY t.name, i.index_id, ic.is_included_column, ic.key_ordinal
"""


def main() -> None:
    log: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        log.append(text)

    out(f"Export masters - {datetime.now():%Y-%m-%d %H:%M:%S}")
    conn = get_connection()
    try:
        # 1. Database settings
        cols, rows, secs, _ = run_select(conn, SQL_DB_SETTINGS)
        save_csv(OUT / "database_settings.csv", cols, rows)
        for col, val in zip(cols, rows[0]):
            out(f"  {col}: {val}")
        out()

        # 2. Indexes -> one summary line per index
        cols, rows, secs, _ = run_select(conn, SQL_INDEXES)
        save_csv(OUT / "indexes_raw.csv", cols, rows)
        summary: dict[tuple, dict] = {}
        for r in rows:
            rec = dict(zip(cols, r))
            key = (rec["table_name"], rec["index_name"] or "(heap)", rec["index_type"])
            entry = summary.setdefault(key, {"unique": rec["is_unique"], "keys": [], "includes": []})
            if rec["column_name"]:
                target = "includes" if rec["is_included_column"] else "keys"
                entry[target].append(rec["column_name"])
        index_rows = [
            (t, name, itype, e["unique"], ", ".join(e["keys"]), ", ".join(e["includes"]))
            for (t, name, itype), e in summary.items()
        ]
        save_csv(OUT / "indexes.csv",
                 ["table_name", "index_name", "index_type", "is_unique", "key_columns", "included_columns"],
                 index_rows)
        out(f"Indexes: {len(index_rows)} indexes on {len({r[0] for r in index_rows})} tables -> indexes.csv")
        out()

        # 3. Full export of small tables
        for table in MASTER_TABLES:
            sql = f"SELECT TOP 10000 * FROM dbo.{table}"
            cols, rows, secs, truncated = run_select(conn, sql, max_rows=10000)
            save_csv(OUT / "masters" / f"{table}.csv", cols, rows)
            note = "  (TRUNCATED!)" if truncated else ""
            out(f"  {table:<34} {len(rows):>6} rows  {secs:5.2f}s{note}")
    finally:
        conn.close()

    (OUT / "export_masters_log.txt").write_text("\n".join(log), encoding="utf-8")
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    main()
