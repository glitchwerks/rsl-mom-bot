"""Pure occurrence-date math for the per-member notification system (#269).

Provides two public functions with zero DB or Discord dependency:

- :func:`clamped_anchor_day` — clamp an anchor day-of-month to the last
  day of a given month when the anchor day does not exist in that month.
- :func:`is_occurrence_date` — return ``True`` iff ``today`` is a
  cadence occurrence date for the given ``anchor_date`` and ``cadence``.

These functions are the highest-value test surface in the feature because
the monthly-clamp logic cannot be expressed cleanly in SQL (it needs
:func:`calendar.monthrange`), making the Python layer the only correctness
gate.

Spec reference: #269 per-member notifications § 2.3a and § 4.
"""

from __future__ import annotations

import calendar
import datetime

__all__ = ["clamped_anchor_day", "is_occurrence_date"]


def clamped_anchor_day(
    anchor_day: int,
    year: int,
    month: int,
) -> int:
    """Clamp an anchor day-of-month to the last valid day of a given month.

    When the anchor's day-of-month does not exist in the target month (e.g.
    day 31 in April which only has 30 days), the occurrence falls on the
    *last day* of that month — it is never skipped.

    Args:
        anchor_day: The anchor notification's day-of-month (1–31).
        year: The target year (e.g. 2027).
        month: The target month (1–12).

    Returns:
        The clamped day-of-month: ``min(anchor_day, last_day_of_month)``.

    Examples:
        >>> clamped_anchor_day(31, 2027, 4)
        30
        >>> clamped_anchor_day(31, 2027, 2)
        28
        >>> clamped_anchor_day(31, 2028, 2)
        29
        >>> clamped_anchor_day(15, 2027, 3)
        15
    """
    last_day = calendar.monthrange(year, month)[1]
    return min(anchor_day, last_day)


def is_occurrence_date(
    anchor_date: datetime.date,
    cadence: str,
    today: datetime.date,
) -> bool:
    """Return True iff *today* is a cadence occurrence date for *anchor_date*.

    Evaluates the cadence predicate described in spec § 2.3a:

    - **weekly**: fires every 7 days from the anchor; delta must be a
      non-negative multiple of 7.
    - **biweekly**: fires every 14 days from the anchor; delta must be a
      non-negative multiple of 14.
    - **monthly**: fires on the clamped anchor day-of-month in every month
      at or after the anchor month, using :func:`clamped_anchor_day` to
      handle short months.

    The ``delta >= 0`` guard on every cadence ensures that a notification
    created with a future anchor date never fires before that date.

    Args:
        anchor_date: The notification's first occurrence date.
        cadence: One of ``'weekly'``, ``'biweekly'``, or ``'monthly'``.
        today: The date to test.

    Returns:
        ``True`` if *today* is an occurrence date; ``False`` otherwise.

    Raises:
        ValueError: If *cadence* is not one of the three valid values.

    Examples:
        >>> from datetime import date
        >>> anchor = date(2026, 6, 3)  # Wednesday
        >>> is_occurrence_date(anchor, "weekly", date(2026, 6, 3))
        True
        >>> is_occurrence_date(anchor, "weekly", date(2026, 6, 10))
        True
        >>> is_occurrence_date(anchor, "weekly", date(2026, 6, 4))
        False
    """
    if cadence == "weekly":
        delta = (today - anchor_date).days
        return delta >= 0 and delta % 7 == 0

    if cadence == "biweekly":
        delta = (today - anchor_date).days
        return delta >= 0 and delta % 14 == 0

    if cadence == "monthly":
        # The occurrence is at or after the anchor month/year.
        anchor_ym = (anchor_date.year, anchor_date.month)
        today_ym = (today.year, today.month)
        if today_ym < anchor_ym:
            return False
        # Must also not be before the anchor date within the anchor's own
        # month (handles "before anchor date in same month" edge case).
        if today_ym == anchor_ym and today < anchor_date:
            return False
        expected_day = clamped_anchor_day(anchor_date.day, today.year, today.month)
        return today.day == expected_day

    raise ValueError(
        f"Unknown cadence {cadence!r}; expected 'weekly', 'biweekly', or " f"'monthly'."
    )
