"""
Phase 1, step 2 - Date ranges, sample rows and code values for the key tables.

Run export_masters.py FIRST: this script reads outputs/phase1/indexes.csv to
decide which queries are safe.

What it does (read-only):
  For each key table:
    a) Date range: first and last valid date, number of 1900-01-01 rows,
       number of future-dated rows.
    b) Sample rows from the LATEST valid date (5-300 rows depending on table).
    c) Distinct values of code columns (e.g. ParamCode, OutageType) with counts.

  LARGE tables (Performance_DayCum_*, Data_Generation, Data_Outage, Data_Partial,
  XML_RealView) are only queried if their date column has an index. Every query
  on them filters on the date column and seeks via that index:
     - first/last date: SELECT TOP 1 ... ORDER BY date  (index seek, instant)
     - samples and code values: ONLY the latest date (one day of data)
  If there is no usable index, the table is SKIPPED and reported.

  Medium tables (under ~2 million rows) are read with simple TOP-limited
  queries; scanning them takes a second or two.

Output: outputs/phase1/date_ranges.csv, samples/*.csv, distinct/*.csv, profile_log.txt

Run from the project folder:
    .\\.venv\\Scripts\\python.exe scripts\\profile_tables.py
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUTS_DIR  # noqa: E402
from app.db.connection import get_connection  # noqa: E402
from app.db.query import run_select, save_csv  # noqa: E402

OUT = OUTPUTS_DIR / "phase1"

# (table, date column, code columns, large?, sample rows, sample "group" column)
# sample group column: take the sample rows for ONE value of this column
# (e.g. all parameters of one plant) instead of 5 random rows.
TABLES = [
    ("Performance_DayCum_Station", "ReportDate", ["ParamCode"], True, 300, "PlantCode"),
    ("Performance_DayCum_Stage", "ReportDate", ["ParamCode"], True, 600, "PlantCode"),
    ("Performance_DayCum_Unit", "ReportDate", ["ParamCode"], True, 2000, "PlantCode"),
    ("Data_Generation", "p_date", ["skf_code", "UM", "p_code"], True, 300, "p_code"),
    ("XML_RealView", "ReportDate", ["PlantCode", "TYPE", "REGION", "STATUS", "BLK"], True, 300, "PlantCode"),
    ("Data_Outage", "OutageDate", ["OutageType", "RSN_Code", "PlantCode"], True, 20, None),
    ("Data_Partial", "OutageDate", ["OutageType", "RSN_Code", "PlantCode"], True, 20, None),
    ("Report_Generation", "ReportDate", ["CY_Group", "CY_Fuel", "PlantCode"], False, 40, None),
    ("XML_DayFig_Station", "ReportDate", ["stn_type", "stn_fuel_link", "stn_cd"], False, 5, None),
    ("XML_DayFig_Stage", "ReportDate", ["stn_cd"], False, 5, None),
    ("XML_DayFig_Unit", "ReportDate", ["comstatus", "cstg"], False, 5, None),
    ("XML_CumYearFig_Station", "ReportDate", ["stn_type"], False, 5, None),
    ("XML_CumYearFig_Stage", "ReportDate", [], False, 5, None),
    ("XML_CumYearFig_Unit", "ReportDate", ["cstg"], False, 5, None),
    ("Data_DailyFigures", "thisday", ["PlantCode", "PROJECT"], False, 5, None),
    ("Data_Generation_JV", "p_date", ["JVstation_code"], False, 5, None),
    ("Report_OutageBreakup", "ReportDate", ["OutageType", "OutageCategory", "AreaName", "FinYear"], False, 10, None),
    ("Data_Trips", "TRP_DT", ["TRIP_TYPE", "PLN_CD"], False, 10, None),
    ("Report_CoalTripData", "ReportDate", ["TYPE", "REASON"], False, 5, None),
    ("XML_CoalTrip", "TRP_DT", ["TRIP_TYPE", "FuelType"], False, 5, None),
    ("Data_FuelReceipt", "txn_date", ["fuel_code", "p_code"], False, 10, None),
    ("Data_FuelConsumption", "txn_date", ["fuel_code", "txn_code"], False, 10, None),
    ("Data_SPCoal", "year", ["PlantCode"], False, 5, None),
    ("Data_DCSG", "gendate", ["site", "parameter"], False, 5, None),
    ("DC_SG_MobileApp_dashboard", "Date", [], False, 5, None),
    ("URS_MobileApp", "Date", [], False, 5, None),
    ("MorningMeeting_Remarks", "ReportDate", ["RemarkKey"], False, 5, None),
    ("Data_RealTime_Alert", "UpdatedOn", ["PlantCode"], False, 24, None),
]

# "Valid" dates: after 1900-01-01 and not later than today (server clock)
TOMORROW = "DATEADD(day, 1, CAST(GETDATE() AS date))"


def load_date_indexed_tables() -> set[tuple[str, str]]:
    """(table, column) pairs where the column is the FIRST key of an index, or table has a columnstore."""
    path = OUT / "indexes.csv"
    if not path.exists():
        sys.exit("outputs/phase1/indexes.csv not found - run scripts/export_masters.py first.")
    usable = set()
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            keys = [k.strip() for k in row["key_columns"].split(",") if k.strip()]
            if keys:
                usable.add((row["table_name"], keys[0].lower()))
            if "COLUMNSTORE" in row["index_type"]:
                usable.add((row["table_name"], "*"))
    return usable


def main() -> None:
    usable = load_date_indexed_tables()
    log: list[str] = []
    ranges: list[tuple] = []

    def out(text: str = "") -> None:
        print(text)
        log.append(text)

    out(f"Profile tables - {datetime.now():%Y-%m-%d %H:%M:%S}")
    conn = get_connection()
    try:
        for table, dcol, code_cols, large, n_sample, group_col in TABLES:
            out(f"\n=== {table} (date column: {dcol}) ===")
            t = f"dbo.{table}"
            try:
                if large:
                    if (table, dcol.lower()) not in usable and (table, "*") not in usable:
                        out(f"  SKIPPED: no index starts with {dcol} - querying it would scan the whole table")
                        ranges.append((table, dcol, "SKIPPED (no date index)", "", "", "", "", ""))
                        continue
                    # Index seeks only: first date, last date, last future date
                    _, r, s1, _ = run_select(conn, f"SELECT TOP 1 {dcol} FROM {t} WHERE {dcol} >= '1900-01-02' ORDER BY {dcol} ASC")
                    min_valid = r[0][0] if r else None
                    _, r, s2, _ = run_select(conn, f"SELECT TOP 1 {dcol} FROM {t} WHERE {dcol} >= '1900-01-02' AND {dcol} < {TOMORROW} ORDER BY {dcol} DESC")
                    max_valid = r[0][0] if r else None
                    _, r, s3, _ = run_select(conn, f"SELECT TOP 1 {dcol} FROM {t} WHERE {dcol} >= {TOMORROW} ORDER BY {dcol} DESC")
                    max_future = r[0][0] if r else None
                    _, r, s4, _ = run_select(conn, f"SELECT TOP 1 {dcol} FROM {t} WHERE {dcol} < '1900-01-02' ORDER BY {dcol}")
                    has_1900 = "yes" if r else "no"
                    ranges.append((table, dcol, "index seek", min_valid, max_valid, has_1900, max_future or "", ""))
                    out(f"  valid range {min_valid} .. {max_valid}   1900 rows: {has_1900}   latest future date: {max_future}   ({s1+s2+s3+s4:.2f}s)")
                else:
                    sql = f"""
                        SELECT MIN(CASE WHEN {dcol} >= '1900-01-02' AND {dcol} < {TOMORROW} THEN {dcol} END) AS min_valid,
                               MAX(CASE WHEN {dcol} >= '1900-01-02' AND {dcol} < {TOMORROW} THEN {dcol} END) AS max_valid,
                               SUM(CASE WHEN {dcol} < '1900-01-02' THEN 1 ELSE 0 END) AS rows_1900,
                               MAX(CASE WHEN {dcol} >= {TOMORROW} THEN {dcol} END) AS max_future,
                               SUM(CASE WHEN {dcol} >= {TOMORROW} THEN 1 ELSE 0 END) AS rows_future,
                               SUM(CASE WHEN {dcol} IS NULL THEN 1 ELSE 0 END) AS rows_null
                        FROM {t}"""
                    _, r, secs, _ = run_select(conn, sql)
                    min_valid, max_valid, rows_1900, max_future, rows_future, rows_null = r[0]
                    ranges.append((table, dcol, "scan", min_valid, max_valid, rows_1900, max_future or "", rows_future))
                    out(f"  valid range {min_valid} .. {max_valid}   1900 rows: {rows_1900}   future rows: {rows_future} (up to {max_future})   NULL dates: {rows_null}   ({secs:.2f}s)")

                if max_valid is None:
                    out("  no valid dates - no samples")
                    continue

                # Samples from the latest valid date
                if group_col:
                    sql = (f"SELECT TOP {n_sample} * FROM {t} WHERE {dcol} = ? AND {group_col} = "
                           f"(SELECT TOP 1 {group_col} FROM {t} WHERE {dcol} = ?)")
                    params = (max_valid, max_valid)
                else:
                    sql = f"SELECT TOP {n_sample} * FROM {t} WHERE {dcol} = ?"
                    params = (max_valid,)
                cols, rows, secs, _ = run_select(conn, sql, params, max_rows=n_sample)
                save_csv(OUT / "samples" / f"{table}.csv", cols, rows)
                out(f"  sample: {len(rows)} rows from {max_valid}  ({secs:.2f}s)")

                # Distinct code values (large: latest date only; medium: whole table)
                for col in code_cols:
                    where = f"WHERE {dcol} = ?" if large else ""
                    params = (max_valid,) if large else ()
                    sql = f"SELECT TOP 2000 {col}, COUNT(*) AS row_count FROM {t} {where} GROUP BY {col} ORDER BY COUNT(*) DESC"
                    cols, rows, secs, truncated = run_select(conn, sql, params, max_rows=2000)
                    save_csv(OUT / "distinct" / f"{table}__{col}.csv", cols, rows)
                    scope = f"on {max_valid}" if large else "all rows"
                    out(f"  distinct {col}: {len(rows)}{'+' if truncated else ''} values ({scope}, {secs:.2f}s)")
            except Exception as exc:  # keep going with the next table; the error is logged
                out(f"  ERROR: {type(exc).__name__}: {exc}")
                ranges.append((table, dcol, f"ERROR: {type(exc).__name__}", "", "", "", "", ""))
    finally:
        conn.close()

    save_csv(OUT / "date_ranges.csv",
             ["table", "date_column", "method", "min_valid_date", "max_valid_date", "rows_1900_or_found",
              "max_future_date", "rows_future"], ranges)
    (OUT / "profile_log.txt").write_text("\n".join(log), encoding="utf-8")
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    main()
