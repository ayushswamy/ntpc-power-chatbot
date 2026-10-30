"""
Historical tools - answers from the daily performance tables
(Performance_DayCum_Station / _Stage / _Unit, April 2023 .. yesterday).

  get_daily_metric(entity, metric, date)
  get_metric_range(entity, metric, start, end, daily_breakdown=False)
  compare_periods(entity, metric, period1, period2)      - a period may be "today" (live)
  get_trend(entity, metric, start, end, granularity)      - day / month / fy
  rank_entities(metric, start, end, top_n, order, level, scope)
  get_coal(entity, metric, start, end)

How values are combined (see semantic/metrics.yaml):
  additive  -> SUM(DayCY)
  ratio     -> SUM(value * weight) / SUM(weight)   (or formula num / den)
  snapshot  -> value on the last date
  Month-to-date / FY-to-date use the official MonthCY / YearCY columns.
"""
from datetime import date, timedelta

from app.db.masters import get_masters
from app.db.runner import get_runner
from app.tools.common import (Entity, Period, ToolError, ToolResult, clamp_to_daily_range, fail, fy_start,
                              get_metric, latest_daily_date, parse_period, round_value)

LEVEL_TABLE = {"plant": "Performance_DayCum_Station", "stage": "Performance_DayCum_Stage", "unit": "Performance_DayCum_Unit"}
LEVEL_KEYS = {"plant": ["PlantCode"], "stage": ["PlantCode", "Stage"], "unit": ["PlantCode", "Stage", "Unit"]}
LEVEL_WORD = {"plant": "station", "stage": "stage", "unit": "unit"}
MAX_DAILY_POINTS = 400


# ============================================================ query engine

def _plan(metric: dict, level: str, use_formula: bool = False) -> dict:
    """Which ParamCodes to read: v = value, w = weight, g = zero cross-check,
    t0, t1, ... = formula terms (signed, see metrics.yaml), d = formula denominator."""
    daily = metric.get("daily")
    if not daily:
        raise ToolError(f"{metric['name']} is not a daily metric.")
    if LEVEL_WORD[level] in daily["levels"] and not (use_formula and daily.get("formula")):
        plan = {"v": daily["param"]}
        if metric["kind"] == "ratio" and daily.get("weight"):
            plan["w"] = daily["weight"]
        if daily.get("zero_check"):
            plan["g"] = daily["zero_check"]     # e.g. gross generation: tells a real 0 from "not reported"
        return plan
    if daily.get("formula"):
        f = daily["formula"]
        plan = {f"t{i}": param for i, param in enumerate(f["terms"])}
        plan["d"] = f["den"]
        return plan
    raise ToolError(f"{metric['name']} is not available at {LEVEL_WORD[level]} level "
                    f"(available for: {', '.join(daily['levels'])}).")


def _query(level: str, plan: dict, metric: dict, codes: list[str], stage=None, unit=None, *,
           col="DayCY", start=None, end=None, dates=None, group_by="entity", purpose=""):
    """Build and run the aggregation query. Returns rows with sum_v, sum_vw, sum_w, sum_n, sum_d, n_v, n_rows..."""
    table, keys = LEVEL_TABLE[level], LEVEL_KEYS[level]
    params: list = []

    cases = []
    for alias, param in plan.items():
        cases.append(f"MAX(CASE WHEN ParamCode = ? THEN {col} END) AS {alias}")
        params.append(param)

    where = []
    if dates:
        where.append("ReportDate IN (" + ", ".join("?" * len(dates)) + ")")
        params += dates
    else:
        where.append("ReportDate BETWEEN ? AND ?")
        params += [start, end]
    param_codes = sorted(set(plan.values()))
    where.append("ParamCode IN (" + ", ".join("?" * len(param_codes)) + ")")
    params += param_codes
    where.append("PlantCode IN (" + ", ".join(["CAST(? AS varchar(50))"] * len(codes)) + ")")
    params += codes
    if stage:
        where.append("Stage = CAST(? AS varchar(50))" if level == "unit" else "Stage = ?")
        params.append(stage)
    if unit:
        where.append("Unit = CAST(? AS varchar(50))")
        params.append(unit)

    inner = (f"SELECT {', '.join(keys)}, ReportDate, {', '.join(cases)} FROM dbo.{table} "
             f"WHERE {' AND '.join(where)} GROUP BY {', '.join(keys)}, ReportDate")

    groups = {
        "entity": ([f"k.{c}" for c in keys], [f"k.{c}" for c in keys]),
        "date": (["k.ReportDate"], ["k.ReportDate"]),
        "month": (["YEAR(k.ReportDate) AS yr", "MONTH(k.ReportDate) AS mo"], ["YEAR(k.ReportDate)", "MONTH(k.ReportDate)"]),
    }[group_by]

    agg = []
    if "v" in plan:
        cond = "k.v IS NOT NULL" + (" AND k.v <> 0" if metric["daily"].get("zero_is_missing") else "")
        if "g" in plan:
            # decisions 8/9: a 0 on a day the plant generated is "not reported", a 0 on a day without generation is real
            cond += " AND NOT (k.v = 0 AND ISNULL(k.g, 0) > 0)"
            agg.append("COUNT(CASE WHEN k.v = 0 AND k.g > 0 THEN 1 END) AS n_unreported")
        agg += [f"SUM(CASE WHEN {cond} THEN k.v END) AS sum_v", f"MAX(CASE WHEN {cond} THEN k.v END) AS max_v",
                f"COUNT(CASE WHEN {cond} THEN 1 END) AS n_v"]
        if "w" in plan:
            agg += [f"SUM(CASE WHEN {cond} THEN k.v * k.w END) AS sum_vw", f"SUM(CASE WHEN {cond} THEN k.w END) AS sum_w"]
    if "d" in plan:
        terms = [alias for alias in plan if alias.startswith("t")]
        agg += [f"SUM(k.{a}) AS sum_{a}" for a in terms] + ["SUM(k.d) AS sum_d", "COUNT(k.d) AS n_v"]
    agg += ["COUNT(*) AS n_rows", "MIN(k.ReportDate) AS first_date", "MAX(k.ReportDate) AS last_date"]

    sql = (f"SELECT TOP 5000 {', '.join(groups[0] + agg)} FROM ({inner}) k "
           f"GROUP BY {', '.join(groups[1])} ORDER BY {', '.join(groups[1])}")
    return get_runner().run(sql, params, purpose=purpose).rows


def _value(row: dict, metric: dict, plan: dict):
    """Turn one aggregated row into the metric value (None = no data)."""
    if "v" in plan:
        if not row.get("n_v"):
            return None
        if metric["kind"] == "ratio":
            if row["n_v"] == 1:
                return row["max_v"]                     # one plant, one value: the official number
            if row.get("sum_w"):
                return row["sum_vw"] / row["sum_w"]
            return None
        return row["sum_v"]
    if row.get("sum_d"):
        f = metric["daily"]["formula"]
        numerator = sum(sign * (row.get(f"sum_t{i}") or 0) for i, sign in enumerate(f["terms"].values()))
        return numerator / row["sum_d"] * f["scale"]
    return None


def _combine(rows: list[dict]) -> dict:
    """Add up aggregated rows of several plants into one row (for group totals)."""
    total = {"n_v": 0, "n_rows": 0, "n_unreported": 0}
    for r in rows:
        for k, value in r.items():
            if k.startswith("sum_") and value is not None:
                total[k] = total.get(k, 0) + value
        total["n_v"] += r.get("n_v") or 0
        total["n_rows"] += r.get("n_rows") or 0
        total["n_unreported"] += r.get("n_unreported") or 0
        if r.get("max_v") is not None:
            total["max_v"] = r["max_v"]
    return total


def _query_level(entity: Entity, requested: str | None = None) -> str:
    return (requested or "plant") if entity.level == "group" else entity.level


def period_value(entity: Entity, metric_key: str, period: Period, notes: list[str]) -> dict | None:
    """
    Value of a metric for an entity over a period (daily tables only).
    Returns {"value", "unit", "period", "method", "plants_with_data", "plants_total"} or None if no data.
    """
    metric = get_metric(metric_key)
    p = clamp_to_daily_range(period, notes)
    if p is None:
        return None
    level = _query_level(entity)
    _check_coal_plant(metric_key, entity)
    codes = entity.plant_codes()
    if not codes:
        raise ToolError("No active plants match this group.")

    # Choose the cheapest correct way to read the period
    if metric["kind"] == "snapshot":
        col, s, e, method = "DayCY", p.end, p.end, f"closing value on {p.end:%d %b %Y}"
        if p.start != p.end:
            notes.append(f"{metric['name']} is a closing value - shown for the last date ({p.end:%d %b %Y}).")
    elif p.start == p.end:
        col, s, e, method = "DayCY", p.end, p.end, "day value"
    elif metric["daily"].get("cumulative") and p.start == p.end.replace(day=1):
        col, s, e, method = "MonthCY", p.end, p.end, "official month-to-date value"
    elif metric["daily"].get("cumulative") and p.start == fy_start(p.end):
        col, s, e, method = "YearCY", p.end, p.end, "official FY-to-date value"
    else:
        col, s, e, method = "DayCY", p.start, p.end, "combined from daily values"

    combining = entity.level == "group" or method == "combined from daily values"
    plan = _plan(metric, level, use_formula=combining and metric["daily"].get("aggregate_by_formula", False))
    rows = _query(level, plan, metric, codes, entity.stage, entity.unit, col=col, start=s, end=e,
                  group_by="entity", purpose=f"{metric_key} {col} {s}..{e}")
    if col != "DayCY" and not any(r.get("n_v") for r in rows):
        # No row on the end date for the cumulative column - fall back to combining daily values
        col, method = "DayCY", "combined from daily values"
        plan = _plan(metric, level, use_formula=metric["daily"].get("aggregate_by_formula", False))
        rows = _query(level, plan, metric, codes, entity.stage, entity.unit, col=col, start=p.start, end=p.end,
                      group_by="entity", purpose=f"{metric_key} fallback {p.start}..{p.end}")
    if "d" in plan and metric["daily"].get("aggregate_by_formula"):
        method += " (calculated from capacity and losses)"

    with_data = [r for r in rows if r.get("n_v")]
    unreported = sum(r.get("n_unreported") or 0 for r in rows)
    if not with_data:
        if unreported:
            notes.append(f"{metric['name']} is recorded as 0 for {entity.label()} in {p.text()} although the plant "
                         "generated, so it is treated as not reported by this source.")
        elif metric.get("missing_note"):
            notes.append(metric["missing_note"])
        else:
            notes.append(f"No {metric['name']} data found for {entity.label()} in {p.text()}.")
        return None
    combined = with_data[0] if len(with_data) == 1 else _combine(with_data)
    value = _value(combined, metric, plan)

    if entity.level == "group":
        if len(with_data) < len(codes):
            notes.append(f"{len(with_data)} of {len(codes)} plants in {entity.label()} have {metric['name']} data for this period.")
        if metric_key == "coal_stock_days":
            notes.append("Coal stock days cannot be added across plants - see the per-plant table.")
            value = None
    elif col == "DayCY" and p.days() > 1 and metric["kind"] != "snapshot" and combined["n_rows"] < p.days():
        notes.append(f"Data found for {combined['n_rows']} of {p.days()} days.")
    if unreported:
        notes.append(f"{unreported} value(s) recorded as 0 on days with generation were treated as not reported.")
    _quality_notes(metric, metric_key, value, entity, p, notes)
    if metric.get("unit_note"):
        notes.append(metric["unit_note"])

    result = {"value": round_value(value, metric["unit"]), "unit": metric["unit"], "period": p.text(),
              "start": p.start.isoformat(), "end": p.end.isoformat(), "method": method}
    if entity.level == "group":
        result.update(plants_with_data=len(with_data), plants_total=len(codes))
    return result


COAL_METRIC_KEYS = {"coal_receipt", "coal_consumption", "coal_stock", "coal_stock_days", "specific_coal_consumption"}


def _check_coal_plant(metric_key: str, entity: Entity) -> None:
    """A coal question about a gas / hydro / solar plant gets a clear answer instead of zeros."""
    if metric_key in COAL_METRIC_KEYS and entity.level != "group":
        p = get_masters().plants.get(entity.plant_code) or {}
        if p.get("FuelType") and p["FuelType"] != "C":
            raise ToolError(f"{get_masters().plant_name(entity.plant_code)} is a {(p.get('EnergyType') or 'non-coal').lower()} "
                            f"plant, so it has no coal figures.")


def _quality_notes(metric: dict, metric_key: str, value, entity: Entity, p: Period, notes: list[str]) -> None:
    """Data-quality warnings. Values are never changed - only flagged (decisions 3 and 9)."""
    if value is None:
        return
    limit = metric["daily"].get("warn_above")
    if limit is not None and value > limit:
        notes.append(f"Data-quality flag: this value is above {limit}{metric['unit']} as recorded in the source data.")
    other = metric["daily"].get("must_not_exceed")
    if other:
        check = period_value(entity, other, p, [])
        if check and check["value"] is not None and value > check["value"]:
            other_name = get_metric(other)["name"]
            notes.append(f"Data-quality warning: {metric['name']} ({round_value(value, metric['unit'])} {metric['unit']}) is higher "
                         f"than {other_name} ({check['value']} {check['unit']}) for this period, which is not expected; "
                         "the source values are being checked.")


def _entity_rows(rows: list[dict], level: str, metric: dict, plan: dict) -> list[dict]:
    """Per-plant/stage/unit rows -> [{name, value}] (for tables and rankings)."""
    m = get_masters()
    out = []
    for r in rows:
        v = _value(r, metric, plan)
        if v is None:
            continue
        if level == "unit":
            name = m.unit_label(r["PlantCode"], r["Unit"])
        elif level == "stage":
            name = m.stage_name(r["PlantCode"], r["Stage"])
        else:
            name = m.plant_name(r["PlantCode"])
        out.append({"name": name, "plant_code": r["PlantCode"], "value": round_value(v, metric["unit"]),
                    "unit": metric["unit"]})
    return out


def _sources(level: str) -> list[str]:
    return [LEVEL_TABLE[level]]


# ============================================================ tools

def get_daily_metric(entity, metric: str, day) -> ToolResult:
    """Value of a metric for one day (yesterday or earlier)."""
    tool = "get_daily_metric"
    try:
        e = Entity.parse(entity)
        p = parse_period(day)
        p = Period(p.end, p.end, p.label if p.start == p.end else "")
        notes: list[str] = []
        res = period_value(e, metric, p, notes)
        return ToolResult(tool, ok=res is not None, answer={"entity": e.label(), "metric": get_metric(metric)["name"], **(res or {})},
                          sources=_sources(_query_level(e)), notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))


def get_metric_range(entity, metric: str, start, end=None, daily_breakdown: bool = False) -> ToolResult:
    """Total / average of a metric over a date range, optionally with one row per day."""
    tool = "get_metric_range"
    try:
        e = Entity.parse(entity)
        p = parse_period({"start": parse_period(start).start, "end": parse_period(end or start).end})
        notes: list[str] = []
        res = period_value(e, metric, p, notes)
        result = ToolResult(tool, ok=res is not None, answer={"entity": e.label(), "metric": get_metric(metric)["name"], **(res or {})},
                            sources=_sources(_query_level(e)), notes=notes)
        if res and daily_breakdown:
            result.table = get_trend(e, metric, res["start"], res["end"], "day").table
        return result
    except ToolError as exc:
        return fail(tool, str(exc))


def get_trend(entity, metric: str, start, end=None, granularity: str = "day") -> ToolResult:
    """A series for a line/bar chart: one value per day, month or financial year."""
    tool = "get_trend"
    try:
        e = Entity.parse(entity)
        m = get_metric(metric)
        if e.level == "group" and metric == "coal_stock_days":
            raise ToolError("Coal stock days cannot be added across plants - ask for one plant or for coal stock in tonnes.")
        notes: list[str] = []
        p = clamp_to_daily_range(parse_period({"start": parse_period(start).start, "end": parse_period(end or start).end}), notes)
        if p is None:
            return ToolResult(tool, ok=False, answer={"entity": e.label(), "metric": m["name"]}, notes=notes)
        level = _query_level(e)
        _check_coal_plant(metric, e)
        # a group total per day/month is a combination -> formula for metrics that need it (decision 10)
        plan = _plan(m, level, use_formula=e.level == "group" and m["daily"].get("aggregate_by_formula", False))
        codes = e.plant_codes()

        if granularity == "day":
            if p.days() > MAX_DAILY_POINTS:
                p = Period(p.end - timedelta(days=MAX_DAILY_POINTS - 1), p.end)
                notes.append(f"Daily trend limited to the last {MAX_DAILY_POINTS} days; ask for a monthly trend for longer periods.")
            rows = _query(level, plan, m, codes, e.stage, e.unit, start=p.start, end=p.end, group_by="date",
                          purpose=f"trend day {metric}")
            series = [{"period": r["ReportDate"].isoformat() if hasattr(r["ReportDate"], "isoformat") else str(r["ReportDate"]),
                       "value": round_value(_value(r, m, plan), m["unit"])} for r in rows]
        elif granularity in ("month", "fy") and m["daily"].get("cumulative") and m["kind"] != "snapshot":
            # Official cumulative column on the last available day of each month / FY
            last = latest_daily_date()
            points, d = [], p.start
            while d <= p.end:
                if granularity == "month":
                    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
                else:
                    nxt = date(fy_start(d).year + 1, 4, 1)
                points.append(min(nxt - timedelta(days=1), p.end, last))
                d = nxt
            col = "MonthCY" if granularity == "month" else "YearCY"
            rows = _query(level, plan, m, codes, e.stage, e.unit, col=col, dates=points, group_by="date",
                          purpose=f"trend {granularity} {metric}")
            series = []
            for r in rows:
                d = r["ReportDate"]
                label = f"{d:%b %Y}" if granularity == "month" else f"FY {fy_start(d).year}-{str(fy_start(d).year + 1)[2:]}"
                series.append({"period": label, "value": round_value(_value(r, m, plan), m["unit"]), "as_of": d.isoformat()})
            notes.append(f"Each {granularity} is the full calendar {'month' if granularity == 'month' else 'financial year'} "
                         f"(the last one up to {min(p.end, last):%d %b %Y}).")
        elif granularity == "month":
            plan = _plan(m, level, use_formula=m["daily"].get("aggregate_by_formula", False))   # days combined per month
            rows = _query(level, plan, m, codes, e.stage, e.unit, start=p.start, end=p.end, group_by="month",
                          purpose=f"trend month {metric}")
            series = [{"period": f"{date(r['yr'], r['mo'], 1):%b %Y}", "value": round_value(_value(r, m, plan), m["unit"])}
                      for r in rows]
        else:
            raise ToolError(f"Granularity must be day or month{' or fy' if m['daily'].get('cumulative') else ''}.")

        if m.get("unit_note"):
            notes.append(m["unit_note"])
        return ToolResult(tool, answer={"entity": e.label(), "metric": m["name"], "unit": m["unit"],
                                        "granularity": granularity, "start": p.start.isoformat(), "end": p.end.isoformat(),
                                        "points": len(series)},
                          table=series, sources=_sources(level), notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))


def rank_entities(metric: str, start, end=None, top_n: int = 10, order: str = "desc",
                  level: str = "plant", scope: dict | None = None) -> ToolResult:
    """Best / worst plants (or stages / units) by a metric over a period."""
    tool = "rank_entities"
    try:
        m = get_metric(metric)
        scope = dict(scope or {})
        if metric in COAL_METRIC_KEYS and not scope.get("fuel"):
            scope["fuel"] = ["C"]                      # coal rankings only make sense for coal plants
        group = Entity(level="group", scope=scope)
        notes: list[str] = []
        p = clamp_to_daily_range(parse_period({"start": parse_period(start).start, "end": parse_period(end or start).end}), notes)
        if p is None:
            return ToolResult(tool, ok=False, answer={"metric": m["name"]}, notes=notes)
        codes = group.plant_codes()
        if m["kind"] == "snapshot":
            col, s, e = "DayCY", p.end, p.end
        elif p.start == p.end:
            col, s, e = "DayCY", p.end, p.end
        elif m["daily"].get("cumulative") and p.start == p.end.replace(day=1):
            col, s, e = "MonthCY", p.end, p.end
        elif m["daily"].get("cumulative") and p.start == fy_start(p.end):
            col, s, e = "YearCY", p.end, p.end
        else:
            col, s, e = "DayCY", p.start, p.end
            if level == "unit" and p.days() > 92:
                raise ToolError("Unit-level rankings are limited to 92 days (or a full month / FY to date).")
        # a custom period combines days -> formula for metrics that need it (decision 10)
        plan = _plan(m, level, use_formula=(col == "DayCY" and s != e) and m["daily"].get("aggregate_by_formula", False))
        rows = _query(level, plan, m, codes, col=col, start=s, end=e, group_by="entity",
                      purpose=f"rank {metric} {level} {col}")
        ranked = _entity_rows(rows, level, m, plan)
        ranked.sort(key=lambda r: r["value"], reverse=(order != "asc"))
        top_n = max(1, min(int(top_n), 50))
        table = [{"rank": i + 1, **r} for i, r in enumerate(ranked[:top_n])]
        notes.append(f"Ranked {len(ranked)} {level}s in {group.label()} that have data.")
        unreported = sum(1 for r in rows if not r.get("n_v") and r.get("n_unreported"))
        if unreported:
            notes.append(f"{unreported} {level}(s) recorded 0 while generating; treated as not reported and left out.")
        zeros = sum(1 for r in ranked if r["value"] == 0)
        if zeros and order == "asc" and (m["daily"].get("zero_check") or metric in ("gross_generation", "commercial_generation")):
            notes.append(f"{zeros} {level}(s) show 0 with no generation (shut down for the whole period).")
        limit = m["daily"].get("warn_above")
        if limit is not None and any(r["value"] > limit for r in table):
            notes.append(f"Data-quality flag: some values are above {limit}{m['unit']} as recorded in the source data.")
        if m.get("unit_note"):
            notes.append(m["unit_note"])
        return ToolResult(tool, answer={"metric": m["name"], "unit": m["unit"], "period": p.text(), "order": order,
                                        "level": level, "scope": group.label(), "count": len(table)},
                          table=table, sources=_sources(level), notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))


def compare_periods(entity, metric: str, period1, period2) -> ToolResult:
    """Compare a metric between two periods. A period may be 'today' (live, from real-time data)."""
    tool = "compare_periods"
    try:
        e = Entity.parse(entity)
        m = get_metric(metric)
        notes: list[str] = []
        values, sources, is_live, as_of = [], set(), False, None
        for raw in (period1, period2):
            p = parse_period(raw)
            if p.start >= _today():
                # Today alone is not in the daily tables -> live "today so far".
                # (Longer periods ending today are answered from daily data up to yesterday - see clamp note.)
                live_metric = (m.get("live") or {})
                if not live_metric.get("today_so_far"):
                    values.append({"period": "today", "value": None, "unit": m["unit"]})
                    notes.append(f"{m['name']} for today is not available until the daily run tomorrow.")
                    continue
                from app.tools.live import get_today_so_far   # avoid circular import
                live = get_today_so_far(e, metric)
                if not live.ok:
                    values.append({"period": "today", "value": None, "unit": m["unit"]})
                    notes.append(live.error or "Live data for today is not available.")
                    continue
                values.append({"period": f"today so far (live, up to {live.answer.get('up_to')})",
                               "value": live.answer.get("value"), "unit": live.answer.get("unit"), "is_live": True})
                notes += live.notes
                sources.update(live.sources)
                is_live, as_of = True, live.as_of
            else:
                res = period_value(e, metric, p, notes)
                days = (date.fromisoformat(res["end"]) - date.fromisoformat(res["start"])).days + 1 if res else None
                values.append({"period": res["period"] if res else p.text(), "value": res["value"] if res else None,
                               "unit": m["unit"], "method": res["method"] if res else None, "days": days})
                sources.update(_sources(_query_level(e)))
        a, b = values[0]["value"], values[1]["value"]
        answer = {"entity": e.label(), "metric": m["name"], "unit": m["unit"], "period1": values[0], "period2": values[1]}
        if a is not None and b is not None:
            answer["difference"] = round_value(a - b, m["unit"])
            if b:
                answer["change_percent"] = round((a - b) / abs(b) * 100, 2)
            if m["kind"] == "ratio":
                answer["difference_note"] = "difference in percentage points" if m["unit"] == "%" else ""
        if any(v.get("is_live") for v in values) and m["kind"] == "additive":
            notes.append("Today's value covers only part of the day, so it is not directly comparable with a full day.")
        elif m["kind"] == "additive" and all(v.get("days") for v in values) and values[0]["days"] != values[1]["days"]:
            notes.append(f"The periods have different lengths ({values[0]['days']} vs {values[1]['days']} days), "
                         "so the totals are not directly comparable.")
        return ToolResult(tool, ok=a is not None or b is not None, answer=answer, sources=sorted(sources),
                          notes=notes, is_live=is_live, as_of=as_of)
    except ToolError as exc:
        return fail(tool, str(exc))


COAL_METRICS = {"coal_receipt", "coal_consumption", "coal_stock", "coal_stock_days", "specific_coal_consumption"}


def get_coal(entity, metric: str = "coal_stock", start="yesterday", end=None) -> ToolResult:
    """Coal receipt / consumption / stock / stock days (station level). Groups get a per-plant table."""
    tool = "get_coal"
    if metric not in COAL_METRICS:
        return fail(tool, f"Coal metric must be one of: {', '.join(sorted(COAL_METRICS))}.")
    try:
        e = Entity.parse(entity)
        if e.level in ("stage", "unit"):
            e = Entity(level="plant", plant_code=e.plant_code)
        if e.level == "group":
            res = rank_entities(metric, start, end, top_n=50, order="desc" if metric != "coal_stock_days" else "asc",
                                level="plant", scope={**e.scope, "fuel": ["C"]})
            res.tool = tool
            if res.ok and metric != "coal_stock_days":
                total = get_metric_range(Entity(level="group", scope={**e.scope, "fuel": ["C"]}), metric, start, end)
                res.answer["total"] = total.answer.get("value")
                res.notes += [n for n in total.notes if n not in res.notes]
            return res
        return get_metric_range(e, metric, start, end, daily_breakdown=parse_period(start).start != parse_period(end or start).end)
    except ToolError as exc:
        return fail(tool, str(exc))


def _today() -> date:
    from app import clock
    return clock.today()
