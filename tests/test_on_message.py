"""Tests for ``MomBot.on_message`` — first-message activity tracking (#300).

TDD: written before ``MomBot.on_message`` exists. ``discord.Client`` does not
define ``on_message`` itself (it is an optional user override dispatched by
the gateway), so calling ``bot.on_message(...)`` before this feature lands
raises ``AttributeError`` — that is the expected red, not a fixture bug.

Design mirrors ``tests/test_on_member_join_activity_tracking.py`` (same
``mom_bot.main._build_session_factory`` patching convention, in-memory
SQLite + ``Base.metadata.create_all``) and
``tests/test_on_member_join.py`` (same guild-scoping guard style: bot check,
then guild-match check, before any DB work).

Binding contract decisions:

- ``on_message`` ignores any message whose ``author.bot`` is ``True`` —
  covers both other bot accounts and the client's own messages (e.g. the
  #299 welcome message the bot itself posts). Per #300's technical notes,
  a member merely reacting to or receiving the welcome message is not
  activity in the first place, since no ``on_reaction_add`` hook is wired —
  only a genuine message authored by the member counts, and this test suite
  verifies that ONLY calling ``on_message`` with a member-authored message
  records activity.
- A DM message (``message.guild is None``) is ignored — there is no guild
  to scope against, and DMs are outside a guild's auto-kick sweep.
- Guild-scoping mirrors ``on_member_join``: a message in a non-target guild,
  or arriving before ``bot.guild`` is configured, is ignored.
- The recorded ``at`` timestamp source (e.g. ``message.created_at``) is not
  pinned here — these tests only assert presence/absence of a first-message
  timestamp and, for the ordering test, that the value does not change on a
  second call. Exact-value semantics are covered at the service level
  (``tests/member_activity/test_service.py``), which fully controls the
  input.
- These tests seed an existing ``member_activity`` join row directly via the
  ORM before calling ``on_message`` (rather than going through
  ``on_member_join`` first) to keep this file's fixtures independent of the
  join-tracking wiring under test in the sibling file.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base

_MEMBER_ID = 200000000000000042
_TARGET_GUILD_ID = 300000000000000001
_OTHER_GUILD_ID = 300000000000000099
_JOINED_AT = datetime.datetime(2026, 7, 18, 0, 0, 0)


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


def _seed_join(
    engine: Any, *, guild_id: int = _TARGET_GUILD_ID, member_id: int = _MEMBER_ID
) -> None:
    """Insert a pre-existing member_activity row (simulates a prior join)."""
    from mom_bot.member_activity.models import MemberActivity

    with Session(engine) as session:
        session.add(MemberActivity(guild_id=guild_id, member_id=member_id, joined_at=_JOINED_AT))
        session.commit()


def _get_first_message_at(engine: Any, member_id: int = _MEMBER_ID) -> Any:
    """Return the stored ``first_message_at`` for *member_id*, or a sentinel.

    Returns:
        The stored value, or the string ``"NO_ROW"`` if no row exists for
        the member (distinguishable from a real ``None``/NULL value).
    """
    from mom_bot.member_activity.models import MemberActivity

    with Session(engine) as session:
        row = session.query(MemberActivity).filter_by(member_id=member_id).one_or_none()
    if row is None:
        return "NO_ROW"
    return row.first_message_at


def _make_message(
    *,
    author_is_bot: bool = False,
    author_id: int = _MEMBER_ID,
    guild_id: int | None = _TARGET_GUILD_ID,
    created_at: datetime.datetime | None = None,
) -> MagicMock:
    """Build a minimal ``discord.Message`` mock.

    Args:
        author_is_bot: Whether ``message.author.bot`` is ``True``.
        author_id: The message author's snowflake.
        guild_id: If given, sets ``message.guild.id``; if ``None``,
            ``message.guild`` is set to ``None`` (a DM).
        created_at: The message's creation timestamp.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Message``.
    """
    message = MagicMock(spec=discord.Message)
    message.author = MagicMock(spec=discord.Member)
    message.author.bot = author_is_bot
    message.author.id = author_id
    message.created_at = created_at or datetime.datetime(2026, 7, 19, tzinfo=datetime.UTC)
    if guild_id is None:
        message.guild = None
    else:
        message.guild = MagicMock()
        message.guild.id = guild_id
    return message


# ---------------------------------------------------------------------------
# Test 1 — a member's first message records activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_message_records_first_activity() -> None:
    """A member's message in the target guild sets first_message_at."""
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    _seed_join(engine)
    session_factory = _make_session_factory(engine)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    message = _make_message(author_is_bot=False, guild_id=_TARGET_GUILD_ID)

    with patch("mom_bot.main._build_session_factory", return_value=session_factory):
        await bot.on_message(message)

    assert _get_first_message_at(engine) is not None
    assert _get_first_message_at(engine) != "NO_ROW"


# ---------------------------------------------------------------------------
# Test 2 — idempotent: second message does not move first_message_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_message_does_not_change_first_message_at() -> None:
    """A second member message does not overwrite the recorded first one."""
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    _seed_join(engine)
    session_factory = _make_session_factory(engine)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)

    first_at = datetime.datetime(2026, 7, 19, 1, 0, 0, tzinfo=datetime.UTC)
    second_at = datetime.datetime(2026, 7, 19, 5, 0, 0, tzinfo=datetime.UTC)

    with patch("mom_bot.main._build_session_factory", return_value=session_factory):
        await bot.on_message(_make_message(guild_id=_TARGET_GUILD_ID, created_at=first_at))
        recorded_after_first = _get_first_message_at(engine)

        await bot.on_message(_make_message(guild_id=_TARGET_GUILD_ID, created_at=second_at))
        recorded_after_second = _get_first_message_at(engine)

    assert recorded_after_first == recorded_after_second, (
        "Expected first_message_at to remain unchanged after a second "
        f"message; got {recorded_after_first!r} then {recorded_after_second!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — the bot's own message is ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_own_message_is_ignored() -> None:
    """A message authored by a bot account does not record activity."""
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    _seed_join(engine)
    session_factory = _make_session_factory(engine)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    message = _make_message(author_is_bot=True, guild_id=_TARGET_GUILD_ID)

    with patch("mom_bot.main._build_session_factory", return_value=session_factory):
        await bot.on_message(message)

    assert (
        _get_first_message_at(engine) is None
    ), "A bot-authored message must not mark the tracked member active"


# ---------------------------------------------------------------------------
# Test 4 — a DM (no guild) does not crash and is ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_message_is_ignored_without_crashing() -> None:
    """A DM (message.guild is None) is ignored, not treated as an error."""
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    _seed_join(engine)
    session_factory = _make_session_factory(engine)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    message = _make_message(guild_id=None)

    with patch("mom_bot.main._build_session_factory", return_value=session_factory):
        await bot.on_message(message)  # must not raise

    assert _get_first_message_at(engine) is None


# ---------------------------------------------------------------------------
# Test 5 — a message in a non-target guild is ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_in_non_target_guild_is_ignored() -> None:
    """A message in a guild other than the configured target is ignored."""
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    _seed_join(engine, guild_id=_OTHER_GUILD_ID)
    session_factory = _make_session_factory(engine)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    message = _make_message(guild_id=_OTHER_GUILD_ID)

    with patch("mom_bot.main._build_session_factory", return_value=session_factory):
        await bot.on_message(message)

    assert _get_first_message_at(engine) is None


# ---------------------------------------------------------------------------
# Test 6 — target guild not yet configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_before_target_guild_configured_is_ignored() -> None:
    """A message arriving before ``bot.guild`` is configured is ignored."""
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    _seed_join(engine)
    session_factory = _make_session_factory(engine)

    bot = MomBot(intents=build_intents())
    bot.guild = None
    message = _make_message(guild_id=_TARGET_GUILD_ID)

    with patch("mom_bot.main._build_session_factory", return_value=session_factory):
        await bot.on_message(message)

    assert _get_first_message_at(engine) is None
