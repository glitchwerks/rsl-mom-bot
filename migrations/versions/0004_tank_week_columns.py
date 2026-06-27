"""Add month_condition and delivery_target columns to reminders (#268).

Adds two columns to the ``reminders`` table:

- ``month_condition`` (nullable Text): restricts when a calendar-conditional
  reminder fires.  NULL = ordinary reminder (current behavior).
  Allowed non-NULL values are enforced by a CHECK constraint:
  ``month_condition IS NULL OR month_condition IN
  ('tank_week_headsup', 'tank_week_end')``.
  The explicit ``IS NULL OR`` disjunction is mandatory — SQL three-valued
  logic makes ``NULL IN (...)`` evaluate to NULL (not TRUE), so a bare
  ``IN`` clause would reject every NULL-condition row at insert/update time.

- ``delivery_target`` (Text, NOT NULL, server default ``'channel'``): owned
  by Phase A (#268) to avoid a migration-head collision with Phase B (#269).
  All existing rows default to ``'channel'`` (current behavior).  Phase B
  reads this column to route DM reminders.

The CHECK constraint on ``month_condition`` is portable across SQLite and
PostgreSQL — both dialects support ``IS NULL OR ... IN (...)`` syntax.

Revision ID: 0004_tank_week_columns
Revises: 0003_widen_reminder_snowflakes
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_tank_week_columns"
down_revision: str | Sequence[str] | None = "0003_widen_reminder_snowflakes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Return True when the active dialect is PostgreSQL.

    SQLite does not support ``ALTER TABLE ADD CONSTRAINT`` — the CHECK
    must be added via batch mode or omitted for SQLite.  PostgreSQL
    supports it natively.

    Returns:
        ``True`` if the bound engine is PostgreSQL, ``False`` otherwise.
    """
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Add month_condition and delivery_target columns to reminders."""
    op.add_column(
        "reminders",
        sa.Column("month_condition", sa.Text, nullable=True),
    )

    # Create the CHECK constraint only on PostgreSQL.  SQLite does not
    # support ALTER TABLE ADD CONSTRAINT; the constraint is enforced at the
    # ORM/application layer on SQLite and at the DB layer on Postgres.
    if _is_postgres():
        op.create_check_constraint(
            "ck_month_condition",
            "reminders",
            "month_condition IS NULL OR month_condition IN "
            "('tank_week_headsup', 'tank_week_end')",
        )

    op.add_column(
        "reminders",
        sa.Column(
            "delivery_target",
            sa.Text,
            nullable=False,
            server_default=sa.text("'channel'"),
        ),
    )

    # Same dialect guard as month_condition: SQLite cannot ALTER TABLE ADD
    # CONSTRAINT; the constraint is enforced at the ORM layer on SQLite and
    # at the DB layer on Postgres.
    if _is_postgres():
        op.create_check_constraint(
            "ck_delivery_target",
            "reminders",
            "delivery_target IN ('channel', 'dm')",
        )


def downgrade() -> None:
    """Remove month_condition and delivery_target columns from reminders."""
    if _is_postgres():
        op.drop_constraint("ck_delivery_target", "reminders", type_="check")
    op.drop_column("reminders", "delivery_target")
    if _is_postgres():
        op.drop_constraint("ck_month_condition", "reminders", type_="check")
    op.drop_column("reminders", "month_condition")
