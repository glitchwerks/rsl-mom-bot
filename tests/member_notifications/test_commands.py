"""Tests for mom_bot.member_notifications.commands.

Covers: CRUD happy paths, member targeting (member.id → target_discord_id),
duplicate/absent name errors, input validation, officer gate (A+B), error
isolation, and large-snowflake round-trip.

Pattern mirrors tests/post_conditions/test_commands.py exactly:
- MagicMock(spec=discord.Interaction) with response.defer/followup.send as
  AsyncMock — NO FakeInteraction class.
- Handlers called directly (no HTTP/TestClient); service is in-process over
  in-memory SQLite.

Spec reference: #269 per-member notifications, § 2.4–§ 2.8 and § 4.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from mom_bot.member_notifications.commands import register
from mom_bot.member_notifications.service import (
    MemberNotificationService,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OFFICER_DISCORD_ID = 111111111111111111
_TARGET_DISCORD_ID = 999888777666555444
_LARGE_SNOWFLAKE = 1234567890123456789

_DEFAULT_NAME = "test-notification"
_DEFAULT_START_DATE = "2027-06-01"
_DEFAULT_TIME = "09:00"
_DEFAULT_CADENCE = "weekly"
_DEFAULT_MESSAGE = "Hello <member>!"


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


def _make_service(engine) -> MemberNotificationService:
    """Return a MemberNotificationService backed by in-memory SQLite."""
    factory = sessionmaker(bind=engine)
    return MemberNotificationService(session_factory=factory)


def _make_interaction(
    *,
    is_officer: bool = True,
    discord_id: int = _OFFICER_DISCORD_ID,
) -> MagicMock:
    """Build a minimal fake discord.Interaction.

    Args:
        is_officer: Whether interaction.user.guild_permissions.manage_guild
            returns True (officer) or False (regular member).
        discord_id: The invoking user's Discord snowflake.

    Returns:
        A MagicMock with the spec of discord.Interaction, wired with
        AsyncMock for response.defer and followup.send.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = discord_id
    interaction.user.name = "testuser"
    interaction.user.guild_permissions = MagicMock()
    interaction.user.guild_permissions.manage_guild = is_officer
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_member(discord_id: int = _TARGET_DISCORD_ID) -> MagicMock:
    """Build a fake discord.Member whose .id is the given snowflake.

    Args:
        discord_id: The member's Discord snowflake integer.

    Returns:
        A MagicMock with .id set to discord_id.
    """
    member = MagicMock(spec=discord.Member)
    member.id = discord_id
    return member


def _get_add_handler(tree: MagicMock):
    """Extract the registered add-command handler from the tree mock."""
    # register() calls tree.command() as a decorator — we need to retrieve
    # the actual coroutine that was decorated.  The handler is stored on the
    # command object returned by the register() function.  Since the tree is
    # mocked, we access the module directly.
    from mom_bot.member_notifications import commands as _cmd_module

    return _cmd_module._member_notify_add  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Retrieve handlers via register()
# ---------------------------------------------------------------------------
#
# register() wires commands onto a discord.app_commands.CommandTree.
# For unit-testing we import the handler callables directly from the module
# after they are defined; register() is tested separately for wiring.


def _import_handlers():
    """Import handler callables from the commands module.

    Returns:
        Tuple (add, list_all, get, update, remove) coroutine functions.
    """
    from mom_bot.member_notifications import commands as _mod

    return (
        _mod.member_notify_add,
        _mod.member_notify_list,
        _mod.member_notify_get,
        _mod.member_notify_update,
        _mod.member_notify_remove,
    )


# ---------------------------------------------------------------------------
# CRUD happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_defers_before_service_call() -> None:
    """/member-notify-add defers ephemeral before calling the service."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_add_reads_member_id_as_target_discord_id() -> None:
    """/member-notify-add reads member.id and stores it as str(member.id)."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()
    member = _make_member(discord_id=_TARGET_DISCORD_ID)

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    row = service.get(_DEFAULT_NAME)
    assert row is not None
    assert row.target_discord_id == str(_TARGET_DISCORD_ID)


@pytest.mark.asyncio
async def test_add_sends_ephemeral_confirm() -> None:
    """/member-notify-add sends an ephemeral confirmation on success."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_list_defers_and_sends_ephemeral() -> None:
    """/member-notify-list defers and sends an ephemeral list reply."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()

    _, list_cmd, *_ = _import_handlers()
    await list_cmd(interaction, service=service)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_get_sends_ephemeral_detail() -> None:
    """/member-notify-get returns ephemeral detail for an existing name."""
    engine = _make_engine()
    service = _make_service(engine)

    # Pre-create via service directly.
    service.create(
        name=_DEFAULT_NAME,
        target_discord_id=str(_TARGET_DISCORD_ID),
        anchor_date_utc=datetime.date(2027, 6, 1),
        fire_time_utc=datetime.time(9, 0),
        cadence="weekly",
        message_template=_DEFAULT_MESSAGE,
    )

    interaction = _make_interaction()
    _, _, get_cmd, *_ = _import_handlers()
    await get_cmd(interaction, service=service, name=_DEFAULT_NAME)

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_update_enabled_toggle_sends_ephemeral() -> None:
    """/member-notify-update with enabled=false sends ephemeral confirm."""
    engine = _make_engine()
    service = _make_service(engine)

    service.create(
        name=_DEFAULT_NAME,
        target_discord_id=str(_TARGET_DISCORD_ID),
        anchor_date_utc=datetime.date(2027, 6, 1),
        fire_time_utc=datetime.time(9, 0),
        cadence="weekly",
        message_template=_DEFAULT_MESSAGE,
    )

    interaction = _make_interaction()
    _, _, _, update_cmd, _ = _import_handlers()
    await update_cmd(
        interaction,
        service=service,
        name=_DEFAULT_NAME,
        enabled=False,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True

    # Verify toggle persisted.
    row = service.get(_DEFAULT_NAME)
    assert row is not None
    assert row.enabled is False


@pytest.mark.asyncio
async def test_remove_deletes_and_sends_ephemeral() -> None:
    """/member-notify-remove deletes the row and sends ephemeral confirm."""
    engine = _make_engine()
    service = _make_service(engine)

    service.create(
        name=_DEFAULT_NAME,
        target_discord_id=str(_TARGET_DISCORD_ID),
        anchor_date_utc=datetime.date(2027, 6, 1),
        fire_time_utc=datetime.time(9, 0),
        cadence="weekly",
        message_template=_DEFAULT_MESSAGE,
    )

    interaction = _make_interaction()
    *_, remove_cmd = _import_handlers()
    await remove_cmd(interaction, service=service, name=_DEFAULT_NAME)

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True

    assert service.get(_DEFAULT_NAME) is None


# ---------------------------------------------------------------------------
# Duplicate name on add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_duplicate_name_sends_ephemeral_dup_message() -> None:
    """Adding a duplicate name sends ephemeral duplicate msg; no new row."""
    engine = _make_engine()
    service = _make_service(engine)
    service.create(
        name=_DEFAULT_NAME,
        target_discord_id=str(_TARGET_DISCORD_ID),
        anchor_date_utc=datetime.date(2027, 6, 1),
        fire_time_utc=datetime.time(9, 0),
        cadence="weekly",
        message_template=_DEFAULT_MESSAGE,
    )

    interaction = _make_interaction()
    member = _make_member()
    add, *_ = _import_handlers()
    await add(
        interaction,
        service=service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    # The reply must reference the duplicate name.
    content = kwargs.get("content", "") or (
        interaction.followup.send.call_args[0][0] if interaction.followup.send.call_args[0] else ""
    )
    assert _DEFAULT_NAME in content

    # list_all must still show exactly one row (no second row created).
    assert len(service.list_all()) == 1


# ---------------------------------------------------------------------------
# Absent name — get / update / remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_absent_name_sends_ephemeral_not_found() -> None:
    """/member-notify-get with unknown name sends ephemeral not-found msg."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()

    _, _, get_cmd, *_ = _import_handlers()
    await get_cmd(interaction, service=service, name="does-not-exist")

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_update_absent_name_sends_ephemeral_not_found() -> None:
    """/member-notify-update with unknown name sends ephemeral not-found."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()

    _, _, _, update_cmd, _ = _import_handlers()
    await update_cmd(
        interaction,
        service=service,
        name="does-not-exist",
        enabled=True,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_remove_absent_name_sends_ephemeral_not_found() -> None:
    """/member-notify-remove with unknown name sends ephemeral not-found."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()

    *_, remove_cmd = _import_handlers()
    await remove_cmd(interaction, service=service, name="does-not-exist")

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# Validation — malformed start_date / time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_malformed_start_date_sends_validation_message() -> None:
    """/member-notify-add with non-ISO start_date sends validation error."""
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction()
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=mock_service,
        member=member,
        name=_DEFAULT_NAME,
        start_date="not-a-date",
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    # Service.create must NOT have been called.
    mock_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_add_malformed_time_sends_validation_message() -> None:
    """/member-notify-add with non-HH:MM time sends validation error."""
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction()
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=mock_service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time="25:99",
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    mock_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_add_non_minute_boundary_time_sends_validation_message() -> None:
    """/member-notify-add with seconds component sends validation error."""
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction()
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=mock_service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time="09:00:30",  # seconds present — not HH:MM
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    mock_service.create.assert_not_called()


# ---------------------------------------------------------------------------
# Officer gate — § 2.8 (A + B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_non_officer_rejected_service_not_called() -> None:
    """Non-manage_guild invoker gets ephemeral rejection; service not called."""
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction(is_officer=False)
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=mock_service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    mock_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_list_non_officer_rejected_service_not_called() -> None:
    """Non-officer list invocation is rejected without calling the service."""
    engine = _make_engine()
    service = _make_service(engine)
    mock_service = MagicMock(wraps=service)
    interaction = _make_interaction(is_officer=False)

    _, list_cmd, *_ = _import_handlers()
    await list_cmd(interaction, service=mock_service)

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    mock_service.list_all.assert_not_called()


@pytest.mark.asyncio
async def test_officer_proceeds_add() -> None:
    """An officer invoker is allowed through; service.create is called."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction(is_officer=True)
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    row = service.get(_DEFAULT_NAME)
    assert row is not None


def test_commands_have_default_permissions_manage_guild() -> None:
    """Each of the five commands carries @default_permissions(manage_guild=True).

    This asserts the command objects expose a default_permissions attribute
    (option A of the A+B gate — spec § 2.8).
    """
    from mom_bot.member_notifications import commands as _mod

    for cmd_attr in [
        "member_notify_add",
        "member_notify_list",
        "member_notify_get",
        "member_notify_update",
        "member_notify_remove",
    ]:
        cmd = getattr(_mod, cmd_attr, None)
        assert cmd is not None, f"Command {cmd_attr!r} not found on module"
        # discord.app_commands decorates the function; the permissions object
        # is stored as an attribute on the callback or the command object.
        perms = getattr(cmd, "default_permissions", None) or getattr(
            getattr(cmd, "callback", None), "default_permissions", None
        )
        assert perms is not None, (
            f"{cmd_attr} is missing @app_commands.default_permissions — "
            "spec § 2.8 requires manage_guild=True on every command"
        )
        assert (
            getattr(perms, "manage_guild", False) is True
        ), f"{cmd_attr}.default_permissions.manage_guild must be True"


# ---------------------------------------------------------------------------
# Unexpected service error → ephemeral ops-error, exception not leaked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_service_error_sends_generic_ops_error() -> None:
    """An unexpected exception from the service sends a generic ephemeral."""
    mock_service = MagicMock()
    mock_service.create = MagicMock(side_effect=RuntimeError("boom"))
    interaction = _make_interaction()
    member = _make_member()

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=mock_service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("ephemeral") is True
    # The raw exception message must NOT appear in the reply.
    content = kwargs.get("content", "") or (
        interaction.followup.send.call_args[0][0] if interaction.followup.send.call_args[0] else ""
    )
    assert "boom" not in content


# ---------------------------------------------------------------------------
# Large-snowflake round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_snowflake_target_discord_id_round_trips() -> None:
    """A large Discord snowflake is stored and retrieved as the same string."""
    engine = _make_engine()
    service = _make_service(engine)
    interaction = _make_interaction()
    member = _make_member(discord_id=_LARGE_SNOWFLAKE)

    add, *_ = _import_handlers()
    await add(
        interaction,
        service=service,
        member=member,
        name=_DEFAULT_NAME,
        start_date=_DEFAULT_START_DATE,
        time=_DEFAULT_TIME,
        cadence=_DEFAULT_CADENCE,
        message=_DEFAULT_MESSAGE,
    )

    row = service.get(_DEFAULT_NAME)
    assert row is not None
    assert row.target_discord_id == str(_LARGE_SNOWFLAKE)


# ---------------------------------------------------------------------------
# register() wires five commands onto the tree
# ---------------------------------------------------------------------------


def test_register_attaches_five_commands_to_tree() -> None:
    """register(tree, service) attaches exactly five commands to the tree."""
    tree = MagicMock(spec=discord.app_commands.CommandTree)
    tree.command = MagicMock(return_value=lambda f: f)
    service = MagicMock()

    register(tree=tree, service=service)

    assert tree.command.call_count == 5
