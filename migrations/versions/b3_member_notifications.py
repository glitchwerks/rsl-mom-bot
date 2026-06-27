"""member_notification and member_notification_sent tables (#269).

Creates the two tables for the per-member recurring DM notification
system introduced in Phase B (#269).

Tables created
--------------
- ``member_notification`` — the recurring DM schedule row; managed by
  officers via Discord slash commands.
- ``member_notification_sent`` — the per-occurrence idempotency log,
  mirroring the ``reminder_sent`` pattern.

Constraints
-----------
- ``member_notification.fire_time_utc`` CHECK: seconds component must be
  zero (same dialect-aware pattern as migration ``0002``).
- ``member_notification.cadence`` CHECK: value must be IN
  ('weekly', 'biweekly', 'monthly').  No ``IS NULL OR`` guard — the
  column is NOT NULL so there is no NULL row to admit (contrast
  ``month_condition`` in ``0004`` which IS nullable).
- ``member_notification_sent`` UNIQUE (member_notification_id,
  occurrence_date_utc): at most one sent record per notification per
  occurrence date.
- ``member_notification_sent.member_notification_id`` FK →
  ``member_notification.id`` ON DELETE CASCADE.

Dialect notes
-------------
The fire_time CHECK and the cadence CHECK are authored dialect-aware using
the helper functions below, matching the ``0002`` convention.  The Postgres
path adds constraints via ``op.create_check_constraint``; the SQLite path
embeds them in the ``CREATE TABLE`` body (SQLite does not support
``ADD CONSTRAINT`` after the fact).

Revision ID: b3_member_notifications
Revises: 0005_tank_week_seed_rows
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3_member_notifications"
down_revision: str | Sequence[str] | None = "0005_tank_week_seed_rows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Return True if the current dialect is PostgreSQL.

    Returns:
        ``True`` for PostgreSQL; ``False`` for SQLite and any other dialect.
    """
    return op.get_bind().dialect.name == "postgresql"


def _fire_time_check_expr() -> str:
    """Return the dialect-appropriate CHECK expression for fire_time_utc.

    Mirrors the ``0002`` helper: SQLite uses ``strftime``; Postgres uses
    ``EXTRACT``.

    Returns:
        A SQL string suitable for use in a ``CheckConstraint`` on the
        active database dialect.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return "CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0"
    return "EXTRACT(SECOND FROM fire_time_utc) = 0"


def upgrade() -> None:
    """Create ``member_notification`` and ``member_notification_sent``."""
    fire_check = _fire_time_check_expr()
    is_pg = _is_postgres()

    op.create_table(
        "member_notification",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("target_discord_id", sa.Text, nullable=False),
        sa.Column("anchor_date_utc", sa.Date, nullable=False),
        sa.Column("fire_time_utc", sa.Time, nullable=False),
        sa.Column("cadence", sa.Text, nullable=False),
        sa.Column("message_template", sa.Text, nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true" if is_pg else "1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        # Embed CHECKs in CREATE TABLE — required for SQLite (no ADD
        # CONSTRAINT post-DDL); Postgres also accepts them here.
        sa.CheckConstraint(
            fire_check,
            name="ck_member_notification_fire_time_no_seconds",
        ),
        sa.CheckConstraint(
            "cadence IN ('weekly', 'biweekly', 'monthly')",
            name="ck_member_notification_cadence",
        ),
    )

    op.create_table(
        "member_notification_sent",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "member_notification_id",
            sa.Integer,
            sa.ForeignKey(
                "member_notification.id",
                ondelete="CASCADE",
                name="fk_member_notification_sent_notification_id",
            ),
            nullable=False,
        ),
        sa.Column("occurrence_date_utc", sa.Date, nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint(
            "member_notification_id",
            "occurrence_date_utc",
            name="uq_member_notification_sent_per_day",
        ),
    )


def downgrade() -> None:
    """Drop ``member_notification_sent`` then ``member_notification``."""
    op.drop_table("member_notification_sent")
    op.drop_table("member_notification")
