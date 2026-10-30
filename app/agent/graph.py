"""
The chatbot as a LangGraph graph.

    load -> quick_route --(plan needed)--> plan -> resolve --(data)--> execute -> compose -> finish
                 |                                  |
                 +------(greeting / option picked)--+--(reply / clarification)--------------> finish

  load        conversation context + recent messages from SQLite
  quick_route greetings/thanks without the LLM; a reply to a pending "which plant?" question
  plan        ONE LLM call: choose the tool and its arguments (app/agent/planner.py)
  resolve     context merge (follow-ups), entity resolution (names -> codes), time routing (live/daily/mixed)
  execute     run the tool / glossary / text-to-SQL
  compose     answer text (LLM + number check, or template), table, chart
  finish      save messages + context, write the audit log

Progress events (for streaming in the API) are sent through config["configurable"]["emit"].
"""
import json
import re
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent import composer
from app.agent.entities import match_option, resolve as resolve_entity
from app.agent.glossary import explain_term
from app.agent.llm import LLMError
from app.agent.planner import plan as run_planner
from app.agent.text2sql import sql_query
from app.agent.tool_specs import NEEDS_ENTITY, route_time, run_tool
from app.config import PROJECT_ROOT, settings
from app.db.masters import get_masters
from app.db.runner import start_trace
from app.memory.store import get_store
from app.tools.common import Entity, ToolResult

GREETING = re.compile(r"^\s*(hi|hello|hey|namaste|good (morning|afternoon|evening)|thanks|thank you|ok|okay)\b[\s!.]*$", re.I)
HELP_TEXT = ("Hello! I can answer questions about NTPC power data - live generation (\"current generation of Singrauli\"), "
             "today so far, daily and monthly generation, PLF, availability, outages, trips, coal stock and capacity.")


class ChatState(TypedDict, total=False):
    session_id: str
    message: str
    user_text: str          # what the user actually typed (message may be replaced by a pending question)
    context: dict
    history: list
    tool: str
    args: dict
    plan_info: dict
    entity: Any
    source: str
    notes: list
    result: Any
    response: dict
    clarification: dict | None
    sql_attempts: list
    timings: dict
    error: str | None


def _emit(config, kind: str, data: dict | None = None) -> None:
    emit = (config or {}).get("configurable", {}).get("emit")
    if emit:
        emit(kind, data or {})


# ------------------------------------------------------------------ nodes

def load(state: ChatState, config=None) -> dict:
    store = get_store()
    return {"context": store.get_context(state["session_id"]), "history": store.recent_messages(state["session_id"], 6),
            "notes": [], "timings": {}, "clarification": None, "sql_attempts": []}


def quick_route(state: ChatState, config=None) -> dict:
    msg, ctx = state["message"], state["context"]
    if GREETING.match(msg):
        text = "You're welcome! Anything else?" if re.search(r"thank", msg, re.I) else HELP_TEXT
        return {"tool": "reply", "args": {"text": text}}
    pending = ctx.get("pending")
    if pending and pending.get("options"):
        code = match_option(msg, pending["options"])
        if code:
            args = {**pending["args"], "plant": get_masters().plants[code]["StationName"]}
            args.pop("group", None)
            return {"tool": pending["tool"], "args": args, "message": pending["question"],
                    "context": {k: v for k, v in ctx.items() if k != "pending"}}
    return {}


def plan(state: ChatState, config=None) -> dict:
    _emit(config, "status", {"step": "planning"})
    start = time.perf_counter()
    try:
        p = run_planner(state["message"], state["context"], state["history"])
    except LLMError as exc:
        return {"tool": "reply", "args": {"text": f"{exc} Please try again in a minute."}, "error": str(exc),
                "timings": {**state["timings"], "plan": round(time.perf_counter() - start, 1)}}
    return {"tool": p["tool"], "args": p["args"], "plan_info": p,
            "timings": {**state["timings"], "plan": round(time.perf_counter() - start, 1)}}


def resolve(state: ChatState, config=None) -> dict:
    tool, args, ctx = state["tool"], dict(state.get("args") or {}), state["context"]
    notes = list(state.get("notes") or [])
    if tool in ("reply", "ask_clarification", "explain_term", "sql_query"):
        return {"source": "daily" if tool == "sql_query" else "none", "args": args}

    # --- context merge: fill what a follow-up question leaves out
    last = ctx.get("args", {})
    names_entity = any(args.get(k) for k in ("plant", "group", "fuel", "region"))
    if args.get("metric") is None and last.get("metric") and tool not in ("get_today_so_far", "get_outages", "get_trips", "get_capacity"):
        args["metric"] = last["metric"]
    if not args.get("period") and not args.get("start") and tool in ("get_value", "get_trend", "rank", "get_outages", "get_coal") \
            and last.get("period"):
        args["period"] = last["period"]

    # --- entity
    entity = None
    if tool == "rank":
        entity = resolve_entity(group=args.get("group") or "ntpc", fuel=args.get("fuel"), region=args.get("region")).entity
    elif tool in NEEDS_ENTITY:
        if not names_entity and ctx.get("entity") and not (tool == "get_live" and args.get("metric") == "frequency"):
            entity = Entity(**ctx["entity"])
        elif not names_entity:
            entity = Entity(level="group", scope={})
        else:
            metric = args.get("metric") or ""
            coal_question = tool == "get_coal" or metric.startswith(("coal", "specific_coal"))
            r = resolve_entity(args.get("plant"), args.get("unit"), args.get("stage"), args.get("group"),
                               args.get("fuel"), args.get("region"), fuel_hint="C" if coal_question else None)
            if r.clarification:
                m = get_masters()
                return {"clarification": {"question": r.clarification, "options": r.options,
                                          "option_names": [m.plant_name(c) for c in r.options]},
                        "args": args, "source": "none"}
            entity = r.entity
            if not (ctx.get("entity") and Entity(**ctx["entity"]) == entity):   # don't repeat "Assumed X" on follow-ups
                notes += r.notes

    # --- time routing (live / daily / mixed)
    tool, args, time_notes, source = route_time(tool, args)
    return {"tool": tool, "args": args, "entity": entity, "notes": notes + time_notes, "source": source}


def execute(state: ChatState, config=None) -> dict:
    tool, args = state["tool"], state["args"]
    _emit(config, "status", {"step": "querying", "tool": tool})
    start = time.perf_counter()
    attempts = []
    if tool == "explain_term":
        result = explain_term(args.get("term", ""))
    elif tool == "sql_query":
        result, attempts = sql_query(args.get("question") or state["message"])
    else:
        result = run_tool(tool, args, state.get("entity"))
    return {"result": result, "sql_attempts": attempts,
            "timings": {**state["timings"], "tools": round(time.perf_counter() - start, 2)}}


def compose(state: ChatState, config=None) -> dict:
    _emit(config, "status", {"step": "writing answer"})
    start = time.perf_counter()
    response = composer.compose(state["message"], state["result"], state.get("notes"))
    return {"response": response, "timings": {**state["timings"], "compose": round(time.perf_counter() - start, 1)}}


def finish(state: ChatState, config=None) -> dict:
    """Build replies that skipped the tools, then save messages, context and the audit record."""
    store, sid = get_store(), state["session_id"]
    ctx = dict(state["context"])
    response = state.get("response")
    if state.get("clarification"):
        c = state["clarification"]
        response = {"text": c["question"], "clarification": True, "options": c["option_names"]}
        ctx["pending"] = {"question": state["message"], "tool": state["tool"], "args": state.get("args", {}),
                          "options": c["options"], "option_names": c["option_names"]}
    elif response is None:   # reply / ask_clarification from the planner
        text = state["args"].get("text") or state["args"].get("question") or HELP_TEXT
        response = {"text": text, "clarification": state["tool"] == "ask_clarification"}
    else:
        ctx.pop("pending", None)
        result: ToolResult = state["result"]
        if result.ok and state.get("entity") is not None:
            e = state["entity"]
            ctx.update(entity=asdict(e), entity_label=e.label(), tool=state["tool"], args=state["args"])

    response = {"text": "", "table": None, "chart": None, "sources": [], "as_of": None, "is_live": False, **response,
                "source": state.get("source", "none"), "tool": state.get("tool"), "session_id": sid}
    store.add_message(sid, "user", state.get("user_text") or state["message"])
    store.add_message(sid, "assistant", response["text"], {"tool": state.get("tool"), "source": response["source"]})
    store.save_context(sid, ctx)
    return {"response": response, "context": ctx}


# ------------------------------------------------------------------ edges

def after_quick_route(state: ChatState) -> str:
    return "resolve" if state.get("tool") else "plan"


def after_resolve(state: ChatState) -> str:
    if state.get("clarification") or state["tool"] in ("reply", "ask_clarification"):
        return "finish"
    return "execute"


def build_graph():
    g = StateGraph(ChatState)
    for name, fn in (("load", load), ("quick_route", quick_route), ("plan", plan), ("resolve", resolve),
                     ("execute", execute), ("compose", compose), ("finish", finish)):
        g.add_node(name, fn)
    g.add_edge(START, "load")
    g.add_edge("load", "quick_route")
    g.add_conditional_edges("quick_route", after_quick_route, {"plan": "plan", "resolve": "resolve"})
    g.add_edge("plan", "resolve")
    g.add_conditional_edges("resolve", after_resolve, {"execute": "execute", "finish": "finish"})
    g.add_edge("execute", "compose")
    g.add_edge("compose", "finish")
    g.add_edge("finish", END)
    return g.compile()


_graph = None


def audit(record: dict) -> None:
    """One line per question: time, session, question, tool, args, queries (SQL, rows, ms), latency, error.
    No result data and no credentials."""
    from app.logs import append_jsonl
    append_jsonl("audit", record)


def ask(message: str, session_id: str | None = None, emit=None, client: str | None = None) -> dict:
    """Answer one user message. Returns the response dict (see composer.py) plus session_id and timings.
    client: the caller's address (for the audit log only)."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    sid = get_store().ensure_session(session_id)
    message = message.replace("﻿", "")       # invisible byte-order mark (e.g. text piped from PowerShell)
    trace = start_trace()
    start = time.perf_counter()
    error = None
    try:
        state = _graph.invoke({"session_id": sid, "message": message.strip()[:1000], "user_text": message.strip()[:1000]},
                              config={"configurable": {"emit": emit}})
        response = state["response"]
    except Exception as exc:  # never crash the chat: log and apologise
        error = f"{type(exc).__name__}: {exc}"
        state = {}
        response = {"text": "Sorry, something went wrong while answering. The problem has been logged.",
                    "table": None, "chart": None, "sources": [], "as_of": None, "is_live": False,
                    "source": "none", "tool": None, "session_id": sid}
    total = round(time.perf_counter() - start, 1)
    response["timings"] = {**state.get("timings", {}), "total": total}
    if settings.debug:
        response["sql_debug"] = [q["sql"] for q in trace] + [f"text2sql attempt: {s}" for s in state.get("sql_attempts", [])]
        plan_info = state.get("plan_info") or {}
        result = state.get("result")
        entity = state.get("entity")
        response["debug"] = {"args": state.get("args"), "plan": plan_info.get("llm"), "examples": plan_info.get("examples"),
                             "planned": {"tool": plan_info.get("tool"), "args": plan_info.get("args")},
                             "entity": asdict(entity) if entity is not None else None,
                             "result_ok": result.ok if result is not None else None,
                             "result_answer": result.answer if result is not None else None,
                             "result_table": (result.table or [])[:100] if result is not None else None,
                             "composer": response.get("composer")}
    audit({"time": datetime.now().isoformat(timespec="seconds"), "session": sid, "client": client, "question": message[:500],
           "tool": state.get("tool"), "args": state.get("args"), "source": response.get("source"),
           "queries": [{k: q[k] for k in ("purpose", "rows", "ms", "error")} for q in trace],
           "sql": [q["sql"][:500] for q in trace], "latency_s": total, "timings": response["timings"],
           "composer": response.get("composer"), "error": error or state.get("error")})
    return response
