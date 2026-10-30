"""
Phase 4 - Accuracy evaluation of the full chatbot (LLM + tools + composer) on examples/examples.yaml.

For every example question (and every turn of the multi-turn conversations) it:
  1. asks the chatbot (new session per example; one session per conversation)
  2. checks
       tool    - the final tool (after the time router) is the expected one
       args    - plant/unit/stage/group (compared as resolved plant codes), metric, period
                 (compared as real date ranges), top_n, order, level, granularity
       source  - live / daily / mixed / none as expected
       value   - runs the EXPECTED tool call directly on the DB right after the answer and compares
                 the key numbers (daily: exact; live: within 5% because values change every minute;
                 the live cache is cleared first so the direct query is a fresh read)
       text    - the main value appears in the answer text; required phrases (expect_text);
                 a clarifying question when expected (expect_clarification)
  3. records latency (planning, tools, composing, total)

Reads the PRODUCTION database (read-only, through the safe runner) and uses the local LLM.
Saves (after every question, so a stopped run keeps its results):
    outputs/eval/eval_<time>.json   every question with expected/actual/checks
    outputs/eval/eval_<time>.md     summary, accuracy per check and category, failure list, latency

Run from the project folder:
    .\\.venv\\Scripts\\python.exe eval\\run_eval.py --quick        # 20 questions + 1 conversation (~25 min)
    .\\.venv\\Scripts\\python.exe eval\\run_eval.py                # everything (~1.5-2 hours on this PC)
    .\\.venv\\Scripts\\python.exe eval\\run_eval.py --ids D01,L03  # chosen examples
    add --template to skip the answer-writing LLM call (faster; text checks use the template)
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.config  # noqa: E402

object.__setattr__(app.config.settings, "debug", True)      # the eval needs the debug details

from app.agent.composer import _numbers, _to_float  # noqa: E402
from app.agent.entities import resolve  # noqa: E402
from app.agent.graph import ask  # noqa: E402
from app.agent.planner import _fill_dates  # noqa: E402
from app.agent.tool_specs import route_time, run_tool  # noqa: E402
from app.memory.store import get_store  # noqa: E402
from app.tools import live  # noqa: E402
from app.tools.common import Entity, parse_period  # noqa: E402

OUT = ROOT / "outputs" / "eval"
QUICK_IDS = ["L01", "L03", "L04", "L06", "L11", "L12", "M01", "M02", "D01", "D04", "D09", "D10", "C01", "T01",
             "R01", "R02", "O01", "F01", "K02", "X01", "G01", "G06"]
QUICK_CONVERSATIONS = ["CV2"]
DEFAULTS = {"top_n": 10, "order": "desc", "level": "plant", "granularity": "day", "trip_type": None}
PERIOD_DEFAULT = {"get_value": "yesterday", "get_trend": "last 30 days", "rank": "yesterday", "get_outages": "yesterday",
                  "get_trips": "today", "get_coal": "yesterday"}
NO_DATA_TOOLS = {"reply", "ask_clarification", "explain_term", "sql_query"}


# ------------------------------------------------------------------ helpers

def effective_period(tool: str, args: dict, key: str = "period"):
    """(start, end) the tool will really use - so 'last month' and '2026-08' compare equal."""
    try:
        if key == "period" and args.get("start"):
            return (parse_period(args["start"]).start, parse_period(args.get("end") or args["start"]).end)
        value = args.get(key) or (PERIOD_DEFAULT.get(tool) if key == "period" else None)
        if value is None:
            return None
        p = parse_period(value)
        return (p.start, p.end)
    except Exception:
        return ("unparsable", str(args.get(key)))


def expected_entity(tool: str, args: dict):
    if tool == "rank":
        return resolve(group=args.get("group") or "ntpc", fuel=args.get("fuel"), region=args.get("region"))
    coal_question = tool == "get_coal" or (args.get("metric") or "").startswith(("coal", "specific_coal"))
    return resolve(args.get("plant"), args.get("unit"), args.get("stage"), args.get("group"), args.get("fuel"),
                   args.get("region"), fuel_hint="C" if coal_question else None)


def main_values(tool: str, answer: dict | None, table: list | None):
    """The numbers/names that define a correct answer, per tool."""
    a = answer or {}
    if tool in ("get_live", "get_today_so_far", "get_value", "get_capacity"):
        return a.get("value", a.get("capacity_mw"))
    if tool == "compare_periods":
        return [(a.get("period1") or {}).get("value"), (a.get("period2") or {}).get("value")]
    if tool == "get_trend":
        return [row.get("value") for row in (table or [])]
    if tool == "rank" or (tool == "get_coal" and a.get("order")):
        return [row.get("name") for row in (table or [])]
    if tool == "get_coal":
        return a.get("value")
    if tool == "get_outages":
        return [a.get("units_affected"), a.get("new_outages_started"), a.get("generation_loss_mu")]
    if tool == "get_trips":
        return a.get("trips")
    return None


def close(a, b, live_tol: bool) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(close(x, y, live_tol) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        tol = max(abs(b) * (0.05 if live_tol else 0.001), 0.011)
        return abs(a - b) <= tol
    return a == b


def value_in_text(value, text: str) -> bool:
    if not isinstance(value, (int, float)):
        return True
    tol = max(abs(value) * 0.005, 0.011)
    return any(abs(v - value) <= tol for v in (_to_float(t) for t in _numbers(text)) if v is not None)


# ------------------------------------------------------------------ evaluate one question

def evaluate(example: dict, session: str) -> dict:
    exp_tool, exp_args = example["tool"], _fill_dates(example["args"])
    exp_tool_routed, exp_args_routed, _, exp_source = route_time(exp_tool, dict(exp_args))
    start = time.perf_counter()
    r = ask(example["question"], session)
    seconds = round(time.perf_counter() - start, 1)
    dbg = r.get("debug") or {}
    act_tool, act_args = r.get("tool"), dbg.get("args") or {}
    checks, reasons = {}, []

    # clarification expected?
    if example.get("expect_clarification"):
        checks["clarification"] = bool(r.get("clarification"))
        if not checks["clarification"]:
            reasons.append("expected a clarifying question")
        return _record(example, r, seconds, checks, reasons, exp_tool_routed, exp_args_routed, act_tool, act_args)

    # tool
    checks["tool"] = act_tool == exp_tool_routed
    if not checks["tool"]:
        reasons.append(f"tool {act_tool} != {exp_tool_routed}")

    # args (only when the tool is right; data tools only)
    if checks["tool"] and exp_tool_routed not in NO_DATA_TOOLS:
        bad = []
        exp_res = expected_entity(exp_tool_routed, exp_args_routed)
        if exp_res.entity is not None:
            from dataclasses import asdict
            if dbg.get("entity") != asdict(exp_res.entity) and not (exp_args_routed.get("metric") == "frequency"):
                bad.append(f"entity {dbg.get('entity')} != {asdict(exp_res.entity)}")
        if exp_tool_routed not in ("get_today_so_far", "get_outages", "get_trips", "get_capacity"):
            exp_metric = exp_args_routed.get("metric") or ("gross_generation" if exp_tool_routed == "get_live" else None)
            act_metric = act_args.get("metric") or ("gross_generation" if act_tool == "get_live" else None)
            if exp_metric != act_metric:
                bad.append(f"metric {act_metric} != {exp_metric}")
        keys = ["period1", "period2"] if exp_tool_routed == "compare_periods" else \
            ([] if exp_tool_routed in ("get_live", "get_today_so_far", "get_capacity") else ["period"])
        for key in keys:
            e, a = effective_period(exp_tool_routed, exp_args_routed, key), effective_period(act_tool, act_args, key)
            if e != a:
                bad.append(f"{key} {a} != {e}")
        if exp_tool_routed == "get_capacity" and (exp_args_routed.get("as_of") or None) != (act_args.get("as_of") or None):
            bad.append(f"as_of {act_args.get('as_of')} != {exp_args_routed.get('as_of')}")
        for key in ("top_n", "order", "level", "granularity", "trip_type"):
            if key in exp_args_routed and exp_args_routed[key] != act_args.get(key, DEFAULTS[key]):
                bad.append(f"{key} {act_args.get(key)} != {exp_args_routed[key]}")
        checks["args"] = not bad
        reasons += bad

    # source
    act_source = r.get("source")
    checks["source"] = act_source == exp_source or (exp_source == "none" and act_source in ("none", None))
    if not checks["source"]:
        reasons.append(f"source {act_source} != {exp_source}")

    # value: run the expected call directly, right now (fresh live read)
    if checks.get("tool") and checks.get("args") and exp_tool_routed not in NO_DATA_TOOLS:
        exp_res = expected_entity(exp_tool_routed, exp_args_routed)
        live._cache.clear()
        direct = run_tool(exp_tool_routed, exp_args_routed, exp_res.entity)
        want = main_values(exp_tool_routed, direct.answer, direct.table)
        got = main_values(act_tool, dbg.get("result_answer"), dbg.get("result_table"))
        checks["value"] = close(got, want, live_tol=exp_source in ("live", "mixed"))
        if not checks["value"]:
            reasons.append(f"value {got} != direct {want}")
        shown = [got] if isinstance(got, (int, float)) else \
            ([v for v in got if isinstance(v, (int, float))] if exp_tool_routed == "compare_periods" and isinstance(got, list) else [])
        if shown:   # single values, and BOTH values of a comparison, must be in the text
            missing = [v for v in shown if not value_in_text(v, r["text"])]
            checks["value_in_text"] = not missing
            if missing:
                reasons.append(f"value(s) {missing} not in answer text")

    # required phrases
    if example.get("expect_text"):
        checks["text"] = any(p.lower() in r["text"].lower() for p in example["expect_text"])
        if not checks["text"]:
            reasons.append(f"text lacks any of {example['expect_text']}")
    return _record(example, r, seconds, checks, reasons, exp_tool_routed, exp_args_routed, act_tool, act_args)


def _record(example, r, seconds, checks, reasons, exp_tool, exp_args, act_tool, act_args) -> dict:
    dbg = r.get("debug") or {}
    return {"id": example.get("id"), "question": example["question"], "passed": all(checks.values()), "checks": checks,
            "reasons": reasons, "expected": {"tool": exp_tool, "args": exp_args, "source": example.get("source")},
            "actual": {"tool": act_tool, "args": act_args, "planned": dbg.get("planned"), "source": r.get("source"),
                       "entity": dbg.get("entity")},
            "answer": r.get("text"), "composer": dbg.get("composer"), "timings": r.get("timings"), "seconds": seconds}


# ------------------------------------------------------------------ report

def write_report(results: list[dict], path_md: Path, started: datetime, mode: str) -> None:
    n = len(results)
    passed = sum(r["passed"] for r in results)
    lines = [f"# Evaluation {started:%Y-%m-%d %H:%M} ({mode})", "",
             f"**Overall: {passed}/{n} questions passed ({passed / n * 100:.0f}%)**" if n else "No results.", ""]
    all_checks = sorted({c for r in results for c in r["checks"]})
    lines += ["| check | passed | total | % |", "|---|---|---|---|"]
    for c in all_checks:
        vals = [r["checks"][c] for r in results if c in r["checks"]]
        lines.append(f"| {c} | {sum(vals)} | {len(vals)} | {sum(vals) / len(vals) * 100:.0f}% |")
    cats = {}
    for r in results:
        rid = r["id"] or "?"
        cats.setdefault("CV" if rid.startswith("CV") else rid[0], []).append(r["passed"])
    names = {"L": "live", "M": "mixed today vs past", "D": "daily values", "C": "comparisons", "T": "trends", "R": "rankings",
             "O": "outages/trips", "F": "coal", "K": "capacity", "X": "out of range / n.a.", "G": "general/clarify", "S": "text-to-SQL",
             "CV": "conversations (follow-ups)"}
    lines += ["", "| category | passed | total |", "|---|---|---|"]
    for k, v in sorted(cats.items()):
        lines.append(f"| {names.get(k, k)} | {sum(v)} | {len(v)} |")
    secs = [r["seconds"] for r in results]
    if secs:
        lines += ["", f"Latency: median {statistics.median(secs):.0f} s, max {max(secs):.0f} s, "
                      f"90th percentile {sorted(secs)[int(0.9 * (len(secs) - 1))]:.0f} s."]
    fallbacks = sum(1 for r in results if (r.get("composer") or "").startswith("template (number"))
    lines.append(f"Composer fell back to the template (number check failed twice): {fallbacks} times.")
    failures = [r for r in results if not r["passed"]]
    lines += ["", f"## Failures ({len(failures)})", "", "| id | question | reasons | answer |", "|---|---|---|---|"]
    for r in failures:
        answer = (r["answer"] or "").replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| {r['id']} | {r['question']} | {'; '.join(r['reasons'])[:300]} | {answer} |")
    path_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--ids", default="")
    ap.add_argument("--template", action="store_true", help="answer text from templates (no composer LLM call)")
    args = ap.parse_args()
    if args.template:
        object.__setattr__(app.config.settings, "answer_mode", "template")

    doc = yaml.safe_load(open(ROOT / "examples" / "examples.yaml", encoding="utf-8"))
    singles = doc["examples"]
    convs = doc.get("conversations", [])
    if args.ids:
        wanted = set(args.ids.split(","))
        singles = [e for e in singles if e["id"] in wanted]
        convs = [c for c in convs if c["id"] in wanted]
    elif args.quick:
        singles = [e for e in singles if e["id"] in QUICK_IDS]
        convs = [c for c in convs if c["id"] in QUICK_CONVERSATIONS]

    started = datetime.now()
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d_%H%M")
    path_json, path_md = OUT / f"eval_{stamp}.json", OUT / f"eval_{stamp}.md"
    mode = ("quick" if args.quick else "ids" if args.ids else "full") + (", template answers" if args.template else "")
    total = len(singles) + sum(len(c["turns"]) for c in convs)
    print(f"Evaluating {total} questions ({mode}) - results in {path_md}", flush=True)

    results, store = [], get_store()
    items = [(e, None) for e in singles] + [({**t, "id": f"{c['id']}-{i}"}, c["id"]) for c in convs
                                            for i, t in enumerate(c["turns"], 1)]
    sessions: dict[str, str] = {}
    for n, (example, conv) in enumerate(items, 1):
        session = sessions.setdefault(conv, store.new_session("eval")) if conv else store.new_session("eval")
        try:
            res = evaluate(example, session)
        except Exception as exc:   # an eval crash on one question must not stop the run
            res = {"id": example.get("id"), "question": example["question"], "passed": False, "checks": {"crash": False},
                   "reasons": [f"{type(exc).__name__}: {exc}"], "answer": None, "seconds": 0, "composer": None}
        results.append(res)
        print(f"{n:>3}/{total} {'PASS' if res['passed'] else 'FAIL'} {res['seconds']:5.0f}s {example['id']}: "
              f"{example['question'][:60]}{'  -> ' + '; '.join(res['reasons'])[:150] if not res['passed'] else ''}", flush=True)
        path_json.write_text(json.dumps(results, default=str, indent=1), encoding="utf-8")
        write_report(results, path_md, started, mode)
    passed = sum(r["passed"] for r in results)
    print(f"\nDone: {passed}/{len(results)} passed. Report: {path_md}", flush=True)


if __name__ == "__main__":
    main()
