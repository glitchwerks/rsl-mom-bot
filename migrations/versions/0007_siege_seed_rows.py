"""Insert Siege 48h Heads-up and Siege 24h Heads-up reminder rows.

Data migration for already-seeded databases, mirroring the tank-week data
migration for environments where first-boot seeding has already run.

The two Siege rows share the channel_id and role_mention_id from the
existing ``Hydra`` reminder row, with no Discord gateway access at migration
time.

Idempotency: each INSERT is guarded by a ``WHERE NOT EXISTS`` sub-select on
the reminder name, so running the migration twice is a safe no-op.

Fresh-DB path: when no ``Hydra`` row exists (clean/empty DB that will be
seeded by ``_maybe_seed_reminders`` on first boot), the migration returns
without inserting rows; first-boot seeding covers that path instead.

Revision ID: 0007_siege_seed_rows
Revises: 0006_siege_month_conditions
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_siege_seed_rows"
down_revision: str | Sequence[str] | None = "0006_siege_month_conditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Message templates — must match SIEGE_48H_HEADSUP_TEMPLATE and
# SIEGE_24H_HEADSUP_TEMPLATE in seed.py exactly so already-seeded
# environments receive the same text as a first-boot seed.
_HEADSUP_48H_TEMPLATE: str = (
    ":crossed_swords: **Siege Incoming!** :crossed_swords:\n"
    "Siege starts in 48 hours — set your defences and check your post assignment now!\n"
    "An empty post is a free win for the other clan. Don't leave a hole in the line."
)

_HEADSUP_24H_TEMPLATE: str = (
    ":crossed_swords: **Siege — Final Day!** :crossed_swords:\n"
    "There are less than 24 hours until Siege — lock in your defences and confirm your post!\n"
    "Every missing player costs the clan points. Speak up now if you can't make it."
)


def upgrade() -> None:
    """Insert the two Siege reminder rows, copying channel/role from Hydra.

    Skips gracefully when no Hydra row exists (fresh-boot path).
    """
    bind = op.get_bind()

    # Resolve channel_id and role_mention_id from the existing Hydra row.
    # If no Hydra row exists, the migration is a no-op.
    result = bind.execute(
        sa.text("SELECT channel_id, role_mention_id FROM reminders WHERE name = 'Hydra'")
    ).fetchone()

    if result is None:
        # Fresh DB — first-boot seeding covers this path.
        return

    channel_id = result[0]
    role_mention_id = result[1]

    # Insert Siege 48h Heads-up (idempotent).
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
            "name": "Siege 48h Heads-up",
            "channel_id": channel_id,
            "weekday": 6,
            "fire_time": "10:00:00",
            "template": _HEADSUP_48H_TEMPLATE,
            "role_id": role_mention_id,
            "cond": "siege_48h_headsup",
        },
    )

    # Insert Siege 24h Heads-up (idempotent).
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
            "name": "Siege 24h Heads-up",
            "channel_id": channel_id,
            "weekday": 0,
            "fire_time": "10:00:00",
            "template": _HEADSUP_24H_TEMPLATE,
            "role_id": role_mention_id,
            "cond": "siege_24h_headsup",
        },
    )


def downgrade() -> None:
    """Remove the two Siege reminder rows."""
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM reminders " "WHERE name IN ('Siege 48h Heads-up', 'Siege 24h Heads-up')"
        )
    )
