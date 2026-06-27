"""Insert Hydra Tank Week Heads-up and Hydra Tank Week End reminder rows.

Data migration for already-seeded databases (dev + prod confirmed seeded).

The two tank-week rows share the channel_id and role_mention_id from the
existing ``Hydra`` reminder row (no Discord gateway access at migration
time — copying from the DB is the only valid approach per spec §3 item 2).

Idempotency: each INSERT is guarded by a ``WHERE NOT EXISTS`` sub-select on
the reminder name, so running the migration twice is a safe no-op.

Fresh-DB path: when no ``Hydra`` row exists (clean/empty DB that will be
seeded by ``_maybe_seed_reminders`` on first boot), the migration is a
no-op — the ``WHERE EXISTS (SELECT 1 FROM reminders WHERE name='Hydra')``
guard ensures the INSERT bodies are never executed.

Revision ID: 0005_tank_week_seed_rows
Revises: 0004_tank_week_columns
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_tank_week_seed_rows"
down_revision: str | Sequence[str] | None = "0004_tank_week_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Message templates — placeholder text until officers supply wording.
# Matches the HYDRA_TANK_WEEK_HEADSUP_TEMPLATE and
# HYDRA_TANK_WEEK_END_TEMPLATE constants in seed.py.
_HEADSUP_TEMPLATE: str = "<TODO: officer to supply>"
_TANK_END_TEMPLATE: str = "<TODO: officer to supply>"


def upgrade() -> None:
    """Insert the two tank-week reminder rows, copying channel/role from Hydra.

    Skips gracefully when no Hydra row exists (fresh-boot path).
    """
    bind = op.get_bind()

    # Resolve channel_id and role_mention_id from the existing Hydra row.
    # If no Hydra row exists, the migration is a no-op.
    result = bind.execute(
        sa.text(
            "SELECT channel_id, role_mention_id FROM reminders WHERE name = 'Hydra'"
        )
    ).fetchone()

    if result is None:
        # Fresh DB — first-boot seeding covers this path.
        return

    channel_id = result[0]
    role_mention_id = result[1]

    # Insert Hydra Tank Week Heads-up (idempotent).
    bind.execute(
        sa.text(
            "INSERT INTO reminders "
            "(name, channel_id, weekday, fire_time_utc, message_template, "
            "role_mention_id, month_condition, delivery_target) "
            "SELECT :name, :channel_id, :weekday, :fire_time, :template, "
            ":role_id, :cond, 'channel' "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM reminders WHERE name = :name"
            ")"
        ),
        {
            "name": "Hydra Tank Week Heads-up",
            "channel_id": channel_id,
            "weekday": 1,
            "fire_time": "07:00:00",
            "template": _HEADSUP_TEMPLATE,
            "role_id": role_mention_id,
            "cond": "tank_week_headsup",
        },
    )

    # Insert Hydra Tank Week End (idempotent).
    bind.execute(
        sa.text(
            "INSERT INTO reminders "
            "(name, channel_id, weekday, fire_time_utc, message_template, "
            "role_mention_id, month_condition, delivery_target) "
            "SELECT :name, :channel_id, :weekday, :fire_time, :template, "
            ":role_id, :cond, 'channel' "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM reminders WHERE name = :name"
            ")"
        ),
        {
            "name": "Hydra Tank Week End",
            "channel_id": channel_id,
            "weekday": 1,
            "fire_time": "07:00:00",
            "template": _TANK_END_TEMPLATE,
            "role_id": role_mention_id,
            "cond": "tank_week_end",
        },
    )


def downgrade() -> None:
    """Remove the two tank-week reminder rows."""
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM reminders "
            "WHERE name IN ('Hydra Tank Week Heads-up', 'Hydra Tank Week End')"
        )
    )
