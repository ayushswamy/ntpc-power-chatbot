"""
The chatbot's clock. All code asks this module for "now" / "today" instead of
calling datetime.now() directly, so tests can fix the time (e.g. replay a
recording made on 2026-09-28 at 11:00).

The DB server clock and this PC's clock were checked in Phase 0 (same time, IST).
"""
from datetime import date, datetime

_fixed_now: datetime | None = None


def now() -> datetime:
    return _fixed_now or datetime.now()


def today() -> date:
    return now().date()


def set_fixed_now(value: datetime | None) -> None:
    """Tests only: freeze the clock (None = use the real clock again)."""
    global _fixed_now
    _fixed_now = value
