"""Predefined, tested query tools. Each returns a ToolResult (app/tools/common.py)."""
from app.tools.capacity import get_capacity
from app.tools.events import get_outages, get_trips
from app.tools.historical import (compare_periods, get_coal, get_daily_metric, get_metric_range, get_trend,
                                  rank_entities)
from app.tools.live import get_realtime, get_realtime_summary, get_today_so_far

ALL_TOOLS = {f.__name__: f for f in (
    get_realtime, get_realtime_summary, get_today_so_far,
    get_daily_metric, get_metric_range, compare_periods, get_trend, rank_entities,
    get_outages, get_trips, get_coal, get_capacity,
)}
