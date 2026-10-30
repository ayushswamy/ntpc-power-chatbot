"""
Replay test: runs every tool case recorded by scripts/test_tools_db.py against the
saved query results (tests/fixtures/tools_fixture.json) - NO database needed.
It fails if a tool's answer changes, i.e. it guards against regressions.

Re-record after changing tool SQL on purpose:  .\\.venv\\Scripts\\python.exe scripts\\test_tools_db.py
"""
import json
from pathlib import Path

import pytest

from app import clock
from app.db.masters import reset_masters
from app.db.runner import ReplayRunner, set_runner

FIXTURES = Path(__file__).parent / "fixtures"
CASES_FILE = FIXTURES / "tool_cases.json"
pytestmark = pytest.mark.skipif(not CASES_FILE.exists(), reason="fixtures not recorded yet")


@pytest.fixture(scope="module", autouse=True)
def replay():
    from app.tools import common, live
    runner = ReplayRunner(FIXTURES / "tools_fixture.json")
    set_runner(runner)
    clock.set_fixed_now(runner.now)
    reset_masters()
    live._cache.clear()
    common._status_cache.clear()
    yield
    clock.set_fixed_now(None)
    set_runner(None)
    reset_masters()


def _cases():
    return json.loads(CASES_FILE.read_text(encoding="utf-8")) if CASES_FILE.exists() else []


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['case']}-{c['tool']}")
def test_tool_answer_unchanged(case):
    from app.tools import ALL_TOOLS
    result = ALL_TOOLS[case["tool"]](*case["args"]).to_dict()
    assert result["ok"] == case["ok"], result.get("error")
    assert json.loads(json.dumps(result["answer"], default=str)) == case["answer"]
