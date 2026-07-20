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
- Update (guild-scoping reconciliation pass): Tests 3-7 now configure
  ``bot.guild`` and pass a matching ``guild_id`` to ``_make_member`` (see
  the "Target-guild scoping tests" note below) so the guild guard added
  for Tests 8-9 is a no-op for their scenarios — this setup addition does
  not change any assertion. Confirmed by re-running this module: all of
  Tests 3-7 pass against the current implementation (full failure-handling
  has since landed for every path, superseding the "Tests 4-7 fail today"
  status captured above at authoring time).

Target-guild scoping tests (CodeRabbit review of PR #303; issue #299
follow-up):
- The bot can be a member of multiple Discord guilds (``bot.guilds``,
  plural), but only one is the configured target, tracked on
  ``bot.guild: discord.Object | None`` (set from the KV-configured
  ``guild-id`` during startup wireup). Tests 8-9 lock down that
  ``on_member_join`` must ignore any join that is not in that configured
  target guild — before any of the channel-resolution machinery
  (``load_secret``, ``get_channel``) runs — mirroring the bot-skip
  assertion style of Test 2.
- Test 8 covers a join in a guild other than the configured target
  (``member.guild.id != bot.guild.id``). Test 9 covers the
  not-yet-configured case (``bot.guild is None``). Both were **red as
  authored**: no guild-matching check existed yet in ``on_member_join``,
  so at that time ``load_secret``/``get_channel`` were called (and, in
  Test 8, ``fake_channel.send`` too) regardless of which guild the join
  came from. This was resolved once the guild-scoping guard landed; both
  tests now pass against the current implementation.

Welcome-copy revision (issue #306):
- The welcome message shipped under #299 tells new members to "check the
  pinned rules and pick your roles in #roles to get going." Issue #306
  removes that line entirely (role self-assignment via #roles is being
  dropped from the onboarding flow) and replaces it with a request for
  the new member to post a picture/screenshot of their in-game player
  profile.
- Test 1b (below) is authored FIRST, before this copy change lands, and
  is expected to be **red**: the current implementation still contains
  the "#roles"/"pick your roles" phrasing and has no profile-screenshot
  ask, so both halves of the assertion fail against it today.
- Exact prose is left to the implementer. The test only pins the
  substrings needed to make "the #roles line is gone" and "a
  profile-screenshot ask exists" observable, without prescribing the
  full sentence.
- Conflict resolution with Test 1: Test 1 (pre-existing, from #299)
  originally asserted ``"next" in lowered`` as part of its "what to do
  next placeholder instruction" check. That word came only from the
  #299 line #306 deletes ("Next, check the pinned rules and pick your
  roles..."); #306's spec has no requirement that the replacement line
  contain "next". Asserting it would have made an accidental word
  choice from the old copy an unintended, unspecified constraint on
  #306's replacement text. Resolved by relaxing Test 1 to drop the
  "next" substring check while keeping its other assertions (single
  send, member mention, "welcome" line, "shortly" line) — see Test 1's
  own docstring for the detail. Test 1 passes against current
  (pre-#306) code; Test 1b is the red covering #306's actual
  requirement.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

_NEW_MEMBERS_CHANNEL_ID = 555555555555555555
_MEMBER_ID = 200000000000000042
_TARGET_GUILD_ID = 300000000000000001
_OTHER_GUILD_ID = 300000000000000099


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


def _make_member(*, is_bot: bool = False, guild_id: int | None = None) -> MagicMock:
    """Build a minimal ``discord.Member`` mock.

    Args:
        is_bot: Whether the joining account is itself a bot, mirroring the
            real ``discord.Member.bot`` attribute.
        guild_id: If given, sets ``member.guild.id`` to this snowflake, so
            tests can simulate a join in a specific (possibly non-target)
            guild. If omitted, ``member.guild`` remains an unconfigured
            auto-mock, which is fine for tests that never inspect it.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Member``.
    """
    member = MagicMock(spec=discord.Member)
    member.id = _MEMBER_ID
    member.mention = f"<@{_MEMBER_ID}>"
    member.bot = is_bot
    if guild_id is not None:
        member.guild.id = guild_id
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
    4. The message contains, in the same post, a welcome line and a
       "someone will help you shortly" line.

    Deliberately NOT asserted here: the content of the "what to do next"
    instruction. Issue #299's original copy satisfied that clause with a
    line containing the literal word "next" (pointing at #roles); issue
    #306 replaces that line with a profile-screenshot ask that has no
    reason to contain "next". Pinning to that word would make this test
    an accidental, unintended constraint on #306's copy change. The "what
    to do next" instruction itself is still covered — by Test 1b below,
    which pins the #306-specific substrings without reintroducing a
    dependency on the deleted line's wording.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

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
    assert "shortly" in lowered, (
        f"Expected a 'someone will help you shortly' line in message; " f"got {message!r}"
    )


# ---------------------------------------------------------------------------
# Test 1b — welcome message drops the #roles line, adds a profile-screenshot ask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_welcome_message_drops_roles_asks_for_profile_screenshot() -> None:
    """The welcome message must drop the #roles instruction and ask for a profile screenshot.

    Issue #306 revises the welcome copy shipped under #299:

    1. The line pointing new members at "#roles" to self-assign roles is
       removed entirely — neither the channel reference ("#roles") nor
       the "pick your roles" phrasing may appear anywhere in the posted
       message.
    2. A new line asks the member to post a picture or screenshot of
       their in-game player profile.

    Exact prose is left to the implementer; this test pins only the
    substrings needed to make both halves of the change observable, so
    an implementation can't satisfy it by e.g. merely renaming the
    channel reference while keeping the roles instruction, or by adding
    an unrelated "next steps" line that never mentions a profile
    screenshot.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
    ):
        await bot.on_member_join(member)

    fake_channel.send.assert_awaited_once()
    call_args = fake_channel.send.call_args
    message: str = call_args.args[0] if call_args.args else call_args.kwargs.get("content", "")
    lowered = message.lower()

    assert "#roles" not in lowered, (
        f"Expected the '#roles' channel reference to be removed from the "
        f"welcome message; got {message!r}"
    )
    assert "pick your roles" not in lowered, (
        f"Expected the 'pick your roles' instruction to be removed from "
        f"the welcome message; got {message!r}"
    )
    assert "screenshot" in lowered or "picture" in lowered, (
        f"Expected the welcome message to ask for a picture/screenshot of "
        f"the member's in-game profile; got {message!r}"
    )
    assert "profile" in lowered, (
        f"Expected the welcome message to reference the member's in-game "
        f"profile when asking for a screenshot/picture; got {message!r}"
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
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

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
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

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
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

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
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

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
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

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
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_TARGET_GUILD_ID)

    with caplog.at_level(logging.WARNING, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch.object(bot, "get_channel", return_value=non_messageable_channel),
        ):
            await bot.on_member_join(member)

    # Reaching this point already proves no exception propagated.
    _assert_warning_logged_by_main(caplog)


# ---------------------------------------------------------------------------
# Test 8 — join in a guild other than the configured target guild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_guild_mismatched() -> None:
    """A join in a non-target guild must not trigger the welcome flow.

    The bot can be a member of multiple Discord guilds (``bot.guilds``,
    plural), but only one is configured as the target via ``bot.guild``
    (a ``discord.Object`` set from the KV-configured ``guild-id``). A
    member joining any OTHER guild the bot happens to be in must be
    ignored entirely — before any of the channel-resolution machinery
    (``load_secret``, ``get_channel``) runs, and before any message is
    sent — otherwise a join in an unrelated secondary guild would post
    the welcome message into the *primary* guild's configured channel.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(is_bot=False, guild_id=_OTHER_GUILD_ID)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret) as load_secret_mock,
        patch.object(bot, "get_channel", return_value=fake_channel) as get_channel_mock,
    ):
        await bot.on_member_join(member)

    fake_channel.send.assert_not_called()
    load_secret_mock.assert_not_called()
    get_channel_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9 — target guild not yet configured (self.guild is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_returns_when_target_guild_not_configured() -> None:
    """No configured target guild means no welcome flow runs at all.

    Before startup wireup resolves the configured ``guild-id`` into
    ``bot.guild``, ``bot.guild`` is ``None``. A member join arriving in
    this window must be ignored — before any of the channel-resolution
    machinery (``load_secret``, ``get_channel``) runs — rather than
    falling back to treating every guild the bot is in as the target.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = None
    member = _make_member(is_bot=False)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret) as load_secret_mock,
        patch.object(bot, "get_channel", return_value=fake_channel) as get_channel_mock,
    ):
        await bot.on_member_join(member)

    fake_channel.send.assert_not_called()
    load_secret_mock.assert_not_called()
    get_channel_mock.assert_not_called()
