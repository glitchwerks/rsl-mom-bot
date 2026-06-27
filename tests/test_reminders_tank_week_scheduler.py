"""Tests for tank-week calendar-conditional scheduler behavior (#268).

Covers:
- tank_week_headsup row fires only on the heads-up Tuesday.
- tank_week_end row fires only on the end-of-tank Tuesday.
- Replace semantics (suppression pre-filter): on the end-of-tank Tuesday,
  tank_week_end fires and the normal Hydra row's mark_sent is NEVER called.
- On ordinary Tuesdays, normal Hydra fires and tank-week rows are silent.
- Idempotency: each new row fires at most once per its date.

Uses the same FakeBot/FakeChannel/time_machine/in-memory-SQLite harness
as test_reminders_scheduler.py.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, patch

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

_CHANNEL_ID = 111111111111111111
_ROLE_ID = 222222222222222222

# May 2026: first Tuesday = May 5 (end-of-tank), heads-up = Apr 28 (May 5 − 7).
_TANK_END_TUESDAY = datetime.datetime(2026, 5, 5, 7, 0, 0, tzinfo=datetime.UTC)
_HEADSUP_TUESDAY = datetime.datetime(2026, 4, 28, 7, 0, 0, tzinfo=datetime.UTC)
# An ordinary Tuesday that is neither: second Tuesday of May 2026 = May 12.
_ORDINARY_TUESDAY = datetime.datetime(2026, 5, 12, 7, 0, 0, tzinfo=datetime.UTC)

# ---------------------------------------------------------------------------
# Test doubles (mirroring test_reminders_scheduler.py)
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
# Helpers
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
    weekday: int = 1,
    fire_time_utc: datetime.time = datetime.time(7, 0, 0),
    channel_id: int = _CHANNEL_ID,
    role_mention_id: int | None = _ROLE_ID,
    month_condition: str | None = None,
) -> Reminder:
    """Insert a Reminder row with optional month_condition and return it."""
    reminder = Reminder(
        name=name,
        channel_id=channel_id,
        weekday=weekday,
        fire_time_utc=fire_time_utc,
        message_template="Test message",
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
# tank_week_headsup row fires only on heads-up Tuesday
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headsup_row_fires_on_headsup_tuesday() -> None:
    """tank_week_headsup reminder fires on the heads-up Tuesday (Apr 27, 2026)."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankHeadsup", month_condition="tank_week_headsup")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    await _run_one_tick(scheduler, _HEADSUP_TUESDAY)

    channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_headsup_row_silent_on_ordinary_tuesday() -> None:
    """tank_week_headsup reminder does NOT fire on an ordinary Tuesday."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankHeadsup", month_condition="tank_week_headsup")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    await _run_one_tick(scheduler, _ORDINARY_TUESDAY)

    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_headsup_row_silent_on_tank_end_tuesday() -> None:
    """tank_week_headsup reminder does NOT fire on the end-of-tank Tuesday."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankHeadsup", month_condition="tank_week_headsup")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    await _run_one_tick(scheduler, _TANK_END_TUESDAY)

    channel.send.assert_not_called()


# ---------------------------------------------------------------------------
# tank_week_end row fires only on end-of-tank Tuesday
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tank_end_row_fires_on_end_of_tank_tuesday() -> None:
    """tank_week_end reminder fires on the end-of-tank Tuesday (May 5, 2026)."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankEnd", month_condition="tank_week_end")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    await _run_one_tick(scheduler, _TANK_END_TUESDAY)

    channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_tank_end_row_silent_on_ordinary_tuesday() -> None:
    """tank_week_end reminder does NOT fire on an ordinary Tuesday."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankEnd", month_condition="tank_week_end")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    await _run_one_tick(scheduler, _ORDINARY_TUESDAY)

    channel.send.assert_not_called()


# ---------------------------------------------------------------------------
# Replace semantics: suppression pre-filter (spec §2.4 — the critical contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tank_end_suppresses_normal_hydra_mark_sent_on_end_of_tank_tuesday() -> None:
    """On the end-of-tank Tuesday, tank_week_end fires; normal Hydra mark_sent
    is NEVER called — not post-hoc skipped, but prevented by the pre-filter.

    This asserts the suppression happens BEFORE any side effect, matching the
    required four-step sequence (collect → calendar filter → suppress → send).
    Asserting on mark_sent absence (not merely on ReminderSent row absence)
    catches a buggy send-then-delete implementation that would pass a row check.
    """
    engine = _make_engine()
    hydra_id: int
    with Session(engine) as s:
        hydra = _seed_reminder(s, name="Hydra", month_condition=None)
        hydra_id = hydra.id
        _seed_reminder(s, name="TankEnd", month_condition="tank_week_end")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    # Spy on the sent-store: intercept calls where reminder_id matches Hydra.
    hydra_mark_sent_called = False

    original_init = ReminderSent.__init__

    def _spying_init(self: ReminderSent, **kwargs: object) -> None:
        nonlocal hydra_mark_sent_called
        if kwargs.get("reminder_id") == hydra_id:
            hydra_mark_sent_called = True
        original_init(self, **kwargs)

    with patch.object(ReminderSent, "__init__", _spying_init):
        await _run_one_tick(scheduler, _TANK_END_TUESDAY)

    # The tank_week_end row must have fired exactly once.
    assert channel.send.call_count == 1

    # The normal Hydra row's ReminderSent must NEVER have been instantiated.
    assert not hydra_mark_sent_called, (
        "Normal Hydra row's mark_sent was called on end-of-tank Tuesday — "
        "suppression pre-filter did not prevent it."
    )

    # Confirm via DB: only one ReminderSent row (for TankEnd, not Hydra).
    with Session(engine) as s:
        sent_rows = s.query(ReminderSent).all()
    assert len(sent_rows) == 1
    assert (
        sent_rows[0].reminder_id != hydra_id
    ), "ReminderSent row belongs to normal Hydra — it should not have been written."


@pytest.mark.asyncio
async def test_normal_hydra_fires_on_ordinary_tuesday_tank_end_silent() -> None:
    """On ordinary Tuesdays, normal Hydra fires; tank_week_end is silent."""
    engine = _make_engine()
    hydra_id: int
    tank_end_id: int
    with Session(engine) as s:
        hydra = _seed_reminder(s, name="Hydra", month_condition=None)
        hydra_id = hydra.id
        tank_end = _seed_reminder(s, name="TankEnd", month_condition="tank_week_end")
        tank_end_id = tank_end.id

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    await _run_one_tick(scheduler, _ORDINARY_TUESDAY)

    # Exactly one send (normal Hydra).
    assert channel.send.call_count == 1

    # The ReminderSent row must be for Hydra, NOT tank_week_end.
    with Session(engine) as s:
        sent_rows = s.query(ReminderSent).all()
    assert len(sent_rows) == 1
    assert sent_rows[0].reminder_id == hydra_id, (
        f"Expected ReminderSent for Hydra (id={hydra_id}), "
        f"got reminder_id={sent_rows[0].reminder_id}"
    )
    _ = tank_end_id  # referenced in assertion above; suppress unused warning


# ---------------------------------------------------------------------------
# Idempotency: tank-week rows fire at most once per date
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headsup_row_fires_at_most_once_per_date() -> None:
    """tank_week_headsup row fires exactly once even across multiple ticks."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankHeadsup", month_condition="tank_week_headsup")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    # Two separate tick runs at the same date — idempotency must prevent re-fire.
    with time_machine.travel(_HEADSUP_TUESDAY, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.20)  # multiple ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 1

    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_tank_end_row_fires_at_most_once_per_date() -> None:
    """tank_week_end row fires exactly once even across multiple ticks."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, name="TankEnd", month_condition="tank_week_end")

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine)

    with time_machine.travel(_TANK_END_TUESDAY, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.20)  # multiple ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 1

    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1
