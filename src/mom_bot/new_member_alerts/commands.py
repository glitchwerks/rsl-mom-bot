"""Discord slash command for officer new-member alert subscriptions."""

from __future__ import annotations

import logging

import discord
import discord.app_commands as app_commands
from discord.app_commands import Choice

from mom_bot.discord_authz import require_manage_guild
from mom_bot.new_member_alerts.service import NewMemberAlertService

__all__ = ["notify_new_members", "register"]

_logger = logging.getLogger(__name__)

_OPS_ERROR_MSG = "An internal error occurred. Please try again later."
_INVALID_STATE_MSG = "Invalid state — choose 'on' or 'off'."
_STATE_CHOICES: list[Choice[str]] = [
    Choice(name="On", value="on"),
    Choice(name="Off", value="off"),
]
_MANAGE_GUILD_PERMS = discord.Permissions(manage_guild=True)


@app_commands.default_permissions(manage_guild=True)
@require_manage_guild
async def notify_new_members(
    interaction: discord.Interaction,
    *,
    service: NewMemberAlertService,
    state: str,
) -> None:
    """Toggle new-member join alerts for the invoking officer.

    Args:
        interaction: The Discord slash-command interaction.
        service: Subscription service used to persist the toggle.
        state: Either ``"on"`` or ``"off"``.
    """
    if state not in {"on", "off"}:
        await interaction.followup.send(_INVALID_STATE_MSG, ephemeral=True)
        return

    try:
        service.set_subscription(
            guild_id=str(interaction.guild_id),
            user_id=str(interaction.user.id),
            enabled=state == "on",
        )
        await interaction.followup.send(
            f"New-member alerts are now {state}.",
            ephemeral=True,
        )
    except Exception:
        _logger.exception("Unexpected error toggling new-member alerts")
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)


notify_new_members.default_permissions = _MANAGE_GUILD_PERMS  # type: ignore[attr-defined]


def register(
    tree: app_commands.CommandTree,
    service: NewMemberAlertService,
) -> None:
    """Register the new-member alert command on a command tree.

    Args:
        tree: Discord application-command tree.
        service: Subscription service captured by the command closure.
    """

    @tree.command(
        name="notify-new-members",
        description="Turn officer alerts for new member joins on or off.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(state=_STATE_CHOICES)
    async def _notify_new_members(
        interaction: discord.Interaction,
        state: str,
    ) -> None:
        """Delegate a Discord invocation to the module-level handler.

        Args:
            interaction: The Discord slash-command interaction.
            state: Either ``"on"`` or ``"off"``.
        """
        await notify_new_members(interaction, service=service, state=state)
