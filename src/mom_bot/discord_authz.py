"""Shared Discord authorization helpers for mom_bot slash commands.

Provides reusable permission-gate decorators for ``discord.app_commands``
handler functions.

This module is the authoritative home for runtime authorization checks that
span multiple command modules.  It is the ``@require_admin_role`` stand-in
described in ``docs/discord-permissions-reference.md`` — a single boundary
rather than duplicated inline guards scattered across handlers.

Usage example::

    from mom_bot.discord_authz import require_manage_guild

    @app_commands.default_permissions(manage_guild=True)
    @require_manage_guild
    async def my_command(interaction: discord.Interaction, *, service: ...) -> None:
        # Only reached if invoker has manage_guild permission.
        ...

Note: ``@app_commands.default_permissions(manage_guild=True)`` is the Discord
UI-layer gate (spec option A).  ``@require_manage_guild`` is the in-process
runtime gate (spec option B).  Both must be present on officer-only commands —
the decorator enforces B; the ``default_permissions`` decorator enforces A.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Coroutine
from typing import Any

import discord

__all__ = ["require_manage_guild"]

_OFFICERS_ONLY_MSG = "This command is restricted to officers (Manage Server permission)."


def require_manage_guild[F: Callable[..., Coroutine[Any, Any, None]]](
    func: F,
) -> F:
    """Decorator that gates a slash-command handler on manage_guild permission.

    Defers the interaction ephemerally, then checks whether the invoking
    user has the ``manage_guild`` permission.  If not, sends an ephemeral
    "officers only" rejection and returns without calling the wrapped handler.
    If the check passes, calls the wrapped handler (which must NOT call
    ``interaction.response.defer`` itself — the decorator handles it).

    This is the runtime half of the A+B officer gate described in spec § 2.8:

    - **(A)** ``@app_commands.default_permissions(manage_guild=True)`` — UI
      layer, applied separately on each command.
    - **(B)** ``@require_manage_guild`` — this decorator, runtime boundary.

    Args:
        func: The async slash-command handler to wrap.  Its first positional
            argument must be ``interaction: discord.Interaction``.

    Returns:
        A wrapped coroutine that enforces the manage_guild permission gate.

    Example::

        @app_commands.default_permissions(manage_guild=True)
        @require_manage_guild
        async def my_command(
            interaction: discord.Interaction,
            *,
            service: MyService,
        ) -> None:
            # Reached only when invoker is an officer.
            ...
    """

    @functools.wraps(func)
    async def wrapper(
        interaction: discord.Interaction,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Defer, gate on manage_guild, then delegate to the wrapped handler.

        Args:
            interaction: The Discord slash-command interaction.
            *args: Positional arguments forwarded to the wrapped handler.
            **kwargs: Keyword arguments forwarded to the wrapped handler.
        """
        await interaction.response.defer(ephemeral=True)

        if not bool(interaction.user.guild_permissions.manage_guild):  # type: ignore[union-attr]
            await interaction.followup.send(_OFFICERS_ONLY_MSG, ephemeral=True)
            return

        await func(interaction, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
