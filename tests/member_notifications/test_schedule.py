"""Tests for pure occurrence-date math in mom_bot.member_notifications.schedule.

Covers is_occurrence_date() and clamped_anchor_day() with zero DB or
Discord dependencies.  These are the highest-value tests because the
occurrence predicate cannot be expressed cleanly in SQL (monthly clamp
needs calendar.monthrange), so the correctness of the Python layer is
the only gate.

Spec reference: #269 per-member notifications, § 2.3a and § 4.
"""

from __future__ import annotations

import datetime

import pytest
from mom_bot.member_notifications.schedule import (
    clamped_anchor_day,
    is_occurrence_date,
)

# ---------------------------------------------------------------------------
# clamped_anchor_day
# ---------------------------------------------------------------------------


class TestClampedAnchorDay:
    """Unit tests for clamped_anchor_day(anchor_day, year, month)."""

    # --- day <= 28 : clamp never fires ---

    def test_day_28_january(self) -> None:
        """Day 28 always exists; clamp leaves it unchanged."""
        assert clamped_anchor_day(28, 2027, 1) == 28

    def test_day_28_february_non_leap(self) -> None:
        """Day 28 in non-leap February is the last day; clamp returns 28."""
        assert clamped_anchor_day(28, 2027, 2) == 28

    def test_day_28_february_leap(self) -> None:
        """Day 28 in leap February exists; clamp returns 28 (not 29)."""
        assert clamped_anchor_day(28, 2028, 2) == 28

    def test_day_1_any_month(self) -> None:
        """Day 1 exists in every month; clamp never applies."""
        for month in range(1, 13):
            assert clamped_anchor_day(1, 2027, month) == 1

    # --- day 29 ---

    def test_day_29_non_leap_february(self) -> None:
        """Day 29 in non-leap Feb clamps to 28."""
        assert clamped_anchor_day(29, 2027, 2) == 28

    def test_day_29_leap_february(self) -> None:
        """Day 29 in leap Feb stays 29 (Feb has 29 days in a leap year)."""
        assert clamped_anchor_day(29, 2028, 2) == 29

    def test_day_29_march(self) -> None:
        """Day 29 in March (31 days) is unchanged."""
        assert clamped_anchor_day(29, 2027, 3) == 29

    def test_day_29_all_non_feb_months(self) -> None:
        """Day 29 is unchanged for all months with >= 29 days (not Feb)."""
        for month in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            assert clamped_anchor_day(29, 2027, month) == 29

    # --- day 30 ---

    def test_day_30_non_leap_february(self) -> None:
        """Day 30 in non-leap Feb clamps to 28."""
        assert clamped_anchor_day(30, 2027, 2) == 28

    def test_day_30_leap_february(self) -> None:
        """Day 30 in leap Feb clamps to 29."""
        assert clamped_anchor_day(30, 2028, 2) == 29

    def test_day_30_months_with_31_days(self) -> None:
        """Day 30 is unchanged for months with 31 days."""
        for month in [1, 3, 5, 7, 8, 10, 12]:
            assert clamped_anchor_day(30, 2027, month) == 30

    def test_day_30_april(self) -> None:
        """Day 30 in April (30-day month) is unchanged."""
        assert clamped_anchor_day(30, 2027, 4) == 30

    def test_day_30_june(self) -> None:
        """Day 30 in June (30-day month) is unchanged."""
        assert clamped_anchor_day(30, 2027, 6) == 30

    # --- day 31 ---

    def test_day_31_january(self) -> None:
        """Day 31 in January (31-day month) is unchanged."""
        assert clamped_anchor_day(31, 2027, 1) == 31

    def test_day_31_non_leap_february(self) -> None:
        """Day 31 in non-leap Feb clamps to 28."""
        assert clamped_anchor_day(31, 2027, 2) == 28

    def test_day_31_leap_february(self) -> None:
        """Day 31 in leap Feb clamps to 29."""
        assert clamped_anchor_day(31, 2028, 2) == 29

    def test_day_31_march(self) -> None:
        """Day 31 in March (31 days) is unchanged."""
        assert clamped_anchor_day(31, 2027, 3) == 31

    def test_day_31_april(self) -> None:
        """Day 31 in April (30-day month) clamps to 30."""
        assert clamped_anchor_day(31, 2027, 4) == 30

    def test_day_31_may(self) -> None:
        """Day 31 in May (31 days) is unchanged."""
        assert clamped_anchor_day(31, 2027, 5) == 31

    def test_day_31_june(self) -> None:
        """Day 31 in June (30-day month) clamps to 30."""
        assert clamped_anchor_day(31, 2027, 6) == 30

    def test_day_31_july(self) -> None:
        """Day 31 in July (31 days) is unchanged."""
        assert clamped_anchor_day(31, 2027, 7) == 31

    def test_day_31_august(self) -> None:
        """Day 31 in August (31 days) is unchanged."""
        assert clamped_anchor_day(31, 2027, 8) == 31

    def test_day_31_september(self) -> None:
        """Day 31 in September (30 days) clamps to 30."""
        assert clamped_anchor_day(31, 2027, 9) == 30

    def test_day_31_october(self) -> None:
        """Day 31 in October (31 days) is unchanged."""
        assert clamped_anchor_day(31, 2027, 10) == 31

    def test_day_31_november(self) -> None:
        """Day 31 in November (30 days) clamps to 30."""
        assert clamped_anchor_day(31, 2027, 11) == 30

    def test_day_31_december(self) -> None:
        """Day 31 in December (31 days) is unchanged."""
        assert clamped_anchor_day(31, 2027, 12) == 31


# ---------------------------------------------------------------------------
# is_occurrence_date — weekly cadence
# ---------------------------------------------------------------------------

# Anchor on a known Wednesday.
_WEEKLY_ANCHOR = datetime.date(2026, 6, 3)  # Wednesday 2026-06-03


class TestWeekly:
    """is_occurrence_date correctness for cadence='weekly'."""

    def test_fires_on_anchor(self) -> None:
        """Delta == 0 (anchor itself) is a valid occurrence."""
        assert is_occurrence_date(_WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR)

    def test_fires_at_delta_7(self) -> None:
        """Delta == 7 (one week after anchor) is a valid occurrence."""
        assert is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR + datetime.timedelta(days=7)
        )

    def test_fires_at_delta_14(self) -> None:
        """Delta == 14 (two weeks after anchor) is a valid occurrence."""
        assert is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR + datetime.timedelta(days=14)
        )

    def test_fires_at_delta_49(self) -> None:
        """Delta == 49 (seven weeks after anchor) is a valid occurrence."""
        assert is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR + datetime.timedelta(days=49)
        )

    @pytest.mark.parametrize("offset", range(1, 7))
    def test_does_not_fire_between_week_boundaries(self, offset: int) -> None:
        """Days 1-6 after anchor are NOT occurrences."""
        assert not is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR + datetime.timedelta(days=offset)
        )

    def test_does_not_fire_at_delta_8(self) -> None:
        """Delta == 8 (not a multiple of 7) is not an occurrence."""
        assert not is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR + datetime.timedelta(days=8)
        )

    def test_does_not_fire_before_anchor(self) -> None:
        """A date before the anchor (negative delta) is not an occurrence."""
        assert not is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR - datetime.timedelta(days=1)
        )

    def test_does_not_fire_one_week_before_anchor(self) -> None:
        """One full week before the anchor is not an occurrence."""
        assert not is_occurrence_date(
            _WEEKLY_ANCHOR, "weekly", _WEEKLY_ANCHOR - datetime.timedelta(days=7)
        )


# ---------------------------------------------------------------------------
# is_occurrence_date — biweekly cadence
# ---------------------------------------------------------------------------

_BIWEEKLY_ANCHOR = datetime.date(2026, 6, 1)  # Monday 2026-06-01


class TestBiweekly:
    """is_occurrence_date correctness for cadence='biweekly'."""

    def test_fires_on_anchor(self) -> None:
        """Delta == 0 (anchor itself) is a valid occurrence."""
        assert is_occurrence_date(_BIWEEKLY_ANCHOR, "biweekly", _BIWEEKLY_ANCHOR)

    def test_fires_at_delta_14(self) -> None:
        """Delta == 14 (two weeks after anchor) is a valid occurrence."""
        assert is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR + datetime.timedelta(days=14),
        )

    def test_fires_at_delta_28(self) -> None:
        """Delta == 28 (four weeks after anchor) is a valid occurrence."""
        assert is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR + datetime.timedelta(days=28),
        )

    def test_does_not_fire_at_delta_7(self) -> None:
        """Delta == 7 (weekly interval, not biweekly) is NOT an occurrence.

        This is the key weekly-vs-biweekly discriminator.
        """
        assert not is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR + datetime.timedelta(days=7),
        )

    def test_does_not_fire_at_delta_1(self) -> None:
        """Delta == 1 is not an occurrence."""
        assert not is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR + datetime.timedelta(days=1),
        )

    def test_does_not_fire_at_delta_21(self) -> None:
        """Delta == 21 (three weeks — odd multiple) is not an occurrence."""
        assert not is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR + datetime.timedelta(days=21),
        )

    def test_does_not_fire_before_anchor(self) -> None:
        """A date one day before anchor is not an occurrence."""
        assert not is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR - datetime.timedelta(days=1),
        )

    def test_does_not_fire_14_days_before_anchor(self) -> None:
        """14 days before anchor is not an occurrence (delta < 0)."""
        assert not is_occurrence_date(
            _BIWEEKLY_ANCHOR,
            "biweekly",
            _BIWEEKLY_ANCHOR - datetime.timedelta(days=14),
        )


# ---------------------------------------------------------------------------
# is_occurrence_date — monthly cadence, day-31 anchor
# ---------------------------------------------------------------------------


class TestMonthlyDay31:
    """Monthly cadence with anchor day 31 fires on last-day-of-short-months."""

    # Anchor: 2027-01-31 (Jan 31)
    _ANCHOR_JAN31 = datetime.date(2027, 1, 31)

    def test_fires_on_anchor_january_31(self) -> None:
        """Anchor date itself (Jan 31) is always an occurrence."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 1, 31))

    def test_fires_february_28_non_leap(self) -> None:
        """Feb 28 (non-leap 2027) is the clamped occurrence for day-31."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 2, 28))

    def test_does_not_fire_february_27_non_leap(self) -> None:
        """Feb 27 is not the clamped occurrence (28 is)."""
        assert not is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 2, 27))

    def test_fires_march_31(self) -> None:
        """March 31 (31-day month) fires normally for day-31 anchor."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 3, 31))

    def test_fires_april_30(self) -> None:
        """April 30 (30-day month) is the clamped occurrence for day-31."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 4, 30))

    def test_does_not_fire_april_29(self) -> None:
        """April 29 is not the clamped occurrence for day-31 in April."""
        assert not is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 4, 29))

    def test_fires_may_31(self) -> None:
        """May 31 fires normally for day-31 anchor."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 5, 31))

    def test_fires_june_30(self) -> None:
        """June 30 is the clamped occurrence for day-31 in June."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 6, 30))

    def test_fires_july_31(self) -> None:
        """July 31 fires normally."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 7, 31))

    def test_fires_august_31(self) -> None:
        """August 31 fires normally."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 8, 31))

    def test_fires_september_30(self) -> None:
        """September 30 is the clamped occurrence for day-31 in September."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 9, 30))

    def test_fires_october_31(self) -> None:
        """October 31 fires normally."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 10, 31))

    def test_fires_november_30(self) -> None:
        """November 30 is the clamped occurrence for day-31 in November."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 11, 30))

    def test_fires_december_31(self) -> None:
        """December 31 fires normally."""
        assert is_occurrence_date(self._ANCHOR_JAN31, "monthly", datetime.date(2027, 12, 31))

    # --- Leap year walk for day-31 anchor (2028) ---

    _ANCHOR_JAN31_LEAP = datetime.date(2028, 1, 31)

    def test_leap_fires_february_29(self) -> None:
        """Feb 29 (leap 2028) is the clamped occurrence for day-31."""
        assert is_occurrence_date(self._ANCHOR_JAN31_LEAP, "monthly", datetime.date(2028, 2, 29))

    def test_leap_fires_april_30(self) -> None:
        """April 30 is the clamped occurrence for day-31 in April (leap year)."""
        assert is_occurrence_date(self._ANCHOR_JAN31_LEAP, "monthly", datetime.date(2028, 4, 30))

    def test_leap_fires_june_30(self) -> None:
        """June 30 is the clamped occurrence for day-31 in June (leap year)."""
        assert is_occurrence_date(self._ANCHOR_JAN31_LEAP, "monthly", datetime.date(2028, 6, 30))

    def test_leap_fires_march_31(self) -> None:
        """March 31 fires normally in a leap year."""
        assert is_occurrence_date(self._ANCHOR_JAN31_LEAP, "monthly", datetime.date(2028, 3, 31))

    def test_never_skips_a_month_non_leap(self) -> None:
        """Exactly one occurrence per calendar month for day-31 in 2027."""
        anchor = datetime.date(2027, 1, 31)
        for month in range(1, 13):
            import calendar

            year = 2027
            last_day = calendar.monthrange(year, month)[1]
            expected_day = min(31, last_day)
            occurrence = datetime.date(year, month, expected_day)
            assert is_occurrence_date(anchor, "monthly", occurrence), (
                f"Expected occurrence on {occurrence} for day-31 anchor "
                f"but is_occurrence_date returned False"
            )
            # No other day in that month should be an occurrence.
            for day in range(1, last_day + 1):
                if day == expected_day:
                    continue
                non_occurrence = datetime.date(year, month, day)
                assert not is_occurrence_date(anchor, "monthly", non_occurrence), (
                    f"Expected no occurrence on {non_occurrence} but "
                    f"is_occurrence_date returned True"
                )

    def test_never_skips_a_month_leap(self) -> None:
        """Exactly one occurrence per calendar month for day-31 in 2028."""
        anchor = datetime.date(2028, 1, 31)
        for month in range(1, 13):
            import calendar

            year = 2028
            last_day = calendar.monthrange(year, month)[1]
            expected_day = min(31, last_day)
            occurrence = datetime.date(year, month, expected_day)
            assert is_occurrence_date(anchor, "monthly", occurrence), (
                f"Expected occurrence on {occurrence} for day-31 anchor "
                f"(leap year) but is_occurrence_date returned False"
            )


# ---------------------------------------------------------------------------
# is_occurrence_date — monthly cadence, day-30 anchor
# ---------------------------------------------------------------------------


class TestMonthlyDay30:
    """Monthly cadence with anchor day 30."""

    _ANCHOR_MAR30 = datetime.date(2027, 3, 30)

    def test_fires_on_30th_every_long_month(self) -> None:
        """Day 30 exists in all months with >= 30 days (all except Feb)."""
        for month in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            year = 2027 if month >= 3 else 2028
            candidate = datetime.date(year, month, 30)
            assert is_occurrence_date(
                self._ANCHOR_MAR30, "monthly", candidate
            ), f"Day-30 anchor should fire on {candidate}"

    def test_fires_on_february_28_non_leap(self) -> None:
        """Day-30 anchor clamps to Feb 28 in non-leap year."""
        assert is_occurrence_date(self._ANCHOR_MAR30, "monthly", datetime.date(2028, 2, 28))

    def test_fires_on_february_29_leap(self) -> None:
        """Day-30 anchor clamps to Feb 29 in a leap year."""
        assert is_occurrence_date(self._ANCHOR_MAR30, "monthly", datetime.date(2032, 2, 29))

    def test_does_not_fire_on_31_in_31_day_month(self) -> None:
        """Day-30 anchor does NOT fire on the 31st even in 31-day months."""
        assert not is_occurrence_date(self._ANCHOR_MAR30, "monthly", datetime.date(2027, 5, 31))


# ---------------------------------------------------------------------------
# is_occurrence_date — monthly cadence, day-29 anchor
# ---------------------------------------------------------------------------


class TestMonthlyDay29:
    """Monthly cadence with anchor day 29."""

    _ANCHOR_MAY29 = datetime.date(2027, 5, 29)

    def test_fires_on_29_non_feb(self) -> None:
        """Day-29 anchor fires on the 29th of all months with >= 29 days."""
        for month in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            year = 2027 if month >= 5 else 2028
            candidate = datetime.date(year, month, 29)
            assert is_occurrence_date(self._ANCHOR_MAY29, "monthly", candidate)

    def test_fires_on_february_28_non_leap_2027(self) -> None:
        """Day-29 clamps to Feb 28 in non-leap 2027."""
        assert is_occurrence_date(self._ANCHOR_MAY29, "monthly", datetime.date(2028, 2, 28))

    def test_fires_on_february_29_leap_2028(self) -> None:
        """Day-29 stays as Feb 29 in leap year 2028."""
        assert is_occurrence_date(datetime.date(2028, 1, 29), "monthly", datetime.date(2028, 2, 29))

    def test_does_not_fire_february_27_non_leap(self) -> None:
        """Day-29 anchor does NOT fire on Feb 27 in a non-leap year."""
        assert not is_occurrence_date(self._ANCHOR_MAY29, "monthly", datetime.date(2028, 2, 27))


# ---------------------------------------------------------------------------
# is_occurrence_date — monthly cadence, day <= 28 anchor
# ---------------------------------------------------------------------------


class TestMonthlyDayLeq28:
    """Monthly cadence with anchor day <= 28 — clamp never applies."""

    _ANCHOR_15 = datetime.date(2027, 1, 15)

    def test_fires_on_15th_every_month(self) -> None:
        """Day 15 anchor fires on the 15th of every month."""
        for month in range(1, 13):
            year = 2027 if month >= 1 else 2028
            candidate = datetime.date(year, month, 15)
            assert is_occurrence_date(self._ANCHOR_15, "monthly", candidate)

    def test_does_not_fire_on_14th(self) -> None:
        """Day 15 anchor does not fire on the 14th of a month."""
        assert not is_occurrence_date(self._ANCHOR_15, "monthly", datetime.date(2027, 3, 14))

    def test_does_not_fire_on_16th(self) -> None:
        """Day 15 anchor does not fire on the 16th of a month."""
        assert not is_occurrence_date(self._ANCHOR_15, "monthly", datetime.date(2027, 3, 16))

    _ANCHOR_1 = datetime.date(2027, 6, 1)

    def test_fires_on_first_of_every_month(self) -> None:
        """Day 1 anchor fires on the 1st of every month."""
        for month in range(1, 13):
            year = 2027 if month >= 6 else 2028
            candidate = datetime.date(year, month, 1)
            assert is_occurrence_date(self._ANCHOR_1, "monthly", candidate)


# ---------------------------------------------------------------------------
# is_occurrence_date — future-month anchor does not fire early
# ---------------------------------------------------------------------------


class TestMonthlyFutureAnchor:
    """Monthly notifications with a future anchor do not fire before it."""

    def test_future_anchor_does_not_fire_in_prior_month(self) -> None:
        """A monthly notification whose anchor is next month does not fire now."""
        anchor = datetime.date(2027, 8, 15)  # August 15
        today = datetime.date(2027, 7, 15)  # July 15 — anchor's month not yet
        assert not is_occurrence_date(anchor, "monthly", today)

    def test_monthly_before_anchor_date_in_same_month(self) -> None:
        """A date before the anchor in the anchor's own month does not fire."""
        anchor = datetime.date(2027, 8, 20)
        today = datetime.date(2027, 8, 15)
        assert not is_occurrence_date(anchor, "monthly", today)

    def test_monthly_fires_on_anchor_month_and_day(self) -> None:
        """The anchor date itself fires (delta == 0)."""
        anchor = datetime.date(2027, 8, 15)
        assert is_occurrence_date(anchor, "monthly", anchor)

    def test_monthly_fires_one_month_after_anchor(self) -> None:
        """One month after the anchor (same day) fires."""
        anchor = datetime.date(2027, 8, 15)
        assert is_occurrence_date(anchor, "monthly", datetime.date(2027, 9, 15))
