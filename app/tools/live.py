"""
Live (real-time) tools - answers from XML_RealView, queried at request time.

  get_realtime(entity, metric)            - current MW of a plant / stage / unit, or frequency
  get_realtime_summary(scope, metric)     - current total MW for NTPC or a group, with coverage
  get_today_so_far(entity, metric)        - energy (MU) generated today from completed 15-min blocks

XML_RealView has one row per stage per 15-minute block; the CURRENT block's rows
are rewritten about every minute. Each unit value has its own timestamp
(TAGTIME1..13, AGTAGTIME). Only values newer than LIVE_FRESHNESS_MINUTES are
used as "current"; older ones are reported as "last available value at <time>".

The current block is cached for LIVE_CACHE_TTL_SECONDS (max 60 s), so many
users asking at once cause one query per minute.
"""
from datetime import datetime, timedelta
from statistics import median

from app import clock
from app.config import settings
from app.db.cache import TTLCache
from app.db.masters import get_masters
from app.db.runner import get_runner
from app.tools.common import Entity, ToolError, ToolResult, fail, get_metric, round_value

SOURCE = "XML_RealView"
UNIT_COLS = [f"{p}{i}" for i in range(1, 14) for p in ("U", "TAGTIME", "UCAP")]
BLOCK_COLUMNS = ["ReportDate", "PlantCode", "Stage", "NAME", "TYPE", "BLK", "TIME", "AG", "AGTAGTIME", "FREQ",
                 "TOTALGEN", "STNCAP", "UpdatedOn"] + UNIT_COLS
FUEL_NAMES = {"T": "Coal", "G": "Gas", "H": "Hydro", "S": "Solar", "W": "Wind"}

_cache = TTLCache(settings.live_cache_ttl)

# Which XML_RealView value "today so far" energy uses. Validated 2026-09-28 (outputs/tools/checks_*.json):
# sum of 96 blocks of TOTALGEN matched official gross_gen_mu within 0.7% for 10 plants.
# AG was off by up to 9% (e.g. stages whose AG tag is dead) - so AG today-so-far is NOT offered.
TODAY_SO_FAR_COLUMN = {"gross_generation": "TOTALGEN"}


def _parse_time(text) -> datetime | None:
    if not text:
        return None
    if isinstance(text, datetime):
        return text
    try:
        return datetime.strptime(str(text).strip(), "%d/%m/%Y %H:%M:%S")
    except ValueError:
        return None


def _fresh(t: datetime | None, now: datetime) -> bool:
    return t is not None and now - t <= timedelta(minutes=settings.live_freshness_minutes)


# ============================================================ current block

def _load_current_block() -> dict:
    run = get_runner().run
    today = clock.today()
    latest = run("SELECT TOP 1 ReportDate, BLK FROM dbo.XML_RealView WHERE ReportDate BETWEEN ? AND ? "
                 "ORDER BY ReportDate DESC, BLK DESC", [today - timedelta(days=1), today], purpose="live: latest block").rows
    if not latest:
        raise ToolError("No real-time data is available (XML_RealView has no rows for today or yesterday).")
    day, blk = latest[0]["ReportDate"], latest[0]["BLK"]
    rows = run(f"SELECT TOP 1000 {', '.join(BLOCK_COLUMNS)} FROM dbo.XML_RealView WHERE ReportDate = ? AND BLK = ?",
               [day, blk], purpose="live: current block").rows
    return {"day": day, "blk": blk, "rows": rows}


def current_block() -> dict:
    return _cache.get_or_load("current_block", _load_current_block)


def _stage_view(row: dict, now: datetime) -> dict:
    """One XML_RealView row -> units with MW, time, freshness; AG with freshness."""
    units = []
    for i in range(1, 14):
        mw, t, cap = row.get(f"U{i}"), _parse_time(row.get(f"TAGTIME{i}")), row.get(f"UCAP{i}")
        if mw is None and t is None:
            continue
        units.append({"unit": f"UNIT{i:02d}", "mw": mw, "time": t, "fresh": _fresh(t, now) and mw is not None,
                      "capacity": cap})
    ag_time = _parse_time(row.get("AGTAGTIME"))
    any_unit_fresh = any(u["fresh"] for u in units)
    ag_fresh = row.get("AG") is not None and (_fresh(ag_time, now) if ag_time else any_unit_fresh)
    if not ag_time and any_unit_fresh:
        ag_time = max(u["time"] for u in units if u["fresh"])
    return {"plant_code": str(row["PlantCode"]), "stage": row["Stage"], "type": row.get("TYPE"),
            "capacity": row.get("STNCAP") or 0, "units": units, "ag": row.get("AG"), "ag_time": ag_time,
            "ag_fresh": ag_fresh, "freq": row.get("FREQ"), "row_time": _parse_time(row.get("TIME"))}


def _stage_mw(sv: dict, metric: str) -> tuple[float | None, list[datetime], str]:
    """
    Current MW of one stage from FRESH values only.
    gross_generation: sum of fresh unit values (solar/wind stages without unit values use AG)
    actual_generation: AG
    Returns (mw or None, timestamps used, how).
    """
    if metric == "actual_generation":
        return (sv["ag"], [sv["ag_time"]], "AG") if sv["ag_fresh"] else (None, [], "")
    fresh_units = [u for u in sv["units"] if u["fresh"]]
    if fresh_units:
        return sum(u["mw"] for u in fresh_units), [u["time"] for u in fresh_units], "units"
    if not sv["units"] and sv["ag_fresh"]:
        return sv["ag"], [sv["ag_time"]], "AG"
    return None, [], ""


def _live_metric(metric: str) -> str:
    if metric in ("gross_generation", "generation", "power"):
        return "gross_generation"
    if metric in ("actual_generation", "frequency"):
        return metric
    get_metric(metric)  # raises a clear error for unknown/disabled metrics
    raise ToolError(f"{get_metric(metric)['name']} is not available live. Live metrics: gross_generation, "
                    "actual_generation, frequency.")


def _fmt(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m-%d %H:%M") if t else None


def _as_of(times: list[datetime], notes: list[str]) -> datetime | None:
    times = [t for t in times if t]
    if not times:
        return None
    newest, oldest = max(times), min(times)
    if newest - oldest > timedelta(minutes=5):
        notes.append(f"Values used range from {oldest:%H:%M} to {newest:%H:%M}.")
    return newest


# ============================================================ tools

def get_realtime(entity, metric: str = "gross_generation") -> ToolResult:
    """Current value for a plant, stage or unit (or grid frequency)."""
    tool = "get_realtime"
    try:
        metric = _live_metric(metric)
        e = Entity.parse(entity)
        if e.level == "group":
            return get_realtime_summary(e.scope, metric)
        block, now, notes = current_block(), clock.now(), []
        if metric == "frequency":
            return _frequency(block, now)
        # U1..U13 are the plant's unit numbers (UNIT03 is U3 in its stage's row) - verified in Phase 2
        views = [_stage_view(r, now) for r in block["rows"] if str(r["PlantCode"]) == e.plant_code
                 and (e.level == "plant" or r["Stage"] == e.stage)]
        if not views:
            return fail(tool, f"No real-time data row for {e.label()}.")

        table, used_times, total, last_total, last_times = [], [], 0.0, 0.0, []
        found = False
        for sv in views:
            for u in sv["units"]:
                if e.level == "unit" and u["unit"] != e.unit:
                    continue
                table.append({"unit": get_masters().unit_label(e.plant_code, u["unit"]),
                              "mw": round_value(u["mw"], "MW"), "capacity_mw": round_value(u["capacity"], "MW"),
                              "time": _fmt(u["time"]), "status": "live" if u["fresh"] else "stale"})
            if e.level == "unit":
                u = next((u for u in sv["units"] if u["unit"] == e.unit), None)
                if u is None:
                    continue
                if metric == "actual_generation":
                    raise ToolError("Actual generation (AG) is measured per stage, not per unit.")
                if u["fresh"]:
                    total, found = total + u["mw"], True
                    used_times.append(u["time"])
                elif u["mw"] is not None:
                    last_total += u["mw"]
                    last_times.append(u["time"])
                continue
            mw, times, how = _stage_mw(sv, metric)
            if mw is not None:
                total, found = total + mw, True
                used_times += times
                if how == "AG" and metric == "gross_generation":
                    notes.append("This plant reports only station output (AG), used as its generation.")
            elif metric == "actual_generation":
                # last available AG comes from the AG tag itself (not from the unit values)
                last_total += sv["ag"] or 0
                last_times.append(_parse_time(next((r["AGTAGTIME"] for r in block["rows"]
                                                    if str(r["PlantCode"]) == sv["plant_code"] and r["Stage"] == sv["stage"]), None)))
            else:
                stale = [u for u in sv["units"] if u["mw"] is not None]
                last_total += sum(u["mw"] for u in stale) if stale else (sv["ag"] or 0)
                last_times += [u["time"] for u in stale] or [sv["ag_time"]]
            stale_units = [u["unit"] for u in sv["units"] if not u["fresh"]]
            if stale_units and found:
                notes.append(f"{len(stale_units)} unit value(s) are not live (feed older than "
                             f"{settings.live_freshness_minutes} min) and are not included.")

        answer = {"entity": e.label(), "metric": "Current " + ("gross generation" if metric == "gross_generation" else "actual generation (AG)"),
                  "unit": "MW"}
        if found:
            as_of = _as_of(used_times, notes)
            answer["value"] = round_value(total, "MW")
            if e.level == "unit":
                u = next((u for sv in views for u in sv["units"] if u["unit"] == e.unit), None)
                answer["capacity_mw"] = round_value(u["capacity"], "MW") if u and u["capacity"] else None
            else:
                answer["capacity_mw"] = round_value(sum(v["capacity"] for v in views), "MW")
                live_units = [row for row in table if row["status"] == "live"]
                answer["units_running"] = [f"{row['unit']} ({row['mw']} MW)" for row in live_units if (row["mw"] or 0) > 5]
                answer["units_not_running"] = [row["unit"] for row in live_units if (row["mw"] or 0) <= 5]
                answer["units_without_live_data"] = [row["unit"] for row in table if row["status"] != "live"]
            return ToolResult(tool, answer=answer, table=table or None, as_of=_fmt(as_of), is_live=True,
                              sources=[SOURCE], notes=notes)
        # Nothing fresh: say the feed may be delayed and show the last available value
        last_time = max([t for t in last_times if t], default=None)
        if last_time is None:
            notes.append(f"{e.label()} has no live data feed in the real-time system.")
            answer.update(value=None, has_live_feed=False, **_latest_daily(e, metric, notes))
            return ToolResult(tool, answer=answer, is_live=True, sources=[SOURCE, "Performance_DayCum"], notes=notes)
        if now - last_time > timedelta(days=1):
            # A feed silent for more than a day is broken; the value still in the row may be carried
            # forward or calculated, so it is NOT shown as a measurement.
            answer.update(value=None, feed_stopped_since=_fmt(last_time), **_latest_daily(e, metric, notes))
            notes.insert(0, f"The live feed for {e.label()} has not updated since {last_time:%d %b %Y %H:%M}, "
                            "so no current value can be given.")
            return ToolResult(tool, answer=answer, is_live=True, sources=[SOURCE, "Performance_DayCum"], notes=notes)
        answer.update(value=None, last_available_value=round_value(last_total, "MW"), last_available_time=_fmt(last_time))
        notes.append(f"The live feed for {e.label()} may be delayed: no value newer than "
                     f"{settings.live_freshness_minutes} minutes. Last available value "
                     f"{round_value(last_total, 'MW')} MW at {last_time:%d %b %H:%M}.")
        return ToolResult(tool, answer=answer, table=table or None, as_of=_fmt(last_time), is_live=True,
                          sources=[SOURCE], notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))


def _latest_daily(e: Entity, metric: str, notes: list[str]) -> dict:
    """Decision 11: when there is no trustworthy live value, give the latest daily (Day-1) figure instead."""
    from app.tools.common import Period, latest_daily_date
    from app.tools.historical import period_value
    if e.level == "unit" and metric == "actual_generation":
        return {}
    day = latest_daily_date()
    try:
        res = period_value(e, metric, Period(day, day), [])
    except ToolError:
        return {}
    if not res or res["value"] is None:
        return {}
    name = get_metric(metric)["name"]
    notes.append(f"Latest daily figure: {name} {res['value']:,} {res['unit']} on {day:%d %b %Y} (daily report).")
    return {"latest_daily_value": res["value"], "latest_daily_unit": res["unit"], "latest_daily_date": day.isoformat()}


def get_realtime_summary(scope: dict | None = None, metric: str = "gross_generation", breakdown: str = "fuel") -> ToolResult:
    """Current total for NTPC or a group, counting only plants with a live feed; breakdown by fuel or plant."""
    tool = "get_realtime_summary"
    try:
        metric = _live_metric(metric)
        block, now, notes = current_block(), clock.now(), []
        if metric == "frequency":
            return _frequency(block, now)
        m = get_masters()
        group = Entity(level="group", scope=scope or {})
        codes = set(group.plant_codes())
        total, times, groups = 0.0, [], {}
        plants_live, plants_seen, cap_live, cap_total = set(), set(), 0.0, 0.0
        for r in block["rows"]:
            code = m.resolve_code(r["PlantCode"])
            if code not in codes:
                continue
            sv = _stage_view(r, now)
            plants_seen.add(code)
            cap_total += sv["capacity"]
            mw, used, _ = _stage_mw(sv, metric)
            key = FUEL_NAMES.get(sv["type"], sv["type"] or "Other") if breakdown == "fuel" else m.plant_name(code)
            g = groups.setdefault(key, {"name": key, "mw": 0.0, "capacity_live_mw": 0.0, "capacity_mw": 0.0, "stages_live": 0, "stages": 0})
            g["capacity_mw"] += sv["capacity"]
            g["stages"] += 1
            if mw is None:
                continue
            total += mw
            times += used
            plants_live.add(code)
            cap_live += sv["capacity"]
            g["mw"] += mw
            g["capacity_live_mw"] += sv["capacity"]
            g["stages_live"] += 1

        missing = sorted(codes - plants_live, key=lambda c: m.plant_name(c))
        table = sorted(({**g, "mw": round_value(g["mw"], "MW"), "capacity_live_mw": round_value(g["capacity_live_mw"], "MW"),
                         "capacity_mw": round_value(g["capacity_mw"], "MW")} for g in groups.values()),
                       key=lambda g: -g["mw"])
        coverage = cap_live / cap_total * 100 if cap_total else 0
        notes.append(f"Includes {len(plants_live)} of {len(codes)} plants with a live feed "
                     f"({round(cap_live):,} of {round(cap_total):,} MW capacity, {coverage:.0f}%). "
                     f"Plants without a live value in the last {settings.live_freshness_minutes} min are not counted.")
        if missing:
            names = ", ".join(m.plant_name(c) for c in missing[:8])
            notes.append(f"Not included (no live feed): {names}{' and others' if len(missing) > 8 else ''}.")
        as_of = _as_of(times, notes)
        return ToolResult(tool, ok=bool(plants_live),
                          answer={"entity": group.label(), "metric": "Current " + ("gross generation" if metric == "gross_generation" else "actual generation (AG)"),
                                  "value": round_value(total, "MW"), "unit": "MW", "plants_included": len(plants_live),
                                  "plants_total": len(codes), "capacity_covered_mw": round_value(cap_live, "MW"),
                                  "capacity_total_mw": round_value(cap_total, "MW"), "coverage_percent": round(coverage, 1),
                                  "block": block["blk"]},
                          table=table, as_of=_fmt(as_of), is_live=True, sources=[SOURCE], notes=notes,
                          error=None if plants_live else "No plant in this group has a live feed right now.")
    except ToolError as exc:
        return fail(tool, str(exc))


def _frequency(block: dict, now: datetime) -> ToolResult:
    values, times = [], []
    for r in block["rows"]:
        sv = _stage_view(r, now)
        f = sv["freq"]
        if f and 45 < f < 55 and any(u["fresh"] for u in sv["units"]):
            values.append(f)
            times += [u["time"] for u in sv["units"] if u["fresh"]]
    if not values:
        return fail("get_realtime", "No live grid frequency value is available right now.")
    return ToolResult("get_realtime", answer={"entity": "Grid", "metric": "Grid frequency", "value": round(median(values), 2),
                                              "unit": "Hz", "readings": len(values)},
                      as_of=_fmt(max(times)), is_live=True, sources=[SOURCE],
                      notes=["Median of the frequency readings reported by plants with a live feed."])


# ============================================================ today so far

def block_energy(day, max_blk: int, codes: list[str] | None, column: str, stage: str | None = None,
                 unit_no: int | None = None) -> dict:
    """
    Energy (MU) from 15-minute blocks 1..max_blk of `day`: SUM(MW) * 0.25 h / 1000.
    A stage-block counts only if one of its timestamps is within LIVE_FRESHNESS_MINUTES of the block end
    (so a frozen feed is not counted again and again). Returns per-plant MU and coverage.
    """
    run = get_runner().run
    value_col = f"U{unit_no}" if unit_no else column
    time_cols = [f"TAGTIME{unit_no}"] if unit_no else ["AGTAGTIME"] + [f"TAGTIME{i}" for i in range(1, 14)]
    cols = ["PlantCode", "Stage", "BLK", value_col] + time_cols
    per_plant, counted, skipped = {}, 0, 0
    code_filter, code_params = "", []
    if codes is not None and len(codes) <= 20:
        code_filter = " AND PlantCode IN (" + ", ".join("?" * len(codes)) + ")"
        code_params = [int(c) for c in codes]
    wanted = set(codes) if codes is not None else None
    m = get_masters()
    for first in range(1, max_blk + 1, 24):            # 24 blocks x 162 stages < 5000 rows per query
        last = min(first + 23, max_blk)
        sql = (f"SELECT TOP 5000 {', '.join(cols)} FROM dbo.XML_RealView WHERE ReportDate = ? AND BLK BETWEEN ? AND ?"
               + code_filter + (" AND Stage = ?" if stage else ""))
        params = [day, first, last] + code_params + ([stage] if stage else [])
        for r in run(sql, params, purpose=f"today so far blocks {first}-{last}").rows:
            code = m.resolve_code(r["PlantCode"])
            if wanted is not None and code not in wanted:
                continue
            block_end = datetime.combine(day, datetime.min.time()) + timedelta(minutes=15 * r["BLK"])
            times = [_parse_time(r[c]) for c in time_cols]
            ok = any(t and abs(block_end - t) <= timedelta(minutes=settings.live_freshness_minutes) for t in times)
            value = r[value_col]
            if not ok or value is None:
                skipped += 1
                continue
            counted += 1
            per_plant[code] = per_plant.get(code, 0.0) + value * 0.25 / 1000
    return {"per_plant_mu": per_plant, "total_mu": sum(per_plant.values()), "stage_blocks_counted": counted,
            "stage_blocks_skipped": skipped}


def get_today_so_far(entity, metric: str = "gross_generation") -> ToolResult:
    """Energy generated today up to the last completed 15-minute block (MU)."""
    tool = "get_today_so_far"
    try:
        metric = _live_metric(metric)
        if metric not in TODAY_SO_FAR_COLUMN:
            raise ToolError("Today's energy so far is available for gross generation only; "
                            "actual generation (AG) for today is published in tomorrow's daily report.")
        e = Entity.parse(entity)
        block = current_block()
        today = clock.today()
        if block["day"] != today:
            raise ToolError("Real-time data for today has not started yet.")
        completed = block["blk"] - 1
        if completed < 1:
            raise ToolError("The first 15-minute block of today is not complete yet.")
        codes = e.plant_codes()
        unit_no = int(e.unit.replace("UNIT", "")) if e.level == "unit" else None
        stage = e.stage if e.level == "stage" else None
        res = block_energy(today, completed, codes, TODAY_SO_FAR_COLUMN[metric], stage, unit_no)
        up_to = (datetime.combine(today, datetime.min.time()) + timedelta(minutes=15 * completed)).strftime("%H:%M")
        notes = [f"Estimated from the {completed} completed 15-minute blocks of today (up to {up_to}); "
                 f"official daily figures are published the next day."]
        if res["stage_blocks_skipped"]:
            notes.append(f"{res['stage_blocks_skipped']} stage-block values had no live data and were not counted.")
        if e.level == "group":
            notes.append(f"{len(res['per_plant_mu'])} of {len(codes)} plants contributed live data.")
        if not res["per_plant_mu"]:
            return fail(tool, f"No live block data for {e.label()} today.")
        m = get_masters()
        table = None
        if e.level == "group":
            table = sorted(({"name": m.plant_name(c), "mu": round(v, 2)} for c, v in res["per_plant_mu"].items()),
                           key=lambda r: -r["mu"])
        return ToolResult(tool, answer={"entity": e.label(), "metric": get_metric(metric)["name"] + " today so far",
                                        "value": round(res["total_mu"], 2), "unit": "MU", "up_to": up_to,
                                        "blocks_completed": completed},
                          table=table, as_of=f"{today:%Y-%m-%d} {up_to}", is_live=True, sources=[SOURCE], notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))
