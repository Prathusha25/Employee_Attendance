"""Unit tests for the time and business-rule helpers (R1-R5, R7, R8). No database needed."""
from datetime import date, datetime, timezone

import pytest

from app import main as m
from tests.helpers import ist, ist_dt

DAY = "2026-07-06"  # a Monday


# ---------------------------------------------------------------- epoch ms conversions
def test_ms_to_dt_is_aware_utc_and_truncates_to_seconds():
    dt = m.ms_to_dt(ist(2026, 7, 6, 9, 40, 0, ms=900))
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0
    assert dt == ist_dt(2026, 7, 6, 9, 40, 0)  # .900 dropped, never rounded up


def test_dt_to_ms_round_trip_and_naive_is_utc():
    ms = ist(2026, 7, 6, 3, 58)
    assert m.dt_to_ms(m.ms_to_dt(ms)) == ms
    naive = datetime(2026, 7, 6, 3, 58)  # what PyMongo returns without tz_aware: UTC wall clock
    assert m.dt_to_ms(naive) == int(datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc).timestamp() * 1000)
    assert m.dt_to_ms(None) is None


def test_ist_date_crosses_midnight_correctly():
    # 2026-07-06 20:00 UTC is 2026-07-07 01:30 IST
    assert m.ist_date(datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)) == date(2026, 7, 7)


# ---------------------------------------------------------------- R1 attendance date
@pytest.mark.parametrize("wall, expected", [
    ((2026, 7, 6, 9, 28), "2026-07-06"),       # day shift
    ((2026, 7, 6, 0, 30), "2026-07-06"),       # day shift, after IST midnight but UTC is still the 5th
])
def test_attendance_date_day_shift(wall, expected):
    assert m.attendance_date(ist_dt(*wall), "09:30", "18:30") == expected


@pytest.mark.parametrize("wall, expected", [
    ((2026, 7, 6, 21, 55), "2026-07-06"),      # before shift start, same evening
    ((2026, 7, 7, 0, 0, 0), "2026-07-06"),     # exactly midnight -> previous day's shift
    ((2026, 7, 7, 5, 30), "2026-07-06"),       # 05:30 < 06:00 -> previous day's shift
    ((2026, 7, 7, 5, 59, 59), "2026-07-06"),
    ((2026, 7, 7, 6, 0, 0), "2026-07-07"),     # not *earlier* than shift_end -> own date
])
def test_attendance_date_overnight_shift(wall, expected):
    assert m.attendance_date(ist_dt(*wall), "22:00", "06:00") == expected


def test_shift_end_equal_to_start_is_overnight_rule():
    assert m.is_overnight("22:00", "06:00") and m.is_overnight("09:00", "09:00") is True
    assert not m.is_overnight("09:30", "18:30")


# ---------------------------------------------------------------- R2 late
@pytest.mark.parametrize("hms, expected", [
    ((9, 28, 0), 0),
    ((9, 30, 0), 0),
    ((9, 40, 0), 0),     # exactly 10:00 after start: not late
    ((9, 40, 1), 10),    # strictly more than 10:00: late, minutes counted from shift start
    ((9, 40, 59), 10),
    ((10, 15, 59), 45),
    ((10, 5, 0), 35),
])
def test_late_minutes_grace_boundary(hms, expected):
    assert m.compute_late_minutes(ist_dt(2026, 7, 6, *hms), "09:30", DAY) == expected


def test_late_minutes_uses_truncated_seconds():
    pin = m.ms_to_dt(ist(2026, 7, 6, 9, 40, 0, ms=900))  # 09:40:00.900 counts as 09:40:00
    assert m.compute_late_minutes(pin, "09:30", DAY) == 0


def test_late_minutes_overnight_after_midnight():
    # shift 22:00-06:00 that started on the 6th; punch-in 00:30 on the 7th is 150 min after start
    pin = ist_dt(2026, 7, 7, 0, 30)
    d = m.attendance_date(pin, "22:00", "06:00")
    assert d == "2026-07-06"
    assert m.compute_late_minutes(pin, "22:00", d) == 150


# ---------------------------------------------------------------- R3 overtime
@pytest.mark.parametrize("hms, expected", [
    ((18, 35, 0), 0),     # 5 min < 30
    ((18, 59, 59), 0),    # 29:59 -> floor 29 -> 0
    ((19, 0, 0), 30),     # exactly 30 counts
    ((19, 10, 0), 40),
    ((17, 0, 0), 0),      # left early
])
def test_overtime(hms, expected):
    assert m.compute_overtime(ist_dt(2026, 7, 6, *hms), "09:30", "18:30", DAY) == expected


def test_overtime_overnight_shift_ends_next_day():
    # sample document 7: shift 22:00-06:00 dated 2026-07-06, out 06:40 on the 7th -> 40 min
    assert m.compute_overtime(ist_dt(2026, 7, 7, 6, 40), "22:00", "06:00", "2026-07-06") == 40


# ---------------------------------------------------------------- R4 / R5 work hours, half day
@pytest.mark.parametrize("seconds, expected", [
    (450, 0.13),      # 0.125 exactly: half-up gives 0.13 (Python round() gives 0.12)
    (1026, 0.29),     # 0.285: float round() gives 0.28
    (16182, 4.50),    # 4.495 -> 4.50
    (32760, 9.10),
    (12600, 3.50),
    (31500, 8.75),
])
def test_work_hours_half_up(seconds, expected):
    pin = ist_dt(2026, 7, 6, 9, 0)
    pout = datetime.fromtimestamp(pin.timestamp() + seconds, tz=timezone.utc)
    assert m.compute_work_hours(pin, pout) == expected


def test_half_day_uses_rounded_hours():
    assert m.compute_half_day(3.5) is True
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.5) is False  # 4.495 h rounds to 4.50 -> not a half day
    assert m.compute_half_day(None) is False


def test_samples_from_data_model():
    # sample 1: in 09:28, out 18:35 -> 9.12 h, no late, overtime 5 -> 0
    d = m.derived_fields("PRESENT", ist_dt(2026, 7, 6, 9, 28), ist_dt(2026, 7, 6, 18, 35),
                         {"shift_start": "09:30", "shift_end": "18:30"}, DAY)
    assert d == {"work_hours": 9.12, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    # sample 2: in 10:05, out 19:10 -> 9.08 h, late 35, overtime 40
    d = m.derived_fields("PRESENT", ist_dt(2026, 7, 6, 10, 5), ist_dt(2026, 7, 6, 19, 10),
                         {"shift_start": "09:30", "shift_end": "18:30"}, DAY)
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    # sample 7: overnight 21:55 -> 06:40 next day: 8.75 h, overtime 40
    d = m.derived_fields("PRESENT", ist_dt(2026, 7, 6, 21, 55), ist_dt(2026, 7, 7, 6, 40),
                         {"shift_start": "22:00", "shift_end": "06:00"}, DAY)
    assert d == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}
    # absent: everything reset
    d = m.derived_fields("ABSENT", None, None, {"shift_start": "09:30", "shift_end": "18:30"}, DAY)
    assert d == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---------------------------------------------------------------- R7 / R8
def test_working_days():
    first, last = m.month_bounds("2026-07")
    assert (first, last) == (date(2026, 7, 1), date(2026, 7, 31))
    assert m.working_days(first, last, "2026-01-01") == 23
    assert m.working_days(first, last, "2026-07-15") == 13   # mid-month joiner
    assert m.working_days(first, last, "2026-07-31") == 1
    assert m.working_days(first, last, "2026-08-05") == 0    # not joined yet
    assert m.month_bounds("2026-02")[1] == date(2026, 2, 28)
    assert m.month_bounds("2026-12")[1] == date(2026, 12, 31)


@pytest.mark.parametrize("value, places, expected", [
    (8.025, 2, 8.03),          # float 8.025 is 8.02499..., round() gives 8.02
    (2.675, 2, 2.68),
    (0.125, 2, 0.13),
    (19.565217, 2, 19.57),
    (0.00625, 4, 0.0063),
    (0.33333333, 4, 0.3333),
    (5, 2, 5.0),
    (None, 2, None),
])
def test_round_half_up(value, places, expected):
    assert m.round_half_up(value, places) == expected
