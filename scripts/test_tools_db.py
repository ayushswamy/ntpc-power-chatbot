"""
Phase 2 - Test every tool against the REAL database (read-only) and record fixtures.

What it does:
  1. Freezes the chatbot clock at the current time (so all tools agree on "today").
  2. Runs each test case below through the tools, with a RecordingRunner that
     remembers every query result. Every query goes through the safe runner:
     validated SELECT, TOP limit, date filter on large tables, 30 s timeout.
  3. Runs data checks that decide open design questions:
       a) "today so far" method: yesterday's 96 blocks summed vs the official daily MU
       b) U1..U13 numbering in XML_RealView (plant unit number or stage position?)
       c) Data_Trips plant codes (PLN_CD) vs plant short names
       d) Data_Outage rows per day
       e) PLF cross-check: plf param vs gross_gen_mu / gross_cap_mu
  4. Saves:
       outputs/tools/tool_results_<time>.json   full results of every case
       outputs/tools/summary_<time>.md          table: case, ok, answer, seconds, queries
       outputs/tools/checks_<time>.json         results of the data checks
       tests/fixtures/tools_fixture.json        recorded query results (for unit tests)
       tests/fixtures/tool_cases.json           the cases + expected answers (for unit tests)

Run from the project folder (takes 1-3 minutes):
    .\\.venv\\Scripts\\python.exe scripts\\test_tools_db.py
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402
from app.db.masters import get_masters, reset_masters  # noqa: E402
from app.db.runner import QueryRunner, RecordingRunner, set_runner  # noqa: E402

OUT = ROOT / "outputs" / "tools"
FIXTURES = ROOT / "tests" / "fixtures"

SING = {"level": "plant", "plant_code": "1004"}          # Singrauli (coal, pithead)
RIHAND_U3 = {"plant_code": "1005", "unit": 3}             # Rihand unit 3 (stage 2)
RIHAND_S2 = {"plant_code": "1005", "stage": "STAGE2"}
KORBA = {"plant_code": "1017"}                            # has dead PI tags since 2009
DADRI_GAS = {"plant_code": "1014"}
AASPL = {"plant_code": "2492"}                            # solar JV without PI feed
NTPC = {"level": "group"}
COAL = {"level": "group", "scope": {"fuel": ["C"]}}
NR = {"level": "group", "scope": {"region": "NR"}}

CASES = [
    # live
    ("get_realtime", [SING]),
    ("get_realtime", [RIHAND_U3]),
    ("get_realtime", [RIHAND_S2, "actual_generation"]),
    ("get_realtime", [KORBA]),
    ("get_realtime", [AASPL]),
    ("get_realtime", [SING, "frequency"]),
    ("get_realtime_summary", [{}]),
    ("get_realtime_summary", [{"fuel": ["C"]}, "gross_generation", "plant"]),
    ("get_realtime_summary", [{"company": "standalone"}, "actual_generation"]),
    ("get_today_so_far", [SING]),
    ("get_today_so_far", [NTPC]),
    ("compare_periods", [SING, "gross_generation", "today", "yesterday"]),
    # historical
    ("get_daily_metric", [SING, "plf", "yesterday"]),
    ("get_daily_metric", [RIHAND_U3, "plf", "yesterday"]),
    ("get_daily_metric", [NTPC, "gross_generation", "yesterday"]),
    ("get_metric_range", [SING, "gross_generation", "this month"]),
    ("get_metric_range", [NTPC, "plf", "last 30 days"]),
    ("get_metric_range", [SING, "plf", "2021-01-01", "2021-03-31"]),
    ("compare_periods", [SING, "plf", "this month", "last month"]),
    ("compare_periods", [NTPC, "gross_generation", "this fy", "last fy"]),
    ("get_trend", [SING, "plf", "this fy", None, "month"]),
    ("get_trend", [DADRI_GAS, "gross_generation", "last 30 days", None, "day"]),
    ("get_trend", [NTPC, "gross_generation", "2023-04-01", "yesterday", "fy"]),
    ("rank_entities", ["plf", "this month", None, 10]),
    ("rank_entities", ["plf", "this month", None, 5, "asc", "plant", {"fuel": ["C"]}]),
    ("rank_entities", ["forced_outage_loss", "last month", None, 5, "desc", "unit"]),
    # events, coal, capacity
    ("get_outages", [SING, "last 7 days"]),
    ("get_outages", [NR, "yesterday"]),
    ("get_trips", [NTPC, "last 7 days", "today"]),
    ("get_trips", [{"plant_code": "1086"}, "last 30 days", "today"]),   # NSTPS: trip-log code 'NPGC' (OPPMS name)
    ("get_coal", [SING, "coal_stock", "yesterday"]),
    ("get_coal", [COAL, "coal_stock_days", "yesterday"]),
    ("get_coal", [SING, "coal_receipt", "last 7 days"]),
    ("get_capacity", [NTPC]),
    ("get_capacity", [SING, "2010-01-01"]),
]


def short(d, n=220):
    text = json.dumps(d, default=str)
    return text if len(text) <= n else text[:n] + "..."


def run_checks(rec: RecordingRunner, now: datetime) -> dict:
    """Data checks a)..e). Uses the same safe runner."""
    from app.tools.common import Entity, Period, latest_daily_date
    from app.tools.historical import period_value
    from app.tools.live import block_energy, current_block, _stage_view

    m = get_masters()
    checks = {}

    # a) today-so-far method on the last complete day
    day = latest_daily_date()
    plants = ["1004", "1005", "1017", "1025", "1033", "1043", "1014", "1020", "2040", "2160"]
    rows = []
    for col in ("TOTALGEN", "AG"):
        res = block_energy(day, 96, plants, col)
        rows.append((col, res))
    comparison = []
    for code in plants:
        notes = []
        official_gross = period_value(Entity("plant", code), "gross_generation", Period(day, day), notes)
        official_ag = period_value(Entity("plant", code), "actual_generation", Period(day, day), notes)
        comparison.append({
            "plant": m.plant_name(code),
            "blocks_TOTALGEN_mu": round(rows[0][1]["per_plant_mu"].get(code, 0), 2),
            "official_gross_gen_mu": official_gross and official_gross["value"],
            "blocks_AG_mu": round(rows[1][1]["per_plant_mu"].get(code, 0), 2),
            "official_ag_mu": official_ag and official_ag["value"],
        })
    checks["a_today_so_far_method"] = {"day": day.isoformat(), "plants": comparison,
                                       "stage_blocks_counted": {c: r["stage_blocks_counted"] for c, r in rows},
                                       "stage_blocks_skipped": {c: r["stage_blocks_skipped"] for c, r in rows}}

    # b) U1..U13 numbering: which U columns are filled for multi-stage plants
    block, numbering = current_block(), []
    for r in block["rows"]:
        if str(r["PlantCode"]) in ("1005", "1018", "1006", "1017"):
            sv = _stage_view(r, now)
            master_units = sorted(u["Unit"] for u in m.units if u["PlantCode"] == str(r["PlantCode"]) and u["Stage"] == r["Stage"])
            numbering.append({"plant": m.plant_name(str(r["PlantCode"])), "stage": r["Stage"],
                              "U_columns_filled": [u["unit"] for u in sv["units"]], "master_units_of_stage": master_units})
    checks["b_unit_column_numbering"] = numbering

    # c) Data_Trips plant codes vs short names
    since = clock.today() - timedelta(days=365)
    trip_codes = rec.run("SELECT TOP 500 PLN_CD, COUNT(*) AS trips FROM dbo.Data_Trips WHERE TRP_DT >= ? GROUP BY PLN_CD",
                         [datetime.combine(since, datetime.min.time())], purpose="check: trip plant codes").rows
    checks["c_trip_plant_codes"] = {
        "mapped": sorted((r["PLN_CD"], m.plant_name(m.by_short_name[r["PLN_CD"].strip().lower()]), r["trips"])
                         for r in trip_codes if r["PLN_CD"] and r["PLN_CD"].strip().lower() in m.by_short_name),
        "not_mapped": sorted((r["PLN_CD"], r["trips"]) for r in trip_codes
                             if not (r["PLN_CD"] and r["PLN_CD"].strip().lower() in m.by_short_name)),
        "trip_types": rec.run("SELECT TOP 20 TRIP_TYPE, COUNT(*) AS n FROM dbo.Data_Trips WHERE TRP_DT >= ? GROUP BY TRIP_TYPE",
                              [datetime.combine(since, datetime.min.time())], purpose="check: trip types").rows,
    }

    # d) Data_Outage rows per day (last 3 days)
    checks["d_outage_rows_per_day"] = rec.run(
        "SELECT TOP 10 OutageDate, COUNT(*) AS n_rows, COUNT(DISTINCT PlantCode) AS plants FROM dbo.Data_Outage "
        "WHERE OutageDate BETWEEN ? AND ? GROUP BY OutageDate ORDER BY OutageDate",
        [day - timedelta(days=2), day], purpose="check: outage rows per day").rows

    # e) PLF cross-check for a few plants on the last complete day
    plf_rows = rec.run(
        "SELECT TOP 200 PlantCode, ParamCode, DayCY FROM dbo.Performance_DayCum_Station WHERE ReportDate = ? "
        "AND ParamCode IN ('plf', 'gross_gen_mu', 'gross_cap_mu') AND PlantCode IN ('1004', '1005', '1025', '1033', '2040')",
        [day], purpose="check: plf formula").rows
    by_plant = {}
    for r in plf_rows:
        by_plant.setdefault(r["PlantCode"], {})[r["ParamCode"]] = r["DayCY"]
    checks["e_plf_formula"] = [{"plant": m.plant_name(c), "plf_param": v.get("plf"),
                                "gen_over_cap_x100": round(v["gross_gen_mu"] / v["gross_cap_mu"] * 100, 2) if v.get("gross_cap_mu") else None}
                               for c, v in sorted(by_plant.items())]
    return checks


def main() -> None:
    from app.db.cache import TTLCache  # noqa: F401  (imported so the cache module is loaded first)
    from app.tools import ALL_TOOLS

    now = datetime.now().replace(microsecond=0)
    clock.set_fixed_now(now)
    rec = RecordingRunner(QueryRunner())
    set_runner(rec)
    reset_masters()
    stamp = now.strftime("%Y%m%d_%H%M")
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Tool tests against the database - clock frozen at {now}")

    results, lines, expected = [], [], []
    lines.append("| # | tool | args | ok | seconds | queries | answer / error |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, (name, args) in enumerate(CASES, 1):
        before = len(rec.recorded)
        start = time.perf_counter()
        try:
            res = ALL_TOOLS[name](*args).to_dict()
        except Exception as exc:  # a crash is a bug - record it and continue
            res = {"tool": name, "ok": False, "error": f"EXCEPTION {type(exc).__name__}: {exc}", "answer": {}, "notes": []}
        secs = time.perf_counter() - start
        queries = len(rec.recorded) - before
        results.append({"case": i, "tool": name, "args": args, "seconds": round(secs, 2), "result": res})
        expected.append({"case": i, "tool": name, "args": args, "ok": res["ok"], "answer": res["answer"], "error": res.get("error")})
        shown = res.get("error") if not res["ok"] and res.get("error") else short(res["answer"])
        lines.append(f"| {i} | {name} | `{short(args, 80)}` | {'yes' if res['ok'] else 'NO'} | {secs:.2f} | {queries} | {shown} |")
        print(f"{i:>2}. {'OK ' if res['ok'] else 'NO '} {secs:5.2f}s  {name} {short(args, 70)}")
        print(f"      {shown}")
        for note in res.get("notes", [])[:3]:
            print(f"      note: {note}")

    print("\nData checks...")
    checks = run_checks(rec, now)
    print(json.dumps(checks, default=str, indent=1)[:6000])

    (OUT / f"tool_results_{stamp}.json").write_text(json.dumps(results, default=str, indent=1), encoding="utf-8")
    (OUT / f"checks_{stamp}.json").write_text(json.dumps(checks, default=str, indent=1), encoding="utf-8")
    (OUT / f"summary_{stamp}.md").write_text(f"# Tool test run {now}\n\n" + "\n".join(lines), encoding="utf-8")
    rec.save(FIXTURES / "tools_fixture.json", now)
    (FIXTURES / "tool_cases.json").write_text(json.dumps(expected, default=str, indent=1), encoding="utf-8")
    ok = sum(1 for r in results if r["result"]["ok"])
    print(f"\n{ok}/{len(results)} cases returned data. Saved outputs/tools/*_{stamp}.* and tests/fixtures/")


if __name__ == "__main__":
    main()
