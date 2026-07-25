"""End-to-end scheduler tests for bi-weekly Siege reminders (#327 — Slice B).

TDD: written before ``scheduler.py`` is wired to the Siege calendar
predicates (``is_siege_48h_headsup_date`` / ``is_siege_24h_headsup_date``,
added to ``calendar.py`` in Slice A / #326).

Deliberately end-to-end rather than predicate-only (plan §8 Slice B test
plan, "Weekday/predicate agreement"): each test seeds a real ``Reminder``
row and drives it through :class:`ReminderScheduler` on a frozen clock,
so it exercises BOTH the SQL ``weekday ==`` predicate
(``scheduler.py:L217``) AND the Python ``month_condition`` calendar-filter
branch (``scheduler.py:L233-L238``) together. A predicate-only unit test
would not catch the two disagreeing (plan §3.2 — the "double-encoding
hazard", the single highest-risk detail in the Siege reminders plan).

Uses the same FakeBot/FakeChannel/time_machine/in-memory-SQLite harness as
``tests/test_reminders_tank_week_scheduler.py``.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock

import pytest
import time_machine
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.scheduler import ReminderScheduler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHANNEL_ID = 333333333333333333
_ROLE_ID = 444444444444444444

# Confirmed anchor: Tuesday 2026-07-21 10:00 UTC, 14-day cadence (plan §1).
# T-48h fires Sunday, T-24h fires Monday, both at 10:00 UTC.
#
# On-cycle dates (offset 12 / 13 in the 14-day cycle from the anchor):
_ON_CYCLE_48H = datetime.datetime(2026, 8, 2, 10, 0, 0, tzinfo=datetime.UTC)  # Sunday
_ON_CYCLE_24H = datetime.datetime(2026, 8, 3, 10, 0, 0, tzinfo=datetime.UTC)  # Monday
# Off-cycle dates: same weekday + time, one week later — wrong cycle.
_OFF_CYCLE_48H = datetime.datetime(2026, 8, 9, 10, 0, 0, tzinfo=datetime.UTC)  # Sunday
_OFF_CYCLE_24H = datetime.datetime(2026, 8, 10, 10, 0, 0, tzinfo=datetime.UTC)  # Monday

# ---------------------------------------------------------------------------
# Test doubles (mirroring tests/test_reminders_tank_week_scheduler.py)
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal stand-in for a discord.TextChannel."""

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake."""
        self.id = channel_id
        self.send = AsyncMock()


class FakeBot:
    """Minimal stand-in for discord.Client."""

    def __init__(self, ready: bool = True) -> None:
        """Initialise with a readiness flag and a channel registry."""
        self._ready = ready
        self._channels: dict[int, FakeChannel] = {}

    def is_ready(self) -> bool:
        """Return the current readiness state."""
        return self._ready

    def add_channel(self, channel: FakeChannel) -> None:
        """Register a fake channel so get_channel can find it."""
        self._channels[channel.id] = channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        """Return a registered fake channel by id."""
        return self._channels.get(channel_id)


# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_reminders_tank_week_scheduler.py)
# ---------------------------------------------------------------------------


def _make_engine() -> object:
    """Create an in-memory SQLite engine with all reminder tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _make_session_factory(engine: object) -> object:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)


def _seed_reminder(
    session: Session,
    *,
    name: str,
    weekday: int,
    fire_time_utc: datetime.time,
    month_condition: str,
    channel_id: int = _CHANNEL_ID,
    role_mention_id: int | None = _ROLE_ID,
) -> Reminder:
    """Insert a Siege Reminder row and return it."""
    reminder = Reminder(
        name=name,
        channel_id=channel_id,
        weekday=weekday,
        fire_time_utc=fire_time_utc,
        message_template="Test Siege message",
        role_mention_id=role_mention_id,
        month_condition=month_condition,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def _make_scheduler(
    bot: FakeBot,
    engine: object,
    tick_seconds: float = 0.05,
) -> ReminderScheduler:
    """Convenience factory for a fast-tick ReminderScheduler."""
    return ReminderScheduler(
        bot=bot,  # type: ignore[arg-type]
        session_factory=_make_session_factory(engine),
        tick_seconds=tick_seconds,
    )


async def _run_one_tick(scheduler: ReminderScheduler, travel_time: datetime.datetime) -> None:
    """Run the scheduler for two ticks at the given fake time, then cancel."""
    with time_machine.travel(travel_time, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Siege 48h heads-up (Sunday, weekday=6) — on-cycle fires, off-cycle silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_siege_48h_headsup_fires_on_cycle_and_silent_off_cycle() -> None:
    """Siege 48h heads-up fires on the on-cycle Sunday, not the off-cycle one.

    2026-08-02 (on-cycle) and 2026-08-09 (off-cycle) are both Sundays at
    10:00 UTC — same SQL weekday/time slot. Only the Python
    ``is_siege_48h_headsup_date`` calendar filter distinguishes them.
    """
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            name="Siege 48h Heads-up",
            weekday=6,
            fire_time_utc=datetime.time(10, 0, 0),
            month_condition="siege_48h_headsup",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    # On-cycle Sunday (2026-08-02): the reminder must fire.
    await _run_one_tick(scheduler, _ON_CYCLE_48H)
    assert channel.send.call_count == 1, (
        "Expected the Siege 48h heads-up reminder to fire on the on-cycle "
        "Sunday (2026-08-02 10:00 UTC) — exercises both the SQL weekday==6 "
        "predicate and the month_condition calendar filter together."
    )

    # Off-cycle Sunday one week later (2026-08-09), same weekday+time: must
    # NOT fire — this is the assertion that proves bi-weekly, not weekly.
    channel.send.reset_mock()
    await _run_one_tick(scheduler, _OFF_CYCLE_48H)
    assert channel.send.call_count == 0, (
        "Siege 48h heads-up fired on the off-cycle Sunday (2026-08-09) — "
        "weekday matched but the mod-14 calendar predicate should have "
        "rejected this date. This is the double-encoding disagreement the "
        "plan (§3.2) calls out as highest-risk."
    )


# ---------------------------------------------------------------------------
# Siege 24h heads-up (Monday, weekday=0) — on-cycle fires, off-cycle silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_siege_24h_headsup_fires_on_cycle_and_silent_off_cycle() -> None:
    """Siege 24h heads-up fires on the on-cycle Monday, not the off-cycle one.

    2026-08-03 (on-cycle) and 2026-08-10 (off-cycle) are both Mondays at
    10:00 UTC — same SQL weekday/time slot. Only the Python
    ``is_siege_24h_headsup_date`` calendar filter distinguishes them.
    """
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            name="Siege 24h Heads-up",
            weekday=0,
            fire_time_utc=datetime.time(10, 0, 0),
            month_condition="siege_24h_headsup",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    # On-cycle Monday (2026-08-03): the reminder must fire.
    await _run_one_tick(scheduler, _ON_CYCLE_24H)
    assert channel.send.call_count == 1, (
        "Expected the Siege 24h heads-up reminder to fire on the on-cycle "
        "Monday (2026-08-03 10:00 UTC) — exercises both the SQL weekday==0 "
        "predicate and the month_condition calendar filter together."
    )

    # Off-cycle Monday one week later (2026-08-10), same weekday+time: must
    # NOT fire.
    channel.send.reset_mock()
    await _run_one_tick(scheduler, _OFF_CYCLE_24H)
    assert channel.send.call_count == 0, (
        "Siege 24h heads-up fired on the off-cycle Monday (2026-08-10) — "
        "weekday matched but the mod-14 calendar predicate should have "
        "rejected this date."
    )
