"""
Answer composer: ToolResult -> the response the user sees.

  text      short answer first (value + unit + period / "as of" time + plant), then caveats
  table     rows for the widget (optional)
  chart     {type: line|bar, title, x, series[{name, data}]} (optional)
  sources   tables used;  as_of;  is_live

Text is written by the LLM (ANSWER_MODE=llm) and then CHECKED: every number in the
text must appear in the tool result (or the question). If not, the LLM gets one more
try; if that also fails - or the LLM is unavailable - a fixed template is used.
So a number the database did not return can never reach the user.
"""
import json
import re

from app.agent.llm import LLMError, chat
from app.config import settings
from app.tools.common import ToolResult

COMPOSER_PROMPT = """You write the final answer of NTPC's power data assistant.
Rules:
- Use ONLY facts and numbers that appear in DATA. Copy numbers exactly (thousands separators allowed). Never calculate, never convert units (MW stays MW, MU stays MU).
- Never add descriptions or opinions that DATA does not state (e.g. "running at full capacity", "good performance").
- First sentence: value with unit, the plant/scope, and the period - or "live, as of HH:MM" when is_live is true.
- Second sentence (only if needed): the most important note from DATA (missing data, coverage, an assumption, data range), in its own meaning.
- Comparisons: give BOTH values with their periods, then the difference.
- Trends (DATA has "granularity"): do not state one overall value; give the first and last points and the highest and lowest from the table.
- If there is a table, name at most the top 3 rows; the full table is shown separately. Do not count or group table rows yourself.
- Query results (answer has "result_columns"): never add a unit (MU, MW, %) that is not part of the column name.
- No filler sentences such as "No additional notes provided" or "The data is not live".
- Plain text, 1 to 3 short sentences. No markdown, no lists."""

NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")

# Answers that are lists of numbers read best (and are always exact) with fixed wording - no LLM needed.
TEMPLATE_TOOLS = {"get_trend", "rank_entities", "get_capacity", "explain_term",
                  # the full eval showed the 8B model mislabels comparisons (swapped periods) and outage/trip lists
                  "compare_periods", "get_outages", "get_trips"}
# The LLM sees table rows only where it has to describe them; elsewhere it tended to count/group rows itself.
TABLE_FOR_LLM = {"sql_query", "get_outages", "get_trips"}


# ---------------------------------------------------------------- number check

def _numbers(text: str) -> list[str]:
    return [n for n in NUMBER.findall(text or "")]


def _to_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def allowed_numbers(result: dict, question: str) -> set[float]:
    blob = json.dumps(result, default=str) + " " + question
    # Only numbers from the data or the question - even small ones ("7 units") must be backed by data
    return {v for v in (_to_float(t) for t in _numbers(blob)) if v is not None}


def unsupported_numbers(text: str, allowed: set[float]) -> list[str]:
    """Numbers in the text that do not match any number in the data (allowing rounding to what was written)."""
    bad = []
    for token in _numbers(text):
        value = _to_float(token)
        if value is None:
            continue
        decimals = len(token.split(".")[1]) if "." in token else 0
        tolerance = 0.5 * 10 ** -decimals + 1e-9
        if not any(abs(value - a) <= tolerance for a in allowed):
            bad.append(token)
    return bad


def _unit_not_in_columns(text: str, columns: list[str]) -> bool:
    """True if the text uses a unit (MU, MW, %) that no result column name mentions."""
    cols = " ".join(columns).lower()
    for unit, hints in (("MU", ("mu",)), ("MW", ("mw",)), ("%", ("per", "plf", "avb", "pct", "percent"))):
        if re.search(rf"(?<![A-Za-z]){re.escape(unit)}(?![A-Za-z])", text) and not any(h in cols for h in hints):
            return True
    return False


# ---------------------------------------------------------------- template (no LLM)

def _fmt(v, unit=""):
    if v is None:
        return "not available"
    if isinstance(v, float) and v.is_integer() and unit in ("MW", "tonnes", "days", "nos"):
        v = int(v)
    text = f"{v:,}" if isinstance(v, (int, float)) else str(v)
    return f"{text} {unit}".strip() if unit and unit != "%" else f"{text}{'%' if unit == '%' else ''}"


def template_text(r: ToolResult) -> str:
    a, t = r.answer, r.tool
    if not r.ok and r.error:
        return r.error
    if t in ("get_realtime", "get_realtime_summary"):
        if a.get("value") is None:
            text = f"No current value is available for {a.get('entity')}."
        else:
            text = f"{a.get('metric')} of {a.get('entity')}: {_fmt(a['value'], a.get('unit'))} (live, as of {(r.as_of or '')[-5:]})."
    elif t == "get_today_so_far":
        text = f"{a['metric']} for {a['entity']}: {_fmt(a['value'], 'MU')} up to {a['up_to']} (live estimate)."
    elif t == "compare_periods":
        p1, p2 = a["period1"], a["period2"]
        text = (f"{a['metric']} of {a['entity']}: {_fmt(p1['value'], a['unit'])} for {p1['period']} vs "
                f"{_fmt(p2['value'], a['unit'])} for {p2['period']}.")
        if a.get("difference") is not None:
            if a["unit"] == "%":   # a change of a percentage is in percentage points
                text += f" Difference: {a['difference']:+} percentage points"
            else:
                text += f" Difference: {_fmt(a['difference'], a['unit'])}"
            text += f" ({a['change_percent']:+}% relative)." if a.get("change_percent") is not None else "."
    elif t == "get_trend":
        pts = [p for p in (r.table or []) if p.get("value") is not None]
        text = f"{a['metric']} of {a['entity']} by {a['granularity']} ({a['points']} points) - see the chart."
        if pts:
            hi, lo = max(pts, key=lambda p: p["value"]), min(pts, key=lambda p: p["value"])
            unit = a.get("unit", "")
            text += (f" From {_fmt(pts[0]['value'], unit)} ({pts[0]['period']}) to {_fmt(pts[-1]['value'], unit)} "
                     f"({pts[-1]['period']}); highest {_fmt(hi['value'], unit)} ({hi['period']}), "
                     f"lowest {_fmt(lo['value'], unit)} ({lo['period']}).")
    elif t == "rank_entities" or (t == "get_coal" and a.get("order")):
        rows = (r.table or [])[:5]
        if not rows:
            text = f"No {a['metric']} data is available for {a['period']} ({a['scope']})."
        else:
            top = ", ".join(f"{row['rank']}. {row['name']} ({_fmt(row['value'], row['unit'])})" for row in rows)
            text = f"{a['metric']} ranking for {a['period']} ({a['scope']}): {top}."
        if a.get("total") is not None:
            text += f" Total: {_fmt(a['total'], a['unit'])}."
    elif t == "get_outages":
        if not a.get("units_affected"):
            text = f"No unit outages were recorded at {a['entity']} for {a['period']}."
        else:
            text = (f"Outages at {a['entity']} for {a['period']}: {a['units_affected']} unit(s) affected, "
                    f"{a['new_outages_started']} new outage(s) started, generation loss {_fmt(a['generation_loss_mu'], 'MU')}; "
                    f"{a['units_still_out_at_end']} unit(s) still out at the end of the period.")
            rows = sorted(r.table or [], key=lambda x: (not x.get("new"), x["date"]))[:4]
            details = [f"{x['unit']} on {x['date']}: out {x['out_from']}-{x['back_at']} ({x['hours']} h), "
                       f"{x['reason']}{' - ' + x['detail'] if x.get('detail') else ''}, loss {x['loss_mu']} MU" for x in rows]
            text += " " + "; ".join(details) + "."
    elif t == "get_trips":
        kind = f" {a['trip_type'].lower()}" if a.get("trip_type") else ""
        text = f"{a['trips']}{kind} trip(s) at {a['entity']} for {a['period']}; {a['not_yet_revived']} not yet revived."
        if a.get("by_type") and not a.get("trip_type") and a["trips"]:
            text += " By type: " + ", ".join(f"{k} {v}" for k, v in a["by_type"].items()) + "."
        details = [f"{x['unit']} at {x['tripped_at']} ({x['type'] + ': ' if x.get('type') else ''}{x['reason'] or 'reason not recorded'}"
                   + (f", revived {x['revived_at']}" if x.get("revived_at") else ", not yet revived") + ")"
                   for x in (r.table or [])[:4]]
        if details:
            text += " Latest: " + "; ".join(details) + "."
    elif t == "get_capacity":
        text = f"Installed capacity of {a['entity']}: {_fmt(a['capacity_mw'], 'MW')}" + (f" as on {a['as_of']}." if a.get("as_of") else ".")
    elif t == "explain_term":
        text = f"{a['term']}" + (f" ({a['full']})" if a.get("full") else "") + f": {a['definition']}"
    elif t == "sql_query":
        rows = r.table or []
        if len(rows) == 1:
            text = "Result: " + ", ".join(f"{k} = {_fmt(v)}" for k, v in rows[0].items()) + "."
        elif rows:
            text = (f"The query returned {a.get('number_of_result_rows', 0)} rows (see the table). First row: "
                    + ", ".join(f"{k} = {_fmt(v)}" for k, v in rows[0].items()) + ".")
        else:
            text = "The query returned no rows."
    elif a.get("value") is not None:
        text = f"{a.get('metric')} of {a.get('entity')} for {a.get('period')}: {_fmt(a['value'], a.get('unit', ''))}."
    elif r.notes:
        return " ".join(r.notes[:4])                         # the notes explain why there is no value
    else:
        text = "No data was found for this question."
    if r.notes:
        text += " Note: " + " ".join(r.notes[:4])
    return text


# ---------------------------------------------------------------- table and chart

def build_table(r: ToolResult) -> dict | None:
    if not r.table:
        return None
    rows = r.table[:100]
    columns = list(rows[0].keys())
    return {"columns": columns, "rows": [[row.get(c) for c in columns] for row in rows]}


def build_chart(r: ToolResult) -> dict | None:
    a = r.answer
    if r.tool == "get_trend" and r.table:
        return {"type": "line" if a.get("granularity") == "day" else "bar",
                "title": f"{a['metric']} - {a['entity']} ({a.get('unit', '')})",
                "x": [p["period"] for p in r.table],
                "series": [{"name": a["metric"], "data": [p["value"] for p in r.table]}]}
    if (r.tool == "rank_entities" or (r.tool == "get_coal" and a.get("order"))) and r.table:
        return {"type": "bar", "title": f"{a['metric']} ({a.get('unit', '')}) - {a['period']}",
                "x": [row["name"] for row in r.table], "series": [{"name": a["metric"], "data": [row["value"] for row in r.table]}]}
    if r.tool == "compare_periods" and a.get("period1", {}).get("value") is not None and a.get("period2", {}).get("value") is not None:
        return {"type": "bar", "title": f"{a['metric']} - {a['entity']} ({a['unit']})",
                "x": [a["period1"]["period"], a["period2"]["period"]],
                "series": [{"name": a["metric"], "data": [a["period1"]["value"], a["period2"]["value"]]}]}
    if r.tool == "get_realtime_summary" and r.table:
        return {"type": "bar", "title": f"{a.get('metric', 'Live generation')} (MW)",
                "x": [row["name"] for row in r.table], "series": [{"name": "MW", "data": [row["mw"] for row in r.table]}]}
    return None


# ---------------------------------------------------------------- main entry

def _compact(r: ToolResult) -> dict:
    """What the LLM sees: the answer numbers, notes, time, and the first rows of the table."""
    data = {"answer": r.answer, "notes": r.notes, "is_live": r.is_live, "as_of": r.as_of}
    if r.table and r.tool in TABLE_FOR_LLM:
        data["table_first_rows"] = r.table[:8]
        data["table_rows_total"] = len(r.table)
    if r.error:
        data["error"] = r.error
    return data


def compose(question: str, r: ToolResult, extra_notes: list[str] | None = None) -> dict:
    """Build the full response. Returns dict with text, table, chart, sources, as_of, is_live, composer."""
    if extra_notes:
        r.notes = list(extra_notes) + [n for n in r.notes if n not in extra_notes]
    response = {"table": build_table(r), "chart": build_chart(r), "sources": r.sources,
                "as_of": r.as_of, "is_live": r.is_live}

    simple = r.tool in TEMPLATE_TOOLS or (r.tool == "get_coal" and r.answer.get("order")) or not r.ok
    if settings.answer_mode != "llm" or simple:
        return {**response, "text": template_text(r), "composer": "template"}

    data = _compact(r)
    allowed = allowed_numbers(data, question)
    messages = [{"role": "system", "content": COMPOSER_PROMPT},
                {"role": "user", "content": f"QUESTION: {question}\nDATA: {json.dumps(data, default=str)}"}]
    try:
        for attempt in (1, 2):
            out = chat(messages, max_tokens=150, temperature=0.1)
            text = " ".join(out["content"].split())          # one paragraph, no stray line breaks
            bad = unsupported_numbers(text, allowed)
            sentences = [s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
            if len(sentences) != len(set(sentences)) or len(sentences) > 5:
                break                                        # repeated / run-away text -> use the template
            if r.tool == "sql_query" and _unit_not_in_columns(text, r.answer.get("result_columns") or []):
                break                                        # e.g. "MU" added to a column whose unit is unknown
            if text and not bad:
                return {**response, "text": text, "composer": f"llm (attempt {attempt}, {out['seconds']}s)"}
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"These numbers are not in DATA: {', '.join(bad)}. "
                                                     "Rewrite the answer using only numbers from DATA."}]
    except LLMError:
        pass
    return {**response, "text": template_text(r), "composer": "template (number check or LLM failed)"}
