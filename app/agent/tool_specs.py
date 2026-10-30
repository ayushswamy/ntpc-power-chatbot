"""
The tools the LLM can choose from (OpenAI "function calling" format) and the code
that runs them.

The LLM only fills simple arguments with the user's words (plant name, unit number,
metric, period phrase). The entity resolver turns names into plant codes and the
time router below checks that live/daily sources are used correctly - the LLM is
not trusted with codes or SQL here.
"""
from app import clock
from app.tools import (compare_periods, get_capacity, get_coal, get_metric_range, get_outages, get_realtime,
                       get_realtime_summary, get_today_so_far, get_trend, get_trips, rank_entities)
from app.tools.common import METRICS, ToolResult, fail, parse_period

# ---------------------------------------------------------------- argument schemas

ENABLED_METRICS = [k for k, m in METRICS.items() if m.get("daily") or m.get("live")]
COAL_METRICS = ["coal_stock", "coal_stock_days", "coal_receipt", "coal_consumption", "specific_coal_consumption"]
LIVE_METRICS = ["gross_generation", "actual_generation", "frequency"]

PERIOD_HELP = ("Period: 'today', 'yesterday', 'this month', 'last month', 'this fy', 'last fy', 'last N days', "
               "a date 'YYYY-MM-DD', a month 'YYYY-MM' or 'fy 2025-26'.")

ENTITY_PROPS = {
    "plant": {"type": "string", "description": "Plant/station name exactly as the user wrote it. Omit for NTPC-wide or group questions."},
    "unit": {"type": "integer", "description": "Unit number, e.g. 3 for 'unit 3'."},
    "stage": {"type": "integer", "description": "Stage number, e.g. 2 for 'stage II'."},
    "group": {"type": "string", "enum": ["ntpc", "ntpc_standalone", "jv"], "description": "Whole-company scope when no single plant is named."},
    "fuel": {"type": "string", "enum": ["coal", "gas", "hydro", "solar", "wind", "renewable"], "description": "Only plants of this type."},
    "region": {"type": "string", "enum": ["NR", "WR", "ER", "SR"], "description": "Only plants in this region."},
}


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


def _metric(enum: list[str], desc: str = "Metric key.") -> dict:
    return {"type": "string", "enum": enum, "description": desc}


PERIOD = {"type": "string", "description": PERIOD_HELP}

TOOL_SPECS = [
    _fn("get_live", "CURRENT / live / right-now value (MW) for a plant, unit, stage or NTPC/group, or grid frequency. "
        "Use for 'now', 'current', 'at present', 'live', 'running'.",
        {**ENTITY_PROPS, "metric": _metric(LIVE_METRICS)}, ["metric"]),
    _fn("get_today_so_far", "Energy (MU) generated TODAY so far (from live 15-minute data).", dict(ENTITY_PROPS), []),
    _fn("get_value", "Value of a metric for a day or period up to yesterday (daily reports): generation, PLF, availability, "
        "heat rate, APC, losses, DC, etc. Use start/end for explicit date ranges.",
        {**ENTITY_PROPS, "metric": _metric(ENABLED_METRICS), "period": PERIOD,
         "start": {"type": "string", "description": "YYYY-MM-DD"}, "end": {"type": "string", "description": "YYYY-MM-DD"}},
        ["metric"]),
    _fn("compare_periods", "Compare a metric between two periods (period1 may be 'today' for live data).",
        {**ENTITY_PROPS, "metric": _metric(ENABLED_METRICS), "period1": PERIOD, "period2": PERIOD},
        ["metric", "period1", "period2"]),
    _fn("get_trend", "Series of values for a chart: by day, month or financial year.",
        {**ENTITY_PROPS, "metric": _metric(ENABLED_METRICS), "period": PERIOD,
         "start": {"type": "string", "description": "YYYY-MM-DD"}, "end": {"type": "string", "description": "YYYY-MM-DD or yesterday"},
         "granularity": {"type": "string", "enum": ["day", "month", "fy"]}}, ["metric", "granularity"]),
    _fn("rank", "Rank plants, stages or units by a metric (top / bottom / best / worst / highest / lowest).",
        {"metric": _metric(ENABLED_METRICS), "period": PERIOD, "top_n": {"type": "integer"},
         "order": {"type": "string", "enum": ["desc", "asc"], "description": "desc = highest first, asc = lowest first"},
         "level": {"type": "string", "enum": ["plant", "stage", "unit"]},
         "group": ENTITY_PROPS["group"], "fuel": ENTITY_PROPS["fuel"], "region": ENTITY_PROPS["region"]},
        ["metric", "order"]),
    _fn("get_outages", "Unit outages / shutdowns with reasons and generation loss (up to yesterday).",
        {**ENTITY_PROPS, "period": PERIOD}, []),
    _fn("get_trips", "Unit trips (automatic shutdowns), including today.",
        {**ENTITY_PROPS, "period": PERIOD,
         "trip_type": {"type": "string", "enum": ["forced", "rsd", "planned"],
                       "description": "Only when the user asks for one type: forced, reserve shutdown (rsd) or planned."}}, []),
    _fn("get_coal", "Coal stock, stock days, receipt or consumption.",
        {**ENTITY_PROPS, "metric": _metric(COAL_METRICS), "period": PERIOD}, ["metric"]),
    _fn("get_capacity", "Installed capacity (MW) now, or on a past date (as_of YYYY-MM-DD).",
        {**ENTITY_PROPS, "as_of": {"type": "string", "description": "YYYY-MM-DD"}}, []),
    _fn("explain_term", "Explain a power-sector term or abbreviation (PLF, DC, MU, heat rate...). No data needed.",
        {"term": {"type": "string"}}, ["term"]),
    _fn("ask_clarification", "Ask the user ONE short question when the request is too vague to answer.",
        {"question": {"type": "string"}}, ["question"]),
    _fn("reply", "Reply without data: greetings, thanks, or questions not about NTPC power plant data.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("sql_query", "LAST RESORT for NTPC data questions that none of the other tools can answer.",
        {"question": {"type": "string", "description": "The full question, restated clearly."}}, ["question"]),
]

TOOL_NAMES = {t["function"]["name"] for t in TOOL_SPECS}
DATA_TOOLS = TOOL_NAMES - {"explain_term", "ask_clarification", "reply", "sql_query"}
LIVE_TOOLS = {"get_live", "get_today_so_far"}
NEEDS_ENTITY = {"get_live", "get_today_so_far", "get_value", "compare_periods", "get_trend", "get_outages",
                "get_trips", "get_coal", "get_capacity"}


# ---------------------------------------------------------------- time router

def _starts_today(period) -> bool:
    try:
        return parse_period(period).start >= clock.today()
    except Exception:
        return False


def route_time(tool: str, args: dict) -> tuple[str, dict, list[str], str]:
    """
    Fix the tool choice for the time asked about. Returns (tool, args, notes, source)
    where source is 'live', 'daily' or 'mixed'.
      - get_value for today: generation -> today so far (live); AG -> live MW; others -> yesterday + note
      - get_live for a metric that is not live (e.g. 'current PLF') -> yesterday's value + note
    """
    notes: list[str] = []
    metric = args.get("metric")
    if tool == "get_value" and not args.get("start") and _starts_today(args.get("period", "")):
        if metric in ("gross_generation", None):
            tool = "get_today_so_far"
        elif metric == "actual_generation":
            tool = "get_live"
        else:
            name = METRICS.get(metric, {}).get("name", metric)
            notes.append(f"{name} is calculated once a day; today's value is not available yet, so yesterday's is shown.")
            args = {**args, "period": "yesterday"}
    if tool in ("get_coal", "rank", "get_outages") and not args.get("start") and _starts_today(args.get("period", "")):
        # these come from the daily reports only - "today" is not there yet, so show the latest day
        notes.append("Daily figures are available up to yesterday, so the latest day is shown.")
        args = {**args, "period": "yesterday"}
    if tool == "get_live" and metric not in LIVE_METRICS:
        name = METRICS.get(metric, {}).get("name", metric)
        notes.append(f"{name} is not measured live; the latest daily value (yesterday) is shown.")
        tool, args = "get_value", {**args, "period": "yesterday"}

    if tool in LIVE_TOOLS or (tool == "get_trips" and _starts_today(args.get("period", "today"))):
        source = "live"
    elif tool == "compare_periods" and (_starts_today(args.get("period1", "")) or _starts_today(args.get("period2", ""))):
        source = "mixed"
    elif tool in DATA_TOOLS or tool == "sql_query":
        source = "daily"
    else:
        source = "none"
    return tool, args, notes, source


# ---------------------------------------------------------------- execution

def run_tool(tool: str, args: dict, entity) -> ToolResult:
    """Run a data tool. `entity` is the resolved Entity (or None for rank)."""
    metric = args.get("metric")
    period = args.get("period")
    if tool == "get_live":
        if metric == "frequency":
            return get_realtime({"level": "group"}, "frequency")
        if entity.level == "group":
            return get_realtime_summary(entity.scope, metric)
        return get_realtime(entity, metric)
    if tool == "get_today_so_far":
        return get_today_so_far(entity, "gross_generation")
    if tool == "get_value":
        if args.get("start"):
            return get_metric_range(entity, metric, args["start"], args.get("end") or args["start"])
        return get_metric_range(entity, metric, period or "yesterday")
    if tool == "compare_periods":
        return compare_periods(entity, metric, args["period1"], args["period2"])
    if tool == "get_trend":
        start = args.get("start") or period or "last 30 days"
        end = args.get("end") or (None if not args.get("start") else "yesterday")
        return get_trend(entity, metric, start, end, args.get("granularity") or "day")
    if tool == "rank":
        return rank_entities(metric, period or "yesterday", None, args.get("top_n") or 10, args.get("order") or "desc",
                             args.get("level") or "plant", entity.scope if entity else {})
    if tool == "get_outages":
        return get_outages(entity, period or "yesterday")
    if tool == "get_trips":
        return get_trips(entity, period or "today", None, args.get("trip_type"))
    if tool == "get_coal":
        return get_coal(entity, metric or "coal_stock", period or "yesterday")
    if tool == "get_capacity":
        return get_capacity(entity, args.get("as_of"))
    return fail(tool, f"Unknown tool {tool}.")
