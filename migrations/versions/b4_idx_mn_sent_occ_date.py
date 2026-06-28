"""Add index on member_notification_sent.occurrence_date_utc (#278).

Adds a standalone B-tree index on ``member_notification_sent.occurrence_date_utc``
to support the ``MemberNotificationService.list_due()`` query, which filters
this column by today's UTC date.

Without this index the query must scan the full table using the leading
``member_notification_id`` column of the existing UNIQUE composite index
``uq_member_notification_sent_per_day (member_notification_id,
occurrence_date_utc)``.  A standalone index on ``occurrence_date_utc``
lets the planner seek directly to today's rows.

Revision ID: b4_idx_mn_sent_occ_date
Revises: b3_member_notifications
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4_idx_mn_sent_occ_date"
down_revision: str | Sequence[str] | None = "b3_member_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create index on member_notification_sent.occurrence_date_utc."""
    op.create_index(
        "ix_member_notification_sent_occurrence_date_utc",
        "member_notification_sent",
        ["occurrence_date_utc"],
    )


def downgrade() -> None:
    """Drop index on member_notification_sent.occurrence_date_utc."""
    op.drop_index(
        "ix_member_notification_sent_occurrence_date_utc",
        table_name="member_notification_sent",
    )
