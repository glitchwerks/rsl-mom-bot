"""Tests for the pure calendar predicates in reminders/calendar.py.

Covers:
- tank_week_ending_tuesday: first Tuesday of a given (year, month).
- heads-up date computation (ending Tuesday - 7 days).
- is_tank_week_headsup_date predicate.
- is_end_of_tank_date predicate.
- Cross-month and cross-year boundary cases.
- is_siege_48h_headsup_date / is_siege_24h_headsup_date predicates (#326):
  bi-weekly (14-day cadence) Siege heads-up dates anchored at
  2026-07-21 (Tuesday), fired on the Sunday (T-48h) and Monday (T-24h)
  preceding each Siege start.

No DB or Discord dependency — all tests are pure date arithmetic.
"""

from __future__ import annotations

import datetime

import pytest

from mom_bot.reminders.calendar import (
    SIEGE_ANCHOR_DATE,
    is_end_of_tank_date,
    is_siege_24h_headsup_date,
    is_siege_48h_headsup_date,
    is_tank_week_headsup_date,
    tank_week_ending_tuesday,
)

# ---------------------------------------------------------------------------
# tank_week_ending_tuesday — first Tuesday of the month
# ---------------------------------------------------------------------------


def test_first_tuesday_when_first_is_tuesday() -> None:
    """When the 1st of the month is a Tuesday, it IS the tank-week end."""
    # 2019-01-01 is a Tuesday — verify the assumption then assert.
    assert datetime.date(2019, 1, 1).weekday() == 1, "fixture: 2019-01-01 must be a Tuesday"
    result = tank_week_ending_tuesday(2019, 1)
    assert result == datetime.date(2019, 1, 1)


def test_first_tuesday_when_first_is_not_tuesday() -> None:
    """When the 1st is not a Tuesday, result is the first Tuesday-dated day."""
    # 2026-05-01 is a Friday (weekday=4); first Tuesday is 2026-05-05.
    assert datetime.date(2026, 5, 1).weekday() == 4, "fixture: 2026-05-01 must be a Friday"
    result = tank_week_ending_tuesday(2026, 5)
    assert result == datetime.date(2026, 5, 5)
    assert result.weekday() == 1


def test_first_tuesday_when_first_is_wednesday() -> None:
    """First is Wednesday → first Tuesday is the 7th of the month."""
    # 2026-07-01 is a Wednesday (weekday=2); first Tuesday is 2026-07-07.
    assert datetime.date(2026, 7, 1).weekday() == 2, "fixture: 2026-07-01 must be a Wednesday"
    result = tank_week_ending_tuesday(2026, 7)
    assert result == datetime.date(2026, 7, 7)
    assert result.weekday() == 1


def test_first_tuesday_is_always_in_days_1_through_7() -> None:
    """The first Tuesday of any month always falls on day 1–7."""
    for year in range(2024, 2028):
        for month in range(1, 13):
            result = tank_week_ending_tuesday(year, month)
            assert result.weekday() == 1, f"Not a Tuesday: {result}"
            assert 1 <= result.day <= 7, f"Day out of range 1-7: {result}"
            assert result.year == year
            assert result.month == month


# ---------------------------------------------------------------------------
# Heads-up date = ending Tuesday − 7 days
# ---------------------------------------------------------------------------


def test_headsup_date_is_seven_days_before_ending_tuesday() -> None:
    """Heads-up date for May 2026 is 2026-05-05 − 7 = 2026-04-28 (Tuesday)."""
    ending = tank_week_ending_tuesday(2026, 5)
    assert ending == datetime.date(2026, 5, 5)
    headsup = ending - datetime.timedelta(days=7)
    assert headsup == datetime.date(2026, 4, 28)
    # Heads-up is the prior clash's ending Tuesday — must be a Tuesday.
    assert headsup.weekday() == 1


def test_headsup_date_crosses_month_boundary() -> None:
    """When ending Tuesday is the 1st–7th, heads-up date is in prior month."""
    # Jan 2019: ending Tuesday = Jan 1 → heads-up = Dec 25, 2018 (Tuesday).
    ending = tank_week_ending_tuesday(2019, 1)
    assert ending == datetime.date(2019, 1, 1)
    headsup = ending - datetime.timedelta(days=7)
    assert headsup == datetime.date(2018, 12, 25)
    assert headsup.month == 12
    assert headsup.year == 2018


# ---------------------------------------------------------------------------
# Cross-year boundary (required per spec §4)
# ---------------------------------------------------------------------------


def test_headsup_crosses_year_boundary_when_january_first_tuesday_is_jan1() -> None:
    """Year where Jan 1 is Tuesday → heads-up date is Dec 25 of prior year.

    Jan 2019: Jan 1 is a Tuesday → heads-up = Dec 25, 2018 (Tuesday).
    This is the cross-year boundary the spec requires explicit coverage for.
    Heads-up = ending − 7 days (the prior clash's ending Tuesday).
    """
    assert datetime.date(2019, 1, 1).weekday() == 1, "fixture: 2019-01-01 must be Tuesday"
    ending = tank_week_ending_tuesday(2019, 1)
    assert ending == datetime.date(2019, 1, 1)
    headsup = ending - datetime.timedelta(days=7)
    assert headsup.year == 2018, f"Expected year 2018, got {headsup.year}"
    assert headsup == datetime.date(2018, 12, 25)
    assert headsup.weekday() == 1, "Cross-year heads-up must be a Tuesday"


# ---------------------------------------------------------------------------
# is_end_of_tank_date predicate
# ---------------------------------------------------------------------------


def test_is_end_of_tank_date_true_on_first_tuesday() -> None:
    """is_end_of_tank_date returns True on the first Tuesday of the month."""
    # 2026-05-05 is the first Tuesday of May 2026.
    assert is_end_of_tank_date(datetime.date(2026, 5, 5)) is True


def test_is_end_of_tank_date_false_on_second_tuesday() -> None:
    """is_end_of_tank_date returns False on the second Tuesday of the month."""
    # 2026-05-12 is the second Tuesday of May 2026.
    assert datetime.date(2026, 5, 12).weekday() == 1
    assert is_end_of_tank_date(datetime.date(2026, 5, 12)) is False


def test_is_end_of_tank_date_false_on_non_tuesday() -> None:
    """is_end_of_tank_date returns False on a non-Tuesday."""
    # 2026-05-06 is a Wednesday.
    assert datetime.date(2026, 5, 6).weekday() == 2
    assert is_end_of_tank_date(datetime.date(2026, 5, 6)) is False


def test_is_end_of_tank_date_true_when_first_is_tuesday() -> None:
    """Returns True when the 1st itself is Tuesday (the tie-break case)."""
    assert is_end_of_tank_date(datetime.date(2019, 1, 1)) is True


# ---------------------------------------------------------------------------
# is_tank_week_headsup_date predicate
# ---------------------------------------------------------------------------


def test_is_headsup_date_true_on_correct_tuesday() -> None:
    """is_tank_week_headsup_date returns True on the heads-up Tuesday."""
    # Heads-up for May 2026 tank week = Apr 28, 2026 (May 5 − 7 days).
    assert is_tank_week_headsup_date(datetime.date(2026, 4, 28)) is True


def test_is_headsup_date_false_on_ordinary_tuesday() -> None:
    """Returns False on a Tuesday that is NOT the heads-up date."""
    # 2026-05-05 is the tank-week END Tuesday for May, not the heads-up.
    assert is_tank_week_headsup_date(datetime.date(2026, 5, 5)) is False


def test_is_headsup_date_false_on_non_tuesday() -> None:
    """Returns False on a non-Tuesday regardless of date."""
    # 2026-04-29 is a Wednesday (the day after the heads-up Tuesday).
    assert datetime.date(2026, 4, 29).weekday() == 2
    assert is_tank_week_headsup_date(datetime.date(2026, 4, 29)) is False


def test_is_headsup_date_true_on_cross_year_boundary() -> None:
    """Returns True on Dec 25, 2018 (heads-up for Jan 2019 tank week)."""
    # Jan 2019 ending Tuesday = Jan 1; heads-up = Jan 1 − 7 = Dec 25, 2018.
    assert is_tank_week_headsup_date(datetime.date(2018, 12, 25)) is True


def test_is_headsup_date_false_on_non_headsup_december_tuesday() -> None:
    """Returns False on a December Tuesday that is NOT the heads-up date."""
    # Dec 18, 2018 is a Tuesday but not the heads-up for Jan 2019 (which is Dec 25).
    assert datetime.date(2018, 12, 18).weekday() == 1
    assert is_tank_week_headsup_date(datetime.date(2018, 12, 18)) is False


# ---------------------------------------------------------------------------
# Headsup and end-of-tank dates never coincide (spec edge-case note)
# ---------------------------------------------------------------------------


def test_headsup_and_end_of_tank_are_never_same_date() -> None:
    """The heads-up Tuesday and the end-of-tank Tuesday are always 7 days apart."""
    for year in range(2024, 2028):
        for month in range(1, 13):
            ending = tank_week_ending_tuesday(year, month)
            headsup = ending - datetime.timedelta(days=7)
            assert headsup != ending


@pytest.mark.parametrize(
    "date,expected_headsup,expected_end",
    [
        # Normal case: May 2026, ending May 5, headsup Apr 28 (May 5 − 7).
        (datetime.date(2026, 4, 28), True, False),
        (datetime.date(2026, 5, 5), False, True),
        # Cross-year: Jan 2019 ending Jan 1, headsup Dec 25 2018 (Jan 1 − 7).
        (datetime.date(2018, 12, 25), True, False),
        (datetime.date(2019, 1, 1), False, True),
        # Ordinary Tuesday — neither predicate fires.
        (datetime.date(2026, 5, 12), False, False),
    ],
)
def test_predicate_truth_table(
    date: datetime.date,
    expected_headsup: bool,
    expected_end: bool,
) -> None:
    """Both predicates agree on their respective trigger dates and are silent otherwise."""
    assert is_tank_week_headsup_date(date) is expected_headsup
    assert is_end_of_tank_date(date) is expected_end


# ---------------------------------------------------------------------------
# Siege bi-weekly heads-up predicates (#326)
#
# Anchor: Siege starts Tuesday 2026-07-21, 10:00 UTC, cadence every 14 days
# (confirmed on issue #325's comment thread — see plan
# docs/superpowers/plans/2026-07-25-325-siege-reminders.md §3.1/§7 D0).
# T-48h fires on the preceding Sunday, T-24h on the preceding Monday.
# offset = (d - SIEGE_ANCHOR_DATE).days % 14; T-48h ⟺ offset == 12,
# T-24h ⟺ offset == 13. Python's ``%`` is non-negative for a positive
# divisor, so pre-anchor dates resolve correctly with no special-casing.
# ---------------------------------------------------------------------------


def test_siege_anchor_is_a_tuesday() -> None:
    """Fixture guard: SIEGE_ANCHOR_DATE (2026-07-21) must be a Tuesday."""
    assert SIEGE_ANCHOR_DATE == datetime.date(2026, 7, 21)
    assert SIEGE_ANCHOR_DATE.weekday() == 1, "fixture: SIEGE_ANCHOR_DATE must be a Tuesday"


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 8, 2),
        datetime.date(2026, 8, 16),
        datetime.date(2026, 8, 30),
    ],
)
def test_siege_48h_headsup_true_on_cycle_sundays(date: datetime.date) -> None:
    """is_siege_48h_headsup_date is True on the three Sundays after the anchor."""
    assert date.weekday() == 6, "fixture: expected date must be a Sunday"
    assert is_siege_48h_headsup_date(date) is True


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 8, 9),
        datetime.date(2026, 8, 23),
    ],
)
def test_siege_48h_headsup_false_on_off_cycle_sundays(date: datetime.date) -> None:
    """is_siege_48h_headsup_date is False on Sundays one week off the 14-day cycle."""
    assert date.weekday() == 6, "fixture: expected date must be a Sunday"
    assert is_siege_48h_headsup_date(date) is False


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 8, 3),
        datetime.date(2026, 8, 17),
        datetime.date(2026, 8, 31),
    ],
)
def test_siege_24h_headsup_true_on_cycle_mondays(date: datetime.date) -> None:
    """is_siege_24h_headsup_date is True on the three Mondays after the anchor."""
    assert date.weekday() == 0, "fixture: expected date must be a Monday"
    assert is_siege_24h_headsup_date(date) is True


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 8, 10),
        datetime.date(2026, 8, 24),
    ],
)
def test_siege_24h_headsup_false_on_off_cycle_mondays(date: datetime.date) -> None:
    """is_siege_24h_headsup_date is False on Mondays one week off the 14-day cycle."""
    assert date.weekday() == 0, "fixture: expected date must be a Monday"
    assert is_siege_24h_headsup_date(date) is False


def test_siege_48h_headsup_true_on_pre_anchor_date() -> None:
    """Pins Python's non-negative modulo for dates before the anchor.

    2026-07-19 is the Sunday two days before the anchor itself
    (2026-07-21) — the very first T-48h heads-up date in the cycle,
    even though it precedes the anchor date. A future refactor that
    swaps ``%`` for ``math.fmod``/``abs()`` would break this.
    """
    date = datetime.date(2026, 7, 19)
    assert date.weekday() == 6, "fixture: expected date must be a Sunday"
    assert (date - SIEGE_ANCHOR_DATE).days == -2
    assert is_siege_48h_headsup_date(date) is True


def test_siege_24h_headsup_true_on_pre_anchor_date() -> None:
    """Pins Python's non-negative modulo for dates before the anchor.

    2026-07-20 is the Monday one day before the anchor (2026-07-21) —
    the very first T-24h heads-up date in the cycle.
    """
    date = datetime.date(2026, 7, 20)
    assert date.weekday() == 0, "fixture: expected date must be a Monday"
    assert (date - SIEGE_ANCHOR_DATE).days == -1
    assert is_siege_24h_headsup_date(date) is True


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 7, 21),  # the anchor itself (Tuesday)
        datetime.date(2026, 8, 1),  # Saturday
        datetime.date(2026, 8, 3),  # an on-cycle T-24h Monday
        datetime.date(2026, 8, 4),  # Tuesday
    ],
)
def test_siege_48h_headsup_false_on_non_sunday(date: datetime.date) -> None:
    """is_siege_48h_headsup_date is False for any non-Sunday date."""
    assert date.weekday() != 6
    assert is_siege_48h_headsup_date(date) is False


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 7, 21),  # the anchor itself (Tuesday)
        datetime.date(2026, 8, 1),  # Saturday
        datetime.date(2026, 8, 2),  # an on-cycle T-48h Sunday
        datetime.date(2026, 8, 4),  # Tuesday
    ],
)
def test_siege_24h_headsup_false_on_non_monday(date: datetime.date) -> None:
    """is_siege_24h_headsup_date is False for any non-Monday date."""
    assert date.weekday() != 0
    assert is_siege_24h_headsup_date(date) is False


def test_siege_headsup_weekday_implication_sweep() -> None:
    """§3.2 mitigation: over a multi-year sweep, True implies the exact weekday.

    Guards against the double-encoding hazard where the mod-14 predicate
    and the seeded row's ``weekday`` column silently disagree — a
    disagreement that produces no error and no fired reminder.
    """
    start = datetime.date(2025, 1, 1)
    end = datetime.date(2029, 12, 31)
    day = start
    one_day = datetime.timedelta(days=1)
    checked_48h = checked_24h = 0
    while day <= end:
        if is_siege_48h_headsup_date(day):
            assert day.weekday() == 6, f"T-48h fired on a non-Sunday: {day}"
            checked_48h += 1
        if is_siege_24h_headsup_date(day):
            assert day.weekday() == 0, f"T-24h fired on a non-Monday: {day}"
            checked_24h += 1
        day += one_day
    # Sanity: the sweep actually exercised both predicates many times.
    assert checked_48h > 100
    assert checked_24h > 100


def test_siege_headsup_density_sweep() -> None:
    """Exactly one T-48h and one T-24h date occur per 14-day window.

    Over a multi-year sweep, consecutive firing dates for each predicate
    are always exactly 14 days apart, and the two predicates never fire
    on the same calendar date.
    """
    start = datetime.date(2025, 1, 1)
    end = datetime.date(2029, 12, 31)
    day = start
    one_day = datetime.timedelta(days=1)
    headsup_48h: list[datetime.date] = []
    headsup_24h: list[datetime.date] = []
    while day <= end:
        if is_siege_48h_headsup_date(day):
            headsup_48h.append(day)
        if is_siege_24h_headsup_date(day):
            headsup_24h.append(day)
        day += one_day

    # strict=False: pairwise zip of a list with its own tail is
    # intentionally one element shorter on the right.
    for earlier, later in zip(headsup_48h, headsup_48h[1:], strict=False):
        assert (later - earlier).days == 14

    for earlier, later in zip(headsup_24h, headsup_24h[1:], strict=False):
        assert (later - earlier).days == 14

    assert set(headsup_48h).isdisjoint(headsup_24h)


def test_siege_headsup_does_not_interfere_with_tank_week_predicates() -> None:
    """Tank-week predicates never fire on the Siege weekdays.

    They are Tuesday-only, so they can never collide with Sunday=6 or
    Monday=0. §3.2 requires this be asserted, not assumed, so a future
    change to either predicate family is caught.
    """
    start = datetime.date(2025, 1, 1)
    end = datetime.date(2029, 12, 31)
    day = start
    one_day = datetime.timedelta(days=1)
    while day <= end:
        if day.weekday() in (6, 0):
            assert is_tank_week_headsup_date(day) is False
            assert is_end_of_tank_date(day) is False
        day += one_day
