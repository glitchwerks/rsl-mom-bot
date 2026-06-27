"""Tests for ReminderScheduler.

Covers cold-start ordering, mid-day reconnects, real-tick smoke,
SIGTERM shutdown, revision-rollout overlap, Discord error taxonomy,
insert-collision short-circuit, fire-time predicate, and UTC-midnight
date-attribution.

Design notes
------------
- A custom ``FakeBot`` class replaces ``discord.Client`` in all tests so
  no gateway connection is attempted.
- ``FakeChannel`` replaces ``discord.TextChannel``; its ``send`` method
  is a coroutine that records calls and can be configured to raise.
- In-memory SQLite + ``Base.metadata.create_all`` is used for all DB
  fixtures (no Alembic in tests).
- ``time-machine`` is used for fake-clock tests where UTC datetime must be
  controlled without real wall-clock waits.
- The ``tick_seconds`` constructor parameter is injected with small values
  in tests to keep runtimes short.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
import time_machine
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.scheduler import ReminderScheduler

# ---------------------------------------------------------------------------
# Deterministic synchronisation helper
# ---------------------------------------------------------------------------


async def _wait_for_send_count(
    channel: FakeChannel,
    count: int,
    *,
    poll_interval: float = 0.005,
    timeout: float = 5.0,
) -> None:
    """Poll until ``channel.send.call_count >= count`` or ``timeout`` expires.

    Replaces ``await asyncio.sleep(<fixed>)`` in tests that need a precise
    number of Discord sends.  A fixed sleep is wall-clock-fragile: on a
    slow CI runner the sleep may expire before the required sends complete,
    giving a false ``call_count < expected`` failure.  Polling the condition
    directly means the test advances the moment the sends have occurred,
    regardless of how long each tick takes on the host.

    Args:
        channel: The :class:`FakeChannel` whose ``send`` call-count is
            checked.
        count: The minimum ``call_count`` to wait for.
        poll_interval: Seconds between checks (default 5 ms).
        timeout: Maximum seconds to wait before raising ``AssertionError``
            (default 5 s — generous enough for any real CI runner, short
            enough to surface genuine hangs quickly).

    Raises:
        AssertionError: If ``call_count`` does not reach ``count`` within
            ``timeout`` seconds.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while channel.send.call_count < count:
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for channel.send.call_count >= {count}; "
                f"got {channel.send.call_count} after {timeout}s"
            )
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal stand-in for a discord.TextChannel."""

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake."""
        self.id = channel_id
        self.send = AsyncMock()


class FakeBot:
    """Minimal stand-in for discord.Client.

    Controls ``is_ready()`` and ``get_channel()`` without a gateway.
    """

    def __init__(self, ready: bool = True) -> None:
        """Initialise with a readiness flag and a channel registry."""
        self._ready = ready
        self._channels: dict[int, FakeChannel] = {}

    def is_ready(self) -> bool:
        """Return the current readiness state."""
        return self._ready

    def set_ready(self, value: bool) -> None:
        """Flip the readiness flag (used in mid-day reconnect tests)."""
        self._ready = value

    def add_channel(self, channel: FakeChannel) -> None:
        """Register a fake channel so get_channel can find it."""
        self._channels[channel.id] = channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        """Return a registered fake channel by id."""
        return self._channels.get(channel_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CHANNEL_ID = 111111111111111111
_ROLE_ID = 222222222222222222

# Tuesday 07:00 UTC = Hydra fire-time; weekday=1
_TUESDAY_0700 = datetime.datetime(2026, 5, 5, 7, 0, 0, tzinfo=datetime.UTC)


def _make_session_factory(engine: Any) -> Any:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with both tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_reminder(
    session: Session,
    *,
    weekday: int = 1,
    fire_time_utc: datetime.time = datetime.time(7, 0, 0),
    channel_id: int = _CHANNEL_ID,
    role_mention_id: int | None = _ROLE_ID,
    name: str = "Hydra",
) -> Reminder:
    """Insert a Reminder row and return it."""
    reminder = Reminder(
        name=name,
        channel_id=channel_id,
        weekday=weekday,
        fire_time_utc=fire_time_utc,
        message_template="Test message",
        role_mention_id=role_mention_id,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def _make_scheduler(
    bot: FakeBot,
    engine: Any,
    tick_seconds: float = 0.05,
) -> ReminderScheduler:
    """Convenience factory for a ReminderScheduler with a fast tick."""
    return ReminderScheduler(
        bot=bot,  # type: ignore[arg-type]
        session_factory=_make_session_factory(engine),
        tick_seconds=tick_seconds,
    )


# ---------------------------------------------------------------------------
# Cold-start regression (plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_start_no_send_before_ready() -> None:
    """Scheduler skips tick and does not send if bot is not ready."""
    engine = _make_engine()
    with Session(engine) as s:
        reminder = _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=False)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)  # let two ticks pass with ready=False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_not_called()
    # Also verify no reminder_sent row was written.
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 0

    # Suppress unused variable warning.
    _ = reminder


@pytest.mark.asyncio
async def test_cold_start_sends_after_ready() -> None:
    """After bot becomes ready, the next tick fires the reminder."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=False)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)  # one tick; still not ready
        bot.set_ready(True)
        await asyncio.sleep(0.08)  # second tick fires
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_called_once()


# ---------------------------------------------------------------------------
# Mid-day reconnect (plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_day_reconnect_skips_tick_when_not_ready() -> None:
    """Mid-loop not-ready guard: tick is skipped, no insert, no send."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)  # first tick fires (ready)
        assert channel.send.call_count == 1

        # Simulate gateway disconnect.
        bot.set_ready(False)
        await asyncio.sleep(0.08)  # second tick skipped (not ready)
        assert channel.send.call_count == 1  # no second send

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_mid_day_reconnect_resumes_after_ready() -> None:
    """After reconnect, a new (different) reminder fires on next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        # Reminder at 07:00 UTC fires first.
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday(), name="Hydra")
        # Second reminder at 08:00 UTC fires later.
        _seed_reminder(
            s,
            weekday=_TUESDAY_0700.weekday(),
            fire_time_utc=datetime.time(8, 0, 0),
            channel_id=_CHANNEL_ID,
            name="Chimera",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    # Start at 07:00 — Hydra fires.
    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        assert channel.send.call_count == 1  # Hydra fired

        # Disconnect, then advance to 08:00 — skip one tick.
        bot.set_ready(False)
        await asyncio.sleep(0.08)
        assert channel.send.call_count == 1  # still 1

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Advance to 08:00 and reconnect — Chimera should fire.
    _0800 = _TUESDAY_0700 + datetime.timedelta(hours=1)
    with time_machine.travel(_0800, tick=False):
        task2 = asyncio.create_task(scheduler.run())
        bot.set_ready(True)
        await asyncio.sleep(0.08)
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2  # Hydra + Chimera


# ---------------------------------------------------------------------------
# Real-tick smoke (plan § 6) — uses wall-clock time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_tick_smoke() -> None:
    """Scheduler fires once after fire-time passes and does not re-fire.

    Uses tick_seconds=0.1 and time_machine to advance 200ms past the
    fire-time boundary.  The fire_time_utc is set to a minute boundary
    one minute in the future, then time_machine jumps past it so the
    second tick fires.  The third tick sees the UNIQUE row and does not
    re-send.

    This tests the real tick-cycle semantics: t=0 (before fire-time) →
    no send; t=1 (after fire-time) → send once; t=2 → no re-send.
    """
    # Anchor: Tuesday 07:00 UTC — fire_time at 07:01 UTC.
    start = datetime.datetime(2026, 5, 5, 7, 0, 0, tzinfo=datetime.UTC)
    fire_at = datetime.time(7, 1, 0)  # minute boundary, seconds=0

    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=start.weekday(),
            fire_time_utc=fire_at,
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.1)

    # Phase 1: sit at 07:00 — fire_time (07:01) is not yet reached.
    with time_machine.travel(start, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # one tick
        assert channel.send.call_count == 0  # not fired yet
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Phase 2: advance to 07:01 — fire_time reached; fires once.
    after_fire = start + datetime.timedelta(minutes=1)
    with time_machine.travel(after_fire, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # one tick
        assert channel.send.call_count == 1  # fired exactly once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Phase 3: still at 07:01 — UNIQUE constraint blocks re-send.
    with time_machine.travel(after_fire, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # one tick
        assert channel.send.call_count == 1  # still exactly once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# SIGTERM during sleep (plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigterm_during_sleep_shuts_down_cleanly() -> None:
    """task.cancel() while sleeping resolves within 100ms."""
    engine = _make_engine()
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, engine, tick_seconds=10.0)

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)  # let it reach asyncio.sleep(10)
    task.cancel()

    try:
        await asyncio.wait_for(task, timeout=0.2)
    except asyncio.CancelledError:
        pass
    # If we get here, shutdown completed within 0.2s — test passes.
    assert task.done()


# ---------------------------------------------------------------------------
# Revision-rollout overlap (defensive, plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revision_rollout_exactly_one_send() -> None:
    """Two concurrent scheduler instances send the reminder exactly once."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot1 = FakeBot(ready=True)
    bot1.add_channel(channel)
    bot2 = FakeBot(ready=True)
    bot2.add_channel(channel)

    scheduler1 = _make_scheduler(bot1, engine, tick_seconds=0.05)
    scheduler2 = _make_scheduler(bot2, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task1 = asyncio.create_task(scheduler1.run())
        task2 = asyncio.create_task(scheduler2.run())
        await asyncio.sleep(0.12)
        task1.cancel()
        task2.cancel()
        for t in [task1, task2]:
            try:
                await t
            except asyncio.CancelledError:
                pass

    # Exactly one send across both schedulers.
    assert channel.send.call_count == 1


# ---------------------------------------------------------------------------
# Discord error taxonomy (plan § 5) — one test per row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_forbidden_row_stays_no_retry() -> None:
    """discord.Forbidden → row stays, no re-send on second tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Missing Access"), "test"
    )
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until the first (and only) send fires, then cancel — avoids
        # wall-clock races on slow CI runners.
        await _wait_for_send_count(channel, 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # send was attempted once; subsequent ticks see UNIQUE collision.
    assert channel.send.call_count == 1
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_discord_not_found_row_stays_no_retry() -> None:
    """discord.NotFound → row stays, no re-send on second tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = discord.NotFound(
        MagicMock(status=404, reason="Unknown Channel"), "test"
    )
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until the first (and only) send fires, then cancel.
        await _wait_for_send_count(channel, 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 1
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_discord_http_5xx_row_deleted_retried() -> None:
    """discord.HTTPException with status>=500 → row deleted, retry next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    # First call raises 503; second call succeeds.
    channel.send.side_effect = [
        discord.HTTPException(MagicMock(status=503, reason="Service Unavailable"), "test"),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until both sends have fired (first failed, second succeeded)
        # before cancelling — avoids wall-clock races on slow CI runners.
        await _wait_for_send_count(channel, 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Two send attempts: first failed+retried, second succeeded.
    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_discord_rate_limited_treated_as_5xx() -> None:
    """discord.RateLimited → row deleted, retry next tick (treated as 5xx)."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = [
        discord.RateLimited(retry_after=1.0),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until both sends have fired before cancelling.
        await _wait_for_send_count(channel, 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_aiohttp_client_error_row_deleted_retried() -> None:
    """aiohttp.ClientError → row deleted, retry next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = [
        aiohttp.ClientError("connection reset"),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until both sends have fired before cancelling.
        await _wait_for_send_count(channel, 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_asyncio_timeout_error_row_deleted_retried() -> None:
    """asyncio.TimeoutError → row deleted, retry next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = [
        TimeoutError(),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until both sends have fired before cancelling.
        await _wait_for_send_count(channel, 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_uncaught_exception_row_stays_critical_logged() -> None:
    """Uncaught Exception → row stays, CRITICAL logged, no re-send."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = ValueError("unexpected error")
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    # Alembic's fileConfig (run by test_alembic.py earlier in the suite)
    # calls logging.config.fileConfig which replaces root logger handlers.
    # Use a direct handler on the module logger to capture records reliably
    # regardless of root handler state.
    captured: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _CapturingHandler()
    sched_logger = logging.getLogger("mom_bot.reminders.scheduler")
    # Alembic's fileConfig with disable_existing_loggers=True (the default)
    # can mark mom_bot loggers as disabled if they existed before the call.
    # Force re-enable so the logger emits in this test.
    sched_logger.disabled = False
    sched_logger.addHandler(handler)
    sched_logger.setLevel(logging.DEBUG)

    try:
        with time_machine.travel(_TUESDAY_0700, tick=False):
            task = asyncio.create_task(scheduler.run())
            # Wait until the first (and only) send fires, then cancel.
            await _wait_for_send_count(channel, 1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        sched_logger.removeHandler(handler)

    # Exactly one send attempt; subsequent ticks see UNIQUE collision.
    assert channel.send.call_count == 1
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1
    critical_records = [r for r in captured if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1


# ---------------------------------------------------------------------------
# Insert-collision short-circuit (plan § 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_collision_skips_discord_call() -> None:
    """Pre-existing reminder_sent row → no Discord send on tick."""
    engine = _make_engine()
    today = _TUESDAY_0700.date()

    with Session(engine) as s:
        reminder = _seed_reminder(s, weekday=_TUESDAY_0700.weekday())
        # Pre-insert the idempotency row.
        sent = ReminderSent(reminder_id=reminder.id, fire_date_utc=today)
        s.add(sent)
        s.commit()

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_not_called()


# ---------------------------------------------------------------------------
# Fire-time predicate (plan § 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_time_predicate_only_fires_eligible() -> None:
    """Only the reminder matching weekday + fire_time fires; others skip."""
    engine = _make_engine()
    now = _TUESDAY_0700  # Tuesday 07:00 UTC

    with Session(engine) as s:
        # Should fire: weekday=1 (Tuesday), fire_time=07:00 <= 07:00.
        _seed_reminder(
            s,
            weekday=1,
            fire_time_utc=datetime.time(7, 0, 0),
            channel_id=_CHANNEL_ID,
            name="ShouldFire",
        )
        # Should skip: wrong weekday (Wednesday=2).
        _seed_reminder(
            s,
            weekday=2,
            fire_time_utc=datetime.time(7, 0, 0),
            channel_id=_CHANNEL_ID,
            name="WrongWeekday",
        )
        # Should skip: fire_time > now (08:00 > 07:00).
        _seed_reminder(
            s,
            weekday=1,
            fire_time_utc=datetime.time(8, 0, 0),
            channel_id=_CHANNEL_ID,
            name="TooLate",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(now, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Exactly one send for "ShouldFire".
    assert channel.send.call_count == 1


# ---------------------------------------------------------------------------
# NULL role_mention_id — no ping prefix (scheduler.py:L243-L244)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_role_mention_id_sends_bare_message() -> None:
    """Reminder with role_mention_id=None sends message without <@& prefix.

    Exercises the scheduler.py:L243-L244 NULL-check branch introduced in
    #28 and made the default by #45.  When ``role_mention_id`` is ``None``
    the scheduler must deliver the bare ``message_template`` string without
    prepending a role mention.
    """
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=_TUESDAY_0700.weekday(),
            role_mention_id=None,
            name="NoMention",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_called_once()
    sent_message: str = channel.send.call_args.args[0]
    assert "<@&" not in sent_message, (
        "Expected no role-mention prefix when role_mention_id is None, "
        f"but got: {sent_message!r}"
    )
    assert "Test message" in sent_message


# ---------------------------------------------------------------------------
# Role mention — allowed_mentions + message prefix (#51)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_mention_id_set_prepends_markup_and_passes_allowed_mentions() -> None:
    """When role_mention_id is set, message starts with <@&id> prefix.

    Also verifies that channel.send is called with
    ``allowed_mentions=discord.AllowedMentions(roles=True)`` so the ping
    actually notifies guild members in that role (not just renders visually).
    Without ``allowed_mentions``, Discord silently suppresses the notification
    even though the markup renders — this was the silent-failure mode noted in
    issue #51.
    """
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=_TUESDAY_0700.weekday(),
            role_mention_id=_ROLE_ID,
            name="HydraWithRole",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_called_once()
    call_args = channel.send.call_args

    # The first positional arg must start with the role-mention markup.
    sent_message: str = call_args.args[0]
    expected_prefix = f"<@&{_ROLE_ID}>"
    assert sent_message.startswith(expected_prefix), (
        f"Expected message to start with {expected_prefix!r}, " f"got: {sent_message!r}"
    )

    # channel.send must receive allowed_mentions=AllowedMentions(roles=True)
    # so that Discord actually notifies members (not just renders the markup).
    allowed = call_args.kwargs.get("allowed_mentions")
    assert allowed is not None, (
        "channel.send was called without allowed_mentions= kwarg — "
        "Discord will suppress the role notification even though the "
        "markup renders visually."
    )
    assert isinstance(
        allowed, discord.AllowedMentions
    ), f"Expected discord.AllowedMentions, got {type(allowed)!r}"
    assert (
        allowed.roles is True
    ), f"Expected AllowedMentions(roles=True), got roles={allowed.roles!r}"


@pytest.mark.asyncio
async def test_null_role_mention_id_does_not_pass_allowed_mentions() -> None:
    """When role_mention_id is None, send is called without allowed_mentions.

    Graceful-degrade path: reminders without a role mention should post the
    bare message and NOT include an allowed_mentions kwarg (or at minimum,
    must not include a role-mention prefix in the message body).
    """
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=_TUESDAY_0700.weekday(),
            role_mention_id=None,
            name="NoRole",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_called_once()
    call_args = channel.send.call_args

    # No role-mention markup in the message body.
    sent_message: str = call_args.args[0]
    assert "<@&" not in sent_message, (
        f"Expected no role-mention prefix when role_mention_id is None, " f"got: {sent_message!r}"
    )

    # allowed_mentions kwarg must not include roles=True.
    allowed = call_args.kwargs.get("allowed_mentions")
    if allowed is not None:
        # If caller passes AllowedMentions at all, roles must not be True.
        assert not (isinstance(allowed, discord.AllowedMentions) and allowed.roles is True), (
            "channel.send should not enable role pings when " "role_mention_id is None"
        )


# ---------------------------------------------------------------------------
# UTC-midnight date-attribution (plan § 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fire_moment,expected_fire_date",
    [
        # 23:59:00 UTC on Tuesday → attributed to Tuesday.
        (
            datetime.datetime(2026, 5, 5, 23, 59, 0, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 5),
        ),
        # 23:59:59 UTC on Tuesday → attributed to Tuesday.
        (
            datetime.datetime(2026, 5, 5, 23, 59, 59, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 5),
        ),
        # 00:00:00 UTC on Wednesday → attributed to Wednesday.
        (
            datetime.datetime(2026, 5, 6, 0, 0, 0, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 6),
        ),
        # 00:00:01 UTC on Wednesday → attributed to Wednesday.
        (
            datetime.datetime(2026, 5, 6, 0, 0, 1, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 6),
        ),
    ],
)
async def test_utc_midnight_date_attribution(
    fire_moment: datetime.datetime,
    expected_fire_date: datetime.date,
) -> None:
    """fire_date_utc on reminder_sent row matches the UTC calendar date."""
    engine = _make_engine()
    weekday = fire_moment.weekday()
    # Truncate to minute boundary for fire_time_utc column.
    fire_time = fire_moment.time().replace(second=0, microsecond=0)

    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=weekday,
            fire_time_utc=fire_time,
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(fire_moment, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with Session(engine) as s:
        sent_rows = s.query(ReminderSent).all()

    assert len(sent_rows) == 1
    assert sent_rows[0].fire_date_utc == expected_fire_date


# ===========================================================================
# Phase B — DM-branch integration tests (#269 per-member notifications)
# ===========================================================================
#
# New test doubles needed for the DM branch (FakeGuild / FakeMember did not
# exist in the channel-only scheduler).  These extend the file-level harness
# rather than replacing it.
# ===========================================================================


class FakeMember:
    """Minimal stand-in for discord.Member with a DM send coroutine.

    Args:
        discord_id: The member's snowflake integer, mirrors member.id.
    """

    def __init__(self, discord_id: int) -> None:
        """Initialise with a snowflake id and a fresh AsyncMock send."""
        self.id = discord_id
        self.send = AsyncMock()


class FakeGuild:
    """Minimal stand-in for discord.Guild.

    Supports get_member() (cache hit) and fetch_member() (async, may raise).
    """

    def __init__(self) -> None:
        """Initialise with an empty member registry."""
        self._members: dict[int, FakeMember] = {}
        self._fetch_side_effects: dict[int, Exception | None] = {}

    def add_member(self, member: FakeMember) -> None:
        """Register a fake member so get_member() can find it.

        Args:
            member: The FakeMember to register.
        """
        self._members[member.id] = member

    def set_fetch_side_effect(self, discord_id: int, exc: Exception) -> None:
        """Configure fetch_member() to raise exc for a specific id.

        Args:
            discord_id: The snowflake whose fetch should fail.
            exc: The exception to raise.
        """
        self._fetch_side_effects[discord_id] = exc

    def get_member(self, discord_id: int) -> FakeMember | None:
        """Return a registered member or None (cache miss).

        Args:
            discord_id: The snowflake to look up.
        """
        return self._members.get(discord_id)

    async def fetch_member(self, discord_id: int) -> FakeMember:
        """Async fetch; raises configured side effect or returns member.

        Args:
            discord_id: The snowflake to fetch.

        Raises:
            discord.NotFound: If configured as a side effect.
            discord.Forbidden: If configured as a side effect.
        """
        exc = self._fetch_side_effects.get(discord_id)
        if exc is not None:
            raise exc
        member = self._members.get(discord_id)
        if member is None:
            raise discord.NotFound(MagicMock(status=404, reason="Unknown Member"), "test")
        return member


# ---------------------------------------------------------------------------
# DM-branch fixtures
# ---------------------------------------------------------------------------

_MEMBER_DISCORD_ID = 555444333222111000
_NOTIFICATION_ANCHOR = datetime.date(2026, 5, 5)  # Tuesday — same as _TUESDAY_0700

# Weekly anchor — fires every Tuesday; _TUESDAY_0700 is a Tuesday.
_MEMBER_FIRE_TIME = datetime.time(7, 0, 0)


def _make_scheduler_with_guild(
    bot: FakeBot,
    guild: FakeGuild,
    engine: Any,
    tick_seconds: float = 0.05,
) -> ReminderScheduler:
    """Factory for a ReminderScheduler that includes a guild reference.

    Args:
        bot: The FakeBot driving readiness.
        guild: The FakeGuild for member resolution.
        engine: The SQLAlchemy engine (in-memory SQLite).
        tick_seconds: Tick interval; fast in tests.

    Returns:
        A ReminderScheduler configured with guild for DM dispatch.
    """
    return ReminderScheduler(
        bot=bot,  # type: ignore[arg-type]
        guild=guild,  # type: ignore[arg-type]
        session_factory=_make_session_factory(engine),
        tick_seconds=tick_seconds,
    )


def _seed_member_notification(
    session: Session,
    *,
    name: str = "dm-test",
    target_discord_id: int = _MEMBER_DISCORD_ID,
    anchor_date_utc: datetime.date = _NOTIFICATION_ANCHOR,
    fire_time_utc: datetime.time = _MEMBER_FIRE_TIME,
    cadence: str = "weekly",
    message_template: str = "Hello from the bot!",
    enabled: bool = True,
) -> Any:
    """Insert a MemberNotification row and return it.

    Args:
        session: SQLAlchemy session to use for the insert.
        name: Human-readable label (UNIQUE key).
        target_discord_id: The DM recipient's Discord snowflake (stored TEXT).
        anchor_date_utc: First occurrence date.
        fire_time_utc: Time-of-day gate (minute boundary).
        cadence: One of weekly, biweekly, monthly.
        message_template: The static message body.
        enabled: Whether the notification is active.

    Returns:
        The inserted MemberNotification ORM row.
    """
    from mom_bot.member_notifications.models import MemberNotification

    row = MemberNotification(
        name=name,
        target_discord_id=str(target_discord_id),
        anchor_date_utc=anchor_date_utc,
        fire_time_utc=fire_time_utc,
        cadence=cadence,
        message_template=message_template,
        enabled=enabled,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _make_engine_with_member_notifications() -> Any:
    """Create an in-memory SQLite engine with ALL tables (reminders + DM).

    Returns:
        A SQLAlchemy engine with Base.metadata.create_all applied, which
        includes both the existing reminder tables and the new
        member_notification / member_notification_sent tables.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    # Force import of MemberNotification models so they register with Base.
    import mom_bot.member_notifications.models  # noqa: F401

    Base.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# DM-branch happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_branch_fires_on_occurrence_date() -> None:
    """Enabled member_notification sends DM via member.send() on anchor date.

    Resolves the target member by target_discord_id (not username).
    """
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    guild = FakeGuild()
    guild.add_member(member)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            target_discord_id=_MEMBER_DISCORD_ID,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    member.send.assert_called_once()


@pytest.mark.asyncio
async def test_dm_branch_resolves_by_discord_id_not_username() -> None:
    """DM branch uses target_discord_id for member resolution (§ 2.3).

    Two members in the guild: only the one whose id matches fires.
    """
    engine = _make_engine_with_member_notifications()
    target = FakeMember(_MEMBER_DISCORD_ID)
    other = FakeMember(9876543210)
    guild = FakeGuild()
    guild.add_member(target)
    guild.add_member(other)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            target_discord_id=_MEMBER_DISCORD_ID,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    target.send.assert_called_once()
    other.send.assert_not_called()


# ---------------------------------------------------------------------------
# Skip-to-next, NOT catch-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delayed_same_day_tick_fires() -> None:
    """A tick delayed past fire_time on the occurrence date still fires.

    Simulates a brief scheduler outage that recovers later the same day.
    The occurrence date predicate still matches (today is still the anchor
    date), and now_time >= fire_time_utc, so the notification fires.
    """
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    guild = FakeGuild()
    guild.add_member(member)

    # fire_time = 07:00; we tick at 09:00 (two hours late, same day).
    delayed_tick = datetime.datetime(2026, 5, 5, 9, 0, 0, tzinfo=datetime.UTC)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,  # 07:00
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(delayed_tick, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    member.send.assert_called_once()


@pytest.mark.asyncio
async def test_past_occurrence_does_not_fire_late() -> None:
    """Day after a missed occurrence: no DM, no member_notification_sent row.

    Verifies skip-to-next semantics: once the occurrence date has passed,
    the notification is silently dropped — no backlog, no late fire.
    The NEXT genuine occurrence (7 days later for weekly) fires normally.
    """
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    guild = FakeGuild()
    guild.add_member(member)

    # anchor = 2026-05-05 (Tuesday); cadence = weekly.
    # Tick at 2026-05-06 (Wednesday) — not an occurrence date.
    day_after_missed = datetime.datetime(2026, 5, 6, 7, 0, 0, tzinfo=datetime.UTC)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(day_after_missed, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    member.send.assert_not_called()

    from mom_bot.member_notifications.models import MemberNotificationSent

    with Session(engine) as s:
        assert s.query(MemberNotificationSent).count() == 0

    # Next genuine occurrence (7 days after anchor) DOES fire.
    next_occurrence = datetime.datetime(2026, 5, 12, 7, 0, 0, tzinfo=datetime.UTC)
    with time_machine.travel(next_occurrence, tick=False):
        task2 = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass

    member.send.assert_called_once()


# ---------------------------------------------------------------------------
# enabled=false never fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_notification_never_fires() -> None:
    """A member_notification with enabled=False is never sent."""
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    guild = FakeGuild()
    guild.add_member(member)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
            enabled=False,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    member.send.assert_not_called()


# ---------------------------------------------------------------------------
# Per-occurrence idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_branch_idempotency_second_tick_no_resend() -> None:
    """Second tick on the same occurrence date does not re-send the DM.

    The UNIQUE(member_notification_id, occurrence_date_utc) constraint
    blocks re-insertion, so member.send() is called exactly once even
    across multiple ticks within the same calendar day.
    """
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    guild = FakeGuild()
    guild.add_member(member)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine, tick_seconds=0.05)

    # Two full ticks at the same occurrence time.
    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # ~3 ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert member.send.call_count == 1


# ---------------------------------------------------------------------------
# Error taxonomy (mirroring channel branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_forbidden_row_stays_no_retry() -> None:
    """Forbidden DMs (DMs closed) → row stays, no retry on second tick."""
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    member.send.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Cannot send messages to this user"), "test"
    )
    guild = FakeGuild()
    guild.add_member(member)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # send attempted once; UNIQUE row still present (permanent drop).
    assert member.send.call_count == 1
    from mom_bot.member_notifications.models import MemberNotificationSent

    with Session(engine) as s:
        assert s.query(MemberNotificationSent).count() == 1


@pytest.mark.asyncio
async def test_dm_not_found_row_stays_no_retry() -> None:
    """Member left the guild (NotFound) → permanent drop, row stays."""
    engine = _make_engine_with_member_notifications()
    guild = FakeGuild()

    with Session(engine) as s:
        notif = _seed_member_notification(
            s,
            target_discord_id=_MEMBER_DISCORD_ID,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )
        _ = notif  # id used only for assertion

    # Member NOT in guild — get_member returns None and fetch raises NotFound.
    guild.set_fetch_side_effect(
        _MEMBER_DISCORD_ID,
        discord.NotFound(MagicMock(status=404, reason="Unknown Member"), "test"),
    )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    from mom_bot.member_notifications.models import MemberNotificationSent

    # Sent row must exist (insert-first ordering — the slot was claimed).
    with Session(engine) as s:
        assert s.query(MemberNotificationSent).count() == 1


@pytest.mark.asyncio
async def test_dm_5xx_row_deleted_retried_next_tick() -> None:
    """HTTPException status>=500 on DM → row deleted, retry on next tick."""
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    member.send.side_effect = [
        discord.HTTPException(MagicMock(status=503, reason="Service Unavailable"), "test"),
        None,  # second tick succeeds
    ]
    guild = FakeGuild()
    guild.add_member(member)

    with Session(engine) as s:
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        # Wait until exactly two send attempts have occurred.
        deadline = asyncio.get_event_loop().time() + 5.0
        while member.send.call_count < 2:
            if asyncio.get_event_loop().time() >= deadline:
                raise AssertionError("Timed out waiting for 2 DM send attempts")
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert member.send.call_count == 2
    from mom_bot.member_notifications.models import MemberNotificationSent

    with Session(engine) as s:
        assert s.query(MemberNotificationSent).count() == 1


# ---------------------------------------------------------------------------
# Insert-first ordering (finding 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_first_departed_member_consumes_daily_slot() -> None:
    """NotFound for departed member still leaves a sent row (insert-first).

    Spec § 2.3 finding 6: the idempotency row is inserted BEFORE member
    resolution.  This test verifies that a NotFound on resolution still
    results in a member_notification_sent row, so the notification cannot
    loop on subsequent ticks within the same day.
    """
    engine = _make_engine_with_member_notifications()
    guild = FakeGuild()
    guild.set_fetch_side_effect(
        _MEMBER_DISCORD_ID,
        discord.NotFound(MagicMock(status=404, reason="Unknown Member"), "test"),
    )

    with Session(engine) as s:
        _seed_member_notification(
            s,
            target_discord_id=_MEMBER_DISCORD_ID,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    from mom_bot.member_notifications.models import MemberNotificationSent

    with Session(engine) as s:
        count = s.query(MemberNotificationSent).count()

    assert count == 1, (
        "Expected a member_notification_sent row even after NotFound — "
        "insert-first ordering guarantees the slot is consumed before "
        "member resolution so the notification does not loop."
    )


# ---------------------------------------------------------------------------
# Regression guard — existing channel reminders unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_channel_reminders_unaffected_by_dm_branch() -> None:
    """A channel Reminder still fires when a MemberNotification is also due.

    The DM branch must not interfere with or suppress the channel loop.
    """
    engine = _make_engine_with_member_notifications()
    member = FakeMember(_MEMBER_DISCORD_ID)
    guild = FakeGuild()
    guild.add_member(member)

    channel = FakeChannel(_CHANNEL_ID)

    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())
        _seed_member_notification(
            s,
            anchor_date_utc=_NOTIFICATION_ANCHOR,
            fire_time_utc=_MEMBER_FIRE_TIME,
        )

    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler_with_guild(bot, guild, engine)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Channel reminder still fires.
    channel.send.assert_called_once()
    # DM also fires.
    member.send.assert_called_once()
