"""
Shared building blocks for all tools.

  ToolResult  - what every tool returns (numbers, table, notes, sources, as_of...)
  Entity      - WHAT the question is about: one plant / stage / unit, or a group
  Period      - WHEN: a date range (start..end, both inclusive)
  METRICS     - metric definitions loaded from semantic/metrics.yaml
  data range  - first/last date available in the daily performance tables
"""
import calendar
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import yaml

from app import clock
from app.config import PROJECT_ROOT
from app.db.cache import TTLCache
from app.db.masters import get_masters
from app.db.runner import get_runner

PERFORMANCE_FIRST_DATE = date(2023, 4, 1)   # Performance_DayCum_* start (checked in Phase 1)


class ToolError(Exception):
    """A problem the user should be told about in plain words (bad input, no data...)."""


# ============================================================ result

@dataclass
class ToolResult:
    tool: str
    ok: bool = True
    answer: dict = field(default_factory=dict)    # the key numbers: value, unit, entity, period ...
    table: list[dict] | None = None               # optional rows for a table / chart
    as_of: str | None = None                      # data time for live values ("2026-09-28 11:04")
    is_live: bool = False
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # caveats the answer MUST mention
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def fail(tool: str, message: str) -> ToolResult:
    return ToolResult(tool=tool, ok=False, error=message)


# ============================================================ entity

@dataclass
class Entity:
    """
    level: 'plant' | 'stage' | 'unit' | 'group'
    plant_code: '1004'   stage: 'STAGE1'   unit: 'UNIT05'
    scope (group only): see Masters.plants_in_scope, {} = NTPC total
    """
    level: str
    plant_code: str | None = None
    stage: str | None = None
    unit: str | None = None
    scope: dict = field(default_factory=dict)

    @staticmethod
    def parse(value) -> "Entity":
        """Accept an Entity, a dict from the LLM, or a plant code string."""
        if isinstance(value, Entity):
            e = value
        elif isinstance(value, str):
            e = Entity(level="plant", plant_code=value)
        elif isinstance(value, dict):
            e = Entity(level=value.get("level") or ("plant" if value.get("plant_code") else "group"),
                       plant_code=value.get("plant_code"), stage=value.get("stage"), unit=value.get("unit"),
                       scope=value.get("scope") or {})
        else:
            raise ToolError("Entity is missing.")
        return e.normalised()

    def normalised(self) -> "Entity":
        m = get_masters()
        if self.level not in ("plant", "stage", "unit", "group"):
            raise ToolError(f"Unknown entity level '{self.level}'.")
        if self.level == "group":
            return self
        code = m.resolve_code(self.plant_code) if self.plant_code else None
        if not code:
            raise ToolError(f"Unknown plant code '{self.plant_code}'.")
        self.plant_code = code
        if self.unit:
            number = re.sub(r"\D", "", str(self.unit))
            if not number:
                raise ToolError(f"Unknown unit '{self.unit}'.")
            self.unit = f"UNIT{int(number):02d}"
            self.stage = m.stage_of_unit(code, self.unit)
            if not self.stage:
                raise ToolError(f"{m.plant_name(code)} has no {self.unit}.")
            self.level = "unit"
        elif self.stage:
            number = re.sub(r"\D", "", str(self.stage))
            self.stage = f"STAGE{int(number)}" if number else self.stage.upper()
            self.level = "stage"
        return self

    def label(self) -> str:
        m = get_masters()
        if self.level == "group":
            return m.scope_label(self.scope)
        if self.level == "unit":
            return m.unit_label(self.plant_code, self.unit)
        if self.level == "stage":
            return m.stage_name(self.plant_code, self.stage)
        return m.plant_name(self.plant_code)

    def plant_codes(self) -> list[str]:
        return get_masters().plants_in_scope(self.scope) if self.level == "group" else [self.plant_code]


# ============================================================ period

@dataclass
class Period:
    start: date
    end: date
    label: str = ""

    def days(self) -> int:
        return (self.end - self.start).days + 1

    def text(self) -> str:
        if self.label:
            return self.label
        if self.start == self.end:
            return self.start.strftime("%d %b %Y")
        return f"{self.start:%d %b %Y} to {self.end:%d %b %Y}"


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def parse_period(value) -> Period:
    """
    Accepts a Period, a dict {"start": "2026-09-01", "end": "2026-09-27"}, or a phrase:
    today, yesterday, this month, last month, this fy, last fy, last N days,
    YYYY-MM-DD, YYYY-MM (a month), 'fy 2025-26'.
    """
    if isinstance(value, Period):
        return value
    today = clock.today()
    yesterday = today - timedelta(days=1)
    if isinstance(value, dict):
        start = date.fromisoformat(str(value["start"]))
        end = date.fromisoformat(str(value.get("end") or value["start"]))
        if end < start:
            raise ToolError("The period end is before its start.")
        return Period(start, end, value.get("label", ""))
    text = str(value).strip().lower()
    if text == "today":
        return Period(today, today, "today")
    if text == "yesterday":
        return Period(yesterday, yesterday, f"yesterday ({yesterday:%d %b %Y})")
    if text in ("this month", "month to date", "mtd"):
        return Period(today.replace(day=1), today, f"{today:%B %Y} (month to date)")
    if text == "last month":
        end = today.replace(day=1) - timedelta(days=1)
        return Period(end.replace(day=1), end, f"{end:%B %Y}")
    if text in ("this fy", "this year", "fy to date", "ytd", "current fy"):
        s = fy_start(today)
        return Period(s, today, f"FY {s.year}-{str(s.year + 1)[2:]} (to date)")
    if text in ("last fy", "last year", "previous fy"):
        s = date(fy_start(today).year - 1, 4, 1)
        return Period(s, date(s.year + 1, 3, 31), f"FY {s.year}-{str(s.year + 1)[2:]}")
    if text in ("this week", "week to date"):
        monday = today - timedelta(days=today.weekday())
        return Period(monday, today, "this week")
    if text in ("last week", "past week"):
        text = "last 7 days"
    m = re.fullmatch(r"(?:last|past) (\d+) (day|days|week|weeks|month|months)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("day"):
            return Period(yesterday - timedelta(days=n - 1), yesterday, f"last {n} days")
        if unit.startswith("week"):
            return Period(yesterday - timedelta(days=7 * n - 1), yesterday, f"last {n} weeks")
        first = today.replace(day=1)
        for _ in range(n):
            first = (first - timedelta(days=1)).replace(day=1)
        end = today.replace(day=1) - timedelta(days=1)
        return Period(first, end, f"last {n} full months ({first:%b %Y} to {end:%b %Y})")
    m = re.fullmatch(r"fy ?(\d{4})-(\d{2,4})", text)
    if m:
        s = date(int(m.group(1)), 4, 1)
        return Period(s, date(s.year + 1, 3, 31), f"FY {s.year}-{str(s.year + 1)[2:]}")
    m = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return Period(date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1]), f"{date(y, mo, 1):%B %Y}")
    try:
        d = date.fromisoformat(text)
        return Period(d, d)
    except ValueError:
        raise ToolError(f"Could not understand the period '{value}'.") from None


def parse_date(value) -> date:
    return value if isinstance(value, date) else parse_period(value).end


# ============================================================ metrics

def _load_metrics() -> dict:
    doc = yaml.safe_load(open(PROJECT_ROOT / "semantic" / "metrics.yaml", encoding="utf-8"))
    return doc["metrics"]


METRICS = _load_metrics()


def get_metric(key: str) -> dict:
    metric = METRICS.get(key)
    if metric is None:
        known = ", ".join(sorted(METRICS))
        raise ToolError(f"Unknown metric '{key}'. Known metrics: {known}.")
    if metric.get("enabled") is False:
        raise ToolError(metric.get("disabled_reason", f"{metric['name']} is not available yet."))
    return metric


# ============================================================ data availability

_status_cache = TTLCache(600)


def latest_daily_date() -> date:
    """Last date whose daily performance run is Completed (usually yesterday)."""
    def load():
        rows = get_runner().run(
            "SELECT TOP 1 ReportDate FROM dbo.Performance_DayCum_Status WHERE Status = 'Completed' ORDER BY ReportDate DESC",
            purpose="latest completed daily date").rows
        return rows[0]["ReportDate"] if rows else clock.today() - timedelta(days=1)
    value = _status_cache.get_or_load("latest", load)
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def clamp_to_daily_range(period: Period, notes: list[str]) -> Period | None:
    """Limit a period to the dates the daily tables have; explain any change in `notes`."""
    first, last = PERFORMANCE_FIRST_DATE, latest_daily_date()
    if period.end < first or period.start > last:
        notes.append(f"Daily performance data is available from {first:%d %b %Y} to {last:%d %b %Y}; "
                     f"there is no data for {period.text()}.")
        return None
    start, end = max(period.start, first), min(period.end, last)
    if start == period.start and end != period.end and period.end >= clock.today() - timedelta(days=1):
        # the usual case: "this month" / "this fy" end today, daily figures end yesterday
        notes.append(f"Daily figures are up to {last:%d %b %Y}.")
        return Period(start, end)
    if (start, end) != (period.start, period.end):
        notes.append(f"Daily data is available from {first:%d %b %Y} to {last:%d %b %Y}; "
                     f"answered for {start:%d %b %Y} to {end:%d %b %Y}.")
        return Period(start, end)
    return period


def round_value(value, unit: str):
    if value is None:
        return None
    if unit in ("MW", "tonnes", "kcal/kWh", "nos", "days"):
        return round(value)
    if unit == "Hz":
        return round(value, 2)
    if unit == "kg/kWh":
        return round(value, 3)
    return round(value, 2)
