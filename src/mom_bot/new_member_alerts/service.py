"""Service layer for officer new-member alert subscriptions."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from mom_bot.new_member_alerts.models import NewMemberAlertSubscription

__all__ = ["NewMemberAlertService"]


class NewMemberAlertService:
    """Read and update officer join-alert subscriptions.

    Each operation uses a fresh database session. Enabled subscriptions are
    represented by rows; disabling a subscription deletes its row.

    Attributes:
        _session_factory: Callable returning a fresh SQLAlchemy session.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """Initialize the service.

        Args:
            session_factory: Callable returning a fresh database session.
        """
        self._session_factory = session_factory

    def set_subscription(self, guild_id: str, user_id: str, enabled: bool) -> None:
        """Enable or disable an officer's alerts for a guild.

        Args:
            guild_id: Discord guild snowflake stored as text.
            user_id: Discord officer snowflake stored as text.
            enabled: Whether the officer should receive join alerts.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(NewMemberAlertSubscription).where(
                    NewMemberAlertSubscription.guild_id == guild_id,
                    NewMemberAlertSubscription.user_id == user_id,
                )
            ).scalar_one_or_none()

            if enabled and row is None:
                session.add(
                    NewMemberAlertSubscription(
                        guild_id=guild_id,
                        user_id=user_id,
                    )
                )
                session.commit()
            elif not enabled and row is not None:
                session.delete(row)
                session.commit()

    def is_subscribed(self, guild_id: str, user_id: str) -> bool:
        """Return whether an officer is subscribed in a guild.

        Args:
            guild_id: Discord guild snowflake stored as text.
            user_id: Discord officer snowflake stored as text.

        Returns:
            True when the guild/user subscription exists.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(NewMemberAlertSubscription.id).where(
                    NewMemberAlertSubscription.guild_id == guild_id,
                    NewMemberAlertSubscription.user_id == user_id,
                )
            ).scalar_one_or_none()
            return row is not None

    def list_subscriber_ids(self, guild_id: str) -> list[str]:
        """Return all subscribed officer IDs for a guild.

        Args:
            guild_id: Discord guild snowflake stored as text.

        Returns:
            Subscribed officer snowflakes ordered by insertion ID.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(NewMemberAlertSubscription.user_id)
                .where(NewMemberAlertSubscription.guild_id == guild_id)
                .order_by(NewMemberAlertSubscription.id)
            ).scalars()
            return list(rows)
