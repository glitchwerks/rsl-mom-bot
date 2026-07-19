"""Tests for MomBot.on_member_join — welcome message for new members (#299).

TDD: Tests 1-2 (and the ``load_secret``/``get_channel`` wiring generally)
were written before the implementation and now pass against a landed
``on_member_join`` override on ``mom_bot.main.MomBot``. Tests 3-8 (below)
were added afterward, before the additional failure-handling behaviour they
cover existed — see that section's docstring for their current red/green
status.

Acceptance criteria under test (issue #299):
- ``on_member_join`` posts exactly one message to the configured
  new-members channel when a human member joins.
- Bot-account joins (``member.bot is True``) are skipped entirely — no
  message is sent for other bots joining the guild.
- The posted message mentions the joining member and contains, in a single
  post: a welcome line, a placeholder "what to do next" instruction, and a
  "someone will help you shortly" line.
- The new-members channel is resolved from a configured channel ID (KV
  secret via ``load_secret``, following the ``guild-id`` / ``channel_id``
  snowflake convention already used in ``mom_bot/config.py`` and
  ``mom_bot/reminders/models.py``) — never by searching guild channels for
  a specific display name.

Binding contract decision — NOT freely re-implementable (flagged because no
prior convention exists for this specific secret; it is new in this feature):
- The AC says "env var or KV secret." These frozen tests narrow that to the
  KV path: the channel ID MUST be read via
  ``load_secret("new-members-channel-id")`` (patched here as
  ``mom_bot.main.load_secret``, mirroring ``tests/test_main_wireup.py``),
  then resolved to a channel object via ``bot.get_channel(int(...))`` — the
  client-level lookup the reminder scheduler already uses (see
  ``tests/test_main_wireup.py::test_scheduler_fires_custom_reminder``,
  which patches ``bot.get_channel``), rather than
  ``member.guild.get_channel(...)`` or any other resolution path. This is
  the concrete, enforced assertion for "resolved by ID, not by display
  name": ``get_channel_mock.assert_called_once_with(...)`` pins both the
  call site and the secret name. An implementation using an env var, a
  different secret name, or guild-scoped/async channel resolution will
  fail Test 1 even though it may satisfy the AC's prose — that is a scope
  decision for the router/spec-owner to ratify or override before
  implementation, not something the implementer may silently work around
  by editing this frozen file.
- The message-content and bot-skip assertions do not depend on this
  narrowing and hold under any resolution mechanism.
- No live Discord connection is attempted; ``discord.Member`` and the
  channel are stand-in/mock objects, following the mocking conventions in
  ``tests/roles/test_service.py`` (``MagicMock(spec=discord.Member)``) and
  ``tests/test_main_wireup.py`` (``FakeChannel`` with a recorded
  ``AsyncMock`` send).

Failure-handling tests (added post-PR#303 Codex review; issue #299 follow-up):
- Tests 3-8 below lock down error-handling behaviour for
  ``on_member_join`` covering: a misconfigured/unresolvable channel, a
  broken or malformed ``load_secret`` value, a
  ``discord.Forbidden``/``discord.HTTPException`` raised by
  ``channel.send``, and a resolved channel that is not messageable. In
  every case the handler must catch/detect the failure, log it, and
  return — never let the exception propagate out of the gateway event
  handler (an uncaught exception here would crash the gateway dispatch
  loop).
- Logging convention: these tests use
  ``caplog.at_level(logging.WARNING, logger="mom_bot.main")`` and assert
  only that *some* record at WARNING-or-higher was emitted from the
  ``mom_bot.main`` logger — the exact level (``warning`` vs ``error`` vs
  ``exception``) and message text are left to the implementer. This
  mirrors the logger-name convention already established in
  ``tests/test_main_wireup.py`` (e.g.
  ``test_reminder_task_exception_logs_critical``,
  ``caplog.at_level(logging.CRITICAL, logger="mom_bot.main")``) rather
  than pinning to internal implementation details of ``on_member_join``.
- Current status as of authoring (confirmed by running this module, not
  assumed): the ``get_channel() -> None`` path (Test 3) is **already
  implemented** — it logs at ERROR and returns — so Test 3 passes today
  and is retained as a regression guard, not a red. Tests 4-7 (a broken
  or malformed ``load_secret``, ``discord.Forbidden``,
  ``discord.HTTPException``, and a non-messageable resolved channel) have
  **no** handling yet and each fails today with the underlying exception
  (``RuntimeError``/``ValueError`` from ``load_secret``/``int()``,
  ``discord.Forbidden``, ``discord.HTTPException``, ``AttributeError``
  from the missing ``.send`` on a ``spec=CategoryChannel`` mock)
  propagating uncaught out of ``await bot.on_member_join(member)`` — not
  because of a fixture bug. Once handling lands for each path, the call
  completes without raising and the post-call assertions (logging, no
  further calls) take over. This contradicts the "no error handling
  exists yet" premise for the None-channel case specifically — flagged
  for the router/spec-owner rather than silently adjusted here.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

_NEW_MEMBERS_CHANNEL_ID = 555555555555555555
_MEMBER_ID = 200000000000000042


class FakeChannel:
    """Minimal discord.TextChannel stand-in with a recorded send.

    Mirrors the ``FakeChannel`` helper in ``tests/test_main_wireup.py`` so
    the mocking convention for channel sends stays consistent project-wide.
    """

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake.

        Args:
            channel_id: The Discord channel snowflake this fake represents.
        """
        self.id = channel_id
        self.send = AsyncMock()


def _make_member(*, is_bot: bool = False) -> MagicMock:
    """Build a minimal ``discord.Member`` mock.

    Args:
        is_bot: Whether the joining account is itself a bot, mirroring the
            real ``discord.Member.bot`` attribute.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Member``.
    """
    member = MagicMock(spec=discord.Member)
    member.id = _MEMBER_ID
    member.mention = f"<@{_MEMBER_ID}>"
    member.bot = is_bot
    return member


def _fake_load_secret(name: str) -> str:
    """Stand-in for ``mom_bot.config.load_secret`` during these tests.

    Args:
        name: The unprefixed secret name being requested.

    Returns:
        The configured new-members channel ID as a string, for the
        ``"new-members-channel-id"`` secret name.
    """
    assert name == "new-members-channel-id", (
        f"Expected on_member_join to request the " f"'new-members-channel-id' secret; got {name!r}"
    )
    return str(_NEW_MEMBERS_CHANNEL_ID)


def _make_forbidden() -> discord.Forbidden:
    """Build a real ``discord.Forbidden`` instance for use as a side effect.

    ``discord.Forbidden`` does not accept a bare string — its ``__init__``
    reads ``response.status`` and ``response.reason``, so a minimal mock
    response is required to construct a genuine instance (rather than
    raising a ``TypeError`` from the fixture itself, which would produce a
    false red).

    Returns:
        A constructed ``discord.Forbidden`` carrying a 403 mock response.
    """
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, "Missing Permissions")


def _make_http_exception() -> discord.HTTPException:
    """Build a real ``discord.HTTPException`` instance for use as a side effect.

    See :func:`_make_forbidden` for why a mock response object (rather than
    a bare string) is required to construct this exception.

    Returns:
        A constructed ``discord.HTTPException`` carrying a 500 mock
        response.
    """
    response = MagicMock()
    response.status = 500
    response.reason = "Internal Server Error"
    return discord.HTTPException(response, "Internal Server Error")


def _assert_warning_logged_by_main(caplog: pytest.LogCaptureFixture) -> None:
    """Assert at least one WARNING-or-higher record came from mom_bot.main.

    Coarse on purpose: locks "something at WARNING+ was logged", not the
    exact level or message text, so the implementer is free to choose
    ``logger.warning``, ``logger.error``, or ``logger.exception`` for a
    given failure mode.

    Args:
        caplog: The pytest log-capture fixture, already scoped with
            ``caplog.at_level(logging.WARNING, logger="mom_bot.main")``
            around the call under test.
    """
    matching = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name == "mom_bot.main"
    ]
    assert matching, (
        "Expected a WARNING-or-higher log record from mom_bot.main when "
        "on_member_join hits this failure path, but none was emitted."
    )


# ---------------------------------------------------------------------------
# Test 1 — human join posts one welcome message with the correct mention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_posts_welcome_message_with_mention() -> None:
    """A human member join posts exactly one message mentioning them.

    Verifies:
    1. The new-members channel is resolved via ``bot.get_channel`` using the
       configured integer snowflake (not a name-based lookup).
    2. Exactly one message is sent to that channel.
    3. The message mentions the joining member.
    4. The message contains, in the same post, a welcome line, a "what to
       do next" placeholder instruction, and a "someone will help you
       shortly" line.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel) as get_channel_mock,
    ):
        await bot.on_member_join(member)

    # Channel resolved by configured snowflake ID, not by display name.
    get_channel_mock.assert_called_once_with(_NEW_MEMBERS_CHANNEL_ID)

    # Exactly one message posted. Accept either a positional content arg
    # (``channel.send("...")``) or a ``content=`` kwarg
    # (``channel.send(content="...")``) — both are valid discord.py call
    # shapes and neither is prescribed by the AC.
    fake_channel.send.assert_awaited_once()
    call_args = fake_channel.send.call_args
    message: str = call_args.args[0] if call_args.args else call_args.kwargs.get("content", "")

    assert member.mention in message, (
        f"Expected member mention {member.mention!r} in welcome message; " f"got {message!r}"
    )

    lowered = message.lower()
    assert "welcome" in lowered, f"Expected a welcome line in message; got {message!r}"
    assert "next" in lowered, (
        f"Expected a 'what to do next' placeholder instruction in message; " f"got {message!r}"
    )
    assert "shortly" in lowered, (
        f"Expected a 'someone will help you shortly' line in message; " f"got {message!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — bot-account joins produce no welcome message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_skips_bot_accounts() -> None:
    """Bot-account joins must not trigger a welcome message.

    ``member.bot is True`` (any bot account, not just this bot itself)
    must short-circuit ``on_member_join`` before any message is sent to
    the new-members channel, AND before any of the channel-resolution
    machinery (``load_secret``, ``get_channel``) runs at all — the bot
    check must be the very first thing the handler does, not merely a
    guard in front of ``send``.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=True)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret) as load_secret_mock,
        patch.object(bot, "get_channel", return_value=fake_channel) as get_channel_mock,
    ):
        await bot.on_member_join(member)

    fake_channel.send.assert_not_called()
    load_secret_mock.assert_not_called()
    get_channel_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — get_channel() returns None (misconfigured/unresolvable channel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_channel_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A misconfigured/unresolvable channel ID must not crash the handler.

    When ``bot.get_channel(...)`` returns ``None`` (e.g. the configured
    snowflake no longer exists, or the bot was removed from the guild that
    owns it), ``on_member_join`` must detect this, log it, and return —
    not blow up trying to call ``.send`` on ``None``.

    Unlike Tests 4-7, this path is **already implemented** (confirmed by
    running this test: it currently passes, logging at ERROR and
    returning cleanly). This test is retained as a regression guard for
    that existing behaviour, not as a red — do not "fix" it into failing.
    """
    from mom_bot.main import MomBot, build_intents

    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch.object(bot, "get_channel", return_value=None),
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    _assert_warning_logged_by_main(caplog)


# ---------------------------------------------------------------------------
# Test 4 — load_secret failure / malformed channel ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_load_secret_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken Key Vault lookup must not crash the handler.

    When ``load_secret("new-members-channel-id")`` itself raises (e.g. a
    transient Key Vault outage), ``on_member_join`` must catch it, log it,
    and return — never resolve a channel or attempt a send.

    Right now (no error handling implemented) this is expected to fail
    with the synthetic ``RuntimeError`` propagating uncaught out of
    ``on_member_join`` — that is the correct red, not a fixture bug.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch(
                "mom_bot.main.load_secret",
                side_effect=RuntimeError("Key Vault unavailable"),
            ),
            patch.object(bot, "get_channel", return_value=fake_channel) as get_channel_mock,
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    _assert_warning_logged_by_main(caplog)
    get_channel_mock.assert_not_called()
    fake_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_join_returns_when_channel_id_malformed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-numeric configured channel ID must not crash the handler.

    When ``load_secret("new-members-channel-id")`` returns a value that
    is not a valid snowflake (e.g. an empty string, or a placeholder that
    was never filled in), the handler's ``int(...)`` conversion will raise
    ``ValueError``. ``on_member_join`` must catch this, log it, and
    return — never call ``get_channel`` with a bogus value or attempt a
    send.

    Right now (no error handling implemented) this is expected to fail
    with an uncaught ``ValueError`` from the ``int(...)`` conversion —
    that is the correct red, not a fixture bug.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", return_value="not-a-channel-id"),
            patch.object(bot, "get_channel", return_value=fake_channel) as get_channel_mock,
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    _assert_warning_logged_by_main(caplog)
    get_channel_mock.assert_not_called()
    fake_channel.send.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — channel.send raises discord.Forbidden
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_send_forbidden(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 403 Forbidden on send must not crash the handler.

    When the bot lacks permission to post in the configured new-members
    channel (``channel.send`` raises ``discord.Forbidden``),
    ``on_member_join`` must catch it, log it, and return — never let the
    exception propagate out of the gateway event handler.

    Right now (no error handling implemented) this is expected to fail
    with the constructed ``discord.Forbidden`` propagating uncaught out
    of ``on_member_join`` — that is the correct red, not a fixture bug.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    fake_channel.send.side_effect = _make_forbidden()
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch.object(bot, "get_channel", return_value=fake_channel),
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    fake_channel.send.assert_awaited_once()
    _assert_warning_logged_by_main(caplog)


# ---------------------------------------------------------------------------
# Test 6 — channel.send raises discord.HTTPException
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_send_http_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generic Discord API failure on send must not crash the handler.

    When ``channel.send`` raises ``discord.HTTPException`` (e.g. a 5xx
    from Discord's API, or a payload rejected for reasons other than
    permissions), ``on_member_join`` must catch it, log it, and return —
    never let the exception propagate out of the gateway event handler.

    Right now (no error handling implemented) this is expected to fail
    with the constructed ``discord.HTTPException`` propagating uncaught
    out of ``on_member_join`` — that is the correct red, not a fixture
    bug.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    fake_channel.send.side_effect = _make_http_exception()
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch.object(bot, "get_channel", return_value=fake_channel),
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    fake_channel.send.assert_awaited_once()
    _assert_warning_logged_by_main(caplog)


# ---------------------------------------------------------------------------
# Test 7 — resolved channel is not messageable (e.g. a category channel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_channel_not_messageable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resolved-but-non-messageable channel must not crash the handler.

    If the configured snowflake resolves to a channel type that does not
    support ``.send`` (e.g. a ``discord.CategoryChannel``, which is a real
    ``discord.abc.GuildChannel`` but not a ``discord.abc.Messageable``),
    ``on_member_join`` must detect this, log it, and return — not blow up
    with an ``AttributeError`` trying to call ``.send`` on it.

    The mock is built with ``spec=discord.CategoryChannel`` so it
    genuinely lacks ``.send`` (unlike a bare ``MagicMock``, which would
    auto-create the attribute and hide this failure mode) and so
    ``isinstance(channel, discord.abc.Messageable)`` is correctly
    ``False`` — satisfying an implementation that guards with either
    ``hasattr`` or an ``isinstance`` check.

    Right now (no error handling implemented) this is expected to fail
    with an uncaught ``AttributeError`` from the missing ``.send`` — that
    is the correct red, not a fixture bug.
    """
    from mom_bot.main import MomBot, build_intents

    non_messageable_channel = MagicMock(spec=discord.CategoryChannel)
    non_messageable_channel.id = _NEW_MEMBERS_CHANNEL_ID
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=False)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch.object(bot, "get_channel", return_value=non_messageable_channel),
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    _assert_warning_logged_by_main(caplog)
