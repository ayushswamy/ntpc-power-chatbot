"""
Agent graph tests WITHOUT the LLM and WITHOUT the database:
  - the planner is replaced by a fake that returns a fixed tool call
  - tools answer from the recorded fixture (ReplayRunner)
  - the composer uses the template (no LLM)
Checks routing, clarification, follow-up context, time routing and the number check.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import clock
from app.db.masters import reset_masters
from app.db.runner import ReplayRunner, set_runner
from app.memory.store import MemoryStore, set_store

FIXTURE = Path(__file__).parent / "fixtures" / "tools_fixture.json"
CASES = Path(__file__).parent / "fixtures" / "tool_cases.json"


def expected(case_no: int) -> dict:
    """The answer recorded for a case by scripts/test_tools_db.py (so these tests survive re-recording)."""
    import json
    return next(c["answer"] for c in json.loads(CASES.read_text(encoding="utf-8")) if c["case"] == case_no)


def fmt(value, unit):
    return f"{value:,} {unit}" if unit != "%" else f"{value}%"
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixtures not recorded yet")


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    from app.agent import composer, graph
    from app.tools import common, live
    runner = ReplayRunner(FIXTURE)
    set_runner(runner)
    clock.set_fixed_now(runner.now)
    reset_masters()
    live._cache.clear()
    common._status_cache.clear()
    set_store(MemoryStore(":memory:"))
    monkeypatch.setattr(composer, "settings", SimpleNamespace(answer_mode="template"))
    monkeypatch.setattr(graph, "audit", lambda record: None)
    graph._graph = None
    yield
    clock.set_fixed_now(None)
    set_runner(None)
    reset_masters()


def fake_planner(monkeypatch, plans):
    """plans: list of (tool, args) returned one after another."""
    from app.agent import graph
    queue = list(plans)
    monkeypatch.setattr(graph, "run_planner", lambda message, context, history: {"tool": queue[0][0], "args": queue.pop(0)[1],
                                                                                   "llm": {}, "examples": []})


def test_greeting_needs_no_llm(monkeypatch):
    from app.agent.graph import ask
    fake_planner(monkeypatch, [])                    # would fail if called
    r = ask("hello")
    assert "NTPC" in r["text"] and r["tool"] == "reply"


def test_live_answer(monkeypatch):
    from app.agent.graph import ask
    fake_planner(monkeypatch, [("get_live", {"plant": "Singrauli", "metric": "gross_generation"})])
    r = ask("current generation of singrauli")
    assert r["is_live"] and r["source"] == "live" and fmt(expected(1)["value"], "MW") in r["text"]
    assert r["as_of"]


def test_clarification_then_choice(monkeypatch):
    from app.agent.graph import ask
    # only the first question reaches the planner; the reply "the gas one" is matched without the LLM
    fake_planner(monkeypatch, [("get_live", {"plant": "Dadri", "metric": "gross_generation"})])
    r1 = ask("current generation of dadri")
    assert r1.get("clarification") and "Dadri" in r1["text"]
    r2 = ask("the gas one", r1["session_id"])
    assert r2["tool"] == "get_live" and "Dadri-Gas" in r2["text"]


def test_follow_up_uses_context(monkeypatch):
    from app.agent.graph import ask
    fake_planner(monkeypatch, [("get_value", {"plant": "Singrauli", "metric": "plf", "period": "yesterday"}),
                               ("get_value", {"metric": "gross_generation", "period": "this month"})])
    r1 = ask("PLF of Singrauli yesterday")
    assert fmt(expected(13)["value"], "%") in r1["text"]
    r2 = ask("and generation this month?", r1["session_id"])      # no plant given -> Singrauli from context
    assert "Singrauli" in r2["text"] and fmt(expected(16)["value"], "MU") in r2["text"]


def test_time_router_today_generation(monkeypatch):
    from app.agent.graph import ask
    fake_planner(monkeypatch, [("get_value", {"plant": "Singrauli", "metric": "gross_generation", "period": "today"})])
    r = ask("singrauli generation today")
    assert r["tool"] == "get_today_so_far" and r["source"] == "live" and fmt(expected(10)["value"], "MU") in r["text"]


def test_out_of_range(monkeypatch):
    from app.agent.graph import ask
    fake_planner(monkeypatch, [("get_value", {"plant": "Singrauli", "metric": "plf", "start": "2021-01-01", "end": "2021-03-31"})])
    r = ask("PLF of Singrauli in Q4 of FY 2020-21")
    assert "available from 01 Apr 2023" in r["text"]


def test_tool_call_written_as_text():
    from app.agent.planner import _tool_from_text
    assert _tool_from_text('{"name": "explain_term", "arguments": {"term": "PLF"}}') == ("explain_term", {"term": "PLF"})
    assert _tool_from_text('.reply\n{"text": "Hi"}\n</tool_call>') == ("reply", {"text": "Hi"})
    assert _tool_from_text("Hello there") is None


def test_sql_answer_unit_check():
    from app.agent.composer import _unit_not_in_columns
    assert _unit_not_in_columns("Loss was 2,647,521 MU", ["AreaName", "gen_loss_unit_unconfirmed"])
    assert not _unit_not_in_columns("Loss was 2,647 MU", ["AreaName", "loss_mu"])
    assert not _unit_not_in_columns("PLF was 88%", ["StationName", "plf"])
    assert not _unit_not_in_columns("18 stages", ["stages", "capacity_mw"])


def test_number_check():
    from app.agent.composer import allowed_numbers, unsupported_numbers
    allowed = allowed_numbers({"answer": {"value": 1046, "plf": 62.67}, "as_of": "2026-09-28 10:51"}, "question")
    assert unsupported_numbers("Singrauli is generating 1,046 MW as of 10:51, PLF 62.7%.", allowed) == []
    assert unsupported_numbers("Singrauli is generating 1,100 MW.", allowed) == ["1,100"]
    assert unsupported_numbers("That is 32.6 GW.", allowed) == ["32.6"]
