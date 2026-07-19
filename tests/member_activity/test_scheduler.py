"""Tests for ``AutoKickScheduler`` — the 24h-inactivity auto-kick sweep (#300).

TDD: written before ``mom_bot.member_activity.scheduler`` exists.

Design mirrors ``tests/test_reminders_scheduler.py`` exactly: a ``FakeBot``
replaces ``discord.Client`` (controls ``is_ready()``), a ``FakeGuild``
replaces ``discord.Guild`` (``get_member`` cache-hit / ``fetch_member``
cache-miss, mirroring the DM branch's ``_handle_member_notification``), and
``time_machine`` drives the 24h-elapsed clock. ``tick_seconds`` is injected
small so tests run fast; a polling helper (not a fixed sleep) waits for the
exact call count needed, avoiding wall-clock races on slow CI runners — the
same rationale as ``_wait_for_send_count`` in ``test_reminders_scheduler.py``.

Binding contract decisions (kept deliberately light per the frozen-contract
guidance not to over-pin retry taxonomy that the issue does not specify):

- Member resolution: ``guild.get_member(int)`` (cache) then
  ``await guild.fetch_member(int)`` (miss) — the same two-step pattern
  ``ReminderScheduler._handle_member_notification`` already uses.
- Order of operations per stale member: DM attempt FIRST (best-effort —
  failure does not block the kick), THEN kick. This is the literal AC
  wording ("Best-effort DM ... sent before the kick").
  ``test_dm_sent_before_kick_ordering`` pins the ordering; it does not pin
  the DM message text or the kick reason text beyond "non-empty string" —
  wording is left to the implementer.
  ``test_dm_forbidden_does_not_block_kick`` pins that a Forbidden DM
  (closed DMs) does not prevent the kick — this is a direct AC requirement.
- Kick outcome (success OR any handled exception) removes the member's
  tracking row so a slow-running sweep does not re-attempt the same member
  on a later tick (``test_no_double_kick_across_ticks``). This is a
  simplification versus the reminder scheduler's full
  drop/retry-transient taxonomy — since a kick is a one-shot action (not a
  recurring daily fire), and Discord will reject a second kick of an
  already-departed member as fatally as it accepted the first, treating
  every kick attempt as terminal is the simplest correct behaviour. Flagged
  here as a binding decision for router/spec-owner ratification if
  retry-on-transient-failure semantics are wanted instead.
- A member no longer resolvable (``fetch_member`` raises
  ``discord.NotFound`` — already left the guild) is handled without
  crashing and without a kick attempt — there is nothing left to kick.
- Kick success is logged (audit trail) at WARNING-or-higher OR INFO from
  the ``mom_bot.member_activity.scheduler`` logger — mirroring the coarse
  logger-name-only convention in ``tests/test_on_member_join.py``
  (``_assert_warning_logged_by_main``), adapted here since a successful
  kick is operationally significant enough to log at INFO, not just on
  failure.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import time_machine
from mom_bot.member_activity.models import MemberActivity
from mom_bot.member_activity.service import MemberActivityService
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base

_GUILD_ID = 300000000000000001
_MEMBER_ID = 200000000000000042
_SECOND_MEMBER_ID = 200000000000000099

# Anchor "now" for time_machine. joined_at values are computed relative to
# this so the 24h boundary is exercised deterministically.
_NOW = datetime.datetime(2026, 7, 19, 12, 0, 0, tzinfo=datetime.UTC)
_JOINED_25H_AGO = _NOW - datetime.timedelta(hours=25)
_JOINED_23H_AGO = _NOW - datetime.timedelta(hours=23)


# ---------------------------------------------------------------------------
# Deterministic synchronisation helper (mirrors _wait_for_send_count)
# ---------------------------------------------------------------------------


async def _wait_for_call_count(
    mock: AsyncMock,
    count: int,
    *,
    poll_interval: float = 0.005,
    timeout: float = 5.0,
) -> None:
    """Poll until ``mock.call_count >= count`` or ``timeout`` expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while mock.call_count < count:
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for call_count >= {count}; "
                f"got {mock.call_count} after {timeout}s"
            )
        await asyncio.sleep(poll_interval)


async def _run_for(task_factory: Any, seconds: float) -> None:
    """Run a freshly-created task for *seconds*, then cancel it cleanly."""
    task = asyncio.create_task(task_factory())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeBot:
    """Minimal stand-in for discord.Client — controls is_ready()."""

    def __init__(self, ready: bool = True) -> None:
        """Initialise with a readiness flag."""
        self._ready = ready

    def is_ready(self) -> bool:
        """Return the current readiness state."""
        return self._ready

    def set_ready(self, value: bool) -> None:
        """Flip the readiness flag."""
        self._ready = value


class FakeMember:
    """Minimal stand-in for discord.Member with DM-send and kick coroutines."""

    def __init__(self, discord_id: int) -> None:
        """Initialise with a snowflake id and fresh AsyncMocks."""
        self.id = discord_id
        self.send = AsyncMock()
        self.kick = AsyncMock()


class FakeGuild:
    """Minimal stand-in for discord.Guild — get_member (cache) / fetch_member (miss)."""

    def __init__(self) -> None:
        """Initialise with an empty member registry."""
        self._members: dict[int, FakeMember] = {}
        self._fetch_side_effects: dict[int, Exception] = {}

    def add_member(self, member: FakeMember) -> None:
        """Register a fake member so get_member() can find it."""
        self._members[member.id] = member

    def set_fetch_side_effect(self, discord_id: int, exc: Exception) -> None:
        """Configure fetch_member() to raise exc for a specific id."""
        self._fetch_side_effects[discord_id] = exc

    def get_member(self, discord_id: int) -> FakeMember | None:
        """Return a registered member or None (cache miss)."""
        return self._members.get(discord_id)

    async def fetch_member(self, discord_id: int) -> FakeMember:
        """Async fetch; raises configured side effect or returns member."""
        exc = self._fetch_side_effects.get(discord_id)
        if exc is not None:
            raise exc
        member = self._members.get(discord_id)
        if member is None:
            raise discord.NotFound(MagicMock(status=404, reason="Unknown Member"), "test")
        return member


def _make_forbidden() -> discord.Forbidden:
    """Build a real discord.Forbidden instance for use as a side effect."""
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, "Missing Permissions")


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with the member_activity table."""
    import mom_bot.member_activity.models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _make_session_factory(engine: Any) -> Any:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)


def _seed_member(
    engine: Any,
    *,
    member_id: int = _MEMBER_ID,
    guild_id: int = _GUILD_ID,
    joined_at: datetime.datetime = _JOINED_25H_AGO,
    first_message_at: datetime.datetime | None = None,
) -> None:
    """Insert a member_activity tracking row directly via the ORM."""
    from mom_bot.member_activity.models import MemberActivity

    with Session(engine) as session:
        session.add(
            MemberActivity(
                guild_id=guild_id,
                member_id=member_id,
                joined_at=joined_at.replace(tzinfo=None),
                first_message_at=(
                    first_message_at.replace(tzinfo=None) if first_message_at else None
                ),
            )
        )
        session.commit()


def _make_scheduler(
    bot: FakeBot,
    guild: FakeGuild,
    engine: Any,
    tick_seconds: float = 0.05,
) -> Any:
    """Convenience factory for an AutoKickScheduler with a fast tick."""
    from mom_bot.member_activity.scheduler import AutoKickScheduler

    return AutoKickScheduler(
        bot=bot,  # type: ignore[arg-type]
        guild=guild,  # type: ignore[arg-type]
        session_factory=_make_session_factory(engine),
        tick_seconds=tick_seconds,
    )


# ---------------------------------------------------------------------------
# Test A — stale member is DMed and kicked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_member_is_dmed_and_kicked() -> None:
    """A member past 24h with no message is DMed then kicked."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.send.assert_called_once()
    member.kick.assert_called_once()

    # DM body is a non-empty explanatory string; kick reason likewise.
    dm_body = member.send.call_args.args[0] if member.send.call_args.args else ""
    assert isinstance(dm_body, str) and dm_body.strip(), "Expected a non-empty DM body"

    kick_reason = member.kick.call_args.kwargs.get("reason", "")
    assert (
        isinstance(kick_reason, str) and kick_reason.strip()
    ), "Expected a non-empty kick reason (audit trail)"


# ---------------------------------------------------------------------------
# Test B — DM happens before kick (ordering)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_sent_before_kick_ordering() -> None:
    """The DM is attempted before the kick, per the AC's stated ordering."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    manager = MagicMock()
    manager.attach_mock(member.send, "send")
    manager.attach_mock(member.kick, "kick")

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    call_order = [c[0] for c in manager.mock_calls]
    assert call_order == [
        "send",
        "kick",
    ], f"Expected DM (send) before kick; got call order {call_order}"


# ---------------------------------------------------------------------------
# Test C — DM Forbidden (closed DMs) does not block the kick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_forbidden_does_not_block_kick() -> None:
    """A member with DMs closed is still kicked (DM failure is best-effort)."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    member.send.side_effect = _make_forbidden()
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.send.assert_called_once()
    member.kick.assert_called_once()


# ---------------------------------------------------------------------------
# Test D — a member who has posted is never kicked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_who_posted_is_not_kicked() -> None:
    """A member with a recorded first_message_at is excluded from the sweep."""
    engine = _make_engine()
    _seed_member(
        engine,
        joined_at=_JOINED_25H_AGO,
        first_message_at=_JOINED_25H_AGO + datetime.timedelta(hours=1),
    )

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.send.assert_not_called()
    member.kick.assert_not_called()


# ---------------------------------------------------------------------------
# Test E — a member within the 24h grace period is not kicked yet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_within_grace_period_is_not_kicked() -> None:
    """A member who joined 23h ago (no message) is not yet kicked."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_23H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.kick.assert_not_called()


# ---------------------------------------------------------------------------
# Test F — bot not ready: tick is skipped entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_ready_skips_tick_no_kick() -> None:
    """When the bot is not ready, no kick is attempted this tick."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=False)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.kick.assert_not_called()


# ---------------------------------------------------------------------------
# Test G — no double-kick across multiple ticks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_double_kick_across_ticks() -> None:
    """A stale member is kicked exactly once even across several ticks."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine, tick_seconds=0.05)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.2)  # several ticks

    assert (
        member.kick.call_count == 1
    ), f"Expected exactly one kick across multiple ticks; got {member.kick.call_count}"


# ---------------------------------------------------------------------------
# Test H — loop isolation: one member's kick failure doesn't block another's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kick_forbidden_for_one_member_does_not_block_another() -> None:
    """A Forbidden kick for member 1 does not prevent member 2's kick this tick."""
    engine = _make_engine()
    _seed_member(engine, member_id=_MEMBER_ID, joined_at=_JOINED_25H_AGO)
    _seed_member(engine, member_id=_SECOND_MEMBER_ID, joined_at=_JOINED_25H_AGO)

    first_member = FakeMember(_MEMBER_ID)
    first_member.kick.side_effect = _make_forbidden()
    second_member = FakeMember(_SECOND_MEMBER_ID)

    guild = FakeGuild()
    guild.add_member(first_member)
    guild.add_member(second_member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine, tick_seconds=0.05)

    with time_machine.travel(_NOW, tick=False):
        task = asyncio.create_task(scheduler.run())
        await _wait_for_call_count(second_member.kick, 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    second_member.kick.assert_called_once()


# ---------------------------------------------------------------------------
# Test I — successful kick is logged (audit trail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kick_success_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A successful kick emits an INFO-or-higher record naming the reason."""
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with caplog.at_level(logging.INFO, logger="mom_bot.member_activity.scheduler"):
        with time_machine.travel(_NOW, tick=False):
            await _run_for(scheduler.run, 0.12)

    matching = [
        r
        for r in caplog.records
        if r.levelno >= logging.INFO and r.name == "mom_bot.member_activity.scheduler"
    ]
    assert matching, (
        "Expected an INFO-or-higher log record from "
        "mom_bot.member_activity.scheduler after a successful kick, but "
        "none was emitted."
    )


# ---------------------------------------------------------------------------
# Test J — race A: a message posted during the DM window cancels the kick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_during_dm_window_prevents_kick() -> None:
    """A first message posted while the DM is in flight must cancel the kick.

    ``list_stale()`` hands the scheduler a detached snapshot. If the member
    posts their first message (``on_message`` commits ``first_message_at``)
    while the scheduler is awaiting DM delivery for that same member, the
    scheduler must re-verify eligibility before kicking rather than acting
    on the stale snapshot (#300 race A).
    """
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)
    service = MemberActivityService(_make_session_factory(engine))

    async def _post_message_during_dm(*args: Any, **kwargs: Any) -> None:
        # Simulates on_message committing first_message_at concurrently
        # with the scheduler's in-flight DM for this same member.
        service.record_first_message(_GUILD_ID, _MEMBER_ID, _NOW.replace(tzinfo=None))

    member = FakeMember(_MEMBER_ID)
    member.send.side_effect = _post_message_during_dm
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.send.assert_called_once()
    member.kick.assert_not_called()


# ---------------------------------------------------------------------------
# Test K — race B: a rejoin during the DM window cancels the kick and
# preserves the freshly-rejoined row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejoin_during_dm_window_prevents_kick_and_preserves_new_row() -> None:
    """A rejoin during the DM window must cancel the stale kick.

    ``record_join`` overwrites ``joined_at`` (and clears
    ``first_message_at``) on rejoin. If a member leaves and rejoins while
    the scheduler is mid-sweep on their OLD snapshot, the scheduler must not
    kick based on that stale snapshot, and must not delete the freshly
    rejoined tracking row (#300 race B).
    """
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)
    service = MemberActivityService(_make_session_factory(engine))
    rejoined_at = _NOW.replace(tzinfo=None)

    async def _rejoin_during_dm(*args: Any, **kwargs: Any) -> None:
        # Simulates on_member_join's record_join overwriting joined_at for
        # a leave+rejoin during the DM/kick window for the SAME row.
        service.record_join(_GUILD_ID, _MEMBER_ID, rejoined_at)

    member = FakeMember(_MEMBER_ID)
    member.send.side_effect = _rejoin_during_dm
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    member.send.assert_called_once()
    member.kick.assert_not_called()

    with Session(engine) as session:
        row = session.execute(
            select(MemberActivity).where(
                MemberActivity.guild_id == _GUILD_ID,
                MemberActivity.member_id == _MEMBER_ID,
            )
        ).scalar_one_or_none()
    assert row is not None, "Rejoined row must not be deleted by the stale sweep"
    assert row.joined_at == rejoined_at, "Rejoined row's joined_at must be untouched"
    assert row.first_message_at is None


# ---------------------------------------------------------------------------
# Test L — cleanup failure must not crash _process_member (fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_crash_process_member() -> None:
    """A ``remove_tracking`` failure in the finally block must not propagate.

    The kick itself must still have been attempted -- only the terminal
    cleanup call is failing (#300 fix 3).
    """
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    member = FakeMember(_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        stale = scheduler._service.list_stale(_NOW.replace(tzinfo=None))
        assert len(stale) == 1
        activity = stale[0]

        scheduler._service.remove_tracking = MagicMock(side_effect=RuntimeError("db unavailable"))

        # Must not raise -- a cleanup failure is not the caller's problem.
        await scheduler._process_member(activity)

    member.kick.assert_called_once()


# ---------------------------------------------------------------------------
# Test M — cleanup failure for one member must not block the next member in
# the same tick (fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_block_next_stale_member_same_tick() -> None:
    """A ``remove_tracking`` failure must not kill the ``run()`` sweep loop.

    Both stale members must still be processed in the same tick even though
    cleanup raises for every member processed (#300 fix 3).
    """
    engine = _make_engine()
    _seed_member(engine, member_id=_MEMBER_ID, joined_at=_JOINED_25H_AGO)
    _seed_member(engine, member_id=_SECOND_MEMBER_ID, joined_at=_JOINED_25H_AGO)

    first_member = FakeMember(_MEMBER_ID)
    second_member = FakeMember(_SECOND_MEMBER_ID)
    guild = FakeGuild()
    guild.add_member(first_member)
    guild.add_member(second_member)
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine, tick_seconds=0.05)
    scheduler._service.remove_tracking = MagicMock(side_effect=RuntimeError("db unavailable"))

    # Confined to a single tick (duration < tick_seconds): remove_tracking
    # never actually deletes the rows in this test (it always raises), so a
    # second tick would find both members still stale and kick them again --
    # that would be a correct consequence of fix 3, not a failure of it, and
    # would make a call_count-based assertion tick-count-dependent.
    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.03)

    first_member.kick.assert_called_once()
    second_member.kick.assert_called_once()


# ---------------------------------------------------------------------------
# Test N — a transient Forbidden from fetch_member must not remove tracking
# (fix 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_member_forbidden_does_not_remove_tracking() -> None:
    """A transient ``discord.Forbidden`` from ``fetch_member`` must be retried.

    Unlike ``discord.NotFound`` (the member genuinely left), a transient
    permission/network blip must leave the tracking row intact so the next
    sweep tick retries the member instead of permanently exempting them
    (#300 fix 4).
    """
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    guild = FakeGuild()  # member never registered -> get_member() misses
    guild.set_fetch_side_effect(_MEMBER_ID, _make_forbidden())
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    with Session(engine) as session:
        row = session.execute(
            select(MemberActivity).where(
                MemberActivity.guild_id == _GUILD_ID,
                MemberActivity.member_id == _MEMBER_ID,
            )
        ).scalar_one_or_none()
    assert row is not None, "A transient Forbidden must not remove the tracking row"
    assert row.joined_at == _JOINED_25H_AGO.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Test O — discord.NotFound from fetch_member remains terminal (contrast
# with the transient Forbidden case above)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_member_not_found_removes_tracking() -> None:
    """``fetch_member`` raising ``NotFound`` (member genuinely left) is terminal.

    Contrast with ``test_fetch_member_forbidden_does_not_remove_tracking``:
    only a confirmed-gone member (or an actual kick attempt) should remove
    the tracking row (#300 fix 4).
    """
    engine = _make_engine()
    _seed_member(engine, joined_at=_JOINED_25H_AGO)

    guild = FakeGuild()  # member never registered; fetch_member -> NotFound
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, guild, engine)

    with time_machine.travel(_NOW, tick=False):
        await _run_for(scheduler.run, 0.12)

    with Session(engine) as session:
        row = session.execute(
            select(MemberActivity).where(
                MemberActivity.guild_id == _GUILD_ID,
                MemberActivity.member_id == _MEMBER_ID,
            )
        ).scalar_one_or_none()
    assert row is None, "NotFound (member already left) must remove the tracking row"
