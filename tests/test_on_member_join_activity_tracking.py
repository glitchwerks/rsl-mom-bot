"""Tests for join-tracking persistence in ``MomBot.on_member_join`` (#300).

TDD: written before the join-tracking behaviour exists. The welcome-message
side of ``on_member_join`` is already implemented (#299) and is covered,
frozen, by ``tests/test_on_member_join.py`` — this file adds a NEW,
independent slice of coverage for the auto-kick feature's join-persistence
requirement without touching that frozen file.

Mocking conventions mirror ``tests/test_on_member_join.py`` exactly
(``FakeChannel``, ``_make_member``, patching ``mom_bot.main.load_secret``)
plus the ``mom_bot.main._build_session_factory`` patching convention from
``tests/test_main_wireup.py`` (in-memory SQLite + ``Base.metadata.create_all``,
no Alembic).

Binding contract decisions:

- Join tracking is recorded via ``mom_bot.main._build_session_factory()``
  (patchable exactly like every other DB-touching handler in ``main.py`` —
  ``_start_reminders_after_ready``, ``on_ready``, ``_start_sidecar``).
  Assertions query the ``member_activity`` table directly rather than
  mocking a service call, per the frozen-contract guidance to assert
  observable behaviour, not internal structure.
- Join tracking is scoped to the target guild (``bot.guild``) and skips
  bot accounts — the SAME early-return guards ``on_member_join`` already
  has for the welcome message (bot check first, then guild-match check).
  This mirrors Tests 2/8/9 in ``tests/test_on_member_join.py`` exactly, but
  asserts DB state instead of mock-call absence.
- These tests do not pin the exact stored ``joined_at`` value (e.g. whether
  the implementation uses ``member.joined_at`` or ``datetime.now(UTC)`` at
  handler time) — only that a row exists with the correct
  ``(guild_id, member_id)`` and a NULL ``first_message_at``. The precise
  timestamp source is intentionally left to the implementer; exact-value
  assertions live in ``tests/member_activity/test_service.py`` where the
  test fully controls the input.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base

_NEW_MEMBERS_CHANNEL_ID = 555555555555555555
_MEMBER_ID = 200000000000000042
_TARGET_GUILD_ID = 300000000000000001
_OTHER_GUILD_ID = 300000000000000099


class FakeChannel:
    """Minimal discord.TextChannel stand-in with a recorded send."""

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake."""
        self.id = channel_id
        self.send = AsyncMock()


def _make_member(
    *,
    is_bot: bool = False,
    guild_id: int | None = None,
    joined_at: datetime.datetime | None = None,
) -> MagicMock:
    """Build a minimal ``discord.Member`` mock with a ``joined_at`` field.

    Args:
        is_bot: Whether the joining account is itself a bot.
        guild_id: If given, sets ``member.guild.id``.
        joined_at: If given, sets ``member.joined_at`` (a real
            ``discord.Member`` always has this populated on join).

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Member``.
    """
    member = MagicMock(spec=discord.Member)
    member.id = _MEMBER_ID
    member.mention = f"<@{_MEMBER_ID}>"
    member.bot = is_bot
    member.joined_at = joined_at or datetime.datetime(2026, 7, 19, tzinfo=datetime.UTC)
    if guild_id is not None:
        member.guild.id = guild_id
    return member


def _fake_load_secret(name: str) -> str:
    """Stand-in for ``mom_bot.config.load_secret`` during these tests."""
    assert name == "new-members-channel-id"
    return str(_NEW_MEMBERS_CHANNEL_ID)


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


# ---------------------------------------------------------------------------
# Test 1 — human join in target guild persists a tracking row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_join_persists_member_activity_row() -> None:
    """A human join in the target guild inserts a member_activity row."""
    from mom_bot.member_activity.models import MemberActivity

    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=session_factory),
    ):
        await bot.on_member_join(member)

    with Session(engine) as session:
        rows = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).all()

    assert len(rows) == 1, f"Expected exactly one tracking row; got {len(rows)}"
    assert rows[0].guild_id == _TARGET_GUILD_ID
    assert rows[0].first_message_at is None


# ---------------------------------------------------------------------------
# Test 2 — bot-account joins are never tracked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_account_join_is_not_tracked() -> None:
    """A bot-account join must not create a member_activity row.

    Mirrors ``test_on_member_join_skips_bot_accounts`` — bot-account joins
    already short-circuit before any DB or channel call.
    """
    from mom_bot.member_activity.models import MemberActivity

    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=True, guild_id=_TARGET_GUILD_ID)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=session_factory),
    ):
        await bot.on_member_join(member)

    with Session(engine) as session:
        count = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).count()
    assert count == 0


# ---------------------------------------------------------------------------
# Test 3 — join in a non-target guild is not tracked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guild_mismatched_join_is_not_tracked() -> None:
    """A join in a guild other than the configured target is not tracked.

    Mirrors ``test_on_member_join_returns_when_guild_mismatched``.
    """
    from mom_bot.member_activity.models import MemberActivity

    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_OTHER_GUILD_ID)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=session_factory),
    ):
        await bot.on_member_join(member)

    with Session(engine) as session:
        count = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).count()
    assert count == 0


# ---------------------------------------------------------------------------
# Test 4 — target guild not yet configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_before_target_guild_configured_is_not_tracked() -> None:
    """A join arriving before ``bot.guild`` is configured is not tracked.

    Mirrors ``test_on_member_join_returns_when_target_guild_not_configured``.
    """
    from mom_bot.member_activity.models import MemberActivity

    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = None
    member = _make_member(is_bot=False)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=session_factory),
    ):
        await bot.on_member_join(member)

    with Session(engine) as session:
        count = session.query(MemberActivity).filter_by(member_id=_MEMBER_ID).count()
    assert count == 0
