from datetime import datetime
from zoneinfo import ZoneInfo

from app.api import clamp_employee_exit_to_schedule, worked_minutes_between

TZ = ZoneInfo("Indian/Antananarivo")


def test_full_day_excludes_lunch():
    assert worked_minutes_between(
        datetime(2026, 10, 19, 8, 0, tzinfo=TZ),
        datetime(2026, 10, 19, 17, 0, tzinfo=TZ),
    ) == 480


def test_partial_lunch_overlap_is_excluded():
    assert worked_minutes_between(
        datetime(2026, 10, 19, 11, 30, tzinfo=TZ),
        datetime(2026, 10, 19, 13, 30, tzinfo=TZ),
    ) == 60


def test_no_lunch_deduction_after_13():
    assert worked_minutes_between(
        datetime(2026, 10, 19, 13, 0, tzinfo=TZ),
        datetime(2026, 10, 19, 17, 0, tzinfo=TZ),
    ) == 240


def test_weekend_does_not_apply_weekday_lunch_break():
    assert worked_minutes_between(
        datetime(2026, 10, 24, 11, 30, tzinfo=TZ),
        datetime(2026, 10, 24, 13, 30, tzinfo=TZ),
    ) == 120


def test_clock_out_next_day_is_clamped_to_previous_workday_17():
    start = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
    next_day = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    expected = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    assert clamp_employee_exit_to_schedule(start, next_day) == expected
