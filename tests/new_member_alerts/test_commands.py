"""Tests for mom_bot.new_member_alerts.commands.

Covers ``/notify-new-members <on|off>`` (issue #301): the invoking
officer's own subscription toggle, the officer gate (A+B, mirroring
``mom_bot.discord_authz.require_manage_guild`` and
``@app_commands.default_permissions``), input validation, and
``register()`` wiring onto the command tree.

Binding contract decision (flagged per the frozen-test convention used in
tests/test_on_member_join.py — not freely re-implementable without
updating this file): the handler is named ``notify_new_members`` and
takes a keyword-only ``state: str`` parameter with values ``"on"`` /
``"off"`` (mirroring the existing ``cadence`` Choice-string convention in
``mom_bot.member_notifications.commands``, rather than a raw boolean
Discord option) and persists via
``service.set_subscription(guild_id=str(interaction.guild_id),
user_id=str(interaction.user.id), enabled=...)``.

Pattern mirrors tests/member_notifications/test_commands.py exactly:
- MagicMock(spec=discord.Interaction) with response.defer/followup.send as
  AsyncMock — NO FakeInteraction class.
- Handlers called directly (no HTTP/TestClient); service is in-process over
  in-memory SQLite.

Spec reference: #301 officer join alerts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from mom_bot.new_member_alerts.commands import register
from mom_bot.new_member_alerts.service import NewMemberAlertService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OFFICER_DISCORD_ID = 111111111111111111
_OTHER_OFFICER_DISCORD_ID = 222222222222222222
_GUILD_ID = 300000000000000001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():
    """Create an in-memory SQLite engine with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _make_service(engine) -> NewMemberAlertService:
    """Return a NewMemberAlertService backed by in-memory SQLite."""
    factory = sessionmaker(bind=engine)
    return NewMemberAlertService(session_factory=factory)


def _make_interaction(
    *,
    is_officer: bool = True,
    discord_id: int = _OFFICER_DISCORD_ID,
    guild_id: int = _GUILD_ID,
) -> MagicMock:
    """Build a minimal fake discord.Interaction.

    Args:
        is_officer: Whether interaction.user.guild_permissions.manage_guild
            returns True (officer) or False (regular member).
        discord_id: The invoking user's Discord snowflake.
        guild_id: The guild the interaction was invoked in.

    Returns:
        A MagicMock with the spec of discord.Interaction, wired with
        AsyncMock for response.defer and followup.send.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = discord_id
    interaction.user.name = "testofficer"
    interaction.user.guild_permissions = MagicMock()
    interaction.user.guild_permissions.manage_guild = is_officer
    interaction.guild_id = guild_id
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _import_handler():
    """Import the notify_new_members handler callable from the module."""
    from mom_bot.new_member_alerts import commands as _mod

    return _mod.notify_new_members


# ---------------------------------------------------------------------------
# Toggle on/off persists correctly — AC unit test #1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_defers_before_service_call() -> None:
    """/notify-new-members defers ephemeral before calling the service."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()

    handler = _import_handler()
    await handler(interaction, service=service, state="on")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_toggle_on_persists_subscription_for_invoking_officer() -> None:
    """state='on' subscribes the invoking officer for the invocation guild."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction(discord_id=_OFFICER_DISCORD_ID, guild_id=_GUILD_ID)

    handler = _import_handler()
    await handler(interaction, service=service, state="on")

    assert service.is_subscribed(str(_GUILD_ID), str(_OFFICER_DISCORD_ID)) is True


@pytest.mark.asyncio
async def test_toggle_off_persists_unsubscription_for_invoking_officer() -> None:
    """state='off' unsubscribes a previously-subscribed invoking officer."""
    engine = _make_engine()
    service = _make_service(engine)
    service.set_subscription(str(_GUILD_ID), str(_OFFICER_DISCORD_ID), enabled=True)
    interaction = _make_interaction(discord_id=_OFFICER_DISCORD_ID, guild_id=_GUILD_ID)

    handler = _import_handler()
    await handler(interaction, service=service, state="off")

    assert service.is_subscribed(str(_GUILD_ID), str(_OFFICER_DISCORD_ID)) is False


@pytest.mark.asyncio
async def test_toggle_only_affects_invoking_officers_own_subscription() -> None:
    """Toggling on must not affect a different officer's subscription state."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction(discord_id=_OFFICER_DISCORD_ID, guild_id=_GUILD_ID)

    handler = _import_handler()
    await handler(interaction, service=service, state="on")

    assert service.is_subscribed(str(_GUILD_ID), str(_OTHER_OFFICER_DISCORD_ID)) is False


@pytest.mark.asyncio
async def test_toggle_sends_ephemeral_confirmation() -> None:
    """/notify-new-members sends an ephemeral confirmation on success."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()

    handler = _import_handler()
    await handler(interaction, service=service, state="on")

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# Officer gate — A + B
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_officer_invocation_rejected_service_not_called() -> None:
    """Non-manage_guild invoker gets ephemeral rejection; service not called.

    AC unit test #3: "non-officer invocation is rejected".
    """
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction(is_officer=False)

    handler = _import_handler()
    await handler(interaction, service=mock_service, state="on")

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    mock_service.set_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_officer_invocation_proceeds() -> None:
    """An officer invoker is allowed through; the subscription is persisted."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction(is_officer=True)

    handler = _import_handler()
    await handler(interaction, service=service, state="on")

    assert service.is_subscribed(str(_GUILD_ID), str(_OFFICER_DISCORD_ID)) is True


def test_command_has_default_permissions_manage_guild() -> None:
    """notify_new_members carries @default_permissions(manage_guild=True).

    Asserts the command object exposes a default_permissions attribute
    (option A of the A+B gate).
    """
    from mom_bot.new_member_alerts import commands as _mod

    cmd = getattr(_mod, "notify_new_members", None)
    assert cmd is not None, "notify_new_members not found on module"
    perms = getattr(cmd, "default_permissions", None) or getattr(
        getattr(cmd, "callback", None), "default_permissions", None
    )
    assert perms is not None, (
        "notify_new_members is missing @app_commands.default_permissions — "
        "issue #301 requires manage_guild=True"
    )
    assert (
        getattr(perms, "manage_guild", False) is True
    ), "notify_new_members.default_permissions.manage_guild must be True"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_state_sends_validation_message_service_not_called() -> None:
    """An unrecognised state value sends a validation error; service not called."""
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction()

    handler = _import_handler()
    await handler(interaction, service=mock_service, state="maybe")

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    mock_service.set_subscription.assert_not_called()


# ---------------------------------------------------------------------------
# Unexpected service error → ephemeral ops-error, exception not leaked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_service_error_sends_generic_ops_error() -> None:
    """An unexpected exception from the service sends a generic ephemeral."""
    mock_service = MagicMock()
    mock_service.set_subscription = MagicMock(side_effect=RuntimeError("boom"))
    interaction = _make_interaction()

    handler = _import_handler()
    await handler(interaction, service=mock_service, state="on")

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    content = kwargs.get("content", "") or (
        interaction.followup.send.call_args[0][0] if interaction.followup.send.call_args[0] else ""
    )
    assert "boom" not in content


# ---------------------------------------------------------------------------
# register() wires the command onto the tree
# ---------------------------------------------------------------------------


def test_register_attaches_command_to_tree() -> None:
    """register(tree, service) attaches exactly one command to the tree."""
    tree = MagicMock(spec=discord.app_commands.CommandTree)
    tree.command = MagicMock(return_value=lambda f: f)
    service = MagicMock()

    register(tree=tree, service=service)

    assert tree.command.call_count == 1
