"""Database service for new-member activity tracking."""

from __future__ import annotations

import datetime
from collections.abc import Callable

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from mom_bot.member_activity.models import MemberActivity, MemberJoinClaim

__all__ = ["INACTIVITY_WINDOW", "MemberActivityService"]

INACTIVITY_WINDOW = datetime.timedelta(hours=24)


class MemberActivityService:
    """Persist joins, first messages, stale lookups, and cleanup.

    Attributes:
        _session_factory: A zero-argument callable that creates a fresh
            SQLAlchemy session.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """Initialize the member-activity service.

        Args:
            session_factory: A zero-argument callable returning a fresh
                SQLAlchemy session bound to the bot database.
        """
        self._session_factory = session_factory

    def record_join(
        self,
        guild_id: int,
        member_id: int,
        joined_at: datetime.datetime,
    ) -> None:
        """Insert or restart tracking for a member join.

        A rejoin updates the grace-period start and clears any first-message
        timestamp recorded for the member's previous stay.

        Args:
            guild_id: Discord guild snowflake.
            member_id: Discord member snowflake.
            joined_at: Naive UTC timestamp for the latest join.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(MemberActivity).where(
                    MemberActivity.guild_id == guild_id,
                    MemberActivity.member_id == member_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = MemberActivity(
                    guild_id=guild_id,
                    member_id=member_id,
                    joined_at=joined_at,
                    first_message_at=None,
                )
                session.add(row)
            else:
                row.joined_at = joined_at
                row.first_message_at = None
            session.commit()

    def claim_join(self, guild_id: int, member_id: int, joined_at: datetime.datetime) -> bool:
        """Claim a new Discord join once, including a later rejoin.

        A repeated gateway event or resume scan for the same join is a no-op.
        The caller must serialize claims within the sole production bot process.
        """
        with self._session_factory() as session:
            existing_claim = session.execute(
                select(MemberJoinClaim).where(
                    MemberJoinClaim.guild_id == guild_id,
                    MemberJoinClaim.member_id == member_id,
                    MemberJoinClaim.joined_at == joined_at,
                )
            ).scalar_one_or_none()
            if existing_claim is not None:
                return False
            row = session.execute(
                select(MemberActivity).where(
                    MemberActivity.guild_id == guild_id,
                    MemberActivity.member_id == member_id,
                )
            ).scalar_one_or_none()
            if row is not None and row.joined_at >= joined_at:
                # The pre-ledger handler recorded the join at receipt time.
                # Treat it as already handled and persist the claim so a
                # later scan cannot greet it again.
                session.add(
                    MemberJoinClaim(guild_id=guild_id, member_id=member_id, joined_at=joined_at)
                )
                session.commit()
                return False
            if row is None:
                session.add(
                    MemberActivity(
                        guild_id=guild_id,
                        member_id=member_id,
                        joined_at=joined_at,
                        first_message_at=None,
                    )
                )
            else:
                row.joined_at = joined_at
                row.first_message_at = None
            session.add(
                MemberJoinClaim(guild_id=guild_id, member_id=member_id, joined_at=joined_at)
            )
            session.commit()
            return True

    def has_join_claim(self, guild_id: int, member_id: int, joined_at: datetime.datetime) -> bool:
        """Confirm a join was durably handled, even if activity was removed."""
        with self._session_factory() as session:
            return (
                session.execute(
                    select(MemberJoinClaim.id).where(
                        MemberJoinClaim.guild_id == guild_id,
                        MemberJoinClaim.member_id == member_id,
                        MemberJoinClaim.joined_at == joined_at,
                    )
                ).scalar_one_or_none()
                is not None
            )

    def record_first_message(
        self,
        guild_id: int,
        member_id: int,
        at: datetime.datetime,
    ) -> None:
        """Record a tracked member's first message if it is still unset.

        The conditional update makes later messages and messages from
        untracked members silent no-ops.

        Args:
            guild_id: Discord guild snowflake.
            member_id: Discord member snowflake.
            at: Naive UTC timestamp for the message.
        """
        with self._session_factory() as session:
            session.execute(
                update(MemberActivity)
                .where(
                    MemberActivity.guild_id == guild_id,
                    MemberActivity.member_id == member_id,
                    MemberActivity.first_message_at.is_(None),
                )
                .values(first_message_at=at)
            )
            session.commit()

    def list_stale(self, now: datetime.datetime) -> list[MemberActivity]:
        """Return inactive members whose 24-hour grace period has elapsed.

        Args:
            now: Current naive UTC timestamp.

        Returns:
            Detached activity rows with no first message and a join time at
            or before the inclusive inactivity cutoff.
        """
        cutoff = now - INACTIVITY_WINDOW
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(MemberActivity).where(
                        MemberActivity.first_message_at.is_(None),
                        MemberActivity.joined_at <= cutoff,
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
            return list(rows)

    def get_tracking(self, guild_id: int, member_id: int) -> MemberActivity | None:
        """Return the current tracking row for a guild member, if any.

        Args:
            guild_id: Discord guild snowflake.
            member_id: Discord member snowflake.

        Returns:
            A detached activity row reflecting the current stored state, or
            ``None`` if no tracking row exists.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(MemberActivity).where(
                    MemberActivity.guild_id == guild_id,
                    MemberActivity.member_id == member_id,
                )
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def remove_tracking(self, guild_id: int, member_id: int) -> None:
        """Delete a member's tracking row if one exists.

        Args:
            guild_id: Discord guild snowflake.
            member_id: Discord member snowflake.
        """
        with self._session_factory() as session:
            session.execute(
                delete(MemberActivity).where(
                    MemberActivity.guild_id == guild_id,
                    MemberActivity.member_id == member_id,
                )
            )
            session.commit()
