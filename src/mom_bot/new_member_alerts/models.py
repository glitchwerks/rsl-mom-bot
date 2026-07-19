"""SQLAlchemy models for officer new-member alert subscriptions."""

from __future__ import annotations

from sqlalchemy import Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mom_bot.db import Base

__all__ = ["NewMemberAlertSubscription"]


class NewMemberAlertSubscription(Base):
    """Store one officer's new-member alert subscription for a guild.

    Rows exist only while the subscription is enabled. Disabling alerts
    deletes the corresponding row.

    Attributes:
        id: Surrogate integer primary key.
        guild_id: Discord guild snowflake stored as opaque text.
        user_id: Subscribed officer snowflake stored as opaque text.
    """

    __tablename__ = "new_member_alert_subscription"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "user_id",
            name="uq_new_member_alert_subscription_guild_user",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
