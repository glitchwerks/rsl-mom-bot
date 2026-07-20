"""Async scheduler that removes members inactive for 24 hours."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable

import discord
from sqlalchemy.orm import Session

from mom_bot.member_activity.models import MemberActivity
from mom_bot.member_activity.service import MemberActivityService

__all__ = ["AutoKickScheduler"]

_logger = logging.getLogger(__name__)

_DM_MESSAGE = (
    "You have been removed because no message was posted within 24 hours "
    "of joining. You are welcome to rejoin when you are ready to participate."
)
_KICK_REASON = "No messages posted within 24 hours of joining"


class AutoKickScheduler:
    """Sweep stale member-activity rows on a configurable async tick.

    Attributes:
        _bot: Discord client used for the per-tick readiness gate.
        _guild: Live Discord guild used to resolve tracked members.
        _service: Database service for stale queries and cleanup.
        _tick_seconds: Seconds to sleep between scheduler ticks.
    """

    def __init__(
        self,
        bot: discord.Client,
        guild: discord.Guild,
        session_factory: Callable[[], Session],
        tick_seconds: float = 60.0,
    ) -> None:
        """Initialize the auto-kick scheduler.

        Args:
            bot: Discord client implementing ``is_ready()``.
            guild: Live guild implementing cached and API member lookup.
            session_factory: Callable returning a fresh database session.
            tick_seconds: Seconds to sleep between scheduler ticks.
        """
        self._bot = bot
        self._guild = guild
        self._service = MemberActivityService(session_factory)
        self._tick_seconds = tick_seconds

    async def run(self) -> None:
        """Run stale-member sweeps until the task is cancelled.

        A disconnected gateway skips the entire tick. Each stale member is
        isolated so resolution, DM, or kick failures cannot block later
        members, and every handled attempt removes its tracking row.

        Raises:
            asyncio.CancelledError: Propagated when the task is cancelled.
        """
        while True:
            if not self._bot.is_ready():
                await asyncio.sleep(self._tick_seconds)
                continue

            now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            try:
                stale_members = self._service.list_stale(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("auto-kick: stale-member query failed")
                await asyncio.sleep(self._tick_seconds)
                continue

            for activity in stale_members:
                await self._process_member(activity)

            await asyncio.sleep(self._tick_seconds)

    async def _process_member(self, activity: MemberActivity) -> None:
        """Resolve, notify, and kick one stale member with terminal cleanup.

        Args:
            activity: Detached tracking row selected by the stale query.

        Raises:
            asyncio.CancelledError: Propagated when the task is cancelled.
        """
        member = self._guild.get_member(activity.member_id)
        if member is None:
            try:
                member = await self._guild.fetch_member(activity.member_id)
            except asyncio.CancelledError:
                raise
            except discord.NotFound:
                _logger.info(
                    "auto-kick: member %d already left guild %d",
                    activity.member_id,
                    activity.guild_id,
                )
                self._safe_remove_tracking(activity.guild_id, activity.member_id)
                return
            except (discord.Forbidden, discord.HTTPException):
                _logger.warning(
                    "auto-kick: transient fetch failure for member %d in guild %d; "
                    "will retry on next sweep",
                    activity.member_id,
                    activity.guild_id,
                    exc_info=True,
                )
                return

        try:
            try:
                await member.send(_DM_MESSAGE)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning(
                    "auto-kick: could not DM member %d before kick",
                    activity.member_id,
                    exc_info=True,
                )

            current_activity = self._service.get_tracking(
                activity.guild_id,
                activity.member_id,
            )
            if (
                current_activity is None
                or current_activity.first_message_at is not None
                or current_activity.joined_at != activity.joined_at
            ):
                _logger.info(
                    "auto-kick: skipped kick for member %d in guild %d due to state change",
                    activity.member_id,
                    activity.guild_id,
                )
                return

            await member.kick(reason=_KICK_REASON)
            _logger.info(
                "auto-kick: kicked member %d from guild %d; reason=%s",
                activity.member_id,
                activity.guild_id,
                _KICK_REASON,
            )
            self._safe_remove_tracking(activity.guild_id, activity.member_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "auto-kick: failed to process member %d in guild %d",
                activity.member_id,
                activity.guild_id,
            )
            self._safe_remove_tracking(activity.guild_id, activity.member_id)

    def _safe_remove_tracking(self, guild_id: int, member_id: int) -> None:
        """Remove a tracking row without propagating cleanup failures.

        Args:
            guild_id: Discord guild snowflake.
            member_id: Discord member snowflake.
        """
        try:
            self._service.remove_tracking(guild_id, member_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "auto-kick: failed to remove tracking for member %d in guild %d",
                member_id,
                guild_id,
            )
