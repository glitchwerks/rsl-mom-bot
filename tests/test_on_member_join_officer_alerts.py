"""Tests for MomBot.on_member_join officer DM fan-out (#301).

Extends the ``on_member_join`` handler introduced in #299
(``tests/test_on_member_join.py``, frozen and not modified here) with the
officer join-alert notification behaviour from #301: every officer
currently subscribed via ``/notify-new-members`` receives a DM naming the
new member, and a single subscriber's delivery failure does not block
delivery to the others.

TDD: written before any #301 implementation exists — see the authoring
return for the exact red confirmation per file. This revision layers in
three adversarial-review fixes (Codex review) on top of the original
#301 contract; see each section below for the fix it covers.

Design/seam notes
------------------
- DM recipient resolution mirrors the existing per-member DM pattern in
  ``mom_bot/reminders/scheduler.py::_handle_member_notification``:
  ``member.guild.get_member(int(user_id))`` then ``officer.send(message)``.
  This is the one repo precedent for "resolve a Discord ID to a sendable
  target then DM it" and is treated as a binding contract here too — an
  implementation using ``bot.get_user``/``bot.fetch_user`` instead will
  fail these tests.
- Per issue #301 scope, only per-subscriber delivery failures
  (``discord.Forbidden``, ``discord.HTTPException`` — see the fix-3
  section below) are tested — no broader error taxonomy (NotFound/5xx
  distinctions beyond HTTPException) is in scope, mirroring the reminder
  scheduler's fuller taxonomy being explicitly out of scope for this
  issue.
- Log assertions for per-subscriber delivery failures are deliberately
  NOT pinned to the ``mom_bot.main`` logger name (unlike the frozen #299
  tests in ``tests/test_on_member_join.py``) because the officer-DM
  helper may reasonably live in ``mom_bot.new_member_alerts`` instead of
  inline in ``main.py``; only "some WARNING-or-higher record was
  emitted" is asserted — the load-bearing assertion is that the *other*
  subscribers still get DMed.

Session-factory-reuse contract (fix 1 — resource-exhaustion review;
mirrors the #300 branch's ``on_message``/``on_member_join`` fix)
---------------------------------------------------------------------
The officer-DM fan-out fires on every member join and must NOT build a
fresh SQLAlchemy engine/connection pool (and, for Postgres, a fresh
``ManagedIdentityCredential``) per join event. ``setup_hook`` is
responsible for building ONE shared factory and storing it on
``bot._db_session_factory`` (see the new ``setup_hook`` coverage in
``tests/test_main_wireup.py``); ``on_member_join`` must reuse that
attribute instead of calling ``mom_bot.main._build_session_factory``
itself. Every test below that reaches the officer-DM step therefore:

1. Sets ``bot._db_session_factory`` directly on the bot instance —
   standing in for what ``setup_hook`` does in production — bypassing
   ``setup_hook`` itself, which is out of scope for this file.
2. Patches ``mom_bot.main._build_session_factory`` with
   ``return_value=<the same factory>`` (so a not-yet-fixed
   implementation that still builds its own factory inline continues to
   work end-to-end and the *other* behavioural assertions stay green)
   and additionally asserts ``assert_not_called()`` — this is the
   load-bearing assertion for fix 1: an implementation that still calls
   ``_build_session_factory`` from within ``on_member_join`` fails it,
   independent of whether the DMs themselves went out correctly.

Delivery-time permission check (fix 2)
---------------------------------------
Before DMing a resolved subscriber, the handler must re-check
``officer.guild_permissions.manage_guild is True`` and skip (not DM, not
error) a subscriber who no longer holds it — mirroring the
``interaction.user.guild_permissions.manage_guild`` mock convention
already used in ``tests/new_member_alerts/test_commands.py`` and
``tests/member_notifications/test_commands.py``. ``_make_officer`` below
grows a ``manage_guild`` keyword (default ``True``, matching the
existing officers used across the rest of this file) so the new
permission test can construct a subscriber lacking the permission
without disturbing the other tests' fixtures.

Missing-HTTPException-catch + welcome/officer-DM decoupling (fix 3)
----------------------------------------------------------------------
- Fix 3A: the officer-DM loop must catch ``discord.HTTPException`` (not
  just ``discord.Forbidden``) per subscriber, so a transient
  rate-limit/5xx from one ``officer.send()`` does not abort delivery to
  the rest of the loop. Mirrors the existing Forbidden-does-not-block
  test with a plain ``discord.HTTPException`` side effect instead.
- Fix 3B: the welcome-channel path (#299) and the officer-DM fan-out are
  independent notification paths past the shared bot/guild-scoping
  gates at the top of the handler — a welcome-message failure (channel
  not found, or a ``Forbidden``/``HTTPException`` on the welcome send)
  must NOT prevent the officer-DM fan-out from running. Most tests in
  this file still make the welcome-channel path fully succeed (a
  working ``load_secret`` + ``get_channel`` returning a working
  ``FakeChannel``) simply because that is the common case and is
  orthogonal to what each test is asserting; the two decoupling tests
  below deliberately fail the welcome-message path to prove the
  officer-DM fan-out runs independently of it.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base
from mom_bot.new_member_alerts.service import NewMemberAlertService

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


def _make_officer(discord_id: int, *, manage_guild: bool = True) -> MagicMock:
    """Build a fake resolved officer target with a recorded async send.

    Args:
        discord_id: The officer's Discord snowflake.
        manage_guild: Value for ``officer.guild_permissions.manage_guild``
            (fix 2's delivery-time permission gate). Defaults to ``True``
            so existing tests that don't care about the permission check
            are unaffected.

    Returns:
        A MagicMock standing in for the resolved DM-sendable target
        (e.g. a ``discord.Member``), with ``.send`` as an ``AsyncMock``.
    """
    officer = MagicMock(spec=discord.Member)
    officer.id = discord_id
    officer.send = AsyncMock()
    officer.guild_permissions.manage_guild = manage_guild
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


def _make_http_exception() -> discord.HTTPException:
    """Build a real ``discord.HTTPException`` for use as a side effect.

    Deliberately NOT a ``discord.Forbidden`` (which is already caught by
    the pre-fix-3 implementation) — this represents the transient
    rate-limit/5xx class of failure fix 3A adds a catch for.

    Returns:
        A constructed ``discord.HTTPException`` carrying a 503 mock
        response, distinct in type from ``discord.Forbidden``.
    """
    response = MagicMock()
    response.status = 503
    response.reason = "Service Unavailable"
    return discord.HTTPException(response, "Service Unavailable")


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
    """Every officer subscribed for the joining member's guild is DMed.

    Also covers fix 1: the shared ``bot._db_session_factory`` must be
    reused, and ``_build_session_factory`` must NOT be called from
    within ``on_member_join``.
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A, _OFFICER_B)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    bot._db_session_factory = factory
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A)
    officer_b = _make_officer(_OFFICER_B)
    officers = {_OFFICER_A: officer_a, _OFFICER_B: officer_b}
    member.guild.get_member = MagicMock(side_effect=lambda uid: officers.get(uid))

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        # return_value keeps a not-yet-fixed implementation (that still
        # builds its own factory inline) fully functional end-to-end —
        # assert_not_called() below is the load-bearing fix-1 assertion.
        patch("mom_bot.main._build_session_factory", return_value=factory) as mock_build_factory,
    ):
        await bot.on_member_join(member)

    mock_build_factory.assert_not_called()

    officer_a.send.assert_awaited_once()
    officer_b.send.assert_awaited_once()

    for officer in (officer_a, officer_b):
        message = _sent_message(officer.send)
        assert member.mention in message, (
            f"Expected officer DM to name the new member {member.mention!r}; " f"got {message!r}"
        )


@pytest.mark.asyncio
async def test_on_member_join_no_subscribers_sends_no_dms() -> None:
    """No subscribed officers means no DM attempts and no crash.

    Also covers fix 1's factory-reuse contract (see module docstring).
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()  # no subscriptions seeded

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    bot._db_session_factory = factory
    member = _make_member()
    member.guild.get_member = MagicMock(return_value=None)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=factory) as mock_build_factory,
    ):
        await bot.on_member_join(member)  # must not raise

    mock_build_factory.assert_not_called()
    member.guild.get_member.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 2 — delivery-time manage_guild permission check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_skips_officer_without_manage_guild_permission() -> None:
    """A resolvable subscriber lacking manage_guild is skipped, not DMed.

    ``officer_a`` is a current, resolvable guild member (per
    ``member.guild.get_member``) but has lost ``manage_guild`` — the
    handler must re-check the permission at delivery time and skip them
    without DMing or erroring. ``officer_b`` still holds the permission
    and must still be DMed.
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A, _OFFICER_B)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    bot._db_session_factory = factory
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A, manage_guild=False)
    officer_b = _make_officer(_OFFICER_B, manage_guild=True)
    officers = {_OFFICER_A: officer_a, _OFFICER_B: officer_b}
    member.guild.get_member = MagicMock(side_effect=lambda uid: officers.get(uid))

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=factory),
    ):
        await bot.on_member_join(member)  # must not raise

    officer_a.send.assert_not_awaited()
    officer_b.send.assert_awaited_once()


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
    bot._db_session_factory = factory
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
# Fix 3A — one subscriber's HTTPException doesn't stop the others either
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_http_exception_for_one_subscriber_does_not_block_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A discord.HTTPException for one subscriber must not stop the others'
    DMs — mirrors the Forbidden test above but with the broader exception
    type fix 3A adds a catch for (a plain HTTPException, not a
    Forbidden — Forbidden is already caught pre-fix)."""
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A, _OFFICER_B, _OFFICER_C)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    bot._db_session_factory = factory
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A)
    officer_b = _make_officer(_OFFICER_B)
    officer_b.send.side_effect = _make_http_exception()
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
        "delivery fails with discord.HTTPException."
    )


# ---------------------------------------------------------------------------
# Fix 3B — welcome-message failure must not block the officer-DM fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_welcome_channel_not_found_still_dms_officers() -> None:
    """A missing welcome channel must not prevent officer DMs.

    ``get_channel`` returning ``None`` is the "channel not found"
    early-return path in the welcome-message logic. That failure is
    independent of the officer-DM fan-out per fix 3B — subscribed
    officers must still be DMed.
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A, _OFFICER_B)

    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    bot._db_session_factory = factory
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A)
    officer_b = _make_officer(_OFFICER_B)
    officers = {_OFFICER_A: officer_a, _OFFICER_B: officer_b}
    member.guild.get_member = MagicMock(side_effect=lambda uid: officers.get(uid))

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=None),  # channel not found
        patch.object(
            bot,
            "fetch_channel",
            new_callable=AsyncMock,
            side_effect=discord.NotFound(
                MagicMock(status=404, reason="Unknown Channel"),
                "Unknown Channel",
            ),
        ),
        patch("mom_bot.main._build_session_factory", return_value=factory),
    ):
        await bot.on_member_join(member)  # must not raise

    officer_a.send.assert_awaited_once()
    officer_b.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_member_join_welcome_send_forbidden_still_dms_officers() -> None:
    """A Forbidden welcome-message send must not prevent officer DMs.

    Distinct from the channel-not-found case above: here ``get_channel``
    resolves to a real channel, but the welcome ``channel.send(...)``
    call itself raises ``discord.Forbidden``. Per fix 3B this must not
    block the independent officer-DM fan-out.
    """
    from mom_bot.main import MomBot, build_intents

    factory = _make_session_factory()
    _seed_subscribers(factory, _TARGET_GUILD_ID, _OFFICER_A)

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    fake_channel.send.side_effect = _make_forbidden()
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    bot._db_session_factory = factory
    member = _make_member()

    officer_a = _make_officer(_OFFICER_A)
    member.guild.get_member = MagicMock(return_value=officer_a)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
        patch("mom_bot.main._build_session_factory", return_value=factory),
    ):
        await bot.on_member_join(member)  # must not raise

    officer_a.send.assert_awaited_once()


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
