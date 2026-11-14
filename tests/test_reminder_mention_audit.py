"""Read-only role mention preflight checks with no Discord calls or writes."""

import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mom_bot.db import Base
from mom_bot.reminders.mention_audit import audit_reminder_mentions
from mom_bot.reminders.models import Reminder


def test_reminder_mention_audit_distinguishes_ready_blocked_and_unknown():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for index, role_id in enumerate((11, 12, 13, 14, 15), start=1):
            session.add(
                Reminder(
                    name=f"fixture-{index}",
                    channel_id=20,
                    role_mention_id=role_id,
                    weekday=0,
                    fire_time_utc=datetime.time(12),
                    message_template="fixture",
                    delivery_target="channel",
                )
            )
        session.add(
            Reminder(
                name="dm-fixture",
                channel_id=20,
                role_mention_id=11,
                weekday=0,
                fire_time_utc=datetime.time(12),
                message_template="fixture",
                delivery_target="dm",
            )
        )
        session.flush()

        channel = Mock()
        permissions = SimpleNamespace(view_channel=True, send_messages=True, mention_everyone=False)
        channel.permissions_for.return_value = permissions
        roles = {
            11: SimpleNamespace(mentionable=True),
            12: SimpleNamespace(mentionable=False),
            13: SimpleNamespace(mentionable=False),
            14: SimpleNamespace(mentionable=False),
        }
        guild = Mock(me=object())
        guild.get_channel.side_effect = lambda channel_id: channel if channel_id == 20 else None
        guild.get_role.side_effect = roles.get

        result = audit_reminder_mentions(session, guild)
        assert result == {"configured": 5, "ready": 1, "blocked": 3, "unknown": 1}
        permissions.mention_everyone = True
        assert audit_reminder_mentions(session, guild) == {
            "configured": 5,
            "ready": 4,
            "blocked": 0,
            "unknown": 1,
        }
        permissions.view_channel = False
        assert audit_reminder_mentions(session, guild) == {
            "configured": 5,
            "ready": 0,
            "blocked": 4,
            "unknown": 1,
        }
        assert session.scalar(select(Reminder).where(Reminder.name == "fixture-1")) is not None
        assert channel.send.call_count == 0
