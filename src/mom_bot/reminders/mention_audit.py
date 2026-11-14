"""Read-only audit of configured channel reminder role notifications."""

from __future__ import annotations

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session

from mom_bot.reminders.models import Reminder


def audit_reminder_mentions(session: Session, guild: discord.Guild) -> dict[str, int]:
    """Count effective role-ping paths without reading content or changing Discord.

    An unavailable guild/cache entry remains unknown rather than being treated as
    permission to notify. Counts contain no Discord IDs or reminder content.
    """
    pairs = set(
        session.execute(
            select(Reminder.channel_id, Reminder.role_mention_id).where(
                Reminder.delivery_target == "channel", Reminder.role_mention_id.is_not(None)
            )
        ).all()
    )
    result = {"configured": len(pairs), "ready": 0, "blocked": 0, "unknown": 0}
    member = getattr(guild, "me", None)
    for channel_id, role_id in pairs:
        if role_id is None:
            result["unknown"] += 1
            continue
        channel = guild.get_channel(channel_id)
        role = guild.get_role(role_id)
        if channel is None or role is None or member is None:
            result["unknown"] += 1
            continue
        permissions = channel.permissions_for(member)
        if permissions.view_channel is not True or permissions.send_messages is not True:
            result["blocked"] += 1
        elif role.mentionable is True or permissions.mention_everyone is True:
            result["ready"] += 1
        else:
            result["blocked"] += 1
    return result
