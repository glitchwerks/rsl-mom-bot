"""Keep join notification claims independent of auto-kick tracking.

Revision ID: 0008_member_join_claim
Revises: 0007_siege_seed_rows
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_member_join_claim"
down_revision: str | Sequence[str] | None = "0007_siege_seed_rows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "member_join_claim",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger, nullable=False),
        sa.Column("member_id", sa.BigInteger, nullable=False),
        sa.Column("joined_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("guild_id", "member_id", "joined_at", name="uq_member_join_claim_join"),
    )


def downgrade() -> None:
    op.drop_table("member_join_claim")
