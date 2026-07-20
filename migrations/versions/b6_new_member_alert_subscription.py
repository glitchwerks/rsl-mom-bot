"""Create new-member alert subscriptions (#301).

Revision ID: b6_new_member_alert_subscription
Revises: b5_member_activity
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6_new_member_alert_subscription"
down_revision: str | Sequence[str] | None = "b5_member_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the new-member alert subscription table."""
    op.create_table(
        "new_member_alert_subscription",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.UniqueConstraint(
            "guild_id",
            "user_id",
            name="uq_new_member_alert_subscription_guild_user",
        ),
    )


def downgrade() -> None:
    """Drop the new-member alert subscription table."""
    op.drop_table("new_member_alert_subscription")

