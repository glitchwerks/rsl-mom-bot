"""Tests for MomBot.on_member_join — welcome message for new members (#299).

TDD: these tests were written before the implementation. No ``on_member_join``
override exists yet on ``mom_bot.main.MomBot`` — running this module against
the current codebase is expected to fail with ``AttributeError`` (no such
handler), not because of a typo in the test.

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
"""

from __future__ import annotations

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
    the new-members channel.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    member = _make_member(is_bot=True)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=fake_channel),
    ):
        await bot.on_member_join(member)

    fake_channel.send.assert_not_called()
