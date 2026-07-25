"""Pure calendar predicates for Hydra Tank Week scheduling (#268).

Provides pure functions — no DB, no Discord dependency:

- :func:`tank_week_ending_tuesday`: returns the first Tuesday of a given
  (year, month), which is the Tank Week ending Tuesday for that month.
- :func:`is_tank_week_headsup_date`: returns ``True`` iff *today* is the
  heads-up fire date (ending Tuesday − 7 days) for some month's tank week.
- :func:`is_end_of_tank_date`: returns ``True`` iff *today* is the first
  Tuesday of its month (i.e. the tank-week ending Tuesday).
- :func:`is_siege_start_date`: returns ``True`` iff *today* is a Siege
  start date in the bi-weekly cycle.
- :func:`is_siege_48h_headsup_date`: returns ``True`` iff *today* is two
  days before a Siege start date.
- :func:`is_siege_24h_headsup_date`: returns ``True`` iff *today* is one
  day before a Siege start date.

Design notes:
- Tank Week is the Hydra clash that *ends* in a new month — the clash whose
  ending Tuesday is the **first Tuesday of the month** (day 1–7, inclusive;
  when the 1st is a Tuesday it IS the answer).
- The heads-up date is the Tuesday **7 days before** the ending Tuesday
  (the prior clash's ending Tuesday — always a Tuesday because
  Tuesday − 7 = Tuesday). It can fall in the **previous calendar month**
  (or even previous year) when the tank-week ending Tuesday is the 1st–7th
  of a month.
- ``datetime.date`` arithmetic handles month and year roll-over natively,
  so ``ending_tuesday - timedelta(days=7)`` is always correct.
"""

from __future__ import annotations

import calendar
import datetime
from typing import Final

__all__ = [
    "tank_week_ending_tuesday",
    "is_tank_week_headsup_date",
    "is_end_of_tank_date",
    "is_siege_start_date",
    "is_siege_48h_headsup_date",
    "is_siege_24h_headsup_date",
]

SIEGE_ANCHOR_DATE: Final[datetime.date] = datetime.date(2026, 7, 21)
"""Confirmed Siege start date from a maintainer comment on issue #325.

Re-anchoring requires changing this hardcoded module constant and deploying
the code; it is not stored as a KV secret or database value.
"""

SIEGE_PERIOD_DAYS: Final[int] = 14
"""Number of days between Siege start dates."""


def tank_week_ending_tuesday(year: int, month: int) -> datetime.date:
    """Return the first Tuesday-dated day of (year, month).

    This is the Tank Week ending Tuesday for that month — the date on
    which the first Hydra clash of the month ends.  Per the spec tie-break:
    when the 1st of the month is itself a Tuesday, it IS the answer (day 1).

    Args:
        year: The calendar year (e.g. 2026).
        month: The calendar month, 1-based (1 = January … 12 = December).

    Returns:
        The :class:`datetime.date` of the first Tuesday in the given month.
        Always satisfies ``1 <= result.day <= 7`` and ``result.weekday() == 1``.
    """
    # calendar.weekday(year, month, 1) returns 0=Mon … 6=Sun for the 1st.
    # Tuesday is weekday 1.  The number of days to add to reach the first
    # Tuesday: (1 - first_day_weekday) % 7.  When the 1st is already a
    # Tuesday this evaluates to 0 (no offset).
    first_weekday = calendar.weekday(year, month, 1)
    days_ahead = (1 - first_weekday) % 7
    return datetime.date(year, month, 1 + days_ahead)


def is_end_of_tank_date(today: datetime.date) -> bool:
    """Return True iff *today* is the Tank Week ending Tuesday for its month.

    Equivalent to ``today == tank_week_ending_tuesday(today.year, today.month)``.
    Returns ``False`` immediately for any non-Tuesday.

    Args:
        today: The date to test.

    Returns:
        ``True`` if *today* is the first Tuesday of its month.
        ``False`` otherwise.
    """
    if today.weekday() != 1:
        return False
    return today == tank_week_ending_tuesday(today.year, today.month)


def is_tank_week_headsup_date(today: datetime.date) -> bool:
    """Return True iff *today* is the Tank Week heads-up fire date.

    The heads-up date for a given month's tank week is:
    ``tank_week_ending_tuesday(year, month) - timedelta(days=7)``.

    Because the ending Tuesday can be as early as the 1st of the month,
    the heads-up can fall in the **previous calendar month** (or previous
    year).  The derivation therefore works backwards from *today*:

    1. Compute the candidate ending Tuesday as
       ``today + timedelta(days=7)``.
    2. Determine which (year, month) that candidate falls in.
    3. Compute the *actual* first Tuesday of that (year, month).
    4. Return ``True`` iff today equals that ending Tuesday − 7 days.

    This correctly handles all cross-month and cross-year boundaries.
    Under −7, the heads-up is always a Tuesday (Tuesday − 7 = Tuesday),
    so the ``today.weekday() != 1`` early-return guard is exact.

    Args:
        today: The date to test.

    Returns:
        ``True`` if *today* is the heads-up date for some month's tank week.
        ``False`` otherwise.
    """
    if today.weekday() != 1:
        return False
    # The candidate target month is the month containing today + 7 days.
    candidate = today + datetime.timedelta(days=7)
    ending = tank_week_ending_tuesday(candidate.year, candidate.month)
    headsup = ending - datetime.timedelta(days=7)
    return today == headsup


def is_siege_start_date(today: datetime.date) -> bool:
    """Return True iff *today* is a Siege start date.

    Siege starts repeat every 14 days from :data:`SIEGE_ANCHOR_DATE`.
    Python's modulo remains non-negative for dates before the anchor, so the
    same calculation covers the full bi-weekly cycle in both directions.

    Args:
        today: The date to test.

    Returns:
        ``True`` if *today* is a Siege start date.
        ``False`` otherwise.
    """
    offset = (today - SIEGE_ANCHOR_DATE).days % SIEGE_PERIOD_DAYS
    return offset == 0


def is_siege_48h_headsup_date(today: datetime.date) -> bool:
    """Return True iff *today* is the 48-hour Siege heads-up date.

    The 48-hour heads-up occurs two days before each bi-weekly Siege start,
    at offset 12 in the 14-day cycle anchored by
    :data:`SIEGE_ANCHOR_DATE`. Python's modulo handles pre-anchor dates
    without special-casing.

    Args:
        today: The date to test.

    Returns:
        ``True`` if *today* is two days before a Siege start date.
        ``False`` otherwise.
    """
    offset = (today - SIEGE_ANCHOR_DATE).days % SIEGE_PERIOD_DAYS
    return offset == 12


def is_siege_24h_headsup_date(today: datetime.date) -> bool:
    """Return True iff *today* is the 24-hour Siege heads-up date.

    The 24-hour heads-up occurs one day before each bi-weekly Siege start,
    at offset 13 in the 14-day cycle anchored by
    :data:`SIEGE_ANCHOR_DATE`. Python's modulo handles pre-anchor dates
    without special-casing.

    Args:
        today: The date to test.

    Returns:
        ``True`` if *today* is one day before a Siege start date.
        ``False`` otherwise.
    """
    offset = (today - SIEGE_ANCHOR_DATE).days % SIEGE_PERIOD_DAYS
    return offset == 13
