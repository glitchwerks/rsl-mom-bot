"""Tests for MomBot.on_member_join officer DM fan-out (#301).

Extends the ``on_member_join`` handler introduced in #299
(``tests/test_on_member_join.py``, frozen and not modified here) with the
officer join-alert notification behaviour from #301: every officer
currently subscribed via ``/notify-new-members`` receives a DM naming the
new member, and a single subscriber's ``discord.Forbidden`` does not
block delivery to the others.

TDD: written before any #301 implementation exists — see the authoring
return for the exact red confirmation per file.

Design/seam notes
------------------
- Seam: mirrors the ``_build_session_factory`` patch seam already used by
  ``on_ready`` (day-role-map seed, ``tests/test_main_wireup.py``) and
  ``setup_hook`` rather than inventing a new
  ``bot._new_member_alert_service`` instance attribute.  ``on_member_join``
  is expected to build its own ``NewMemberAlertService`` inline via
  ``_build_session_factory(_resolve_db_url())``, exactly like ``on_ready``
  does for ``seed_day_role_map``.  Tests patch
  ``mom_bot.main._build_session_factory`` to return an in-memory SQLite
  sessionmaker, then seed real subscription rows through the real
  ``NewMemberAlertService`` — asserting on observable DM sends, not on
  service call arguments.  This is a binding contract for these tests
  (flagged per the frozen-test convention in ``tests/test_on_member_join.py``
  § "Binding contract decision") — an implementation that only builds the
  service inside ``setup_hook``/stores it on a different attribute name
  will fail these tests; that is a scope call for the router/spec-owner to
  ratify or override, not something to silently work around by editing
  this file.
- DM recipient resolution mirrors the existing per-member DM pattern in
  ``mom_bot/reminders/scheduler.py::_handle_member_notification``:
  ``member.guild.get_member(int(user_id))`` then ``officer.send(message)``.
  This is the one repo precedent for "resolve a Discord ID to a sendable
  target then DM it" and is treated as a binding contract here too — an
  implementation using ``bot.get_user``/``bot.fetch_user`` instead will
  fail these tests.
- The welcome-channel path (#299) is made to fully succeed in every test
  here (working ``load_secret`` + ``get_channel`` returning a working
  ``FakeChannel``) so control reaches whatever point the officer-DM step
  is wired at, regardless of the implementer's chosen ordering relative
  to the welcome message. No test here asserts on relative ordering
  between the welcome send and the officer DMs, or on behavior when the
  welcome-channel path itself fails — that would pin an internal
  implementation detail the AC does not specify.
- Per issue #301 scope, only ``discord.Forbidden`` is tested as a
  per-subscriber delivery failure — no broader error taxonomy
  (NotFound/rate-limit/5xx) is in scope, mirroring the reminder
  scheduler's fuller taxonomy being explicitly out of scope for this
  issue.
- Log assertion for the Forbidden case is deliberately NOT pinned to the
  ``mom_bot.main`` logger name (unlike the frozen #299 tests in
  ``tests/test_on_member_join.py``) because the officer-DM helper may
  reasonably live in ``mom_bot.new_member_alerts`` instead of inline in
  ``main.py``; only "some WARNING-or-higher record was emitted" is
  asserted — the load-bearing assertion is that the *other* subscribers
  still get DMed.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from mom_bot.new_member_alerts.service import NewMemberAlertService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base

_NEW_MEMBERS_CHANNEL_ID = 555555555555555555
_MEMBER_ID = 200000000000000042
_TARGET_GUILD_ID = 300000000000000001
_OTHER_GUILD_ID = 300000000000000099
_OFFICER_A = 111111111111111111
_OFFICER_B = 222222222222222222
_OFFICER_C = 333333333333333333


# ---------------------------------------------------------------------------
# Helpers — mirrors tests/test_on_member_join.py's fixture style
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal discord.TextChannel stand-in with a recorded send."""

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake.

        Args:
            channel_id: The Discord channel snowflake this fake represents.
        """
        self.id = channel_id
        self.send = AsyncMock()


def _make_member(*, guild_id: int = _TARGET_GUILD_ID) -> MagicMock:
    """Build a minimal human, target-guild discord.Member mock.

    Args:
        guild_id: The guild snowflake the member joined, set on
            ``member.guild.id``.

    Returns:
        A MagicMock with ``spec=discord.Member``.
    """
    member = MagicMock(spec=discord.Member)
    member.id = _MEMBER_ID
    member.mention = f"<@{_MEMBER_ID}>"
    member.bot = False
    member.guild = MagicMock()
    member.guild.id = guild_id
    return member


def _make_officer(discord_id: int) -> MagicMock:
    """Build a fake resolved officer target with a recorded async send.

    Args:
        discord_id: The officer's Discord snowflake.

    Returns:
        A MagicMock standing in for the resolved DM-sendable target
        (e.g. a ``discord.Member``), with ``.send`` as an ``AsyncMock``.
    """
    officer = MagicMock(spec=discord.Member)
    officer.id = discord_id
    officer.send = AsyncMock()
    return officer


def _fake_load_secret(name: str) -> str:
    """Stand-in for ``mom_bot.config.load_secret`` during these tests.

    Args:
        name: The unprefixed secret name being requested.

    Returns:
        The configured new-members channel ID as a string.
    """
    assert name == "new-members-channel-id", (
        f"Expected on_member_join to request the " f"'new-members-channel-id' secret; got {name!r}"
    )
    return str(_NEW_MEMBERS_CHANNEL_ID)


def _make_forbidden() -> discord.Forbidden:
    """Build a real ``discord.Forbidden`` instance for use as a side effect.

    ``discord.Forbidden`` requires ``response.status``/``response.reason``
    at construction, so a bare string side effect would raise ``TypeError``
    from the fixture itself rather than producing the intended red.

    Returns:
        A constructed ``discord.Forbidden`` carrying a 403 mock response.
    """
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, "Missing Permissions")


def _make_session_factory() -> sessionmaker:
    """Return a sessionmaker bound to a fresh in-memory SQLite engine."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_subscribers(factory: sessionmaker, guild_id: int, *user_ids: int) -> None:
    """Seed real subscription rows via NewMemberAlertService.

    Args:
        factory: The sessionmaker to construct the service with.
        guild_id: The guild snowflake to subscribe under.
        *user_ids: Officer Discord snowflakes to subscribe.
    """
    service = NewMemberAlertService(session_factory=factory)
    for uid in user_ids:
        service.set_subscription(str(guild_id), str(uid), enabled=True)


def _sent_message(send_mock: AsyncMock) -> str:
    """Extract the message text from a recorded ``.send`` call.

    Accepts either a positional content arg or a ``content=`` kwarg — both
    are valid discord.py call shapes and neither is prescribed by the AC.

    Args:
        send_mock: The ``AsyncMock`` standing in for ``.send``.

    Returns:
        The message text passed to ``.send``.
    """
    call_args = send_mock.call_args
    return call_args.args[0] if call_args.args else call_args.kwargs.get("content", "")


# ---------------------------------------------------------------------------
# AC unit test #2 — join event DMs all current subscribers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_dms_all_subscribed_officers() -> None:
    """Every officer subscribed for the joining member's guild is DMed."""
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A, _OFFICER_B)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A)
    officer_b = _make_officer(_OFFICER_B)
    officers = {_OFFICER_A: officer_a, _OFFICER_B: officer_b}
    member.guild.get_member = MagicMock(side_effect=lambda uid: officers.get(uid))

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=factory),
    ):
        await bot.on_member_join(member)

    officer_a.send.assert_awaited_once()
    officer_b.send.assert_awaited_once()

    for officer in (officer_a, officer_b):
        message = _sent_message(officer.send)
        assert member.mention in message, (
            f"Expected officer DM to name the new member {member.mention!r}; " f"got {message!r}"
        )


@pytest.mark.asyncio
async def test_on_member_join_no_subscribers_sends_no_dms() -> None:
    """No subscribed officers means no DM attempts and no crash."""
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()  # no subscriptions seeded

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member()
    member.guild.get_member = MagicMock(return_value=None)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=factory),
    ):
        await bot.on_member_join(member)  # must not raise

    member.guild.get_member.assert_not_called()


# ---------------------------------------------------------------------------
# AC unit test #4 — one subscriber's Forbidden doesn't stop the others
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_forbidden_for_one_subscriber_does_not_block_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A discord.Forbidden for one subscriber must not stop the others' DMs."""
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A, _OFFICER_B, _OFFICER_C)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A)
    officer_b = _make_officer(_OFFICER_B)
    officer_b.send.side_effect = _make_forbidden()
    officer_c = _make_officer(_OFFICER_C)
    officers = {_OFFICER_A: officer_a, _OFFICER_B: officer_b, _OFFICER_C: officer_c}
    member.guild.get_member = MagicMock(side_effect=lambda uid: officers.get(uid))

    with caplog.at_level(logging.WARNING):
        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch.object(bot, "get_channel", return_value=fake_channel),
            patch("mom_bot.main._build_session_factory", return_value=factory),
        ):
            await bot.on_member_join(member)  # must not raise

    officer_a.send.assert_awaited_once()
    officer_b.send.assert_awaited_once()  # attempted, even though it raised
    officer_c.send.assert_awaited_once()

    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "Expected a WARNING-or-higher log record when a subscriber's DM "
        "delivery fails with discord.Forbidden."
    )


# ---------------------------------------------------------------------------
# Existing #299 guards must also short-circuit the new officer-DM step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_bot_account_skips_officer_dms() -> None:
    """A bot-account join must not trigger officer DMs either.

    Mirrors the bot-skip assertion style of test_on_member_join.py Test 2:
    the bot check must short-circuit before any new-member-alert machinery
    runs at all.
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member()
    member.bot = True
    member.guild.get_member = MagicMock()

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret) as load_secret_mock,
        patch("mom_bot.main._build_session_factory", return_value=factory) as factory_mock,
    ):
        await bot.on_member_join(member)

    load_secret_mock.assert_not_called()
    member.guild.get_member.assert_not_called()
    factory_mock.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_join_guild_mismatch_skips_officer_dms() -> None:
    """A join in a non-target guild must not trigger officer DMs.

    Mirrors the guild-scoping assertion style of test_on_member_join.py
    Test 8.
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _OTHER_GUILD_ID, _OFFICER_A)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(guild_id=_OTHER_GUILD_ID)
    member.guild.get_member = MagicMock()

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret) as load_secret_mock,
        patch("mom_bot.main._build_session_factory", return_value=factory) as factory_mock,
    ):
        await bot.on_member_join(member)

    load_secret_mock.assert_not_called()
    member.guild.get_member.assert_not_called()
    factory_mock.assert_not_called()
