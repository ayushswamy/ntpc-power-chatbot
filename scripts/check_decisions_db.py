"""
Check the data behind the decisions in Solution_to_Query.xlsx (read-only, ~1 minute).

What it does - all small SELECTs through the safe runner, on ONE recent complete day
(plus one month for check B):

  A. Heat rate / APC (decision 5): for the latest complete day, compare the daily
     performance table (heatrate, apc) with the XML/SAP summary tables
     (XML_CumYearFig_Station.stn_heat_rate / stn_apc_act / stn_apc_cryr,
      XML_DayFig_Station.stn_apc_act) - which source has valid (non-zero) values?
  B. Outage breakup unit (decision 6): Report_OutageBreakup.GenLoss for last month -
     sum over the month AND the value on the last day - compared with the performance
     table's outage + partial loss in MU (MonthCY / YearCY). Tells whether GenLoss is
     MWh, and whether its rows are daily or cumulative.
  C. Availability formula (decision 10): for every plant on the latest day, test
     avb = (gross_cap_mu - outg_loss) / gross_cap_mu x 100 for Day, Month and Year
     columns, and a version for rsd_avb that adds back reserve shutdown (rsd_mu).
  D. Zero cross-checks (decisions 8, 9): plants with AG, PLF, coal stock or stock days
     = 0 while gross generation > 0 on the latest day (+ BIFPCL's generation).
  E. SG vs DC (decision 3): how many plants have SG > DC on the latest day.

Saves outputs/decisions/checks_<time>.json and a readable checks_<time>.txt

Run from the project folder:
    .\\.venv\\Scripts\\python.exe scripts\\check_decisions_db.py
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.masters import get_masters  # noqa: E402
from app.db.runner import get_runner  # noqa: E402
from app.tools.common import latest_daily_date  # noqa: E402

OUT = ROOT / "outputs" / "decisions"
lines: list[str] = []


def out(text=""):
    print(text, flush=True)
    lines.append(str(text))


def perf(day: date, params: list[str], cols=("DayCY", "MonthCY", "YearCY")) -> dict:
    """{plant: {param: {col: value}}} from Performance_DayCum_Station for one day."""
    sql = (f"SELECT TOP 5000 PlantCode, ParamCode, {', '.join(cols)} FROM dbo.Performance_DayCum_Station "
           f"WHERE ReportDate = ? AND ParamCode IN ({', '.join('?' * len(params))})")
    result = {}
    for r in get_runner().run(sql, [day] + params, purpose="decisions: performance params").rows:
        result.setdefault(r["PlantCode"], {})[r["ParamCode"]] = {c: r[c] for c in cols}
    return result


def main() -> None:
    run = get_runner().run
    m = get_masters()
    day = latest_daily_date()
    checks = {"day": day.isoformat()}
    out(f"Decision checks - latest complete day {day}")

    # ---------------------------------------------------------------- A. heat rate / APC
    out("\n== A. Heat rate / APC sources ==")
    p = perf(day, ["heatrate", "apc", "gross_gen_mu"])
    xml_cum = run("SELECT TOP 500 stn_cd, stn_heat_rate, stn_apc_act, stn_apc_cryr, stn_apc_Norm FROM dbo.XML_CumYearFig_Station "
                  "WHERE ReportDate = ?", [day], purpose="decisions: xml cum heat rate/apc").rows
    xml_day = run("SELECT TOP 500 stn_cd, stn_apc_act, stn_apc_Norm FROM dbo.XML_DayFig_Station WHERE ReportDate = ?",
                  [day], purpose="decisions: xml day apc").rows

    def nz(v):
        return v is not None and v != 0

    generating = {c for c, v in p.items() if nz(v.get("gross_gen_mu", {}).get("DayCY"))}
    summary = {
        "plants_generating": len(generating),
        "performance_heatrate_nonzero_day": sum(nz(p[c].get("heatrate", {}).get("DayCY")) for c in generating),
        "performance_heatrate_nonzero_year": sum(nz(p[c].get("heatrate", {}).get("YearCY")) for c in generating),
        "performance_apc_nonzero_day": sum(nz(p[c].get("apc", {}).get("DayCY")) for c in generating),
        "performance_apc_nonzero_year": sum(nz(p[c].get("apc", {}).get("YearCY")) for c in generating),
        "xml_cum_rows": len(xml_cum),
        "xml_cum_heat_rate_nonzero": sum(nz(r["stn_heat_rate"]) for r in xml_cum),
        "xml_cum_apc_act_nonzero": sum(nz(r["stn_apc_act"]) for r in xml_cum),
        "xml_cum_apc_cryr_nonzero": sum(nz(r["stn_apc_cryr"]) for r in xml_cum),
        "xml_day_apc_act_nonzero": sum(nz(r["stn_apc_act"]) for r in xml_day),
    }
    samples = [{"plant": m.plant_name(r["stn_cd"]), "xml_heat_rate": r["stn_heat_rate"], "xml_apc_act": r["stn_apc_act"],
                "xml_apc_cryr": r["stn_apc_cryr"], "perf_heatrate_year": p.get(r["stn_cd"], {}).get("heatrate", {}).get("YearCY"),
                "perf_apc_year": p.get(r["stn_cd"], {}).get("apc", {}).get("YearCY")}
               for r in xml_cum if nz(r["stn_heat_rate"]) or nz(r["stn_apc_act"])][:15]
    checks["A_heat_rate_apc"] = {"summary": summary, "samples": samples}
    for k, v in summary.items():
        out(f"  {k}: {v}")
    for s in samples[:8]:
        out(f"  {s}")

    # ---------------------------------------------------------------- B. outage breakup unit
    out("\n== B. Report_OutageBreakup.GenLoss unit ==")
    month_end = day.replace(day=1) - timedelta(days=1)
    month_start = month_end.replace(day=1)
    month_sum = run("SELECT TOP 10 OutageType, SUM(GenLoss) AS gen_loss, COUNT(*) AS n_rows, COUNT(DISTINCT ReportDate) AS days "
                    "FROM dbo.Report_OutageBreakup WHERE ReportDate BETWEEN ? AND ? GROUP BY OutageType",
                    [month_start, month_end], purpose="decisions: breakup month sum").rows
    last_day = run("SELECT TOP 10 OutageType, SUM(GenLoss) AS gen_loss, COUNT(*) AS n_rows "
                   "FROM dbo.Report_OutageBreakup WHERE ReportDate = ? GROUP BY OutageType",
                   [month_end], purpose="decisions: breakup last day").rows
    pm = perf(month_end, ["outg_loss", "partl", "total_outage_loss"])
    perf_tot = {param: {col: round(sum((v.get(param, {}).get(col) or 0) for v in pm.values()), 2)
                        for col in ("DayCY", "MonthCY", "YearCY")} for param in ("outg_loss", "partl", "total_outage_loss")}
    checks["B_outage_breakup"] = {"month": f"{month_start}..{month_end}", "breakup_month_sum": month_sum,
                                  "breakup_last_day": last_day, "performance_totals_mu": perf_tot}
    out(f"  month {month_start}..{month_end}")
    out(f"  breakup, sum over the month : {month_sum}")
    out(f"  breakup, last day only      : {last_day}")
    out(f"  performance MU (all plants) : {perf_tot}")

    # ---------------------------------------------------------------- C. availability formula
    out("\n== C. Availability formula ==")
    pa = perf(day, ["avb", "rsd_avb", "gross_cap_mu", "outg_loss", "rsd_mu"])
    result = {"avb": {}, "rsd_avb": {}}
    mismatches = []
    for code, v in pa.items():
        for col in ("DayCY", "MonthCY", "YearCY"):
            cap = (v.get("gross_cap_mu") or {}).get(col)
            loss = (v.get("outg_loss") or {}).get(col) or 0
            rsd = (v.get("rsd_mu") or {}).get(col) or 0
            if not cap:
                continue
            for metric, formula in (("avb", 100 * (cap - loss) / cap), ("rsd_avb", 100 * (cap - loss + rsd) / cap)):
                stored = (v.get(metric) or {}).get(col)
                if stored is None:
                    continue
                ok = abs(stored - formula) <= 0.05
                bucket = result[metric].setdefault(col, {"match": 0, "differ": 0})
                bucket["match" if ok else "differ"] += 1
                if not ok and len(mismatches) < 15:
                    mismatches.append({"plant": m.plant_name(code), "metric": metric, "col": col, "stored": stored,
                                       "formula": round(formula, 2), "rsd_mu": rsd})
    checks["C_availability"] = {"result": result, "mismatch_examples": mismatches}
    out(f"  {result}")
    for x in mismatches[:8]:
        out(f"  differs: {x}")

    # ---------------------------------------------------------------- D. zero cross-checks
    out("\n== D. Zero values while the plant generated ==")
    pz = perf(day, ["gross_gen_mu", "ag_mu", "plf", "coal_stock", "coal_stock_days"], cols=("DayCY",))
    zero = {k: [] for k in ("ag_mu", "plf", "coal_stock", "coal_stock_days")}
    for code, v in pz.items():
        gen = (v.get("gross_gen_mu") or {}).get("DayCY") or 0
        for k in zero:
            val = (v.get(k) or {}).get("DayCY")
            if val == 0 and gen > 0:
                zero[k].append(f"{m.plant_name(code)} (gen {round(gen, 2)} MU)")
    bifpcl = next((c for c, pl in m.plants.items() if "BIFPCL" in (pl.get("StationName") or "")), None)
    checks["D_zero_while_generating"] = {k: v for k, v in zero.items()}
    checks["D_bifpcl"] = pz.get(bifpcl) if bifpcl else None
    for k, v in zero.items():
        out(f"  {k} = 0 while generating: {len(v)} plants  {v[:10]}")
    out(f"  BIFPCL on {day}: {checks['D_bifpcl']}")

    # ---------------------------------------------------------------- E. SG vs DC
    out("\n== E. SG vs DC ==")
    ps = perf(day, ["sg_mu", "dc_mu", "sg_per"], cols=("DayCY",))
    higher = [f"{m.plant_name(c)} (SG {round(v['sg_mu']['DayCY'], 2)} > DC {round(v['dc_mu']['DayCY'], 2)})"
              for c, v in ps.items() if v.get("sg_mu") and v.get("dc_mu")
              and (v["sg_mu"]["DayCY"] or 0) > (v["dc_mu"]["DayCY"] or 0) > 0]
    with_both = sum(1 for v in ps.values() if v.get("sg_mu") and v.get("dc_mu") and (v["dc_mu"]["DayCY"] or 0) > 0)
    checks["E_sg_vs_dc"] = {"plants_with_sg_and_dc": with_both, "sg_higher_than_dc": higher}
    out(f"  plants with SG and DC: {with_both}; SG > DC: {len(higher)}  {higher[:10]}")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    (OUT / f"checks_{stamp}.json").write_text(json.dumps(checks, default=str, indent=1), encoding="utf-8")
    (OUT / f"checks_{stamp}.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved outputs/decisions/checks_{stamp}.json and .txt")


if __name__ == "__main__":
    main()
