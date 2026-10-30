"""
Outage and trip tools.

  get_outages(entity, start, end)  - unit outage events from Data_Outage (max 31 days, NTPC-wide max 7 days)
  get_trips(entity, start, end)    - unit trips from Data_Trips (can include today)

Notes on Data_Outage (see semantic/tables.yaml):
  - one row per unit per day per outage; a unit out all day appears every day with
    TripTime 000000 and SyncTime 240000
  - "new outages" = rows whose TripTime is not 000000 (the outage started that day)
  - RSN_Code / SubRSN_Code -> Master_OutageCodes (area / sub-reason)
"""
from datetime import datetime, timedelta

from app import clock
from app.db.masters import get_masters
from app.db.runner import get_runner
from app.tools.common import Entity, Period, ToolError, ToolResult, fail, parse_period

# Data_Trips.TRIP_TYPE (decision 7, source-backed): S and C are NOT mapped - their meaning is unconfirmed
TRIP_TYPES = {"F": "Forced outage", "G": "Reserve shutdown (RSD)", "P": "Planned outage"}
TRIP_FILTER = {"forced": "F", "rsd": "G", "planned": "P"}

MAX_OUTAGE_DAYS = 31
MAX_OUTAGE_DAYS_GROUP = 7


def _hours(hhmmss) -> float | None:
    text = str(hhmmss or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    return int(text[:2]) + int(text[2:4]) / 60 + int(text[4:]) / 3600


def _hhmm(hhmmss) -> str:
    text = str(hhmmss or "").strip()
    return f"{text[:2]}:{text[2:4]}" if len(text) == 6 else text


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def get_outages(entity, start="yesterday", end=None) -> ToolResult:
    """Outage events for a plant / unit / group over a date range (up to yesterday)."""
    tool = "get_outages"
    try:
        e = Entity.parse(entity)
        p = parse_period({"start": parse_period(start).start, "end": parse_period(end or start).end})
        notes: list[str] = []
        yesterday = clock.today() - timedelta(days=1)
        if p.end > yesterday:
            notes.append(f"Outage data is available up to yesterday ({yesterday:%d %b %Y}).")
            p = Period(min(p.start, yesterday), yesterday)
        limit = MAX_OUTAGE_DAYS_GROUP if e.level == "group" else MAX_OUTAGE_DAYS
        if p.days() > limit:
            p = Period(p.end - timedelta(days=limit - 1), p.end)
            notes.append(f"Outage lists are limited to {limit} days: showing {p.text()}. "
                         "For longer periods ask for outage loss (MU) instead.")

        sql = ("SELECT TOP 5000 OutageDate, PlantCode, Stage, Unit, OutageType, RSN_Code, SubRSN_Code, TripTime, SyncTime, HRS, GenLoss "
               "FROM dbo.Data_Outage WHERE OutageDate BETWEEN ? AND ?")
        params: list = [p.start, p.end]
        if e.level != "group":
            sql += " AND PlantCode = ?"
            params.append(e.plant_code)
            if e.level == "unit":
                sql += " AND Unit = ?"
                params.append(e.unit)
            elif e.level == "stage":
                sql += " AND Stage = ?"
                params.append(e.stage)
        sql += " ORDER BY OutageDate, PlantCode, Unit, TripTime"
        result = get_runner().run(sql, params, purpose=f"outages {p.start}..{p.end}")
        if result.truncated:
            notes.append("More than 5000 outage rows - the list is incomplete; narrow the period or plant.")

        m = get_masters()
        codes = set(e.plant_codes())
        rows, by_area, total_loss, new_events, still_out = [], {}, 0.0, 0, set()
        for r in result.rows:
            code = m.resolve_code(r["PlantCode"])
            if code not in codes:
                continue
            area_cd, sub_cd = _int(r["RSN_Code"]), _int(r["SubRSN_Code"])
            area = m.outage_areas.get(area_cd, f"code {r['RSN_Code']}")
            sub = m.outage_codes.get((area_cd, sub_cd), {}).get("out_sub_desc") or ""
            loss = r["GenLoss"] or 0.0
            total_loss += loss
            is_new = str(r["TripTime"]).strip() != "000000"
            new_events += is_new
            by_area[area] = by_area.get(area, 0.0) + loss
            day = r["OutageDate"]
            if day == p.end and str(r["SyncTime"]).strip() == "240000":
                still_out.add((code, r["Unit"]))
            rows.append({"date": day.isoformat(), "unit": m.unit_label(code, r["Unit"]), "reason": area.title(),
                         "detail": sub.strip().title(), "out_from": _hhmm(r["TripTime"]), "back_at": _hhmm(r["SyncTime"]),
                         "hours": round(_hours(r["HRS"]) or 0, 2), "loss_mu": round(loss, 3), "new": is_new})
        breakdown = sorted(({"reason": k.title(), "loss_mu": round(v, 2)} for k, v in by_area.items()), key=lambda x: -x["loss_mu"])
        units_affected = len({row["unit"] for row in rows})
        notes.append("The table has one row per unit per day out; 'new' = the outage started that day, otherwise the unit "
                     "was already out from an earlier day. 'back_at 24:00' means still out at midnight.")
        return ToolResult(tool, answer={"entity": e.label(), "period": p.text(), "units_affected": units_affected,
                                        "new_outages_started": new_events, "unit_days_out_records": len(rows),
                                        "generation_loss_mu": round(total_loss, 2),
                                        "units_still_out_at_end": len(still_out), "loss_by_reason": breakdown},
                          table=rows, sources=["Data_Outage", "Master_OutageCodes"], notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))


def get_trips(entity, start="today", end=None, trip_type: str | None = None) -> ToolResult:
    """Unit trips (automatic shutdowns) for a plant / unit / group; can include today.
    trip_type: 'forced', 'rsd' or 'planned' to count only that type."""
    tool = "get_trips"
    try:
        e = Entity.parse(entity)
        p = parse_period({"start": parse_period(start).start, "end": parse_period(end or start).end})
        if p.days() > 366:
            raise ToolError("Trip lists are limited to one year - please choose a shorter period.")
        m = get_masters()
        sql = ("SELECT TOP 2000 PLN_CD, PLN_UNIT, TRP_DT, TRP_REASON, REVIVAL_DT, EXPECT_DT, TRIP_TYPE, ANALYSIS "
               "FROM dbo.Data_Trips WHERE TRP_DT >= ? AND TRP_DT < ?")
        params: list = [datetime.combine(p.start, datetime.min.time()),
                        datetime.combine(p.end + timedelta(days=1), datetime.min.time())]
        if e.level != "group":
            names = m.trip_codes(e.plant_code)
            if not names:
                raise ToolError(f"Trips cannot be looked up for {e.label()} (no trip-log code in the master data).")
            sql += " AND PLN_CD IN (" + ", ".join(["CAST(? AS varchar(50))"] * len(names)) + ")"
            params += names
        sql += " ORDER BY TRP_DT DESC"
        result = get_runner().run(sql, params, purpose=f"trips {p.start}..{p.end}")

        wanted_code = TRIP_FILTER.get((trip_type or "").lower()) if trip_type else None
        if trip_type and not wanted_code:
            raise ToolError(f"Trip type must be one of: {', '.join(TRIP_FILTER)}.")
        codes, now, rows, unmapped = set(e.plant_codes()), clock.now(), [], set()
        for r in result.rows:
            code = m.by_short_name.get((r["PLN_CD"] or "").strip().lower())
            if code is None:
                unmapped.add(r["PLN_CD"])
                continue
            if code not in codes:
                continue
            unit = f"UNIT{int(r['PLN_UNIT']):02d}" if str(r["PLN_UNIT"]).strip().isdigit() else str(r["PLN_UNIT"])
            if e.level == "unit" and unit != e.unit:
                continue
            type_code = (r["TRIP_TYPE"] or "").strip()
            if wanted_code and type_code != wanted_code:
                continue
            revived = r["REVIVAL_DT"] and r["REVIVAL_DT"] <= now
            rows.append({"tripped_at": r["TRP_DT"].strftime("%Y-%m-%d %H:%M"), "unit": m.unit_label(code, unit),
                         "reason": (r["TRP_REASON"] or "").strip(),
                         "type": TRIP_TYPES.get(type_code, f"{type_code} (meaning not confirmed)" if type_code else ""),
                         "revived_at": r["REVIVAL_DT"].strftime("%Y-%m-%d %H:%M") if revived else None,
                         "expected_revival": r["EXPECT_DT"].strftime("%Y-%m-%d %H:%M") if (not revived and r["EXPECT_DT"]) else None,
                         "analysis": (r["ANALYSIS"] or "").strip()[:300]})
        notes = []
        if e.level == "group" and unmapped:
            notes.append(f"{len(unmapped)} plant code(s) in the trip log could not be matched to a plant and are not counted.")
        by_type: dict[str, int] = {}
        for row in rows:
            by_type[row["type"] or "type not recorded"] = by_type.get(row["type"] or "type not recorded", 0) + 1
        if any("not confirmed" in t for t in by_type):
            notes.append("Trip types S and C are shown as recorded; their meanings are not confirmed yet.")
        still_out = sum(1 for r in rows if not r["revived_at"])
        answer = {"entity": e.label(), "period": p.text(), "trips": len(rows), "not_yet_revived": still_out,
                  "by_type": by_type}
        if wanted_code:
            answer["trip_type"] = TRIP_TYPES[wanted_code]
        return ToolResult(tool, answer=answer,
                          table=rows, as_of=now.strftime("%Y-%m-%d %H:%M") if p.end >= clock.today() else None,
                          is_live=p.end >= clock.today(), sources=["Data_Trips"], notes=notes)
    except ToolError as exc:
        return fail(tool, str(exc))
