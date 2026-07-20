"""Tests for mom_bot.new_member_alerts.service.

Covers the officer join-alert subscription store (issue #301):
``NewMemberAlertService.set_subscription`` toggles an officer's own
subscription on/off per ``(guild_id, user_id)``, ``is_subscribed`` reads
back the current state, and ``list_subscriber_ids`` returns the
currently-subscribed officer IDs for a given guild.

Storage-shape neutral by design: these tests only assert observable
behaviour (``is_subscribed`` / ``list_subscriber_ids`` return values) so
the implementer is free to persist "off" as either a deleted row or a
soft ``enabled=False`` flag — see ``models.py``'s docstring for whichever
choice is made.

Pattern mirrors tests/member_notifications/test_service.py: in-memory
SQLite via ``Base.metadata.create_all``, service constructed with a
``sessionmaker`` session factory.

Spec reference: #301 officer join alerts.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base
from mom_bot.new_member_alerts.service import NewMemberAlertService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GUILD_A = "300000000000000001"
_GUILD_B = "300000000000000099"
_OFFICER_A = "111111111111111111"
_OFFICER_B = "222222222222222222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service() -> NewMemberAlertService:
    """Return a NewMemberAlertService backed by in-memory SQLite."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return NewMemberAlertService(session_factory=sessionmaker(bind=engine))


# ---------------------------------------------------------------------------
# is_subscribed defaults / reads
# ---------------------------------------------------------------------------


def test_is_subscribed_false_when_never_subscribed() -> None:
    """An officer who has never toggled the command is not subscribed."""
    service = _make_service()

    assert service.is_subscribed(_GUILD_A, _OFFICER_A) is False


# ---------------------------------------------------------------------------
# Toggle on/off persists correctly (AC unit test #1)
# ---------------------------------------------------------------------------


def test_set_subscription_on_persists_and_is_subscribed_true() -> None:
    """Toggling on persists the subscription for that (guild, user)."""
    service = _make_service()

    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)

    assert service.is_subscribed(_GUILD_A, _OFFICER_A) is True


def test_set_subscription_off_persists_and_is_subscribed_false() -> None:
    """Toggling off after on removes the subscription."""
    service = _make_service()
    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)

    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=False)

    assert service.is_subscribed(_GUILD_A, _OFFICER_A) is False


def test_set_subscription_on_is_idempotent() -> None:
    """Calling on twice in a row must not raise and stays subscribed."""
    service = _make_service()
    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)

    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)  # must not raise

    assert service.is_subscribed(_GUILD_A, _OFFICER_A) is True


def test_set_subscription_off_when_never_subscribed_is_noop() -> None:
    """Toggling off a (guild, user) that was never subscribed must not raise."""
    service = _make_service()

    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=False)  # must not raise

    assert service.is_subscribed(_GUILD_A, _OFFICER_A) is False


def test_subscription_is_scoped_per_guild() -> None:
    """The same officer can independently subscribe in two different guilds."""
    service = _make_service()

    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)

    assert service.is_subscribed(_GUILD_A, _OFFICER_A) is True
    assert service.is_subscribed(_GUILD_B, _OFFICER_A) is False


# ---------------------------------------------------------------------------
# list_subscriber_ids — used by the join-event DM fan-out
# ---------------------------------------------------------------------------


def test_list_subscriber_ids_empty_when_no_subscriptions() -> None:
    """A guild with no subscribers returns an empty list."""
    service = _make_service()

    assert service.list_subscriber_ids(_GUILD_A) == []


def test_list_subscriber_ids_returns_all_subscribed_users_for_guild() -> None:
    """All officers subscribed in a guild are returned by list_subscriber_ids."""
    service = _make_service()
    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)
    service.set_subscription(_GUILD_A, _OFFICER_B, enabled=True)

    subscribers = service.list_subscriber_ids(_GUILD_A)

    assert set(subscribers) == {_OFFICER_A, _OFFICER_B}


def test_list_subscriber_ids_excludes_other_guilds() -> None:
    """A subscription in a different guild must not leak into this guild's list."""
    service = _make_service()
    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)
    service.set_subscription(_GUILD_B, _OFFICER_B, enabled=True)

    subscribers = service.list_subscriber_ids(_GUILD_A)

    assert subscribers == [_OFFICER_A]


def test_list_subscriber_ids_excludes_toggled_off_users() -> None:
    """An officer who toggled off must not appear in the subscriber list."""
    service = _make_service()
    service.set_subscription(_GUILD_A, _OFFICER_A, enabled=True)
    service.set_subscription(_GUILD_A, _OFFICER_B, enabled=True)
    service.set_subscription(_GUILD_A, _OFFICER_B, enabled=False)

    subscribers = service.list_subscriber_ids(_GUILD_A)

    assert subscribers == [_OFFICER_A]
