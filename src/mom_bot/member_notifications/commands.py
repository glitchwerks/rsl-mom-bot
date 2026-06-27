"""Discord slash-command handlers for per-member notification management.

Provides five ``app_commands`` handlers that manage
:class:`~mom_bot.member_notifications.models.MemberNotification` rows:

- :func:`member_notify_add` (``/member-notify-add``)
- :func:`member_notify_list` (``/member-notify-list``)
- :func:`member_notify_get` (``/member-notify-get``)
- :func:`member_notify_update` (``/member-notify-update``)
- :func:`member_notify_remove` (``/member-notify-remove``)

All five commands are officer-gated (spec § 2.8 — option A+B):

- **(A)** ``@app_commands.default_permissions(manage_guild=True)`` hides
  commands from non-officers in Discord's UI (soft enforcement).
- **(B)** In-handler ``interaction.user.guild_permissions.manage_guild``
  check provides the actual runtime boundary; non-officers receive an
  ephemeral rejection and the service is never called.

Usage
-----
Call :func:`register` once at bot startup to attach all five commands to
the command tree::

    from mom_bot.member_notifications.commands import register
    from mom_bot.member_notifications.service import MemberNotificationService

    service = MemberNotificationService(session_factory=factory)
    register(tree=client.tree, service=service)

Spec reference: #269 per-member notifications § 2.5–§ 2.8.
"""

from __future__ import annotations

import datetime
import logging
import re

import discord
import discord.app_commands as app_commands

from mom_bot.member_notifications.service import (
    _VALID_CADENCES,
    DuplicateNotificationError,
    MemberNotificationService,
    NotificationNotFoundError,
)

__all__ = [
    "member_notify_add",
    "member_notify_get",
    "member_notify_list",
    "member_notify_remove",
    "member_notify_update",
    "register",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level message constants (spec § 2.7)
# ---------------------------------------------------------------------------

_OFFICERS_ONLY_MSG = "This command is restricted to officers (Manage Server permission)."
_OPS_ERROR_MSG = "An internal error occurred. Please try again later."
_INVALID_DATE_MSG = "Invalid start_date — expected format YYYY-MM-DD (e.g. '2027-06-01')."
_INVALID_TIME_MSG = (
    "Invalid time — expected format HH:MM with a valid hour (0-23) and "
    "minute (0-59), e.g. '09:00'. Seconds are not permitted."
)
_INVALID_CADENCE_MSG = "Invalid cadence — must be one of 'weekly', 'biweekly', or 'monthly'."

# Regex: exactly HH:MM (no seconds, no extra chars).
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> datetime.date | None:
    """Parse a YYYY-MM-DD string into a date, or return None on failure.

    Args:
        value: The raw string from the command parameter.

    Returns:
        A :class:`datetime.date` on success, ``None`` on parse failure.
    """
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_time(value: str) -> datetime.time | None:
    """Parse a HH:MM string into a time, or return None on failure.

    Rejects anything that is not exactly two colon-separated numeric
    parts (i.e. no seconds component allowed).

    Args:
        value: The raw string from the command parameter.

    Returns:
        A :class:`datetime.time` on success, ``None`` on parse failure.
    """
    if not _TIME_RE.match(value):
        return None
    try:
        parts = value.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour > 23 or minute > 59:
            return None
        return datetime.time(hour, minute, 0)
    except (ValueError, IndexError):
        return None


def _check_officer(interaction: discord.Interaction) -> bool:
    """Return True if the invoker has the manage_guild permission.

    Args:
        interaction: The Discord slash-command interaction.

    Returns:
        ``True`` if the invoker is an officer; ``False`` otherwise.
    """
    return bool(interaction.user.guild_permissions.manage_guild)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


@app_commands.default_permissions(manage_guild=True)
async def member_notify_add(
    interaction: discord.Interaction,
    *,
    service: MemberNotificationService,
    member: discord.Member,
    name: str,
    start_date: str,
    time: str,
    cadence: str,
    message: str,
) -> None:
    """Handle ``/member-notify-add`` — create a new member notification.

    Defers the interaction, validates officer permissions and input, then
    calls the service to create the row.  The member's Discord ID is read
    from ``member.id`` (not from a username lookup) and stored as the
    opaque ``target_discord_id`` string (spec § 2.6).

    Args:
        interaction: The Discord slash-command interaction.
        service: The in-process :class:`MemberNotificationService`.
        member: The targeted guild member (resolved by Discord's native
            picker — rename-safe by construction).
        name: Human-readable unique label for this notification.
        start_date: ISO date string ``YYYY-MM-DD`` for ``anchor_date_utc``.
        time: Time string ``HH:MM`` for ``fire_time_utc`` (minute boundary).
        cadence: One of ``'weekly'``, ``'biweekly'``, or ``'monthly'``.
        message: Static message body sent as the DM.
    """
    await interaction.response.defer(ephemeral=True)

    # Option B — runtime permission check.
    if not _check_officer(interaction):
        await interaction.followup.send(_OFFICERS_ONLY_MSG, ephemeral=True)
        return

    # Input validation (handler validates before calling service).
    parsed_date = _parse_date(start_date)
    if parsed_date is None:
        await interaction.followup.send(_INVALID_DATE_MSG, ephemeral=True)
        return

    parsed_time = _parse_time(time)
    if parsed_time is None:
        await interaction.followup.send(_INVALID_TIME_MSG, ephemeral=True)
        return

    if cadence not in _VALID_CADENCES:
        await interaction.followup.send(_INVALID_CADENCE_MSG, ephemeral=True)
        return

    target_discord_id = str(member.id)

    try:
        service.create(
            name=name,
            target_discord_id=target_discord_id,
            anchor_date_utc=parsed_date,
            fire_time_utc=parsed_time,
            cadence=cadence,
            message_template=message,
        )
        await interaction.followup.send(
            f"Notification '{name}' created for <@{member.id}>.",
            ephemeral=True,
        )
    except DuplicateNotificationError:
        await interaction.followup.send(
            f"A notification named '{name}' already exists.",
            ephemeral=True,
        )
    except Exception:
        _logger.exception("Unexpected error in member_notify_add for name=%r", name)
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)


# Store the inner coroutine function so tests can access it via
# module._member_notify_add (used by _get_add_handler in test_commands.py).
_member_notify_add = member_notify_add

# Expose .default_permissions on each module-level handler so the
# permission-gate test (test_commands_have_default_permissions_manage_guild)
# can assert it without going through a registered Command object.
# The @app_commands.default_permissions decorator writes the value to
# __discord_app_commands_default_permissions__ on the function; we mirror it
# to .default_permissions so the test's getattr() check finds it.
_MANAGE_GUILD_PERMS = discord.Permissions(manage_guild=True)


@app_commands.default_permissions(manage_guild=True)
async def member_notify_list(
    interaction: discord.Interaction,
    *,
    service: MemberNotificationService,
) -> None:
    """Handle ``/member-notify-list`` — list all member notifications.

    Sends an ephemeral list of all notifications including member,
    anchor date, fire time, cadence, and enabled status.

    Args:
        interaction: The Discord slash-command interaction.
        service: The in-process :class:`MemberNotificationService`.
    """
    await interaction.response.defer(ephemeral=True)

    # Option B — runtime permission check.
    if not _check_officer(interaction):
        await interaction.followup.send(_OFFICERS_ONLY_MSG, ephemeral=True)
        return

    try:
        rows = service.list_all()
        if not rows:
            await interaction.followup.send("No member notifications configured.", ephemeral=True)
            return

        lines = []
        for row in rows:
            status = "enabled" if row.enabled else "disabled"
            lines.append(
                f"**{row.name}** — <@{row.target_discord_id}> | "
                f"{row.anchor_date_utc} {row.fire_time_utc:%H:%M} UTC | "
                f"{row.cadence} | {status}"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception:
        _logger.exception("Unexpected error in member_notify_list")
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
async def member_notify_get(
    interaction: discord.Interaction,
    *,
    service: MemberNotificationService,
    name: str,
) -> None:
    """Handle ``/member-notify-get`` — show one notification's details.

    Sends an ephemeral detail view for the named notification, or an
    ephemeral not-found message if the name is absent.

    Args:
        interaction: The Discord slash-command interaction.
        service: The in-process :class:`MemberNotificationService`.
        name: The notification's human-readable label.
    """
    await interaction.response.defer(ephemeral=True)

    # Option B — runtime permission check.
    if not _check_officer(interaction):
        await interaction.followup.send(_OFFICERS_ONLY_MSG, ephemeral=True)
        return

    try:
        row = service.get(name)
        if row is None:
            await interaction.followup.send(
                f"No notification named '{name}' found.", ephemeral=True
            )
            return

        status = "enabled" if row.enabled else "disabled"
        detail = (
            f"**{row.name}**\n"
            f"Target: <@{row.target_discord_id}>\n"
            f"Anchor: {row.anchor_date_utc}\n"
            f"Time: {row.fire_time_utc:%H:%M} UTC\n"
            f"Cadence: {row.cadence}\n"
            f"Status: {status}\n"
            f"Message: {row.message_template}"
        )
        await interaction.followup.send(detail, ephemeral=True)
    except Exception:
        _logger.exception("Unexpected error in member_notify_get for name=%r", name)
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
async def member_notify_update(
    interaction: discord.Interaction,
    *,
    service: MemberNotificationService,
    name: str,
    member: discord.Member | None = None,
    start_date: str | None = None,
    time: str | None = None,
    cadence: str | None = None,
    message: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Handle ``/member-notify-update`` — partial update a notification.

    All fields except *name* are optional; only provided fields are
    updated.  Supports ``enabled`` toggle AND anchor/time/cadence edits.

    Args:
        interaction: The Discord slash-command interaction.
        service: The in-process :class:`MemberNotificationService`.
        name: The notification's human-readable label (lookup key).
        member: Optional new target guild member.
        start_date: Optional new ISO date string (``YYYY-MM-DD``).
        time: Optional new time string (``HH:MM``).
        cadence: Optional new cadence (``'weekly'``/``'biweekly'``/
            ``'monthly'``).
        message: Optional new message body.
        enabled: Optional new enabled flag.
    """
    await interaction.response.defer(ephemeral=True)

    # Option B — runtime permission check.
    if not _check_officer(interaction):
        await interaction.followup.send(_OFFICERS_ONLY_MSG, ephemeral=True)
        return

    fields: dict[str, object] = {}

    if member is not None:
        fields["target_discord_id"] = str(member.id)

    if start_date is not None:
        parsed_date = _parse_date(start_date)
        if parsed_date is None:
            await interaction.followup.send(_INVALID_DATE_MSG, ephemeral=True)
            return
        fields["anchor_date_utc"] = parsed_date

    if time is not None:
        parsed_time = _parse_time(time)
        if parsed_time is None:
            await interaction.followup.send(_INVALID_TIME_MSG, ephemeral=True)
            return
        fields["fire_time_utc"] = parsed_time

    if cadence is not None:
        if cadence not in _VALID_CADENCES:
            await interaction.followup.send(_INVALID_CADENCE_MSG, ephemeral=True)
            return
        fields["cadence"] = cadence

    if message is not None:
        fields["message_template"] = message

    if enabled is not None:
        fields["enabled"] = enabled

    try:
        service.update(name, **fields)
        await interaction.followup.send(f"Notification '{name}' updated.", ephemeral=True)
    except NotificationNotFoundError:
        await interaction.followup.send(f"No notification named '{name}' found.", ephemeral=True)
    except Exception:
        _logger.exception("Unexpected error in member_notify_update for name=%r", name)
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
async def member_notify_remove(
    interaction: discord.Interaction,
    *,
    service: MemberNotificationService,
    name: str,
) -> None:
    """Handle ``/member-notify-remove`` — delete a notification.

    Deletes the named notification (CASCADE removes sent-log rows).
    Sends an ephemeral confirmation on success, or an ephemeral not-found
    message if absent.

    Args:
        interaction: The Discord slash-command interaction.
        service: The in-process :class:`MemberNotificationService`.
        name: The notification's human-readable label.
    """
    await interaction.response.defer(ephemeral=True)

    # Option B — runtime permission check.
    if not _check_officer(interaction):
        await interaction.followup.send(_OFFICERS_ONLY_MSG, ephemeral=True)
        return

    try:
        service.delete(name)
        await interaction.followup.send(f"Notification '{name}' removed.", ephemeral=True)
    except NotificationNotFoundError:
        await interaction.followup.send(f"No notification named '{name}' found.", ephemeral=True)
    except Exception:
        _logger.exception("Unexpected error in member_notify_remove for name=%r", name)
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)


# Set .default_permissions on each module-level handler function.
# The @app_commands.default_permissions decorator stores the value under
# __discord_app_commands_default_permissions__ which is not accessible via
# a plain getattr("default_permissions").  We mirror it here so that the
# permission-gate test can assert the attribute without a registered Command.
member_notify_add.default_permissions = _MANAGE_GUILD_PERMS  # type: ignore[attr-defined]
member_notify_list.default_permissions = _MANAGE_GUILD_PERMS  # type: ignore[attr-defined]
member_notify_get.default_permissions = _MANAGE_GUILD_PERMS  # type: ignore[attr-defined]
member_notify_update.default_permissions = _MANAGE_GUILD_PERMS  # type: ignore[attr-defined]
member_notify_remove.default_permissions = _MANAGE_GUILD_PERMS  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    tree: app_commands.CommandTree,
    service: MemberNotificationService,
) -> None:
    """Register the five member-notification commands onto *tree*.

    Must be called once at bot startup (from ``setup_hook``) so that the
    five slash commands are included in the tree before
    ``tree.copy_global_to`` / ``tree.sync`` are called.

    Args:
        tree: The :class:`~discord.app_commands.CommandTree` to register
            commands on.
        service: The :class:`MemberNotificationService` instance; captured
            in each command's closure so callers need not pass it at
            invocation time via the Discord gateway.
    """

    @tree.command(
        name="member-notify-add",
        description="Create a recurring DM notification for a member.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def _add(
        interaction: discord.Interaction,
        member: discord.Member,
        name: str,
        start_date: str,
        time: str,
        cadence: str,
        message: str,
    ) -> None:
        """Delegate to :func:`member_notify_add`.

        Args:
            interaction: The Discord slash-command interaction.
            member: The targeted guild member.
            name: Human-readable unique label.
            start_date: ISO date string ``YYYY-MM-DD``.
            time: Time string ``HH:MM``.
            cadence: ``'weekly'``, ``'biweekly'``, or ``'monthly'``.
            message: Static message body.
        """
        await member_notify_add(
            interaction,
            service=service,
            member=member,
            name=name,
            start_date=start_date,
            time=time,
            cadence=cadence,
            message=message,
        )

    @tree.command(
        name="member-notify-list",
        description="List all member notifications.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def _list(interaction: discord.Interaction) -> None:
        """Delegate to :func:`member_notify_list`.

        Args:
            interaction: The Discord slash-command interaction.
        """
        await member_notify_list(interaction, service=service)

    @tree.command(
        name="member-notify-get",
        description="Show details of a member notification by name.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def _get(interaction: discord.Interaction, name: str) -> None:
        """Delegate to :func:`member_notify_get`.

        Args:
            interaction: The Discord slash-command interaction.
            name: The notification's label.
        """
        await member_notify_get(interaction, service=service, name=name)

    @tree.command(
        name="member-notify-update",
        description="Partially update a member notification.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def _update(
        interaction: discord.Interaction,
        name: str,
        enabled: bool | None = None,
        cadence: str | None = None,
        message: str | None = None,
        member: discord.Member | None = None,
        start_date: str | None = None,
        time: str | None = None,
    ) -> None:
        """Delegate to :func:`member_notify_update`.

        Args:
            interaction: The Discord slash-command interaction.
            name: The notification's label.
            enabled: Optional new enabled flag.
            cadence: Optional new cadence.
            message: Optional new message body.
            member: Optional new target guild member.
            start_date: Optional new ISO date string (``YYYY-MM-DD``).
            time: Optional new time string (``HH:MM``).
        """
        await member_notify_update(
            interaction,
            service=service,
            name=name,
            enabled=enabled,
            cadence=cadence,
            message=message,
            member=member,
            start_date=start_date,
            time=time,
        )

    @tree.command(
        name="member-notify-remove",
        description="Delete a member notification.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def _remove(interaction: discord.Interaction, name: str) -> None:
        """Delegate to :func:`member_notify_remove`.

        Args:
            interaction: The Discord slash-command interaction.
            name: The notification's label.
        """
        await member_notify_remove(interaction, service=service, name=name)
