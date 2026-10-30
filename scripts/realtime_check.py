"""
Phase 1, step 3 - Check how often the live (real-time) tables refresh.

Run export_masters.py FIRST (this script checks the XML_RealView index in outputs/phase1/indexes.csv).

What it does (read-only):
  1. Snapshot A:
     - Data_RealTime_PIData: all rows (about 454 - one row per PI tag).
     - XML_RealView, TODAY only: row count, plants, 15-min blocks, update times,
       plus the 300 newest rows; and row counts for the last 3 days
       (to see whether it keeps intraday history).
  2. Waits 10 minutes (change with --wait-minutes).
  3. Snapshot B: the same queries again.
  4. Compares A and B: how many PI tags got a new timestamp/value, how old the
     tags are (fresh / stale / dead), and whether XML_RealView got new rows.

Output: outputs/phase1/realtime/  (snapshots, stale tag list, realtime_check_log.txt)

Run from the project folder (takes about 10 minutes):
    .\\.venv\\Scripts\\python.exe scripts\\realtime_check.py
"""
import argparse
import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUTS_DIR, settings  # noqa: E402
from app.db.connection import get_connection  # noqa: E402
from app.db.query import run_select, save_csv  # noqa: E402

OUT = OUTPUTS_DIR / "phase1" / "realtime"
TODAY = "CAST(GETDATE() AS date)"

SQL_NOW = "SELECT SYSDATETIME() AS server_now"
SQL_PIDATA = "SELECT TOP 2000 * FROM dbo.Data_RealTime_PIData ORDER BY RID"
SQL_RV_TODAY = f"""
SELECT COUNT(*) AS row_count, COUNT(DISTINCT PlantCode) AS plants, COUNT(DISTINCT BLK) AS blocks,
       MIN(BLK) AS min_blk, MAX(BLK) AS max_blk, COUNT(DISTINCT UpdatedOn) AS update_times,
       MIN(UpdatedOn) AS first_update, MAX(UpdatedOn) AS last_update
FROM dbo.XML_RealView WHERE ReportDate = {TODAY}
"""
SQL_RV_LATEST = f"SELECT TOP 300 * FROM dbo.XML_RealView WHERE ReportDate = {TODAY} ORDER BY UpdatedOn DESC"
SQL_RV_DAYS = f"""
SELECT TOP 10 ReportDate, COUNT(*) AS row_count, COUNT(DISTINCT PlantCode) AS plants,
       COUNT(DISTINCT BLK) AS blocks, COUNT(DISTINCT UpdatedOn) AS update_times,
       MIN(UpdatedOn) AS first_update, MAX(UpdatedOn) AS last_update
FROM dbo.XML_RealView WHERE ReportDate >= DATEADD(day, -3, {TODAY})
GROUP BY ReportDate ORDER BY ReportDate DESC
"""


def realview_is_indexed() -> bool:
    path = OUTPUTS_DIR / "phase1" / "indexes.csv"
    if not path.exists():
        sys.exit("outputs/phase1/indexes.csv not found - run scripts/export_masters.py first.")
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            first_key = row["key_columns"].split(",")[0].strip().lower()
            if row["table_name"] == "XML_RealView" and (first_key == "reportdate" or "COLUMNSTORE" in row["index_type"]):
                return True
    return False


def age_bucket(ts, now) -> str:
    if ts is None:
        return "no timestamp"
    age = now - ts
    if age < timedelta(minutes=10):
        return "a) < 10 min"
    if age < timedelta(minutes=settings.live_freshness_minutes):
        return f"b) 10-{settings.live_freshness_minutes} min"
    if age < timedelta(days=1):
        return "c) < 1 day (stale)"
    return "d) > 1 day (dead)"


def snapshot(conn, label: str, use_realview: bool, out) -> dict:
    now = run_select(conn, SQL_NOW)[1][0][0]
    out(f"\n--- Snapshot {label}  (server time {now:%Y-%m-%d %H:%M:%S}) ---")

    cols, rows, secs, _ = run_select(conn, SQL_PIDATA, max_rows=2000)
    save_csv(OUT / f"pidata_{label}.csv", cols, rows)
    recs = [dict(zip(cols, r)) for r in rows]
    out(f"Data_RealTime_PIData: {len(recs)} rows ({secs:.2f}s)")
    for field in ("TimeStamp", "UpdatedOn"):
        buckets: dict[str, int] = {}
        for rec in recs:
            b = age_bucket(rec[field], now)
            buckets[b] = buckets.get(b, 0) + 1
        out(f"  age by {field}: " + ", ".join(f"{k}: {v}" for k, v in sorted(buckets.items())))
    newest = max((r["UpdatedOn"] for r in recs if r["UpdatedOn"]), default=None)
    out(f"  newest UpdatedOn: {newest}")

    result = {"now": now, "pidata": {r["RID"]: r for r in recs}, "cols": cols}
    if use_realview:
        c, r, secs, _ = run_select(conn, SQL_RV_TODAY)
        today = dict(zip(c, r[0]))
        result["rv_today"] = today
        out(f"XML_RealView today: " + ", ".join(f"{k}={v}" for k, v in today.items()) + f" ({secs:.2f}s)")
        c, r, secs, _ = run_select(conn, SQL_RV_LATEST, max_rows=300)
        save_csv(OUT / f"xml_realview_latest_{label}.csv", c, r)
        c, r, secs, _ = run_select(conn, SQL_RV_DAYS, max_rows=10)
        save_csv(OUT / f"xml_realview_days_{label}.csv", c, r)
        for row in r:
            out("  " + ", ".join(f"{k}={v}" for k, v in zip(c, row)))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-minutes", type=float, default=10)
    args = parser.parse_args()

    log: list[str] = []

    def out(text: str = "") -> None:
        print(text, flush=True)
        log.append(text)

    use_realview = realview_is_indexed()
    out(f"Realtime check - {datetime.now():%Y-%m-%d %H:%M:%S}   freshness threshold: {settings.live_freshness_minutes} min")
    if not use_realview:
        out("XML_RealView: SKIPPED - no index starting with ReportDate (would scan 10 million rows)")

    # A new connection per snapshot: no connection is held open during the wait
    conn = get_connection()
    try:
        a = snapshot(conn, "A", use_realview, out)
    finally:
        conn.close()

    out(f"\nWaiting {args.wait_minutes} minutes...")
    time.sleep(args.wait_minutes * 60)

    conn = get_connection()
    try:
        b = snapshot(conn, "B", use_realview, out)
    finally:
        conn.close()

    # Compare PI tags between A and B
    out("\n--- Comparison A -> B ---")
    ts_changed = val_changed = upd_changed = 0
    for rid, rb in b["pidata"].items():
        ra = a["pidata"].get(rid)
        if not ra:
            continue
        ts_changed += ra["TimeStamp"] != rb["TimeStamp"]
        val_changed += ra["PITag_Value"] != rb["PITag_Value"]
        upd_changed += ra["UpdatedOn"] != rb["UpdatedOn"]
    out(f"PI tags: {len(b['pidata'])} total; new TimeStamp: {ts_changed}; new value: {val_changed}; new UpdatedOn: {upd_changed}")
    if use_realview:
        ra, rb = a["rv_today"], b["rv_today"]
        out(f"XML_RealView today: rows {ra['row_count']} -> {rb['row_count']}, "
            f"last update {ra['last_update']} -> {rb['last_update']}, blocks {ra['max_blk']} -> {rb['max_blk']}")

    # Stale / dead tags (older than the freshness threshold at snapshot B)
    limit = b["now"] - timedelta(minutes=settings.live_freshness_minutes)
    stale = [r for r in b["pidata"].values() if r["TimeStamp"] is None or r["TimeStamp"] < limit]
    cols = b["cols"]
    save_csv(OUT / "stale_tags.csv", cols, [tuple(r[c] for c in cols) for r in stale])
    out(f"Stale tags (TimeStamp older than {settings.live_freshness_minutes} min): {len(stale)} -> stale_tags.csv")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "realtime_check_log.txt").write_text("\n".join(log), encoding="utf-8")
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    main()
