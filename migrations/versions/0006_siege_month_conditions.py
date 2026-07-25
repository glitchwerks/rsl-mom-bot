"""Widen month_condition CHECK constraint for Siege reminders (#326).

Adds ``siege_48h_headsup`` and ``siege_24h_headsup`` to the allowed
non-NULL values in the PostgreSQL ``ck_month_condition`` constraint.
SQLite migration paths remain unchanged because SQLite does not support
``ALTER TABLE ADD CONSTRAINT``; ORM-created SQLite schemas enforce the
widened constraint from ``Reminder.__table_args__`` instead.

Revision ID: 0006_siege_month_conditions
Revises: b6_new_member_alert_subscription
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_siege_month_conditions"
down_revision: str | Sequence[str] | None = "b6_new_member_alert_subscription"
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
    """Allow Siege heads-up values in the month_condition constraint."""
    # SQLite cannot ALTER TABLE ADD CONSTRAINT, and this constraint was
    # never created there by the migration path. The ORM layer enforces it.
    if _is_postgres():
        op.drop_constraint(
            "ck_month_condition",
            "reminders",
            type_="check",
        )
        op.create_check_constraint(
            "ck_month_condition",
            "reminders",
            "month_condition IS NULL OR month_condition IN "
            "('tank_week_headsup', 'tank_week_end', 'siege_48h_headsup', "
            "'siege_24h_headsup')",
        )


def downgrade() -> None:
    """Restore the original two-value month_condition constraint."""
    # SQLite is a no-op for the same reason described in upgrade().
    if _is_postgres():
        op.drop_constraint(
            "ck_month_condition",
            "reminders",
            type_="check",
        )
        op.create_check_constraint(
            "ck_month_condition",
            "reminders",
            "month_condition IS NULL OR month_condition IN "
            "('tank_week_headsup', 'tank_week_end')",
        )
