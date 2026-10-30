"""Unit tests for pure helpers (no database)."""
from datetime import date, datetime

import pytest

from app import clock
from app.db.cache import TTLCache
from app.db.masters import pretty_name
from app.tools.common import ToolError, fy_start, parse_period, round_value


@pytest.fixture(autouse=True)
def fixed_clock():
    clock.set_fixed_now(datetime(2026, 9, 28, 11, 30))
    yield
    clock.set_fixed_now(None)


def test_periods():
    assert parse_period("today").start == date(2026, 9, 28)
    y = parse_period("yesterday")
    assert (y.start, y.end) == (date(2026, 9, 27), date(2026, 9, 27))
    m = parse_period("this month")
    assert (m.start, m.end) == (date(2026, 9, 1), date(2026, 9, 28))
    lm = parse_period("last month")
    assert (lm.start, lm.end) == (date(2026, 8, 1), date(2026, 8, 31))
    fy = parse_period("this fy")
    assert fy.start == date(2026, 4, 1)
    last = parse_period("last fy")
    assert (last.start, last.end) == (date(2025, 4, 1), date(2026, 3, 31))
    d30 = parse_period("last 30 days")
    assert (d30.start, d30.end, d30.days()) == (date(2026, 8, 29), date(2026, 9, 27), 30)
    assert parse_period("fy 2024-25").end == date(2025, 3, 31)
    assert parse_period("this week").start == date(2026, 9, 28)          # a Monday
    assert parse_period("last week").days() == 7
    assert parse_period("last 2 weeks").days() == 14
    m3 = parse_period("last 3 months")
    assert (m3.start, m3.end) == (date(2026, 6, 1), date(2026, 8, 31))
    assert parse_period("2026-02").end == date(2026, 2, 28)
    assert parse_period({"start": "2026-09-01", "end": "2026-09-05"}).days() == 5
    with pytest.raises(ToolError):
        parse_period("sometime soon")
    with pytest.raises(ToolError):
        parse_period({"start": "2026-09-05", "end": "2026-09-01"})


def test_fy_start():
    assert fy_start(date(2026, 3, 31)) == date(2025, 4, 1)
    assert fy_start(date(2026, 4, 1)) == date(2026, 4, 1)


def test_pretty_name():
    assert pretty_name("SINGRAULI") == "Singrauli"
    assert pretty_name("DADRI-COAL") == "Dadri-Coal"
    assert pretty_name("NSTPS") == "NSTPS"
    assert pretty_name("AASPL") == "AASPL"
    assert pretty_name("ASEPL-1") == "ASEPL-1"
    assert pretty_name("Khavda-I") == "Khavda-I"


def test_availability_formula_matches_stored_value():
    """Decision 10: availability = (capacity - outage loss) / capacity x 100 (Singrauli, 24 Sep 2026)."""
    from app.tools.common import METRICS
    from app.tools.historical import _plan, _value
    m = METRICS["availability"]
    plan = _plan(m, "plant", use_formula=True)
    assert plan == {"t0": "gross_cap_mu", "t1": "outg_loss", "d": "gross_cap_mu"}
    day = {"sum_t0": 48.0, "sum_t1": 4.8, "sum_d": 48.0, "n_v": 1}
    month = {"sum_t0": 1152.0, "sum_t1": 260.46, "sum_d": 1152.0, "n_v": 1}
    assert round(_value(day, m, plan), 2) == 90.0         # stored avb DayCY 90.0
    assert round(_value(month, m, plan), 2) == 77.39      # stored avb MonthCY 77.39


def test_time_router_daily_only_tools_today():
    from app.agent.tool_specs import route_time
    tool, args, notes, source = route_time("get_coal", {"plant": "Singrauli", "metric": "coal_stock", "period": "today"})
    assert (tool, args["period"], source) == ("get_coal", "yesterday", "daily") and notes
    tool, args, notes, source = route_time("get_value", {"plant": "Singrauli", "metric": "gross_generation", "period": "today"})
    assert (tool, source) == ("get_today_so_far", "live")


def test_rounding():
    assert round_value(1093.4, "MW") == 1093
    assert round_value(77.2549, "%") == 77.25
    assert round_value(None, "MU") is None


def test_cache_reuses_value():
    cache, calls = TTLCache(60), []
    loader = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert cache.get_or_load("k", loader) == 1
    assert cache.get_or_load("k", loader) == 1
    assert len(calls) == 1
