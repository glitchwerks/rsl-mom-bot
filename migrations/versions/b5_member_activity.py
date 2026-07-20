"""Create the member_activity table for inactive-member tracking (#300).

Revision ID: b5_member_activity
Revises: b4_idx_mn_sent_occ_date
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5_member_activity"
down_revision: str | Sequence[str] | None = "b4_idx_mn_sent_occ_date"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``member_activity`` table."""
    op.create_table(
        "member_activity",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger, nullable=False),
        sa.Column("member_id", sa.BigInteger, nullable=False),
        sa.Column("joined_at", sa.DateTime, nullable=False),
        sa.Column("first_message_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint(
            "guild_id",
            "member_id",
            name="uq_member_activity_guild_member",
        ),
    )


def downgrade() -> None:
    """Drop the ``member_activity`` table."""
    op.drop_table("member_activity")
