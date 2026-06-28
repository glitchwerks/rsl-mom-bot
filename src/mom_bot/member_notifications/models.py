"""SQLAlchemy ORM models for the per-member notification system (#269).

Defines two tables:

- ``member_notification`` — the recurring DM schedule row, managed by
  officers via Discord slash commands.
- ``member_notification_sent`` — the per-occurrence idempotency log,
  mirroring the ``reminder_sent`` pattern exactly.

Both models register on :data:`mom_bot.db.Base.metadata` so Alembic can
detect them and tests can call ``Base.metadata.create_all(engine)``.

Schema design rationale (spec § 2.2):

- ``target_discord_id`` is stored as opaque ``TEXT``, never cast to integer
  except at the ``get_member(int(...))`` boundary — same convention as
  ``MemberRoleSyncState`` (``sidecar/models.py:64-66``).
- ``anchor_date_utc`` (DATE) and ``fire_time_utc`` (TIME) are kept as
  separate columns to match the existing ``Reminder`` pattern and because
  the scheduler predicate decomposes them independently.
- ``cadence`` is TEXT NOT NULL with a CHECK; the dialect-aware constraint
  lives in the Alembic migration, not here (same as ``fire_time_utc``'s
  minute-boundary check).
- ``member_notification_sent`` uses ``occurrence_date_utc`` as the column
  name to make the cadence semantics explicit, but the value is today's
  UTC date at fire time — identical to ``ReminderSent.fire_date_utc``.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mom_bot.db import Base

__all__ = ["MemberNotification", "MemberNotificationSent"]


class MemberNotification(Base):
    """Stores a per-member recurring DM notification schedule row.

    Each row represents one officer-managed notification: which Discord
    member to DM, when (anchor date + time + cadence), and what message
    to send.  Rows are created and managed exclusively via Discord slash
    commands (spec § 2.5).

    Attributes:
        id: Surrogate integer primary key, auto-incremented.
        name: Human-readable label, UNIQUE.  Used as the slash-command
            lookup key (mirrors the ``Reminder.name`` convention).
        target_discord_id: The DM recipient's Discord snowflake, stored
            as opaque TEXT — never cast to integer except at the
            ``get_member(int(...))`` call boundary.
        anchor_date_utc: The first occurrence's UTC calendar date.  The
            notification never fires before this date.
        fire_time_utc: UTC time-of-day gate; must be a minute-boundary
            value (seconds = 0), enforced by the Alembic migration CHECK.
        cadence: One of ``'weekly'``, ``'biweekly'``, or ``'monthly'``.
            Enforced by the Alembic migration CHECK.
        message_template: Static message body sent as the DM.
        enabled: Soft on/off toggle; disabled rows are never fired by the
            scheduler without being deleted.
        created_at: Audit timestamp, set once at INSERT time.
        updated_at: Audit timestamp, updated automatically on every UPDATE
            via SQLAlchemy's ``onupdate`` hook.
    """

    __tablename__ = "member_notification"
    __table_args__ = (
        CheckConstraint(
            "cadence IN ('weekly', 'biweekly', 'monthly')",
            name="ck_member_notification_cadence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    target_discord_id: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_date_utc: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    fire_time_utc: Mapped[datetime.time] = mapped_column(Time, nullable=False)
    cadence: Mapped[str] = mapped_column(Text, nullable=False)
    message_template: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class MemberNotificationSent(Base):
    """Records that a member notification fired on a specific occurrence date.

    Acts as the per-occurrence idempotency log, mirroring
    :class:`~mom_bot.reminders.models.ReminderSent`.  At most one row is
    permitted per ``(member_notification_id, occurrence_date_utc)`` via a
    UNIQUE constraint.

    The send pattern is insert-first: the scheduler claims the slot by
    inserting this row BEFORE resolving the member or sending the DM (spec
    § 2.3 finding 6).  Permanent Discord failures leave the row in place
    (no retry); transient failures delete the row before re-raising so the
    next tick can retry.

    Attributes:
        id: Surrogate integer primary key, auto-incremented.
        member_notification_id: Foreign key to ``member_notification.id``
            with ON DELETE CASCADE so deleting a notification cleans its
            send history.
        occurrence_date_utc: The UTC calendar date of the occurrence being
            claimed.  For a given notification this is today's date at fire
            time — semantically the "occurrence date", but the value is
            always today's UTC date.
        sent_at: Actual wall-clock timestamp of the send attempt, set at
            INSERT time.
    """

    __tablename__ = "member_notification_sent"
    __table_args__ = (
        UniqueConstraint(
            "member_notification_id",
            "occurrence_date_utc",
            name="uq_member_notification_sent_per_day",
        ),
        Index(
            "ix_member_notification_sent_occurrence_date_utc",
            "occurrence_date_utc",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_notification_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("member_notification.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_date_utc: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime.datetime] = mapped_column(
        nullable=False, server_default=func.current_timestamp()
    )
