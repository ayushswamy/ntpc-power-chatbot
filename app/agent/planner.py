"""
Planner: ONE LLM call that reads the user's message and picks exactly one tool
(this is also the router: greetings -> reply, definitions -> explain_term,
vague -> ask_clarification, data -> a data tool, nothing fits -> sql_query).

Prompt layout (for speed on CPU): the long, fixed part (rules + tool list) comes
first so Ollama/vLLM can reuse its cached computation; the short, changing part
(context, similar examples, question) comes last.
"""
import json
import re
from datetime import timedelta

from app import clock
from app.agent.entities import mask_plants
from app.agent.llm import chat
from app.agent.tool_specs import TOOL_NAMES, TOOL_SPECS
from app.rag.store import search

SYSTEM_PROMPT = """You are the planner of NTPC's power-plant data assistant. For every user message call exactly ONE tool.

How to choose:
- "now", "current", "live", "at present", "running", "right now" -> get_live (power in MW).
- "today's generation", "generated today", "so far today" -> get_today_so_far (energy in MU).
- Yesterday or earlier, a month, a financial year -> get_value (one number), compare_periods, get_trend (chart) or rank.
- "today vs yesterday" / "today compared with ..." -> compare_periods with period1 = "today".
- Outages/shutdowns -> get_outages. Trips -> get_trips. Coal stock/receipt/consumption -> get_coal. Capacity -> get_capacity.
- Meaning of a term ("what is PLF") -> explain_term. Greetings, thanks, off-topic -> reply. Too vague -> ask_clarification.
- sql_query ONLY for NTPC data questions no other tool can answer.

Arguments:
- plant: the plant name exactly as the user wrote it (never a code). For NTPC as a whole use group "ntpc" and no plant.
- unit / stage: numbers only ("unit 3" -> unit 3, "stage II" -> stage 2).
- Metric names: generation/output/MU/MW -> gross_generation; AG/ex-bus -> actual_generation; PLF -> plf;
  availability/AvF -> availability; heat rate -> heat_rate; APC/aux -> apc; DC -> declared_capacity;
  SG/schedule -> scheduled_generation (SG % -> scheduled_generation_percent);
  forced outage -> forced_outage_loss; BTL/tube leakage -> btl_count.
- Periods: "today", "yesterday", "this month", "last month", "this fy", "last fy", "last N days" ("last week" = "last 7 days"),
  or dates "YYYY-MM-DD", months "YYYY-MM", "fy 2025-26". Convert dates the user writes into these forms.
- Follow-ups ("and Korba?", "compare with last month", "show the trend", "what about availability?") keep the plant,
  metric and period from the conversation context and change only what the user changed.
"""


OFF_TOPIC_TEXT = ("I can only answer questions about NTPC power plant data - for example live generation, PLF, "
                  "availability, outages, trips, coal stock or capacity.")


def _clean_reply(text) -> bool:
    """False if the model's reply text is garbled (tool-call markup, JSON) or empty."""
    text = str(text or "").strip()
    return bool(text) and not any(bad in text for bad in ("{", "}", "tool_call", "<", ">"))


def _fill_dates(value):
    """Replace {today-1y} / {today-2d} placeholders used in examples.yaml."""
    today = clock.today()
    text = json.dumps(value)
    try:
        last_year = today.replace(year=today.year - 1)
    except ValueError:  # 29 Feb
        last_year = today - timedelta(days=365)
    text = text.replace("{today-1y}", last_year.isoformat()).replace("{today-2d}", (today - timedelta(days=2)).isoformat())
    return json.loads(text)


def context_line(context: dict) -> str:
    if context.get("pending"):
        p = context["pending"]
        return (f"Waiting for the user to choose between: {', '.join(p['option_names'])} "
                f"(for the question: \"{p['question']}\" -> {p['tool']}).")
    if not context.get("entity_label"):
        return "none"
    parts = [f"plant/scope = {context['entity_label']}"]
    for key in ("metric", "period", "period1", "period2"):
        if context.get("args", {}).get(key):
            parts.append(f"{key} = {context['args'][key]}")
    parts.append(f"last tool = {context.get('tool')}")
    return "; ".join(parts)


def plan(message: str, context: dict, history: list[dict] | None = None) -> dict:
    """Returns {"tool", "args", "llm": {seconds, tokens...}, "examples": [ids]}."""
    today = clock.today()
    examples = search(mask_plants(message), "example", k=3)
    shots = []
    for ex in examples:
        args = _fill_dates(ex["args"])
        prefix = f"(follow-up to \"{ex['follow_up_of']}\") " if ex.get("follow_up_of") else ""
        shots.append(f"{prefix}Q: {ex['question']} -> {ex['tool']}({json.dumps(args)})")

    user = (f"Today is {today:%A %d %B %Y} ({today.isoformat()}). The financial year starts on 1 April.\n"
            f"Conversation context: {context_line(context)}\n")
    if history:
        user += "Recent messages:\n" + "\n".join(f"- {h['role']}: {h['text'][:150]}" for h in history[-4:]) + "\n"
    if shots:
        user += "Similar examples:\n" + "\n".join(shots) + "\n"
    user += f"User message: {message}"

    result = chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                  tools=TOOL_SPECS, tool_choice="required", max_tokens=300)
    llm_info = {k: result[k] for k in ("seconds", "prompt_tokens", "cached_tokens", "completion_tokens")}

    if result["tool_calls"]:
        name, raw_args = result["tool_calls"][0]
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            args = {}
        if name == "reply" and not _clean_reply(args.get("text")):
            args = {"text": OFF_TOPIC_TEXT}
        if name in TOOL_NAMES:
            return {"tool": name, "args": args, "llm": llm_info, "examples": [e["id"] for e in examples]}
    # The model wrote the tool call as TEXT (small models sometimes do): try to read it
    parsed = _tool_from_text(result["content"])
    if parsed:
        return {"tool": parsed[0], "args": parsed[1], "llm": llm_info, "examples": [e["id"] for e in examples]}
    text = result["content"] if _clean_reply(result["content"]) else OFF_TOPIC_TEXT
    return {"tool": "reply", "args": {"text": text}, "llm": llm_info, "examples": [e["id"] for e in examples]}


def _tool_from_text(text: str) -> tuple[str, dict] | None:
    """'{"name": "explain_term", "arguments": {"term": "PLF"}}' or '.reply\\n{"text": ...}' -> (tool, args)."""
    text = text or ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES:
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return obj["name"], args if isinstance(args, dict) else {}
    prefix = re.match(r"\W*(\w+)\s*\{", text[: m.start() + 1])
    if prefix and prefix.group(1) in TOOL_NAMES and isinstance(obj, dict):
        return prefix.group(1), obj
    return None
