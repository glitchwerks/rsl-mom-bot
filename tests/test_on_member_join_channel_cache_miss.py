"""Regression test for the silent welcome-message drop on a channel cache miss (#310).

Bug report (issue #310): when a new member joined, the officer join-alert
DM fired correctly, but the welcome message that should have posted to the
configured new-members channel did not appear anywhere — and the failure
was completely silent (no visible error to officers, no message in
Discord).

``_send_welcome_message`` resolves the configured new-members channel via
``bot.get_channel(channel_id)`` (see ``tests/test_on_member_join.py``,
frozen). ``discord.Client.get_channel`` is a **cache-only** lookup — it
consults the gateway's local channel cache and returns ``None`` without
making any HTTP request if the channel is not (yet) in that cache, which
is exactly what happens for some window after a bot reconnect/resume.
When ``get_channel`` returns ``None`` today, the handler already logs at
ERROR and returns cleanly (see ``test_on_member_join.py::
test_on_member_join_returns_when_channel_not_found`` — that path does not
crash and is not what this test is about).

What is missing, and what this test pins down: there is no fallback to
``discord.Client.fetch_channel``, the HTTP-level lookup that would
resolve the channel even when it is absent from the gateway cache. A
cache miss right after a reconnect therefore drops the welcome message
permanently for that join — no retry, no HTTP fallback, and (per the
incident) no signal an officer would ever see. This test locks in the
fix contract: on a ``get_channel`` cache miss, the handler must attempt
``bot.fetch_channel(channel_id)`` as a fallback and, if that succeeds
with a messageable channel, still send the welcome message.

Binding contract decision — NOT freely re-implementable (flagged
because this narrows the fix to one specific mechanism): this test
requires the fallback to go through ``bot.fetch_channel`` specifically,
mirroring the existing ``bot.get_channel`` resolution seam already
locked in by ``tests/test_on_member_join.py`` Test 1. An implementation
that instead avoids the bug some other way — e.g. pre-warming/caching
the channel at startup, resolving via ``member.guild.fetch_channel``, or
retrying ``get_channel`` after a delay — would satisfy the incident's
intent but fail this specific test. That is a scope decision for the
router/spec-owner to ratify or override, not something to be silently
worked around here.

Mocking conventions mirror ``tests/test_on_member_join.py`` exactly
(``FakeChannel``, ``_make_member``, ``_fake_load_secret``, the
target-guild scoping setup for Tests 3-7) so this file reads as a direct
extension of that suite rather than introducing a new style.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

_NEW_MEMBERS_CHANNEL_ID = 555555555555555555
_MEMBER_ID = 200000000000000042
_TARGET_GUILD_ID = 300000000000000001


class FakeChannel:
    """Minimal discord.TextChannel stand-in with a recorded send.

    Mirrors the ``FakeChannel`` helper in ``tests/test_on_member_join.py``
    and ``tests/test_main_wireup.py`` so the mocking convention for
    channel sends stays consistent project-wide.
    """

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake.

        Args:
            channel_id: The Discord channel snowflake this fake
                represents.
        """
        self.id = channel_id
        self.send = AsyncMock()


def _make_member(*, guild_id: int | None = None) -> MagicMock:
    """Build a minimal, non-bot ``discord.Member`` mock.

    Args:
        guild_id: If given, sets ``member.guild.id`` to this snowflake so
            the join is scoped to a specific guild.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Member``.
    """
    member = MagicMock(spec=discord.Member)
    member.id = _MEMBER_ID
    member.mention = f"<@{_MEMBER_ID}>"
    member.bot = False
    if guild_id is not None:
        member.guild.id = guild_id
    return member


def _fake_load_secret(name: str) -> str:
    """Stand-in for ``mom_bot.config.load_secret`` during this test.

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
# get_channel cache miss must fall back to fetch_channel, not drop silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_member_join_falls_back_to_fetch_channel_on_cache_miss() -> None:
    """A gateway cache miss on the new-members channel must not drop the join.

    Simulates the real-world trigger from the incident report: right
    after a bot reconnect, ``bot.get_channel(channel_id)`` returns
    ``None`` because the channel is not yet in the gateway's local cache,
    even though the channel genuinely exists and is reachable over HTTP.

    Today, ``on_member_join`` has no fallback for this case — it logs
    and returns as soon as ``get_channel`` misses, so the welcome message
    is silently dropped for the entire duration of that cache-miss
    window. This test requires the handler to retry via
    ``bot.fetch_channel(channel_id)`` and, when that succeeds, still post
    the welcome message.

    The ``get_channel`` assertion is a self-guard: it proves the handler
    actually reached channel resolution (guild guard passed, secret
    loaded) rather than returning early for some unrelated reason, so a
    failure on the ``fetch_channel``/``send`` assertions below can only
    mean "no HTTP fallback was attempted" — not a miswired fixture.
    """
    from mom_bot.main import MomBot, build_intents

    fake_channel = FakeChannel(_NEW_MEMBERS_CHANNEL_ID)
    bot = MomBot(intents=build_intents())
    bot.guild = discord.Object(id=_TARGET_GUILD_ID)
    member = _make_member(guild_id=_TARGET_GUILD_ID)

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "get_channel", return_value=None) as get_channel_mock,
        patch.object(
            bot, "fetch_channel", new_callable=AsyncMock, return_value=fake_channel
        ) as fetch_channel_mock,
    ):
        await bot.on_member_join(member)

    # Self-guard: confirms the handler actually reached channel
    # resolution (this call already passes today) before the two
    # fallback assertions below, which do not.
    get_channel_mock.assert_called_once_with(_NEW_MEMBERS_CHANNEL_ID)

    fetch_channel_mock.assert_awaited_once_with(_NEW_MEMBERS_CHANNEL_ID)
    fake_channel.send.assert_awaited_once()
