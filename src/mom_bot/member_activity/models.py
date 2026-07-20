"""SQLAlchemy ORM model for new-member activity tracking."""

from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, DateTime, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mom_bot.db import Base

__all__ = ["MemberActivity"]


class MemberActivity(Base):
    """Store the join and first-message timestamps for a guild member.

    Attributes:
        id: Surrogate integer primary key, auto-incremented.
        guild_id: Discord guild snowflake.
        member_id: Discord member snowflake.
        joined_at: Naive UTC timestamp for the member's latest join.
        first_message_at: Naive UTC timestamp for the first message after
            that join, or ``None`` while the member remains inactive.
    """

    __tablename__ = "member_activity"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "member_id",
            name="uq_member_activity_guild_member",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    first_message_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
